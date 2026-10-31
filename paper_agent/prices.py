"""Prices never go stale just because the market is closed.

While the US market is closed the system used to look at hours-old prices. Now, on a schedule you control in Settings, it refreshes the prices of the stocks that matter
(what you hold, what is queued to buy, what was just decided on, and SPY) from Yahoo's pre-market / after-hours bars and writes them into the same cache everything else
values from. These prices are for VALUATION and early warning (e.g. "NVDA is indicated -4% before the open"); trades still need a verified, fresh quote."""
from __future__ import annotations

import datetime as dt
import time
import urllib.parse
from typing import Callable, Dict, List, Optional

from . import db, marketdata as md, net
from .timeutil import iso, market_state, parse_ts, utcnow

UTC = dt.timezone.utc


def symbols_of_interest(conn, limit: int) -> List[str]:
    out: List[str] = ["SPY"]

    def add(rows):
        for r in rows:
            t = (r[0] or "").strip()
            if t and t not in out:
                out.append(t)
    add(conn.execute("SELECT DISTINCT ticker FROM positions WHERE status='OPEN'").fetchall())
    add(conn.execute("SELECT DISTINCT ticker FROM decisions WHERE status IN ('AWAITING_MARKET','AWAITING_QUOTE','AWAITING_CAPACITY','HELD') AND ticker!=''").fetchall())
    add(conn.execute("SELECT ticker FROM decisions WHERE ticker!='' ORDER BY id DESC LIMIT 200").fetchall())
    return out[:max(1, int(limit))]


def fetch_chart(ticker: str, fetch: Callable, extended: bool = True):
    """NETWORK ONLY (safe to run side by side): the raw chart payload for one symbol."""
    url = md.CHART_EXT % (urllib.parse.quote(ticker), "true" if extended else "false")
    payload, _ = fetch(url, headers={"Accept": "application/json"})
    return payload


def refresh_symbol(conn, ticker: str, fetch: Callable, now, extended: bool = True, payload=None) -> Optional[dict]:
    if payload is None:
        payload = fetch_chart(ticker, fetch, extended)
    bar = md.parse_last_bar(payload)
    if not bar:
        return None
    if bar["symbol"] and str(bar["symbol"]).upper() != ticker.upper():
        return None                                                         # identity check: never store another symbol's price
    when = dt.datetime.fromtimestamp(bar["time"], UTC)
    if when > now + dt.timedelta(minutes=5):
        return None                                                         # a timestamp from the future is a bad clock or a bad feed
    session = market_state(when)["session"]
    conn.execute("INSERT INTO quote_cache(ticker,price,source,quoted_at,updated_epoch) VALUES(?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET price=excluded.price,source=excluded.source,"
                 "quoted_at=excluded.quoted_at,updated_epoch=excluded.updated_epoch WHERE excluded.quoted_at>quote_cache.quoted_at",
                 (ticker, bar["price"], "YAHOO_EXT:" + session, iso(when), now.timestamp()))
    return {"ticker": ticker, "price": bar["price"], "as_of": iso(when), "session": session, "prev_close": bar["prev_close"]}


def run(conn, cfg: dict, now=None, fetch: Optional[Callable] = None, max_seconds: float = 25.0) -> dict:
    now = now or utcnow()
    p = cfg.get("prices") or {}
    open_now = market_state(now)["execution"]
    minutes = float(p.get("refresh_open_minutes", 5) if open_now else p.get("refresh_closed_minutes", 10))
    if minutes <= 0:
        return {"skipped": "off"}
    if not db.due(conn, "prices", minutes * 0.9 / 60.0, now):               # 0.9: a cycle that runs a few seconds late must not skip a whole interval
        return {"skipped": "not due"}
    fetch = fetch or net.get_json
    t0 = time.time()
    syms = symbols_of_interest(conn, int(p.get("max_symbols", 25)))
    updated = failed = 0
    stopped = ""
    from . import parallel
    ext = bool(p.get("extended_hours", True))
    # 1. NETWORK (side by side, at most 2 at a time and about 6 a second, below the ~3 a second it already sustained one at a time x2)   2. DATABASE, one symbol at a time in the original order
    workers = int((cfg.get("runtime") or {}).get("fetch_workers", 6))
    outs = parallel.run_jobs([{"key": s, "host": "chart", "fn": (lambda s=s: fetch_chart(s, fetch, ext))} for s in syms], workers=workers,
                             per_host=2, min_gap=({"chart": 0.15} if workers > 1 else {}), deadline_s=max_seconds)
    for s, o in zip(syms, outs):
        if o.timed_out:
            stopped = "time budget"
            break
        try:
            if not o.ok:
                raise o.error
            if refresh_symbol(conn, s, fetch, now, ext, payload=o.value):
                updated += 1
            else:
                failed += 1
        except net.FetchError as e:
            failed += 1
            if e.code == 429:
                stopped = "rate limited (HTTP 429)"
                break
        except Exception:  # noqa: BLE001 - one symbol must never stop the others
            failed += 1
    db.mark_run(conn, "prices", "", now)
    return {"symbols": len(syms), "updated": updated, "failed": failed, "stopped": stopped, "session": "OPEN" if open_now else "CLOSED"}


def indication(conn, ticker: str, ref_price: Optional[float], now=None) -> Optional[dict]:
    """Where the stock is trading right now versus the reference (previous close) it was decided against: {'gap', 'price', 'as_of', 'session', 'age_minutes'}."""
    now = now or utcnow()
    r = conn.execute("SELECT price,source,quoted_at FROM quote_cache WHERE ticker=?", (ticker,)).fetchone()
    if not r or not ref_price:
        return None
    t = parse_ts(r["quoted_at"])
    if not t:
        return None
    return {"gap": float(r["price"]) / float(ref_price) - 1.0, "price": float(r["price"]), "as_of": r["quoted_at"], "session": (r["source"] or "").split(":")[-1], "age_minutes": (now - t).total_seconds() / 60.0}
