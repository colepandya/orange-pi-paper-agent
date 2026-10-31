"""Free options market data: real bid / ask / implied volatility / open interest from four free public feeds, merged by consensus.

CBOE      cdn.cboe.com delayed quotes (no key; the full chain with IV and greeks)        primary
ALPACA    data.alpaca.markets options snapshots, feed=indicative (free with the Alpaca keys you already have)
NASDAQ    api.nasdaq.com option-chain (no key; no IV, so IV is solved from the price)
YAHOO     query1.finance.yahoo.com options (no key; unofficial and the most likely to break)
Nothing here costs money. All of it is DELAYED by about 15 minutes, so a chain is never used as "the price now": it supplies the contract that really exists, the market's implied volatility and the real
spread, and the option is then repriced at the LIVE stock price with that implied volatility ("anchored"). Feeds are fetched side by side, each failure is isolated and recorded, a quote two feeds disagree
about by more than 15% is discarded, and when nothing answers the caller falls back to the model and says so. The shapes these feeds return are parsed defensively because they are not formally documented."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import statistics
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import db, net, parallel
from .timeutil import iso, utcnow

FETCH: Callable = net.get_json
CALL, PUT = "CALL", "PUT"
OCC = re.compile(r"^(.+?)(\d{6})([CP])(\d{8})$")
CACHE_SECONDS, FAIL_CACHE_SECONDS = 240, 90


def use_fetch(fn: Optional[Callable]) -> None:
    global FETCH
    FETCH = fn or net.get_json


# ------------------------------------------------------------------------------------------------ the maths (shared with options.py)
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(kind: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes value of a European option (no dividends). At or past expiry it is the intrinsic value."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if kind == CALL else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == CALL:
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def bs_delta(kind: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return (1.0 if S > K else 0.0) if kind == CALL else (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return _ncdf(d1) if kind == CALL else _ncdf(d1) - 1.0


def implied_vol(kind: str, S: float, K: float, T: float, r: float, price: float) -> Optional[float]:
    """The volatility that makes the model price equal the market price (bisection). None if the price is outside what any volatility can produce."""
    if T <= 0 or price <= 0 or S <= 0 or K <= 0:
        return None
    lo, hi = 0.01, 4.0
    if not (bs_price(kind, S, K, T, r, lo) - 1e-9 <= price <= bs_price(kind, S, K, T, r, hi) + 1e-9):
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_price(kind, S, K, T, r, mid) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def years_left(expiry: dt.date, now: dt.datetime) -> float:
    close = dt.datetime(expiry.year, expiry.month, expiry.day, 20, 0, tzinfo=dt.timezone.utc)
    return max(0.0, (close - now).total_seconds() / (365.0 * 86400.0))


# ------------------------------------------------------------------------------------------------ parsing helpers
def parse_occ(sym: str) -> Optional[Tuple[str, str, str, float]]:
    m = OCC.match((sym or "").strip().upper().replace(" ", ""))
    if not m:
        return None
    root, ymd, cp, k = m.groups()
    try:
        d = dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError:
        return None
    return root, d.isoformat(), CALL if cp == "C" else PUT, int(k) / 1000.0


def build_occ(root: str, expiry: str, kind: str, strike: float) -> str:
    d = dt.date.fromisoformat(expiry)
    return "%s%02d%02d%02d%s%08d" % (root, d.year % 100, d.month, d.day, "C" if kind == CALL else "P", int(round(strike * 1000)))


def _num(x) -> Optional[float]:
    try:
        if x in (None, "", "--", "N/A"):
            return None
        return float(str(x).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _iv(x) -> Optional[float]:
    v = _num(x)
    if v is None or v <= 0:
        return None
    return v / 100.0 if v > 5 else v                                                         # some feeds say 35.2, others 0.352


def _epoch(x) -> Optional[float]:
    if x in (None, ""):
        return None
    try:
        v = float(x)
        return v / 1000.0 if v > 1e11 else v
    except (TypeError, ValueError):
        pass
    try:
        from zoneinfo import ZoneInfo
        s = str(x).replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s) if "T" in s or "-" in s[:5] else None
        if d is None:
            return None
        return (d if d.tzinfo else d.replace(tzinfo=ZoneInfo("America/New_York"))).timestamp()          # an unlabelled time from these feeds is New York time
    except (ValueError, TypeError, ImportError):
        return None


def _c(kind, expiry, strike, bid, ask, last, iv, delta, vol, oi, source, as_of) -> dict:
    return {"kind": kind, "expiry": expiry, "strike": float(strike), "bid": bid, "ask": ask, "last": last, "iv": iv, "delta": delta, "volume": vol, "oi": oi, "source": source, "as_of": as_of}


def _window(now: dt.datetime, cfg: dict) -> Tuple[str, str]:
    mn = int(cfg["instruments"].get("options_min_dte", 21))
    return (now.date() + dt.timedelta(days=max(1, mn - 7))).isoformat(), (now.date() + dt.timedelta(days=mn + 35)).isoformat()


# ------------------------------------------------------------------------------------------------ the four feeds
def cboe(sym: str, fetch: Callable, now: dt.datetime, cfg: dict, S: Optional[float]) -> dict:
    j, _ = fetch("https://cdn.cboe.com/api/global/delayed_quotes/options/%s.json" % sym, headers={"Accept": "application/json"}, timeout=25, max_bytes=15_000_000)
    d = j.get("data") or {}
    lo, hi = _window(now, cfg)
    S = _num(d.get("current_price")) or S
    stamp = _epoch(j.get("timestamp"))
    out = []
    for o in d.get("options") or []:
        p = parse_occ(o.get("option"))
        if not p or not (lo <= p[1] <= hi) or (S and abs(p[3] / S - 1) > 0.18):
            continue
        out.append(_c(p[2], p[1], p[3], _num(o.get("bid")), _num(o.get("ask")), _num(o.get("last_trade_price")), _iv(o.get("iv")), _num(o.get("delta")), _num(o.get("volume")), _num(o.get("open_interest")), "CBOE", stamp))
    return {"source": "CBOE", "underlying": S, "iv30": _iv(d.get("iv30")), "as_of": stamp, "contracts": out}


def alpaca(sym: str, fetch: Callable, now: dt.datetime, cfg: dict, S: Optional[float]) -> dict:
    k, s = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_API_SECRET")
    if not k or not s:
        raise net.FetchError("DISABLED_NO_KEY")
    lo, hi = _window(now, cfg)
    q = "feed=indicative&limit=1000&expiration_date_gte=%s&expiration_date_lte=%s" % (lo, hi)
    if S:
        q += "&strike_price_gte=%.2f&strike_price_lte=%.2f" % (S * 0.85, S * 1.15)
    j, _ = fetch("https://data.alpaca.markets/v1beta1/options/snapshots/%s?%s" % (sym, q), headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}, timeout=25, max_bytes=15_000_000)
    out, stamps = [], []
    for occ, snap in (j.get("snapshots") or {}).items():
        p = parse_occ(occ)
        if not p:
            continue
        lq, lt = snap.get("latestQuote") or {}, snap.get("latestTrade") or {}
        t = _epoch(lq.get("t"))
        if t:
            stamps.append(t)
        out.append(_c(p[2], p[1], p[3], _num(lq.get("bp")), _num(lq.get("ap")), _num(lt.get("p")), _iv(snap.get("impliedVolatility")), _num((snap.get("greeks") or {}).get("delta")), None, None, "ALPACA", t))
    return {"source": "ALPACA", "underlying": S, "iv30": None, "as_of": max(stamps) if stamps else None, "contracts": out}


def nasdaq(sym: str, fetch: Callable, now: dt.datetime, cfg: dict, S: Optional[float]) -> dict:
    lo, hi = _window(now, cfg)
    url = "https://api.nasdaq.com/api/quote/%s/option-chain?assetclass=stocks&limit=1000&fromdate=%s&todate=%s&money=all&type=all" % (sym, lo, hi)
    j, _ = fetch(url, headers={"Accept": "application/json, text/plain, */*", "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}, timeout=25, max_bytes=15_000_000)
    rows = (((j.get("data") or {}).get("table") or {}).get("rows")) or []
    out, group = [], None
    for r in rows:
        g = r.get("expirygroup")
        if g:
            try:
                group = dt.datetime.strptime(g.strip(), "%B %d, %Y").date().isoformat()
            except ValueError:
                pass
        k = _num(r.get("strike"))
        if k is None or not group or (S and abs(k / S - 1) > 0.18):
            continue
        for kind, p in ((CALL, "c_"), (PUT, "p_")):
            out.append(_c(kind, group, k, _num(r.get(p + "Bid")), _num(r.get(p + "Ask")), _num(r.get(p + "Last")), None, None, _num(r.get(p + "Volume")), _num(r.get(p + "Openinterest")), "NASDAQ", None))
    return {"source": "NASDAQ", "underlying": S, "iv30": None, "as_of": None, "contracts": out}


def yahoo(sym: str, fetch: Callable, now: dt.datetime, cfg: dict, S: Optional[float]) -> dict:
    base = "https://query1.finance.yahoo.com/v7/finance/options/%s" % sym
    j, _ = fetch(base, headers={"Accept": "application/json"}, timeout=20)
    res = ((j.get("optionChain") or {}).get("result") or [{}])[0]
    S = _num((res.get("quote") or {}).get("regularMarketPrice")) or S
    lo, hi = _window(now, cfg)
    want = [e for e in (res.get("expirationDates") or []) if lo <= dt.datetime.fromtimestamp(e, dt.timezone.utc).date().isoformat() <= hi][:2]
    out, stamps = [], []
    for e in want:
        jj, _ = fetch("%s?date=%d" % (base, e), headers={"Accept": "application/json"}, timeout=20)
        blk = (((jj.get("optionChain") or {}).get("result") or [{}])[0].get("options") or [{}])[0]
        for kind, key in ((CALL, "calls"), (PUT, "puts")):
            for o in blk.get(key) or []:
                k = _num(o.get("strike"))
                if k is None or (S and abs(k / S - 1) > 0.18):
                    continue
                t = _epoch(o.get("lastTradeDate"))
                out.append(_c(kind, dt.datetime.fromtimestamp(e, dt.timezone.utc).date().isoformat(), k, _num(o.get("bid")), _num(o.get("ask")), _num(o.get("lastPrice")), _iv(o.get("impliedVolatility")), None, _num(o.get("volume")), _num(o.get("openInterest")), "YAHOO", t))
                if t:
                    stamps.append(t)
    return {"source": "YAHOO", "underlying": S, "iv30": None, "as_of": max(stamps) if stamps else None, "contracts": out}


FEEDS = {"CBOE": cboe, "ALPACA": alpaca, "NASDAQ": nasdaq, "YAHOO": yahoo}


def enabled_feeds(cfg: dict) -> List[str]:
    inst = cfg["instruments"]
    return [n for n in FEEDS if inst.get("options_feed_" + n.lower(), True)]


# ------------------------------------------------------------------------------------------------ health, merging, the cache
def record_health(conn, feed: str, ok: bool, ms: int, n: int, err: str, now) -> None:
    conn.execute("INSERT INTO option_feed_health(feed,last_ok,last_error,last_ms,ok_count,fail_count,last_contracts,updated) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(feed) DO UPDATE SET "
                 "last_ok=COALESCE(excluded.last_ok,option_feed_health.last_ok),last_error=excluded.last_error,last_ms=excluded.last_ms,ok_count=option_feed_health.ok_count+excluded.ok_count,"
                 "fail_count=option_feed_health.fail_count+excluded.fail_count,last_contracts=excluded.last_contracts,updated=excluded.updated",
                 (feed, iso(now) if ok else None, None if ok else err[:200], ms, 1 if ok else 0, 0 if ok else 1, n, iso(now)))


def _valid(c: dict) -> bool:
    b, a = c.get("bid"), c.get("ask")
    return b is not None and a is not None and b >= 0 and a > 0 and a >= b


def merge(results: List[dict]) -> List[dict]:
    """One quote per contract: the median across the feeds that list it. Quotes the feeds disagree about by more than 15% are marked disputed and never used."""
    by: Dict[tuple, List[dict]] = {}
    for r in results:
        for c in r["contracts"]:
            if _valid(c):
                by.setdefault((c["kind"], c["expiry"], round(c["strike"], 3)), []).append(c)
    out = []
    for (kind, expiry, strike), qs in by.items():
        mids = [(q["bid"] + q["ask"]) / 2 for q in qs]
        med = statistics.median(mids)
        disputed = len(qs) >= 2 and med >= 0.2 and (max(mids) - min(mids)) / med > 0.15
        bid, ask = statistics.median([q["bid"] for q in qs]), statistics.median([q["ask"] for q in qs])
        ivs, ds = [q["iv"] for q in qs if q.get("iv")], [q["delta"] for q in qs if q.get("delta") is not None]
        stamps = [q["as_of"] for q in qs if q.get("as_of")]
        out.append({"kind": kind, "expiry": expiry, "strike": strike, "bid": bid, "ask": ask, "mid": (bid + ask) / 2, "iv": statistics.median(ivs) if ivs else None, "delta": statistics.median(ds) if ds else None,
                    "volume": max([q["volume"] for q in qs if q.get("volume") is not None] or [None]) if any(q.get("volume") is not None for q in qs) else None,
                    "oi": max([q["oi"] for q in qs if q.get("oi") is not None] or [None]) if any(q.get("oi") is not None for q in qs) else None,
                    "sources": sorted({q["source"] for q in qs}), "as_of": max(stamps) if stamps else None, "disputed": disputed})
    return out


def record_iv(conn, ticker: str, iv30: Optional[float], now) -> None:
    if iv30 and 0.03 < iv30 < 4:
        conn.execute("INSERT OR REPLACE INTO iv_history(ticker,day,iv30) VALUES(?,?,?)", (ticker, now.date().isoformat(), iv30))


def iv_rank(conn, ticker: str, min_days: int = 20) -> Optional[float]:
    """Where today's implied volatility sits in the range we have recorded (0 = the cheapest seen, 100 = the dearest). None until about a month of history has accumulated."""
    v = [r[0] for r in conn.execute("SELECT iv30 FROM iv_history WHERE ticker=? ORDER BY day DESC LIMIT 252", (ticker,))]
    if len(v) < min_days or max(v) - min(v) < 1e-9:
        return None
    return 100.0 * (v[0] - min(v)) / (max(v) - min(v))


def chain_for(conn, cfg: dict, ticker: str, now, S: Optional[float] = None, fetch: Optional[Callable] = None) -> Optional[dict]:
    """The merged chain for a stock, from the cache (4 minutes) or all enabled feeds fetched side by side. None when nothing answered (the caller falls back to the model and says so)."""
    row = conn.execute("SELECT * FROM option_chain_cache WHERE ticker=?", (ticker,)).fetchone()
    if row:
        age = (now - dt.datetime.fromisoformat(row["fetched"])).total_seconds()
        if 0 <= age < (CACHE_SECONDS if row["payload"] != "[]" else FAIL_CACHE_SECONDS):
            c = json.loads(row["payload"])
            return {"contracts": c, "underlying": row["underlying"], "sources": json.loads(row["sources"] or "[]"), "cached": True} if c else None
    fetch = fetch or FETCH
    feeds = enabled_feeds(cfg)
    jobs = [{"key": n, "host": n, "fn": (lambda n=n: FEEDS[n](ticker, fetch, now, cfg, S))} for n in feeds]
    outs = parallel.run_jobs(jobs, workers=len(jobs) or 1, per_host=1, deadline_s=30)
    ok = []
    for n, o in zip(feeds, outs):
        with db.tx(conn):
            if o.ok and o.value["contracts"]:
                record_health(conn, n, True, int(o.seconds * 1000), len(o.value["contracts"]), "", now)
                ok.append(o.value)
            else:
                msg = ("returned no contracts" if o.ok else ("%s: %s" % (type(o.error).__name__, o.error)))
                record_health(conn, n, False, int(o.seconds * 1000), 0, msg, now)
    merged = merge(ok)
    under = next((r["underlying"] for r in ok if r.get("underlying")), S)
    with db.tx(conn):
        conn.execute("INSERT OR REPLACE INTO option_chain_cache(ticker,fetched,underlying,sources,payload) VALUES(?,?,?,?,?)",
                     (ticker, iso(now), under, json.dumps([r["source"] for r in ok]), json.dumps(merged, separators=(",", ":")) if merged else "[]"))
        iv30 = next((r["iv30"] for r in ok if r.get("iv30")), None)
        if iv30 is None and merged and under:
            near = [m["iv"] for m in merged if m["iv"] and abs(m["strike"] / under - 1) < 0.04]
            iv30 = statistics.median(near) if near else None
        record_iv(conn, ticker, iv30, now)
    return {"contracts": merged, "underlying": under, "sources": [r["source"] for r in ok], "cached": False} if merged else None


# ------------------------------------------------------------------------------------------------ choosing and anchoring a contract
def liquid(c: dict, now, cfg: dict) -> Tuple[bool, str]:
    inst = cfg["instruments"]
    if c.get("disputed"):
        return False, "the feeds disagree about its price"
    if c["bid"] <= 0 or c["ask"] <= c["bid"]:
        return False, "no real two-sided market"
    sp = (c["ask"] - c["bid"]) / c["mid"] if c["mid"] > 0 else 9
    if sp > float(inst.get("options_max_spread_pct", 0.15)):
        return False, "spread %.0f%% of the price is too wide" % (sp * 100)
    if c.get("oi") is not None and c["oi"] < float(inst.get("options_min_open_interest", 100)):
        return False, "only %d contracts outstanding" % c["oi"]
    if c.get("as_of") and (now.timestamp() - c["as_of"]) > float(inst.get("options_quote_max_age_minutes", 45)) * 60:
        return False, "the quote is stale"
    return True, ""


def pick(contracts: List[dict], kind: str, S: float, now, cfg: dict) -> Tuple[Optional[dict], str]:
    """The listed contract the bot would really trade: the first expiry at least options_min_dte days out, a call at the first strike on or above the price (a put on or below), and if that one is not liquid
    the next strikes away. Returns (contract, "") or (None, why not)."""
    mn = int(cfg["instruments"].get("options_min_dte", 21))
    exps = sorted({c["expiry"] for c in contracts if c["kind"] == kind and (dt.date.fromisoformat(c["expiry"]) - now.date()).days >= mn})[:2]
    why = "no listed %s expires %d or more days out" % (kind.lower(), mn)
    for e in exps:
        cs = sorted((c for c in contracts if c["kind"] == kind and c["expiry"] == e), key=lambda c: c["strike"])
        side = [c for c in cs if c["strike"] >= S - 1e-9] if kind == CALL else [c for c in reversed(cs) if c["strike"] <= S + 1e-9]
        for c in side[:3]:
            ok, w = liquid(c, now, cfg)
            if ok:
                return c, ""
            why = "%s %s %.2f: %s" % (e, kind.lower(), c["strike"], w)
    return None, why


def find(chain: Optional[dict], o) -> Optional[dict]:
    if not chain:
        return None
    for c in chain["contracts"]:
        if c["kind"] == o["kind"] and c["expiry"] == o["expiry"] and abs(c["strike"] - float(o["strike"])) < 1e-6 and not c.get("disputed"):
            return c
    return None


def anchor(q: dict, S_live: float, S_chain: Optional[float], now, cfg: dict) -> Optional[dict]:
    """Reprice a delayed market quote at the LIVE stock price: take the market's implied volatility (or solve it from the mid price) and its real spread, and value the contract now. None if the quote is
    not consistent with its own volatility (a stale or broken print)."""
    r = float(cfg["instruments"].get("options_risk_free", 0.04))
    T = years_left(dt.date.fromisoformat(q["expiry"]), now)
    S0 = S_chain or S_live
    sigma = q.get("iv")
    if not sigma or not (0.05 <= sigma <= 3.0):
        sigma = implied_vol(q["kind"], S0, q["strike"], T, r, q["mid"])
    if not sigma:
        return None
    if T > 0 and abs(bs_price(q["kind"], S0, q["strike"], T, r, sigma) / q["mid"] - 1) > 0.25:
        return None
    mid = bs_price(q["kind"], S_live, q["strike"], T, r, sigma)
    half_pct = max(0.01, (q["ask"] - q["bid"]) / 2.0 / q["mid"])
    half = max(0.03, half_pct * mid)
    return {"mid": mid, "bid": max(0.01, mid - half), "ask": mid + half, "half": half, "T": T, "iv": sigma}


def log_model_check(conn, ticker: str, q: dict, S: float, model_sigma: float, now, cfg: dict) -> None:
    """How far is the pure model from the real market? One row each time a real contract is priced; the Results page summarises it."""
    r = float(cfg["instruments"].get("options_risk_free", 0.04))
    T = years_left(dt.date.fromisoformat(q["expiry"]), now)
    if T <= 0 or q["mid"] <= 0:
        return
    model = bs_price(q["kind"], S, q["strike"], T, r, model_sigma)
    conn.execute("INSERT INTO option_model_checks(created,ticker,kind,dte,moneyness,market_mid,model_mid,err_pct) VALUES(?,?,?,?,?,?,?,?)",
                 (iso(now), ticker, q["kind"], int(T * 365), q["strike"] / S - 1, q["mid"], model, model / q["mid"] - 1))
