"""Research-gap engine: find what the system does not know, raise research tasks, and auto-resolve those it can."""
from __future__ import annotations

import json
import json
from typing import Callable, Dict, Optional

from . import company, contradiction, db, flows, net
from .timeutil import iso, utcnow


def _task(conn, kind: str, subject: str, desc: str, prio: float, auto: bool, now) -> int:
    return conn.execute("INSERT OR IGNORE INTO research_tasks(created,kind,subject,description,priority,auto) VALUES(?,?,?,?,?,?)", (iso(now), kind, subject, desc, prio, int(auto))).rowcount


def scan(conn, cfg: dict, now=None) -> int:
    now = now or utcnow()
    n = 0
    held = [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM positions WHERE status='OPEN'")]
    for t in held:
        c = conn.execute("SELECT completeness,updated FROM companies WHERE ticker=?", (t,)).fetchone()
        if not c or (c["completeness"] or 0) < 0.4:
            n += _task(conn, "fundamentals", t, "Held position %s has incomplete company fundamentals" % t, 0.8, True, now)
        if not conn.execute("SELECT 1 FROM capital_flows WHERE ticker=? AND kind LIKE 'insider%'", (t,)).fetchone():
            n += _task(conn, "insiders", t, "No insider-transaction history for held position %s" % t, 0.5, True, now)
        q = conn.execute("SELECT 1 FROM quote_consensus WHERE ticker=? AND status='PASS' AND created>? LIMIT 1", (t, iso(now - __import__("datetime").timedelta(days=2)))).fetchone()
        if not q:
            n += _task(conn, "quote_gap", t, "No verified multi-provider quote for %s in 48h (exits will be blocked)" % t, 0.9, False, now)
    since = iso(now - __import__("datetime").timedelta(days=3))
    for e in conn.execute("SELECT id,event_type,headline,amount_usd,tickers_json FROM events WHERE created>? AND stale=0 AND novelty>=0.9 AND tickers_json!='[]'", (since,)):
        if e["event_type"] in ("contract_win", "merger_acquisition") and not e["amount_usd"]:
            n += _task(conn, "missing_contract_value", "event:%d" % e["id"], "Unknown deal value: %s" % e["headline"][:110], 0.6, False, now)
        if e["event_type"] in ("earnings_beat", "earnings_miss") and "guidance" not in e["headline"].lower() and "outlook" not in e["headline"].lower():
            n += _task(conn, "missing_guidance", "event:%d" % e["id"], "Guidance not stated: %s" % e["headline"][:110], 0.55, False, now)
        if e["event_type"] == "contract_win" and not any(w in e["headline"].lower() for w in ("pentagon", "navy", "army", "air force", "nasa", "dod", "department", "agency", "government", "nato")):
            n += _task(conn, "missing_customer", "event:%d" % e["id"], "Customer unidentified: %s" % e["headline"][:110], 0.5, False, now)
    for t in conn.execute("SELECT key,name,probability,confidence_quality,last_evidence FROM theses WHERE active=1"):
        age_days = 999.0
        if t["last_evidence"]:
            from .timeutil import parse_ts
            age_days = (now - parse_ts(t["last_evidence"])).total_seconds() / 86400
        if t["probability"] >= 0.5 and age_days > 14:
            n += _task(conn, "thesis_stale", t["key"], "Thesis '%s' (P=%.0f%%) has had no evidence for %.0f days: seek confirming or contradicting information" % (t["name"], t["probability"] * 100, min(age_days, 999)), 0.5, True, now)
        if t["probability"] >= 0.6 and t["confidence_quality"] < 0.3:
            n += _task(conn, "thesis_low_quality", t["key"], "Thesis '%s' is high-probability but low evidence quality (%.2f)" % (t["name"], t["confidence_quality"]), 0.6, False, now)
    from . import sources as _sources
    off = _sources.disabled_ids(cfg)                                          # a feed the owner switched off is not a problem to research
    for sid in off:
        conn.execute("UPDATE research_tasks SET status='RESOLVED',resolved_at=?,result='feed switched off in Settings' WHERE kind='source_down' AND subject=? AND status='OPEN'", (iso(now), sid))
    for s in conn.execute("SELECT source_id,failures,last_error FROM source_health WHERE failures>=3"):
        if s["source_id"] in off:
            continue
        n += _task(conn, "source_down", s["source_id"], "Source %s failing (%d consecutive): %s" % (s["source_id"], s["failures"], (s["last_error"] or "")[:80]), 0.4, False, now)
    n += _task(conn, "institutional_13f", "global", "Institutional ownership / 13F changes are not available from free feeds: add a data source or EDGAR 13F-HR parsing", 0.3, False, now)
    return n


def resolve_auto(conn, cfg: dict, universe, fetch_json: Callable = net.get_json, fetch: Callable = net.get, now=None, budget: int = 4) -> dict:
    now = now or utcnow()
    res = {"resolved": 0, "failed": 0}
    for t in conn.execute("SELECT * FROM research_tasks WHERE status='OPEN' AND auto=1 AND attempts<3 ORDER BY priority DESC LIMIT ?", (budget,)).fetchall():
        ok, out = False, ""
        try:
            if t["kind"] == "fundamentals":
                r = company.refresh_company(conn, t["subject"], universe.meta(t["subject"]) if universe is not None else {}, cfg, fetch_json, now)
                ok, out = r["scores"]["completeness"] >= 0.3, "completeness %.2f" % r["scores"]["completeness"]
            elif t["kind"] == "insiders":
                k = flows.fetch_insiders(conn, t["subject"], fetch_json)
                ok, out = True, "%d insider rows" % k
            elif t["kind"] == "thesis_stale":
                db.mark_run(conn, "contra:" + t["subject"], "", now)  # force
                conn.execute("DELETE FROM task_schedule WHERE task=?", ("contra:" + t["subject"],))
                r = contradiction.search(conn, cfg, fetch, now, max_theses=1, sleep=lambda s: None)
                ok, out = True, json.dumps(r)
        except Exception as e:  # noqa: BLE001
            out = "%s: %s" % (type(e).__name__, e)
        conn.execute("UPDATE research_tasks SET attempts=attempts+1,status=?,resolved_at=?,result=? WHERE id=?", ("RESOLVED" if ok else "OPEN", iso(now) if ok else None, out[:300], t["id"]))
        res["resolved" if ok else "failed"] += 1
    return res
