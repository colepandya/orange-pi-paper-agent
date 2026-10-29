"""Quote consensus. Fail-closed for execution; valuation may use fewer sources.

Fixes vs v4.0.2: provider timestamps parsed robustly (Alpaca nanosecond strings used to disable the freshness check),
providers without a timestamp (Alpha Vantage) can value but never execute, per-provider rate budgets/backoff (free tiers),
a keyless Yahoo provider so quorum is reachable, and the ``quote_cache`` table always exists.
"""
from __future__ import annotations

import concurrent.futures
import json
import math
import os
import time
import urllib.parse
from typing import Callable, Dict, List, Optional

from . import db, marketdata, net
from .timeutil import iso, market_state, parse_ts, utcnow


def obs(provider: str, ticker: str, price=None, bid=None, ask=None, stamp=None, returned=None, error=None, http=None) -> dict:
    def f(x):
        try:
            return float(x) if x not in (None, "") else None
        except (TypeError, ValueError):
            return None
    ts = parse_ts(stamp)
    return {"provider": provider, "ticker": ticker, "returned_ticker": returned or ticker, "price": f(price), "bid": f(bid), "ask": f(ask),
            "provider_timestamp": iso(ts) if ts else None, "received_timestamp": iso(), "error": error, "http_status": http}


def _nokey(p, t):
    return obs(p, t, error="DISABLED_NO_KEY")


def twelve(t, fetch):
    k = os.getenv("TWELVE_DATA_API_KEY")
    if not k:
        return _nokey("TWELVE_DATA", t)
    j, h = fetch("https://api.twelvedata.com/quote?symbol=%s&apikey=%s" % (urllib.parse.quote(t), urllib.parse.quote(k)))
    if j.get("status") == "error":
        return obs("TWELVE_DATA", t, error="API:%s" % j.get("code"), http=j.get("code"))
    return obs("TWELVE_DATA", t, j.get("close"), j.get("bid"), j.get("ask"), j.get("timestamp"), j.get("symbol"), http=h)


def finnhub(t, fetch):
    k = os.getenv("FINNHUB_API_KEY")
    if not k:
        return _nokey("FINNHUB", t)
    j, h = fetch("https://finnhub.io/api/v1/quote?symbol=%s&token=%s" % (urllib.parse.quote(t), urllib.parse.quote(k)))
    return obs("FINNHUB", t, j.get("c"), stamp=j.get("t"), http=h)


def alpaca(t, fetch):
    k, s = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_API_SECRET")
    if not k or not s:
        return _nokey("ALPACA", t)
    j, h = fetch("https://data.alpaca.markets/v2/stocks/%s/snapshot" % urllib.parse.quote(t), headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s})
    q, tr = j.get("latestQuote") or {}, j.get("latestTrade") or {}
    return obs("ALPACA", t, tr.get("p"), q.get("bp"), q.get("ap"), tr.get("t"), http=h)


def massive(t, fetch):
    k = os.getenv("MASSIVE_API_KEY")
    if not k:
        return _nokey("MASSIVE", t)
    j, h = fetch("https://api.massive.com/v2/last/trade/%s?apiKey=%s" % (urllib.parse.quote(t), urllib.parse.quote(k)))
    r = j.get("results") or {}
    return obs("MASSIVE", t, r.get("p"), stamp=r.get("t") or r.get("sip_timestamp"), http=h)


def alpha(t, fetch):
    k = os.getenv("ALPHA_VANTAGE_API_KEY")
    if not k:
        return _nokey("ALPHA_VANTAGE", t)
    j, h = fetch("https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=%s&apikey=%s" % (urllib.parse.quote(t), urllib.parse.quote(k)))
    r = j.get("Global Quote") or {}
    if not r:
        return obs("ALPHA_VANTAGE", t, error="RATE_LIMITED_OR_EMPTY", http=429 if "Note" in j or "Information" in j else h)
    return obs("ALPHA_VANTAGE", t, r.get("05. price"), stamp=None, returned=r.get("01. symbol"), http=h)   # no timestamp => valuation only


def tiingo(t, fetch):
    k = os.getenv("TIINGO_API_KEY")
    if not k:
        return _nokey("TIINGO", t)
    j, h = fetch("https://api.tiingo.com/iex/" + urllib.parse.quote(t), headers={"Authorization": "Token " + k})
    r = j[0] if isinstance(j, list) and j else {}
    return obs("TIINGO", t, r.get("last") or r.get("tngoLast"), r.get("bidPrice"), r.get("askPrice"), r.get("timestamp"), r.get("ticker", "").upper() or t, http=h)


def eodhd(t, fetch):
    k = os.getenv("EODHD_API_KEY")
    if not k:
        return _nokey("EODHD", t)
    j, h = fetch("https://eodhd.com/api/real-time/%s.US?api_token=%s&fmt=json" % (urllib.parse.quote(t), urllib.parse.quote(k)))
    return obs("EODHD", t, j.get("close"), stamp=j.get("timestamp"), returned=(j.get("code", "") or t).split(".")[0], http=h)


def yahoo(t, fetch):
    j, h = fetch(marketdata.CHART % (urllib.parse.quote(t), "1d"))
    m = marketdata.parse_meta(j)
    return obs("YAHOO", t, m["price"], stamp=m["time"], returned=m["symbol"], http=h)


PROVIDERS: List[Callable] = [twelve, finnhub, alpaca, massive, alpha, tiingo, eodhd, yahoo]
_NAMES = {"twelve": "TWELVE_DATA", "finnhub": "FINNHUB", "alpaca": "ALPACA", "massive": "MASSIVE", "alpha": "ALPHA_VANTAGE", "tiingo": "TIINGO", "eodhd": "EODHD", "yahoo": "YAHOO"}


def validate(x: dict, now=None, max_age: float = 900) -> dict:
    now = now or utcnow()
    x["identity_verified"] = (x.get("returned_ticker") or "").upper() == x["ticker"].upper()
    ts = parse_ts(x.get("provider_timestamp"))
    age = (now - ts).total_seconds() if ts else None
    x["age_seconds"] = age
    p = x.get("price")
    valid = isinstance(p, (int, float)) and math.isfinite(p) and p > 0
    bid, ask = x.get("bid"), x.get("ask")
    spread_ok = not (bid and ask) or (bid <= ask and valid and (ask - bid) / p <= 0.02)
    fresh_or_unknown = age is None or -300 <= age <= max_age
    x["accepted"] = bool(not x.get("error") and x["identity_verified"] and valid and spread_ok and fresh_or_unknown)
    x["timestamp_known"] = age is not None
    x["stale_only"] = bool(not x.get("error") and x["identity_verified"] and valid and spread_ok and not fresh_or_unknown)      # a usable price, merely OLD (normal when the market is closed)
    x["reject_reason"] = x.get("error") or (None if x["accepted"] else "VALIDATION_FAILED")
    return x


def _budget_ok(conn, name: str, cfg: dict, now_s: float) -> bool:
    lim = cfg["quotes"]["limits"].get(name)
    h = conn.execute("SELECT backoff_until FROM provider_health WHERE provider=?", (name,)).fetchone()
    if h and float(h["backoff_until"] or 0) > now_s:
        return False
    if not lim:
        return True
    n, window = lim
    conn.execute("DELETE FROM provider_calls WHERE ts<?", (now_s - 86400,))
    c = conn.execute("SELECT COUNT(*) FROM provider_calls WHERE provider=? AND ts>?", (name, now_s - window)).fetchone()[0]
    return c < n


def _run_provider(fn, ticker, fetch):
    name = _NAMES.get(fn.__name__, fn.__name__.upper())
    try:
        return fn(ticker, fetch)
    except net.FetchError as e:
        return obs(name, ticker, error=str(e), http=e.code)
    except Exception as e:  # noqa: BLE001
        return obs(name, ticker, error=type(e).__name__)


def consensus(conn, ticker: str, cfg: dict, now=None, providers: Optional[List[Callable]] = None, fetch: Callable = net.get_json, purpose: str = "exit", only: Optional[set] = None) -> dict:
    now = now or utcnow()
    now_s = now.timestamp()
    q = cfg["quotes"]
    max_age = float(q["entry_max_age_seconds"] if purpose == "entry" else q["max_age_seconds"])
    raw: List[dict] = []
    todo = []
    for fn in providers or PROVIDERS:
        name = _NAMES.get(fn.__name__, fn.__name__.upper())
        if only is not None and name not in only:
            continue
        if conn is not None and providers is None and not _budget_ok(conn, name, cfg, now_s):
            continue
        todo.append(fn)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(todo))) as ex:
        results = list(ex.map(lambda f: _run_provider(f, ticker, fetch), todo))
    for r in results:
        r = validate(r, now)
        if r["error"] == "DISABLED_NO_KEY":
            continue
        raw.append(r)
        if conn is not None and providers is None:
            conn.execute("INSERT INTO provider_calls(provider,ts) VALUES(?,?)", (r["provider"], now_s))
    good = [x for x in raw if x["accepted"]]

    def _median(xs):
        xs = sorted(xs)
        return (xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2) if xs else None
    fresh = [x for x in good if x["timestamp_known"] and x["age_seconds"] <= max_age]
    med_fresh = _median([x["price"] for x in fresh])
    # agreement is judged among FRESH quotes only: an hours-old price from a slow provider must not veto a fresh one
    timed = [x for x in fresh if med_fresh and abs(x["price"] / med_fresh - 1) <= float(q["max_deviation"])]
    med = med_fresh if fresh else _median([x["price"] for x in good])
    agreed = timed or [x for x in good if med and abs(x["price"] / med - 1) <= float(q["max_deviation"])]
    need = 1 if q.get("allow_single_source") else int(q["min_providers"])
    status = "PASS" if len(timed) >= need else ("VALUATION_ONLY" if agreed else "FAILED")
    note = ""
    lim = float(q.get("single_source_max_move_pct", 0.20) or 0)
    if status == "PASS" and len(timed) == 1 and need == 1 and lim and conn is not None:      # one source has no cross-check: it must at least be plausible against the last close
        from . import marketdata as md
        cl = md.closes(conn, ticker, 1)
        last = cl[-1] if cl else None
        if last and abs(timed[0]["price"] / last - 1) > lim:
            status, note = "VALUATION_ONLY", "single source %s says %.2f, %.0f%% away from the last close %.2f: needs a second source to confirm" % (timed[0]["provider"], timed[0]["price"], abs(timed[0]["price"] / last - 1) * 100, last)
    clk = market_state(now)
    price = None
    if agreed:
        pool = timed if timed else agreed
        ps = sorted(x["price"] for x in pool)
        price = ps[len(ps) // 2] if len(ps) % 2 else (ps[len(ps) // 2 - 1] + ps[len(ps) // 2]) / 2
    newest = max((parse_ts(x["provider_timestamp"]) for x in (timed or []) if x.get("provider_timestamp")), default=None)
    return {"ticker": ticker, "session": clk["session"], "status": status, "price": price, "providers_used": [x["provider"] for x in (timed or agreed)],
            "provider_count": len(timed or agreed), "quoted_at": iso(newest) if newest else None, "age_seconds": (now - newest).total_seconds() if newest else None,
            "max_deviation": max((abs(x["price"] / med - 1) for x in agreed), default=None) if med else None,
            "execution_eligible": bool(status == "PASS" and clk["execution"]), "observations": raw, "note": note}


def health_of(x: dict) -> tuple:
    """-> (state, reachable, fresh). 'Reachable' = the provider answered with a usable price (however old). 'Fresh' = it is also recent enough to trade on.
    A provider that answers with an old price (market closed) is STALE, which is normal: it is NOT a failure."""
    http = x.get("http_status")
    if x.get("error"):
        return ("AUTH_FAILED" if http in (401, 403) else "RATE_LIMITED" if http == 429 else "DOWN"), False, False
    if x.get("accepted"):
        return "HEALTHY", True, True
    if x.get("stale_only"):
        return "STALE", True, False
    return "DEGRADED", False, False


def store(conn, c: dict, now=None, cache: bool = True, backoff: bool = True) -> None:
    now = now or utcnow()
    conn.execute("INSERT INTO quote_consensus(ticker,created,session,status,consensus_price,provider_count,max_deviation,execution_eligible,providers_json,observations_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (c["ticker"], iso(now), c["session"], c["status"], c["price"], c["provider_count"], c["max_deviation"], int(c["execution_eligible"]), json.dumps(c["providers_used"]), json.dumps(c["observations"], sort_keys=True, default=str)))
    for x in c["observations"]:
        p = x["provider"]
        st, reachable, fresh = health_of(x)
        back = (now.timestamp() + 900 if st == "RATE_LIMITED" else (now.timestamp() + 3600 if st == "AUTH_FAILED" else 0)) if backoff else None     # None = leave any existing pause as it is
        conn.execute("INSERT OR IGNORE INTO provider_health(provider) VALUES(?)", (p,))
        conn.execute("UPDATE provider_health SET requests=0,successes=0,failures=0,auth_failures=0,rate_limits=0,last_success=NULL,last_error=NULL WHERE provider=? AND last_checked IS NULL", (p,))   # numbers left by the OLD app are discarded
        conn.execute("UPDATE provider_health SET requests=requests+1,successes=successes+?,failures=failures+?,auth_failures=auth_failures+?,rate_limits=rate_limits+?,last_checked=?,"
                     "last_success=CASE WHEN ? THEN ? ELSE last_success END,last_fresh=CASE WHEN ? THEN ? ELSE last_fresh END,last_error=?,state=?,backoff_until=COALESCE(?,backoff_until) WHERE provider=?",
                     (int(reachable), int(not reachable), int(x.get("http_status") in (401, 403)), int(x.get("http_status") == 429), iso(now), int(reachable), iso(now), int(fresh), iso(now),
                      x.get("reject_reason") if not reachable else None, st, back, p))
    if cache and c["price"] and c["status"] in ("PASS", "VALUATION_ONLY") and c["quoted_at"]:
        conn.execute("INSERT INTO quote_cache(ticker,price,source,quoted_at,updated_epoch) VALUES(?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET price=excluded.price,source=excluded.source,quoted_at=excluded.quoted_at,updated_epoch=excluded.updated_epoch",
                     (c["ticker"], c["price"], "CONSENSUS:" + "+".join(c["providers_used"]), c["quoted_at"], now.timestamp()))


def probe(conn, cfg: dict, now=None, fetch: Callable = net.get_json) -> dict:
    """Keep the provider table TRUE: a cheap SPY check of every configured provider on a schedule you set (Settings -> Execution), market open or closed.
    Providers with hourly/daily budgets (Alpha Vantage: ~20 a day) are checked at most every 3 hours: the trade path needs those calls more than a health check does."""
    now = now or utcnow()
    mins = float((cfg.get("quotes") or {}).get("probe_minutes", 10) or 0)
    if mins <= 0:
        return {"skipped": "off"}
    if not db.due(conn, "quote_probe", mins * 0.9 / 60.0, now):
        return {"skipped": "not due"}
    only = set()
    for fn in PROVIDERS:
        name = _NAMES.get(fn.__name__, fn.__name__.upper())
        lim = cfg["quotes"]["limits"].get(name)
        row = conn.execute("SELECT last_checked FROM provider_health WHERE provider=?", (name,)).fetchone()
        last = parse_ts(row["last_checked"]) if row and row["last_checked"] else None
        scarce = bool(lim) and lim[1] >= 3600
        if not scarce or last is None or (now - last).total_seconds() >= 3 * 3600:
            only.add(name)
    db.mark_run(conn, "quote_probe", "", now)
    try:
        c = consensus(conn, "SPY", cfg, now, None, fetch, "exit", only=only)
        store(conn, c, now, cache=False, backoff=False)          # observe only: no price-cache write (an old consensus must not overwrite a newer after-hours price) and NO pause: a health check must never block a trading provider
    except Exception as e:  # noqa: BLE001 - a health check must never fail a trading cycle
        return {"skipped": "probe failed: %s" % type(e).__name__}
    return {"checked": sorted(x["provider"] for x in c["observations"]), "status": c["status"]}


def get_quote(conn, ticker: str, cfg: dict, purpose: str = "exit", now=None, providers=None, fetch: Callable = net.get_json) -> dict:
    """Fresh consensus quote (cached for a few seconds so entry+exit checks in one cycle don't double-spend rate limits)."""
    now = now or utcnow()
    max_age = float(cfg["quotes"]["entry_max_age_seconds"] if purpose == "entry" else cfg["quotes"]["max_age_seconds"])
    row = conn.execute("SELECT * FROM quote_consensus WHERE ticker=? AND status='PASS' ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
    if row and providers is None:
        c_at = parse_ts(row["created"])
        qc = conn.execute("SELECT quoted_at FROM quote_cache WHERE ticker=?", (ticker,)).fetchone()
        qa = parse_ts(qc[0]) if qc else None
        if c_at and qa and (now - c_at).total_seconds() < 20 and (now - qa).total_seconds() <= max_age:
            return {"ticker": ticker, "status": "PASS", "price": row["consensus_price"], "quoted_at": iso(qa), "age_seconds": (now - qa).total_seconds(),
                    "execution_eligible": bool(market_state(now)["execution"]), "providers_used": json.loads(row["providers_json"] or "[]"), "provider_count": row["provider_count"], "session": market_state(now)["session"]}
    c = consensus(conn, ticker, cfg, now, providers, fetch, purpose)
    store(conn, c, now)
    return c


def valuation_price(conn, ticker: str, now=None, max_age_hours: float = 72) -> Optional[dict]:
    """Best-effort mark-to-market price (never used for execution)."""
    now = now or utcnow()
    r = conn.execute("SELECT price,source,quoted_at FROM quote_cache WHERE ticker=?", (ticker,)).fetchone()
    t = parse_ts(r["quoted_at"]) if r else None
    if r and t and (now - t).total_seconds() <= max_age_hours * 3600:
        return {"price": float(r["price"]), "source": r["source"], "quoted_at": r["quoted_at"], "age_seconds": (now - t).total_seconds()}
    row = conn.execute("SELECT close,day FROM market_series WHERE symbol=? ORDER BY day DESC LIMIT 1", (ticker,)).fetchone()
    if row:
        return {"price": float(row["close"]), "source": "DAILY_CLOSE", "quoted_at": row["day"], "age_seconds": None}
    return None
