"""Does this outlet's news actually lead price? Measured, shrunk toward the pool, applied only once there is enough evidence.

The hand-set credibility of a feed did not line up with outcomes in the data (feeds rated below 0.7 followed through the most; feeds rated 0.8-0.9 the least), and the learned hit/miss blend was recorded in 'observe' mode and never reached a decision.
This measures it directly. For every directional, single-company event it asks: three sessions after publication, did the stock move the way the story said, against the market? One row per event (reputation_events).
For each outlet (or feed, when the outlet is unknown) the follow-through rate is shrunk toward the pooled rate of ALL outlets with `reputation_prior_n` pseudo-observations (so a source needs dozens of events before it moves), and only an
outlet with at least `reputation_min_n` measured events changes anything: its credibility is multiplied by posterior / pooled, held within 0.85 to 1.15. With few events everything stays at 1.0, which is the honest answer.
It is a RELATIVE measure (an outlet against the pool), so the market's own drift cancels. It says nothing about why: an outlet can lead price because it is accurate, early, or merely about stocks that were about to move.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import time
from typing import Callable, Dict, List, Optional

from . import outlets
from .timeutil import iso, utcnow

FACTOR_LO, FACTOR_HI = 0.85, 1.15
HORIZON = 3                                                                                         # sessions after the publication day
MIN_DIRECTION = 0.3


def factor(conn, cfg: dict, key) -> float:
    """The multiplier on an outlet's credibility: 1.0 unless it has been measured enough."""
    ic = (cfg or {}).get("intelligence") or {}
    if not key or not ic.get("reputation_apply", True):
        return 1.0
    try:
        min_n = int(ic.get("reputation_min_n", 30))
        r = conn.execute("SELECT n,factor FROM source_reputation WHERE key=?", (str(key),)).fetchone()
    except Exception:  # noqa: BLE001 - a missing table, no connection or a bad setting is "no record yet": neutral, never an error in evidence processing
        return 1.0
    if not r or r[0] is None or r[0] < min_n:
        return 1.0
    try:
        f = float(r[1])
    except (TypeError, ValueError):
        return 1.0
    return max(FACTOR_LO, min(FACTOR_HI, f)) if math.isfinite(f) else 1.0


def _bars(conn, symbol: str, day: str):
    """The last close on or before `day`, and the closes of the next HORIZON sessions: [(day, close) x (1 + HORIZON)] or None."""
    a = conn.execute("SELECT day,close FROM market_series WHERE symbol=? AND day<=? AND close>0 ORDER BY day DESC LIMIT 1", (symbol, day)).fetchone()
    if not a:
        return None
    nxt = conn.execute("SELECT day,close FROM market_series WHERE symbol=? AND day>? AND close>0 ORDER BY day LIMIT ?", (symbol, a[0], HORIZON)).fetchall()
    return [tuple(a)] + [tuple(x) for x in nxt]


def measure_one(conn, ticker: str, published_day: str, direction: float):
    """The signed, market-adjusted return from the publication day's close to the close HORIZON sessions later, in the direction the story pointed. None when the prices are not there (yet)."""
    s, m = _bars(conn, ticker, published_day), _bars(conn, "SPY", published_day)
    if not s or not m or len(s) < 1 + HORIZON or len(m) < 1 + HORIZON:
        return None
    if s[0][0] != m[0][0] or s[HORIZON][0] != m[HORIZON][0]:
        return None                                                                                   # the stock and the market must be measured over the same days
    r = (s[HORIZON][1] / s[0][1] - 1.0) - (m[HORIZON][1] / m[0][1] - 1.0)
    return (1.0 if direction > 0 else -1.0) * r


def run(conn, cfg: dict, now=None, max_events: int = 300, budget_s: float = 2.0, clock: Callable = time.monotonic) -> dict:
    """The per-cycle step: measure events old enough to have a result, then refresh the table. Bounded; never raises into the cycle."""
    now = now or utcnow()
    t0 = clock()
    out = {"status": "ok", "measured": 0, "unmeasurable": 0, "keys": 0}
    try:
        cutoff = iso(now - dt.timedelta(days=4))
        rows = conn.execute("SELECT e.id,e.source,e.outlet,e.direction,e.tickers_json,e.published FROM events e LEFT JOIN reputation_events r ON r.event_id=e.id "
                            "WHERE r.event_id IS NULL AND e.stale=0 AND ABS(e.direction)>=? AND e.published IS NOT NULL AND e.published!='' AND e.published<=? ORDER BY e.id DESC LIMIT ?",
                            (MIN_DIRECTION, cutoff, int(max_events) * 3)).fetchall()
        done = 0
        for r in rows:
            if done >= max_events or clock() - t0 > budget_s:
                break
            try:
                tk = json.loads(r["tickers_json"] or "[]")
            except (ValueError, TypeError):
                tk = []
            key = r["outlet"] or r["source"] or ""
            kind = "outlet" if r["outlet"] else "source"
            if len(tk) != 1 or not key or not isinstance(tk[0], str):
                conn.execute("INSERT OR IGNORE INTO reputation_events(event_id,key,kind,measured,signed_ret,followed) VALUES(?,?,?,?,NULL,NULL)", (r["id"], key, kind, iso(now)))
                out["unmeasurable"] += 1
                done += 1
                continue
            ret = measure_one(conn, tk[0], str(r["published"])[:10], float(r["direction"]))
            if ret is None:
                old = str(r["published"])[:10] < (now - dt.timedelta(days=12)).date().isoformat()
                if old:                                                                               # no prices and too old to wait for: settle it, so it is not rescanned for ever
                    conn.execute("INSERT OR IGNORE INTO reputation_events(event_id,key,kind,measured,signed_ret,followed) VALUES(?,?,?,?,NULL,NULL)", (r["id"], key, kind, iso(now)))
                    out["unmeasurable"] += 1
                    done += 1
                continue
            conn.execute("INSERT OR IGNORE INTO reputation_events(event_id,key,kind,measured,signed_ret,followed) VALUES(?,?,?,?,?,?)", (r["id"], key, kind, iso(now), ret, 1 if ret > 0 else 0))
            out["measured"] += 1
            done += 1
        out["keys"] = refresh(conn, cfg, now)
        conn.commit()
    except Exception as ex:  # noqa: BLE001 - measuring reputation must never stop a trading cycle
        out["status"] = "error"
        out["error"] = "%s: %s" % (type(ex).__name__, str(ex)[:120])
    return out


def refresh(conn, cfg: dict, now=None) -> int:
    """Recompute every outlet's posterior and factor from the measured events. Returns the number of keys."""
    ic = (cfg or {}).get("intelligence") or {}
    try:
        n0 = max(1, int(ic.get("reputation_prior_n", 40)))
    except (TypeError, ValueError, OverflowError):
        n0 = 40
    rows = conn.execute("SELECT key,kind,COUNT(*) n,SUM(followed) f,AVG(signed_ret) m FROM reputation_events WHERE followed IS NOT NULL AND key!='' GROUP BY key").fetchall()
    tn, tf = sum(r["n"] for r in rows), sum(r["f"] or 0 for r in rows)
    p0 = (tf / tn) if tn >= 20 else 0.5
    p0 = min(0.95, max(0.05, p0))
    stamp = iso(now or utcnow())
    for r in rows:
        n, f = int(r["n"]), int(r["f"] or 0)
        post = (f + n0 * p0) / (n + n0)
        fac = max(FACTOR_LO, min(FACTOR_HI, post / p0))
        _tier, prior = outlets.tier_of(r["key"])
        conn.execute("INSERT INTO source_reputation(key,kind,n,follow_n,mean_ret,prior,posterior,factor,updated) VALUES(?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET kind=excluded.kind,n=excluded.n,follow_n=excluded.follow_n,mean_ret=excluded.mean_ret,prior=excluded.prior,posterior=excluded.posterior,factor=excluded.factor,updated=excluded.updated",
                     (r["key"], r["kind"], n, f, r["m"], prior, post, fac, stamp))
    return len(rows)


def table(conn, limit: int = 20) -> List[Dict]:
    """The measured outlets, most-measured first, for the dashboard."""
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM source_reputation ORDER BY n DESC, key LIMIT ?", (int(limit),)).fetchall()]
    except (sqlite3.Error, TypeError, ValueError):
        return []
