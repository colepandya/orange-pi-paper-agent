"""Decision funnel: news -> events -> candidates -> decisions -> trades, with the REASON each item dropped out.
Answers "why are there no decisions?" with evidence instead of guesses, and flags the usual causes (skipped cycles, no news, nothing qualifying)."""
from __future__ import annotations

import datetime as dt
import json
from typing import Dict, List, Optional

from .evidence import GATE_LABELS
from .timeutil import iso, market_state, to_nz, utcnow


def report(conn, hours: float = 24, now=None, samples: int = 3) -> dict:
    now = now or utcnow()
    since = iso(now - dt.timedelta(hours=hours))
    one = lambda q, *a: conn.execute(q, a).fetchone()[0]  # noqa: E731
    cyc = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM cycle_runs WHERE started>? GROUP BY status", (since,))}
    skip_reasons = []
    for r in conn.execute("SELECT report_json FROM cycle_runs WHERE status='SKIPPED' AND started>? ORDER BY id DESC LIMIT 20", (since,)):
        try:
            skip_reasons.append(json.loads(r["report_json"] or "{}").get("skipped") or "?")
        except ValueError:
            pass
    gates = {(r["g"] or "not recorded (older build)"): r["n"] for r in conn.execute("SELECT gate g,COUNT(*) n FROM events WHERE created>? GROUP BY gate ORDER BY n DESC", (since,))}
    sample = {}
    for g in gates:
        key = None if g.startswith("not recorded") else g
        rows = conn.execute("SELECT headline,event_type,tickers_json,direction,magnitude,created FROM events WHERE created>? AND (gate IS ? OR gate=?) ORDER BY id DESC LIMIT ?", (since, key, key or "", samples)).fetchall()
        sample[g] = [{"headline": (r["headline"] or "")[:130], "type": r["event_type"], "tickers": json.loads(r["tickers_json"] or "[]"), "direction": round(r["direction"], 2), "magnitude": round(r["magnitude"], 2), "seen": r["created"]} for r in rows]
    dec = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM decisions WHERE created>? GROUP BY status", (since,))}
    mna = [{"headline": (r["headline"] or "")[:120], "gate": r["gate"] or "?", "tickers": json.loads(r["tickers_json"] or "[]"), "decision": r["status"] or "-", "seen": r["created"]}
           for r in conn.execute("SELECT e.headline,e.gate,e.tickers_json,e.created,(SELECT status FROM decisions d WHERE d.event_id=e.id ORDER BY d.id DESC LIMIT 1) status "
                                 "FROM events e WHERE e.event_type='merger_acquisition' AND e.created>? ORDER BY e.id DESC LIMIT 15", (since,))]
    rep = {"hours": hours, "now": iso(now), "market": market_state(now)["session"], "cycles": cyc, "skip_reasons": sorted(set(skip_reasons)),
           "last_cycle": one("SELECT MAX(started) FROM cycle_runs"), "last_ok_cycle": one("SELECT MAX(started) FROM cycle_runs WHERE status='OK'"),
           "news": one("SELECT COUNT(*) FROM news WHERE ingested>?", since), "last_news": one("SELECT MAX(ingested) FROM news"),
           "events": one("SELECT COUNT(*) FROM events WHERE created>?", since), "last_event": one("SELECT MAX(created) FROM events"),
           "gates": gates, "samples": sample,
           "candidates": sum(n for g, n in gates.items() if g.startswith("candidate")),
           "decisions": dec, "decisions_total": sum(dec.values()), "last_decision": one("SELECT MAX(created) FROM decisions"),
           "executed": one("SELECT COUNT(*) FROM positions WHERE opened>?", since), "mna": mna}
    rep["findings"] = findings(conn, rep, now)
    return rep


def findings(conn, rep: dict, now) -> List[dict]:
    out: List[dict] = []
    two_h, six_h = iso(now - dt.timedelta(hours=2)), iso(now - dt.timedelta(hours=6))
    if not rep["last_cycle"] or rep["last_cycle"] < two_h:
        out.append({"severity": "HIGH", "text": "No cycle has started in the last 2 hours (last: %s). The timer may be stopped or the service failing." % (to_nz(rep["last_cycle"]) if rep["last_cycle"] else "never"),
                    "fix": "systemctl status paper-agent-cycle.timer paper-agent-cycle.service; journalctl -u paper-agent-cycle -n 50 --no-pager"})
    if rep["cycles"].get("SKIPPED"):
        out.append({"severity": "HIGH", "text": "%d cycle(s) were SKIPPED in the last %dh (%s). A skipped cycle ingests no news and makes NO decisions." % (rep["cycles"]["SKIPPED"], rep["hours"], "; ".join(rep["skip_reasons"]) or "?"),
                    "fix": "sudo bash /opt/paper-agent/scripts/cleanup-ram.sh   (frees leftover RAM-disk files; check the [memory] line in the health check)"})
    if rep["cycles"].get("ERROR"):
        out.append({"severity": "MEDIUM", "text": "%d cycle(s) finished with stage errors in the last %dh." % (rep["cycles"]["ERROR"], rep["hours"]), "fix": "sudo /opt/paper-agent/scripts/doctor.sh"})
    ev6 = conn.execute("SELECT COUNT(*) FROM events WHERE created>?", (six_h,)).fetchone()[0]
    if rep["last_event"] and rep["last_event"] < iso(now - dt.timedelta(hours=3)) and rep["last_cycle"] and rep["last_cycle"] >= two_h:
        out.append({"severity": "HIGH", "text": "Cycles are running but NO news item has been turned into an event since %s: the feeds may be failing or everything is being seen as duplicate." % to_nz(rep["last_event"]),
                    "fix": "Settings -> News & data feeds (check status); run the health check"})
    if ev6 >= 15:
        cand6 = conn.execute("SELECT COUNT(*) FROM events WHERE created>? AND gate LIKE 'candidate%'", (six_h,)).fetchone()[0]
        if cand6 == 0:
            top = conn.execute("SELECT gate,COUNT(*) n FROM events WHERE created>? GROUP BY gate ORDER BY n DESC LIMIT 3", (six_h,)).fetchall()
            out.append({"severity": "INFO", "text": "%d events in the last 6h, none qualified as trade candidates. Top reasons: %s. (Normal overnight in the US; investigate if this persists during US hours.)" % (ev6, ", ".join("%s x%d" % (r["gate"] or "unrecorded", r["n"]) for r in top)), "fix": ""})
    return out


def text(rep: dict) -> str:
    L = ["=== DECISION FUNNEL, last %gh  (now %s, US market: %s) ===" % (rep["hours"], to_nz(rep["now"]), rep["market"])]
    L.append("cycles: %s | last cycle %s | last OK cycle %s" % (", ".join("%s=%d" % kv for kv in sorted(rep["cycles"].items())) or "none", to_nz(rep["last_cycle"]) if rep["last_cycle"] else "never", to_nz(rep["last_ok_cycle"]) if rep["last_ok_cycle"] else "never"))
    L.append("news items ingested: %d (last %s)" % (rep["news"], to_nz(rep["last_news"]) if rep["last_news"] else "never"))
    L.append("events created:     %d (last %s)" % (rep["events"], to_nz(rep["last_event"]) if rep["last_event"] else "never"))
    L.append("  -> trade candidates: %d" % rep["candidates"])
    L.append("  -> decisions:        %d  %s (last %s)" % (rep["decisions_total"], dict(rep["decisions"]), to_nz(rep["last_decision"]) if rep["last_decision"] else "never"))
    L.append("  -> trades opened:    %d" % rep["executed"])
    L.append("")
    L.append("WHY events did or did not become candidates:")
    for g, n in rep["gates"].items():
        L.append("  %5d  %-26s %s" % (n, g, GATE_LABELS.get(g, "")))
        for s in rep["samples"].get(g, [])[:2]:
            L.append("           e.g. [%s %s dir=%+.2f] %s" % (s["type"], ",".join(s["tickers"]) or "-", s["direction"], s["headline"]))
    if rep["mna"]:
        L.append("")
        L.append("Recent merger / acquisition events:")
        for m in rep["mna"][:10]:
            L.append("  [%-22s decision=%-14s %s] %s" % (m["gate"], m["decision"], ",".join(m["tickers"]) or "no company", m["headline"]))
    L.append("")
    L.append("FINDINGS:" if rep["findings"] else "FINDINGS: none - the pipeline looks healthy")
    for f in rep["findings"]:
        L.append("  [%s] %s%s" % (f["severity"], f["text"], ("\n        fix: " + f["fix"]) if f["fix"] else ""))
    return "\n".join(L)
