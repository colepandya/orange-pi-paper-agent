"""Long price history for the research lab.

The bot judges its own assumptions on whatever prices it happens to hold: about a year. A year is one market regime, so almost any pattern can be found in it by accident. This module backfills several years of DAILY bars for the stocks
the bot tracks, a few symbols per cycle, so the lab (lab.py) can test an idea on more than one regime BEFORE any trading rule is changed to use it.

Observation only: it writes price bars and its own status table; nothing here is read by a trade decision. It reuses the existing chart fetch and store (marketdata.fetch_chart / parse_chart / store_series), so a symbol fetched twice is
simply overwritten with the same bars. Closes are split-adjusted but not dividend-adjusted (irrelevant for studies over a few days, and every study is market-adjusted).

Limits it keeps: a few symbols per cycle and a time budget, an immediate stop on a rate-limit error, exponential backoff per symbol (6, 12, 24 ... 72 hours; after five failures a retry once a week).
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from . import marketdata as md, net
from .timeutil import iso, parse_ts, utcnow
import datetime as dt

BENCHMARKS = ("SPY",)
MAX_TRIES = 5


def _int(x, default: int) -> int:
    try:
        return int(x)
    except (TypeError, ValueError, OverflowError):
        return default


def range_for(years) -> str:
    """The chart range that covers this many years."""
    y = _int(years, 5)
    return "1y" if y <= 1 else "2y" if y <= 2 else "5y" if y <= 5 else "10y" if y <= 10 else "max"


def wanted_symbols(conn, cfg: dict) -> List[str]:
    """The stocks worth a long history, most important first: the benchmark, then the stocks the bot has decided on (most decisions first), then every other symbol it already tracks."""
    cap = _int((cfg.get("history") or {}).get("max_symbols", 500), 500)
    weight: Dict[str, int] = {s: 10 ** 9 for s in BENCHMARKS}
    for t, n in conn.execute("SELECT ticker,COUNT(*) FROM decisions WHERE ticker IS NOT NULL AND ticker<>'' GROUP BY ticker").fetchall():
        weight[t] = weight.get(t, 0) + int(n) * 1000
    for (s,) in conn.execute("SELECT DISTINCT symbol FROM market_series").fetchall():
        weight.setdefault(s, 0)
    return sorted(weight, key=lambda s: (-weight[s], s))[:max(0, cap)]


def pending(conn, cfg: dict, now=None) -> List[str]:
    """Symbols still to fetch: never tried, tried with a shorter history than now asked for, or failed and past their retry time."""
    now = now or utcnow()
    years = _int((cfg.get("history") or {}).get("years", 5), 5)
    st = {r["symbol"]: r for r in conn.execute("SELECT * FROM history_status").fetchall()}
    out = []
    for s in wanted_symbols(conn, cfg):
        r = st.get(s)
        if r is None:
            out.append(s)
        elif r["status"] == "done":
            if (r["years"] or 0) < years:
                out.append(s)                                                                      # a longer history was asked for since
        else:
            nt = parse_ts(r["next_try"]) if r["next_try"] else None
            if nt is None or nt <= now:
                out.append(s)
    return out


def _record(conn, symbol: str, now, **f) -> None:
    conn.execute("INSERT INTO history_status(symbol,status,years,first_day,last_day,rows,tries,next_try,last_error,updated) VALUES(?,?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(symbol) DO UPDATE SET status=excluded.status,years=excluded.years,first_day=excluded.first_day,last_day=excluded.last_day,rows=excluded.rows,tries=excluded.tries,next_try=excluded.next_try,last_error=excluded.last_error,updated=excluded.updated",
                 (symbol, f["status"], f.get("years"), f.get("first_day"), f.get("last_day"), f.get("rows"), f.get("tries", 0), f.get("next_try"), f.get("last_error"), iso(now)))


def backfill_one(conn, symbol: str, years: int, now=None, fetch: Optional[Callable] = None) -> dict:
    """Fetch and store one symbol's history and record the outcome. Never raises: a failure is recorded with its backoff."""
    now = now or utcnow()
    try:
        bars = md.parse_chart(md.fetch_chart(symbol, range_for(years), fetch))
        if not bars:
            raise net.FetchError("no bars")
        n = md.store_series(conn, symbol, bars)
        _record(conn, symbol, now, status="done", years=int(years), first_day=bars[0]["day"], last_day=bars[-1]["day"], rows=n, tries=0, next_try=None, last_error=None)
        return {"symbol": symbol, "ok": True, "rows": n, "first_day": bars[0]["day"]}
    except Exception as ex:  # noqa: BLE001 - a bad symbol or a bad response must not stop the others
        prev = conn.execute("SELECT tries FROM history_status WHERE symbol=?", (symbol,)).fetchone()
        tries = (prev[0] if prev else 0) + 1
        wait_h = 7 * 24 if tries >= MAX_TRIES else min(72, 6 * 2 ** (tries - 1))
        err = "%s: %s" % (type(ex).__name__, str(ex)[:120])
        try:
            _record(conn, symbol, now, status="failed", years=int(years), tries=tries, next_try=iso(now + dt.timedelta(hours=wait_h)), last_error=err)
        except Exception:  # noqa: BLE001
            pass
        return {"symbol": symbol, "ok": False, "error": err}


def run(conn, cfg: dict, now=None, fetch: Optional[Callable] = None, clock: Optional[Callable] = None) -> dict:
    """The per-cycle step: a few symbols, a time budget, never an exception into the cycle."""
    hc = cfg.get("history") or {}
    if not hc.get("enabled", True):
        return {"status": "disabled"}
    now = now or utcnow()
    clock = clock or time.monotonic
    t0 = clock()
    out: dict = {"status": "ok", "fetched": 0, "failed": 0, "remaining": 0}
    try:
        per, years = _int(hc.get("symbols_per_cycle", 3), 3), _int(hc.get("years", 5), 5)
        try:
            budget = float(hc.get("max_seconds", 12.0))
        except (TypeError, ValueError):
            budget = 12.0
        todo = pending(conn, cfg, now)
        for s in todo[:max(0, per)]:
            if clock() - t0 > budget:
                break
            r = backfill_one(conn, s, years, now, fetch)
            out["fetched" if r["ok"] else "failed"] += 1
            if not r["ok"] and "429" in r.get("error", ""):
                break                                                                              # rate-limited: stop for this cycle instead of hammering
        out["remaining"] = max(0, len(todo) - out["fetched"])
        conn.commit()
    except Exception as ex:  # noqa: BLE001
        out["status"] = "error"
        out["error"] = "%s: %s" % (type(ex).__name__, str(ex)[:120])
    return out


def backfill_now(conn, cfg: dict, limit: int = 50, delay: float = 2.0, fetch: Optional[Callable] = None, sleep: Callable = time.sleep, now=None) -> dict:
    """From the command line: fetch up to `limit` pending symbols with a pause between requests (the chart feed allows about 30 a minute)."""
    now = now or utcnow()
    years = _int((cfg.get("history") or {}).get("years", 5), 5)
    todo = pending(conn, cfg, now)
    out = {"fetched": 0, "failed": 0, "remaining": len(todo), "errors": []}
    for s in todo[:max(0, _int(limit, 0))]:
        r = backfill_one(conn, s, years, now, fetch)
        out["fetched" if r["ok"] else "failed"] += 1
        if not r["ok"]:
            out["errors"].append("%s %s" % (s, r["error"]))
            if "429" in r["error"]:
                break
        sleep(delay)
    out["remaining"] = max(0, len(todo) - out["fetched"])
    return out


def coverage(conn, cfg: dict) -> dict:
    """How much history there is, for the dashboard and the command line."""
    wanted = wanted_symbols(conn, cfg)
    st = {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM history_status").fetchall()}
    years = _int((cfg.get("history") or {}).get("years", 5), 5)
    done = [s for s in wanted if st.get(s, {}).get("status") == "done" and (st[s].get("years") or 0) >= years]
    failed = [s for s in wanted if st.get(s, {}).get("status") == "failed"]
    firsts = [st[s]["first_day"] for s in done if st[s].get("first_day")]
    rows = conn.execute("SELECT COUNT(*) FROM market_series").fetchone()[0]
    span = conn.execute("SELECT MIN(day),MAX(day) FROM market_series").fetchone()
    return {"years_wanted": years, "symbols_wanted": len(wanted), "done": len(done), "failed": len(failed), "pending": len(wanted) - len(done) - len(failed),
            "rows": rows, "earliest_day": span[0], "latest_day": span[1], "median_first_day": sorted(firsts)[len(firsts) // 2] if firsts else None,
            "failures": [(s, st[s].get("last_error")) for s in failed[:5]]}
