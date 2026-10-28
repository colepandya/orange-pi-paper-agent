"""Forecast revision ledger + decomposed confidence log. Every change to a target/stop/horizon/thesis probability is recorded with its reason,
so forecast quality can be audited and measured over time."""
from __future__ import annotations

import json
from typing import List, Optional

from .timeutil import iso


def record(conn, now, subject_type: str, subject: str, ticker: Optional[str], field: str, old, new, reason: str, conf_old=None, conf_new=None, detail=None) -> None:
    if old is not None and new is not None and abs(float(old) - float(new)) < 1e-9:
        return
    conn.execute("INSERT INTO forecast_revisions(ts,subject_type,subject,ticker,field,old_value,new_value,reason,conf_old,conf_new,detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (iso(now), subject_type, str(subject), ticker, field, old, new, (reason or "")[:400], conf_old, conf_new, json.dumps(detail or {}, default=str)))


def last_value(conn, subject_type: str, subject: str, field: str) -> Optional[float]:
    r = conn.execute("SELECT new_value FROM forecast_revisions WHERE subject_type=? AND subject=? AND field=? ORDER BY id DESC LIMIT 1", (subject_type, str(subject), field)).fetchone()
    return float(r[0]) if r and r[0] is not None else None


def history(conn, ticker: Optional[str] = None, limit: int = 40) -> List[dict]:
    if ticker:
        rows = conn.execute("SELECT * FROM forecast_revisions WHERE ticker=? ORDER BY id DESC LIMIT ?", (ticker, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM forecast_revisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def log_confidence(conn, position_id: int, now, thesis: float, forecast: float, target: float, position: float, valuation: str, detail=None, min_change: float = 0.03) -> bool:
    """Insert only when any component moved by >= min_change (or valuation view changed). Returns True if a row was written."""
    last = conn.execute("SELECT * FROM confidence_log WHERE position_id=? ORDER BY id DESC LIMIT 1", (position_id,)).fetchone()
    cur = (thesis, forecast, target, position)
    if last:
        prev = (last["thesis"], last["forecast"], last["target"], last["position"])
        if all(abs((a or 0) - (b or 0)) < min_change for a, b in zip(cur, prev)) and last["valuation"] == valuation:
            return False
    conn.execute("INSERT INTO confidence_log(position_id,ts,thesis,forecast,target,position,valuation,detail_json) VALUES(?,?,?,?,?,?,?,?)",
                 (position_id, iso(now), thesis, forecast, target, position, valuation, json.dumps(detail or {}, default=str)))
    return True
