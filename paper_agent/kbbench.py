"""Resource report for the Orange Pi: database size, rows, query latency of the pages that read the knowledge base, peak memory, and the cost of one bounded knowledge stage. Measured where it runs."""
from __future__ import annotations

import os
import resource
import time
from typing import Dict

from . import db, kbsnap, kbstage, whybundle
from .timeutil import utcnow


def resources(conn, cfg: dict, watch: dict) -> Dict[str, object]:
    out: Dict[str, object] = {"measured_on": os.uname().machine, "python": __import__("sys").version.split()[0]}
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        out["database_mb"] = round(os.path.getsize(path) / 1e6, 1)
    except Exception:  # noqa: BLE001
        out["database_mb"] = None
    out["kb_rows"] = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in sorted(db.tables(conn)) if t.startswith("kb_") or t in ("knowledge_coverage", "event_lifecycle", "event_materiality", "company_event_states", "company_event_state_history", "market_regimes", "redteam_interventions", "intervention_outcomes")}
    lat = {}
    tick = [r[0] for r in conn.execute("SELECT ticker FROM knowledge_coverage ORDER BY priority LIMIT 25")] or ["NVDA"]
    t0 = time.time()
    for t in tick:
        whybundle.render(whybundle.case_file(conn, cfg, ticker=t))
    lat["company_page_ms_each"] = round((time.time() - t0) / len(tick) * 1000, 1)
    t0 = time.time()
    conn.execute("SELECT COUNT(*) FROM kb_entities").fetchone()
    eid = kbsnap.entity_of(conn, tick[0])
    for _ in range(200):
        conn.execute("SELECT * FROM kb_relationships WHERE src_entity=? OR dst_entity=?", (eid, eid)).fetchall()
    lat["indexed_relationship_lookup_ms"] = round((time.time() - t0) / 200 * 1000, 3)
    out["latency"] = lat
    now = utcnow()
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.time()
    st = kbstage.run(conn, cfg, watch, now, None, sleep=lambda s: None)
    out["one_knowledge_stage_seconds"] = round(time.time() - t0, 2)
    out["stage_detail_seconds"] = st.get("seconds")
    out["peak_memory_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    out["memory_growth_during_stage_mb"] = round((resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss0) / 1024.0, 1)
    return out
