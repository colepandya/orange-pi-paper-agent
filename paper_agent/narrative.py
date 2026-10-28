"""Narrative engine: strength, velocity, acceleration, crowding, exhaustion, decay per thesis."""
from __future__ import annotations

import datetime as dt
import json
import math
from typing import List, Optional

from . import marketdata as md
from .timeutil import iso, parse_ts, utcnow


def _mass(conn, key: str, since: dt.datetime, until: dt.datetime) -> float:
    rows = conn.execute("SELECT cluster_id,MAX(credibility*strength) m FROM evidence WHERE thesis_key=? AND created>=? AND created<? GROUP BY cluster_id", (key, iso(since), iso(until))).fetchall()
    return float(sum(r["m"] or 0 for r in rows))


def price_extension(conn, tickers: List[str]) -> Optional[float]:
    """Mean 60d excess return vs SPY of the thesis' beneficiaries, scaled so +50% outperformance = 1.0."""
    spy = md.closes(conn, "SPY", 80)
    spy_r = md.ret_n(spy, 60)
    if spy_r is None:
        return None
    ex = []
    for t in tickers[:6]:
        r = md.ret_n(md.closes(conn, t, 80), 60)
        if r is not None:
            ex.append(r - spy_r)
    if len(ex) < 2:
        return None
    return max(0.0, min(1.0, (sum(ex) / len(ex)) / 0.5))


def update(conn, now: Optional[dt.datetime] = None) -> List[dict]:
    now = now or utcnow()
    keys = [r["key"] for r in conn.execute("SELECT key FROM theses WHERE active=1")]
    m7 = {k: _mass(conn, k, now - dt.timedelta(days=7), now) for k in keys}
    total7 = sum(m7.values()) or 1.0
    out = []
    for k in keys:
        m3 = _mass(conn, k, now - dt.timedelta(days=3), now)
        mprev = _mass(conn, k, now - dt.timedelta(days=6), now - dt.timedelta(days=3))
        m30 = _mass(conn, k, now - dt.timedelta(days=30), now)
        velocity = max(-3.0, min(3.0, (m3 / 3 - mprev / 3) / (mprev / 3 + 0.05)))
        prev = conn.execute("SELECT velocity FROM narrative_states WHERE thesis_key=? AND ts<=? ORDER BY ts DESC LIMIT 1", (k, iso(now - dt.timedelta(hours=12)))).fetchone()
        accel = velocity - float(prev["velocity"]) if prev and prev["velocity"] is not None else 0.0
        strength = math.tanh(m7[k] / 1.5)
        share = min(1.0, (m7[k] / total7) * len(keys) / 4.0)
        tick = [r["ticker"] for r in conn.execute("SELECT ticker FROM thesis_companies WHERE thesis_key=? AND role='beneficiary' ORDER BY weight DESC", (k,))]
        ext = price_extension(conn, tick)
        crowding = share if ext is None else 0.5 * share + 0.5 * ext
        exhaustion = max(0.0, min(1.0, crowding * (0.4 + 0.6 * max(0.0, min(1.0, -accel))) * (0.5 + 0.5 * (ext or 0.0))))
        last = conn.execute("SELECT MAX(created) c FROM evidence WHERE thesis_key=?", (k,)).fetchone()["c"]
        days_since = (now - parse_ts(last)).total_seconds() / 86400 if last else 30.0
        decay = max(0.0, min(1.0, days_since / 30.0))
        conn.execute("INSERT INTO narrative_states(thesis_key,ts,mentions,strength,velocity,acceleration,crowding,exhaustion,decay,detail_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (k, iso(now), m7[k], strength, velocity, accel, crowding, exhaustion, decay, json.dumps({"m3": m3, "mprev": mprev, "m30": m30, "share": share, "ext": ext})))
        out.append({"key": k, "strength": strength, "velocity": velocity, "acceleration": accel, "crowding": crowding, "exhaustion": exhaustion, "decay": decay})
    conn.execute("DELETE FROM narrative_states WHERE ts<?", (iso(now - dt.timedelta(days=60)),))
    return out
