"""Contradiction engine: actively search for evidence that WEAKENS held / high-probability theses (devil's advocate).

Targeted GDELT queries (keyless) pull counter-narrative articles into the normal news pipeline, where thesis rules score them as
contradicting evidence. Support / contradiction / net strength live on the thesis row."""
from __future__ import annotations

import time
from typing import Callable, List

from . import db, net, sources
from .theses_seed import SEED_THESES
from .timeutil import iso, utcnow

COUNTER = {t["key"]: t.get("counter_queries", []) for t in SEED_THESES}
SRC = {"id": "gdelt_contra", "name": "GDELT (contradiction search)", "type": "gdelt", "trust": 0.5}


def search(conn, cfg: dict, fetch: Callable = net.get, now=None, max_theses: int = 2, sleep: Callable = time.sleep) -> dict:
    now = now or utcnow()
    hours = float(cfg["intelligence"]["contradiction_hours"])
    held = {r[0] for r in conn.execute("SELECT DISTINCT thesis_key FROM positions WHERE status='OPEN' AND thesis_key IS NOT NULL")}
    rows = conn.execute("SELECT key,probability FROM theses WHERE active=1 ORDER BY probability DESC").fetchall()
    todo = [r["key"] for r in rows if (r["key"] in held or r["probability"] >= 0.5) and COUNTER.get(r["key"]) and db.due(conn, "contra:" + r["key"], hours, now)]
    res = {"searched": [], "new_items": 0, "errors": []}
    for key in todo[:max_theses]:
        q = " OR ".join('"%s"' % x for x in COUNTER[key][:4])
        src = dict(SRC, query="(%s)" % q, timespan="24h", max=40)
        try:
            items, _ = sources.fetch_source(src, fetch)
            res["new_items"] += sources.store_items(conn, src, items, now)
            res["searched"].append(key)
        except Exception as e:  # noqa: BLE001
            res["errors"].append("%s:%s" % (key, type(e).__name__))
        db.mark_run(conn, "contra:" + key, "", now)
        sleep(5.5)   # GDELT asks for <= 1 request / 5 s
    return res


def report(conn, key: str, n: int = 3, now=None) -> List[dict]:
    rows = conn.execute("SELECT e.headline,e.source,e.published,ev.direction,ev.credibility*ev.strength AS w FROM evidence ev JOIN events e ON e.id=ev.event_id "
                        "WHERE ev.thesis_key=? AND ev.direction<0 AND ev.expiry_date>? ORDER BY w DESC LIMIT ?", (key, iso(now or utcnow()), n)).fetchall()
    return [dict(r) for r in rows]
