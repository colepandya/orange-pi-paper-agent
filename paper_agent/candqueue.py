"""Durable queue of trade candidates.

Candidates used to live only in memory between the evidence stage and the entries stage. If the cycle was killed in between (timeout, out-of-memory,
reboot), the events were already saved and marked processed but the candidates were LOST: hundreds of events, no decisions, no error. Now a candidate is
written here the moment it is created, and removed only when a decision has been recorded for it (or it is too old to trade)."""
from __future__ import annotations

import datetime as dt
import json
from typing import List, Optional

from .timeutil import iso, parse_ts


def put(conn, c: dict, now) -> bool:
    """Idempotent: one row per (event, ticker)."""
    if c.get("event_id") is None:
        return False
    try:
        conn.execute("INSERT OR IGNORE INTO candidate_queue(created,event_id,ticker,payload_json,status) VALUES(?,?,?,?,'NEW')", (iso(now), c["event_id"], c["ticker"], json.dumps(c, default=str)))
        return True
    except Exception:  # noqa: BLE001 - queueing must never break event processing
        return False


def load(conn, now, max_age_hours: float, limit: int = 400) -> List[dict]:
    """NEW candidates still young enough to trade; anything older is marked EXPIRED (and counted by the funnel)."""
    cutoff = iso(now - dt.timedelta(hours=float(max_age_hours)))
    conn.execute("UPDATE candidate_queue SET status='EXPIRED',decided=? WHERE status='NEW' AND created<?", (iso(now), cutoff))
    out = []
    for r in conn.execute("SELECT id,payload_json,created FROM candidate_queue WHERE status='NEW' ORDER BY id LIMIT ?", (limit,)).fetchall():
        try:
            c = json.loads(r["payload_json"])
        except ValueError:
            conn.execute("UPDATE candidate_queue SET status='BAD',decided=? WHERE id=?", (iso(now), r["id"]))
            continue
        pub = parse_ts(r["created"])
        c["age_hours"] = float(c.get("age_hours") or 0) + (max(0.0, (now - pub).total_seconds() / 3600) if pub else 0.0)     # it has aged since it was queued
        c["queue_id"] = r["id"]
        out.append(c)
    return out


def done(conn, c: dict, now, status: str = "DONE") -> None:
    if c.get("queue_id") is not None:
        conn.execute("UPDATE candidate_queue SET status=?,decided=? WHERE id=?", (status, iso(now), c["queue_id"]))
    elif c.get("event_id") is not None:
        conn.execute("UPDATE candidate_queue SET status=?,decided=? WHERE event_id=? AND ticker=?", (status, iso(now), c["event_id"], c["ticker"]))


def counts(conn) -> dict:
    return {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM candidate_queue GROUP BY status")}
