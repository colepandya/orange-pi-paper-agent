"""Decision funnel: news -> events -> candidates -> decisions -> trades, with the REASON each item dropped out.
Answers "why are there no decisions?" with evidence instead of guesses, and flags the usual causes (skipped cycles, no news, nothing qualifying)."""
from __future__ import annotations

import datetime as dt
import json
from typing import Dict, List, Optional

from . import config, candqueue
from .evidence import GATE_LABELS
from .timeutil import iso, market_state, parse_ts, to_nz, utcnow, SESSION_LABEL


def indication_text(ind: dict) -> str:
    """'[trading -18.4% vs previous close, pre-market, 8 min ago -> WILL BE CANCELLED at the open if it holds]'"""
    flag = {"cancel_down": " -> WILL BE CANCELLED at the open if it holds (gap down)", "cancel_up": " -> WILL BE CANCELLED at the open if it holds (the move already happened)"}.get(ind.get("verdict"), "")
    return "[trading %+.1f%% vs previous close, %s%s]" % (ind["gap"] * 100, indication_age(ind), flag)


def indication_age(ind: dict) -> str:
    m = ind["age_minutes"]
    return "%s, %s" % (SESSION_LABEL.get(ind["session"], ind["session"].lower() or "quote"),
                       "just now" if m < 2 else ("%d min ago" % m if m < 120 else "%.0fh ago" % (m / 60)))


def report(conn, hours: float = 24, now=None, samples: int = 3, cfg: Optional[dict] = None) -> dict:
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
    # cycles that never finished: killed by the unit timeout / out-of-memory / a restart. Each leaves the stage it was in (checkpointed before every stage).
    died = []
    for r in conn.execute("SELECT id,started,status,report_json FROM cycle_runs WHERE (status='KILLED' OR (status='RUNNING' AND started<?)) AND started>? ORDER BY id DESC LIMIT 30",
                          (iso(now - dt.timedelta(minutes=12)), since)):
        try:
            stage = (json.loads(r["report_json"] or "{}").get("current")) or "before the first stage"
        except ValueError:
            stage = "?"
        died.append({"started": r["started"], "stage": stage})
    slow = []
    last_ok = conn.execute("SELECT report_json FROM cycle_runs WHERE status IN ('OK','ERROR') ORDER BY id DESC LIMIT 1").fetchone()
    if last_ok:
        try:
            st = json.loads(last_ok["report_json"] or "{}").get("stages", {})
            slow = sorted(((k, v.get("seconds", 0)) for k, v in st.items() if isinstance(v, dict)), key=lambda kv: -kv[1])[:4]
        except ValueError:
            pass
    pend = []
    for r in conn.execute("SELECT ticker,status,created,ev_net,reason FROM decisions WHERE status IN ('AWAITING_MARKET','AWAITING_QUOTE','AWAITING_CAPACITY','HELD') ORDER BY id DESC LIMIT 30"):
        try:
            rj = json.loads(r["reason"] or "{}")
            pgates = rj.get("gates") or []
            ref = rj.get("ref_price")
        except ValueError:
            pgates, ref = [], None
        from . import prices as _prices
        ind = _prices.indication(conn, r["ticker"], ref, now)                   # where it is trading right now (pre-market / after-hours) versus the price it was decided against
        if ind:
            rk = (cfg or config.DEFAULTS)["risk"]                                # what the last look will do with that gap at the open (your thresholds)
            ind["verdict"] = "cancel_down" if ind["gap"] <= -float(rk["max_gap_down_pct"]) else ("cancel_up" if ind["gap"] >= float(rk["max_gap_up_pct"]) else None)
        pend.append({"ticker": r["ticker"], "status": r["status"], "since": r["created"], "ev_net": r["ev_net"], "waiting_for": pgates[-1] if pgates else "", "indication": ind})
    from . import db as _db
    base = _db.error_baseline(conn, now)
    err_since = max(since, base) if base else since
    recent_err = conn.execute("SELECT COUNT(*) FROM cycle_runs WHERE status='ERROR' AND started>?", (err_since,)).fetchone()[0]
    old_err = conn.execute("SELECT COUNT(*) FROM cycle_runs WHERE status='ERROR' AND started>? AND started<=?", (since, err_since)).fetchone()[0] if base else 0
    errs: Dict[str, int] = {}
    for r in conn.execute("SELECT report_json FROM cycle_runs WHERE status='ERROR' AND started>? ORDER BY id DESC LIMIT 400", (err_since,)):
        try:
            rj = json.loads(r["report_json"] or "{}")
        except ValueError:
            continue
        for name, st in (rj.get("stages") or {}).items():
            if isinstance(st, dict) and st.get("error"):
                k = "%s: %s%s" % (name, str(st["error"])[:160], (" [%s]" % st["where"]) if st.get("where") else "")
                errs[k] = errs.get(k, 0) + 1
    stage_errors = sorted(errs.items(), key=lambda kv: -kv[1])[:6]
    q = candqueue.counts(conn)
    oldest = conn.execute("SELECT MIN(created) FROM candidate_queue WHERE status='NEW'").fetchone()[0]
    rep = {"hours": hours, "now": iso(now), "market": market_state(now)["session"], "cycles": cyc, "skip_reasons": sorted(set(skip_reasons)), "died": died, "pending": pend, "stage_errors": stage_errors, "error_cycles_now": recent_err, "error_cycles_history": old_err, "baseline": base, "slowest_stages": slow, "queue": q, "queue_oldest": oldest,
           "last_cycle": one("SELECT MAX(started) FROM cycle_runs"), "last_ok_cycle": one("SELECT MAX(started) FROM cycle_runs WHERE status='OK'"),
           "news": one("SELECT COUNT(*) FROM news WHERE ingested>?", since), "last_news": one("SELECT MAX(ingested) FROM news"),
           "events": one("SELECT COUNT(*) FROM events WHERE created>?", since), "last_event": one("SELECT MAX(created) FROM events"),
           "gates": gates, "samples": sample,
           "candidates": sum(n for g, n in gates.items() if g.startswith("candidate")),
           "decisions": dec, "decisions_total": sum(dec.values()), "last_decision": one("SELECT MAX(created) FROM decisions"),
           "executed": one("SELECT COUNT(*) FROM positions WHERE opened>?", since), "mna": mna}
    try:
        rows = conn.execute("SELECT via,status,merge_note,domain FROM article_reads WHERE fetched_at>?", (since,)).fetchall()
        blocked = [r["domain"] for r in conn.execute("SELECT domain FROM article_domains WHERE blocked_until>? ORDER BY domain", (iso(now),))]
        rep["articles"] = {"read": len(rows), "page": sum(1 for r in rows if r["via"] == "page"), "snippet": sum(1 for r in rows if r["via"] != "page"), "changed": sum(1 for r in rows if r["merge_note"]),
                           "left_alone": blocked[:12], "left_alone_n": len(blocked)}
    except Exception:  # noqa: BLE001
        rep["articles"] = {"read": 0, "page": 0, "snippet": 0, "changed": 0, "left_alone": [], "left_alone_n": 0}
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
    if rep["died"]:
        by_stage: Dict[str, int] = {}
        for d in rep["died"]:
            by_stage[d["stage"]] = by_stage.get(d["stage"], 0) + 1
        out.append({"severity": "HIGH", "text": "%d cycle(s) in the last %dh STARTED BUT NEVER FINISHED (killed by the time limit, out-of-memory, or a restart). They died in: %s. Events were saved but, before build 14, the trade candidates were lost with them; now candidates are queued and decided on the next cycle."
                    % (len(rep["died"]), rep["hours"], ", ".join("%s x%d" % kv for kv in sorted(by_stage.items(), key=lambda kv: -kv[1]))),
                    "fix": "journalctl -u paper-agent-cycle --since '-3h' --no-pager | grep -iE 'timed out|killed|oom|memory' ; sudo bash /opt/paper-agent/scripts/cleanup-ram.sh"})
    if rep["queue"].get("NEW") and rep["queue_oldest"] and parse_ts(rep["queue_oldest"]) and (now - parse_ts(rep["queue_oldest"])).total_seconds() > 45 * 60:
        out.append({"severity": "MEDIUM", "text": "%d trade candidate(s) have been waiting more than 45 minutes in the queue (oldest %s): the entries stage is not draining them." % (rep["queue"]["NEW"], to_nz(rep["queue_oldest"])), "fix": "check the last cycle's report: sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent funnel"})
    mk = market_state(now)["execution"]
    stuck = [p for p in rep["pending"] if p["status"] == "AWAITING_QUOTE"]
    if stuck and mk:
        out.append({"severity": "MEDIUM", "text": "%d BUY decision(s) cannot execute because no verified price is available while the market is OPEN. Example: %s %s" % (len(stuck), stuck[0]["ticker"], stuck[0]["waiting_for"][:230]),
                    "fix": "Settings -> API keys (need >= 2 quote providers with fresh timestamps); run the pre-open checklist"})
    waiting = [p for p in rep["pending"] if p["status"] == "AWAITING_MARKET"]
    if waiting and not mk:
        from .timeutil import next_market_open
        out.append({"severity": "INFO", "text": "%d BUY decision(s) are waiting for the US market to open (%s). They are re-evaluated and executed by the first cycle after the open (one is scheduled at 09:35 New York)." % (len(waiting), to_nz(next_market_open(now))), "fix": ""})
    if rep.get("error_cycles_history"):
        out.append({"severity": "INFO", "text": "%d earlier cycle(s) with errors predate the current build / were acknowledged and are NOT counted (still stored as history)." % rep["error_cycles_history"], "fix": ""})
    if rep["error_cycles_now"]:
        top = "; ".join("%s (x%d)" % kv for kv in rep["stage_errors"][:3]) or "no message recorded (older build)"
        out.append({"severity": "MEDIUM", "text": "%d cycle(s) finished with stage errors since the current build started. Top errors: %s" % (rep["error_cycles_now"], top), "fix": "send this line to whoever maintains the code; each error names the stage and the exact exception"})
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
    if rep.get("error_cycles_history"):
        L.append("(%d earlier cycle(s) with errors are from before this build / acknowledged: kept as history, not counted)" % rep["error_cycles_history"])
    L.append("cycles: %s | last cycle %s | last OK cycle %s" % (", ".join("%s=%d" % kv for kv in sorted(rep["cycles"].items())) or "none", to_nz(rep["last_cycle"]) if rep["last_cycle"] else "never", to_nz(rep["last_ok_cycle"]) if rep["last_ok_cycle"] else "never"))
    ar = rep.get("articles") or {}
    if ar.get("read"):
        L.append("articles read: %d (full page %d, feed snippet only %d); the article changed the reading of %d%s" % (ar["read"], ar["page"], ar["snippet"], ar["changed"],
                 ("; sites being left alone after refusing us: %s" % ", ".join(ar["left_alone"])) if ar.get("left_alone_n") else ""))
    if rep["died"]:
        L.append("!! %d cycle(s) STARTED BUT NEVER FINISHED; died in: %s" % (len(rep["died"]), ", ".join(sorted({d["stage"] for d in rep["died"]}))))
    if rep["stage_errors"]:
        L.append("STAGE ERRORS (what actually failed):")
        for m, n in rep["stage_errors"]:
            L.append("   x%-4d %s" % (n, m))
    if rep["slowest_stages"]:
        L.append("slowest stages of the last cycle: " + ", ".join("%s %.0fs" % kv for kv in rep["slowest_stages"]))
    L.append("candidate queue: %s%s" % (rep["queue"] or "empty", (" (oldest waiting since %s)" % to_nz(rep["queue_oldest"])) if rep["queue_oldest"] else ""))
    if rep["pending"]:
        L.append("BUY decisions waiting (%d):" % len(rep["pending"]))
        for p in rep["pending"][:8]:
            L.append("   %-6s %-17s EV %s  %s%s" % (p["ticker"], p["status"], ("%+.2f%%" % (p["ev_net"] * 100)) if p["ev_net"] is not None else "n/a", p["waiting_for"][:150], ("  " + indication_text(p["indication"])) if p.get("indication") else ""))
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
