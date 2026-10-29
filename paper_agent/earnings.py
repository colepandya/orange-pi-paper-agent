"""Earnings awareness. A swing stop of 1-3% cannot protect against an earnings gap of 8%, so (1) a BUY is not opened when a report is due inside its holding window
(unless the news IS the earnings release), and (2) a position is sold before the close when a report would land overnight. Data: Finnhub's free earnings calendar."""
from __future__ import annotations

import datetime as dt
import os
import urllib.parse
from typing import Callable, Dict, List, Optional

from . import db, net
from .timeutil import NY, add_trading_days, iso, is_trading_day, parse_ts, utcnow

EARNINGS_EVENT_TYPES = {"earnings_beat", "earnings_miss", "guidance_raise", "guidance_cut", "earnings_report"}
HOUR = {"bmo": "before the open", "amc": "after the close", "dmh": "during the session", "": "time not stated"}


def parse(j) -> List[dict]:
    out = []
    for r in (j.get("earningsCalendar") if isinstance(j, dict) else None) or []:
        if not isinstance(r, dict):
            continue
        sym, day = str(r.get("symbol") or "").strip().upper(), str(r.get("date") or "")[:10]
        if sym and len(day) == 10 and day[4] == "-" and day[7] == "-":
            out.append({"ticker": sym, "date": day, "hour": str(r.get("hour") or "").lower()[:3]})
    return out


def refresh(conn, cfg: dict, now=None, fetch: Callable = net.get_json, days: int = 10) -> dict:
    now = now or utcnow()
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        return {"skipped": "no FINNHUB_API_KEY"}
    if not db.due(conn, "earnings_calendar", 11, now):
        return {"skipped": "not due"}
    if not db.due(conn, "earnings_calendar_retry", 1, now):
        return {"skipped": "retrying later"}
    d0 = now.astimezone(NY).date()
    try:
        j, _ = fetch("https://finnhub.io/api/v1/calendar/earnings?from=%s&to=%s&token=%s" % (d0, d0 + dt.timedelta(days=days), urllib.parse.quote(key)))
    except Exception as e:  # noqa: BLE001 - a calendar outage must NEVER fail a trading cycle; retry in an hour
        db.mark_run(conn, "earnings_calendar_retry", str(e)[:100], now)
        return {"skipped": "fetch failed: %s" % type(e).__name__}
    rows = parse(j)
    with db.tx(conn):
        conn.execute("DELETE FROM earnings_calendar WHERE date>=?", (str(d0),))
        for r in rows:
            conn.execute("INSERT OR REPLACE INTO earnings_calendar(ticker,date,hour,fetched) VALUES(?,?,?,?)", (r["ticker"], r["date"], r["hour"], iso(now)))
    db.mark_run(conn, "earnings_calendar", str(len(rows)), now)
    return {"releases": len(rows)}


def upcoming(conn, ticker: str, now=None) -> Optional[dict]:
    """The next report that has NOT happened yet, or None. A 'before the open' report dated today is already out by the time anything can trade."""
    today = (now or utcnow()).astimezone(NY).date()
    for r in conn.execute("SELECT date,hour FROM earnings_calendar WHERE ticker=? AND date>=? ORDER BY date", (ticker, str(today))):
        d = dt.date.fromisoformat(r["date"])
        if d == today and r["hour"] == "bmo":
            continue
        return {"date": r["date"], "hour": r["hour"], "days": (d - today).days, "label": "%s (%s)" % (dt.date.fromisoformat(r["date"]).strftime("%a %d %b"), HOUR.get(r["hour"], r["hour"]))}
    return None


def inside_hold(conn, ticker: str, now, hold_days: int) -> Optional[dict]:
    u = upcoming(conn, ticker, now)
    if not u:
        return None
    last = add_trading_days(now, max(1, hold_days)).astimezone(NY).date()
    return u if dt.date.fromisoformat(u["date"]) <= last else None


def lands_overnight(conn, ticker: str, now) -> Optional[dict]:
    """A report that would hit while we hold overnight: today after the close / during the session, or tomorrow before the open."""
    u = upcoming(conn, ticker, now)
    if not u:
        return None
    today = now.astimezone(NY).date()
    nxt = add_trading_days(now, 1).astimezone(NY).date()
    d = dt.date.fromisoformat(u["date"])
    if d == today and u["hour"] in ("amc", "dmh", ""):
        return u
    if d == nxt and u["hour"] in ("bmo", "dmh", ""):
        return u
    return None
