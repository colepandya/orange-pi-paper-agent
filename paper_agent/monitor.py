"""Monitoring engine: for every open position track original thesis, supporting vs contradicting evidence, thesis health and trend,
revise the plan within bounds, and raise exit signals (Thesis Broken / Risk Escalation; Superior Opportunity comes from the trader)."""
from __future__ import annotations

import datetime as dt
import json
import math
from typing import Dict, List, Optional

from . import db, ev, evidence as evidence_engine, marketdata as md, redteam, revisions, thesis as thesis_engine, trader
from .executor import swing_cap
from .timeutil import add_trading_days, iso, parse_ts, utcnow

VERSION = "5.0.0"


def tighten_stop(old: float, direction: float) -> float:
    """Adverse evidence tightens (never widens) the stop; floor -8%, ceiling -1.2%."""
    if direction >= -0.1:
        return old
    return max(-0.08, min(-0.012, old * (1 - 0.35 * min(1.0, abs(direction)))))


def ensure_plans(conn, cfg: dict, regime: dict, now) -> int:
    """Legacy / orphan positions without an active plan get an EV-derived one so the executor can manage them."""
    n = 0
    for p in conn.execute("SELECT p.* FROM positions p LEFT JOIN position_forecast_plans fp ON fp.position_id=p.id AND fp.active=1 WHERE p.status='OPEN' AND fp.id IS NULL").fetchall():
        cand = {"ticker": p["ticker"], "direction": 0.6 * (-1.0 if (p["side"] or "LONG") == "SHORT" else 1.0), "magnitude": 0.4, "event_type": "thesis_signal", "credibility": 0.6, "conf": 0.7, "age_hours": 0, "horizon": 8}
        f = ev.build(conn, cand, cfg, regime, now)
        f["target_return"] = f["target_return"] or 0.03
        with db.tx(conn):
            trader.insert_plan(conn, p["id"], p["decision_id"] or 0, f, float(p["entry_price"]), p["opened"], VERSION)
            db.audit(conn, "monitor", "plan_created_for_orphan", p["ticker"], {"position_id": p["id"]})
        n += 1
    return n


def _evidence_since(conn, ticker: str, views: List[dict], since_iso: str, now) -> dict:
    keys = {v["key"]: v["sign"] for v in views}
    rows = conn.execute("SELECT e.ticker,e.thesis_key,e.credibility,e.strength,e.novelty,e.direction,e.event_id FROM evidence e JOIN events ev ON ev.id=e.event_id "
                        "WHERE ev.created>? AND (e.ticker=? OR e.thesis_key IN (%s))" % ",".join("?" * len(keys)) if keys else
                        "SELECT e.ticker,e.thesis_key,e.credibility,e.strength,e.novelty,e.direction,e.event_id FROM evidence e JOIN events ev ON ev.id=e.event_id WHERE ev.created>? AND e.ticker=?",
                        ([since_iso, ticker] + list(keys)) if keys else [since_iso, ticker]).fetchall()
    num = den = 0.0
    support = contra = 0.0
    for r in rows:
        w = r["credibility"] * r["strength"] * r["novelty"]
        if w <= 0:
            continue
        d = r["direction"] * (1.0 if r["ticker"] else keys.get(r["thesis_key"], 1.0))
        num += w * d
        den += w
        if d > 0:
            support += w * d
        else:
            contra += w * -d
    return {"dir": num / den if den else 0.0, "mass": den, "n": len(rows), "support": support, "contra": contra}


def monitor(conn, cfg: dict, regime: dict, now=None, stress: Optional[dict] = None, holdings: Optional[List[dict]] = None) -> dict:
    now = now or utcnow()
    out = {"open_positions": 0, "plans_changed": 0, "signals": [], "details": []}
    ensure_plans(conn, cfg, regime, now)
    worst_contrib = None
    if stress and holdings and stress["worst_loss"] > cfg["risk"]["max_stress_loss_pct"] * 1.25:
        from .portfolio import _shock
        worst_s = min(stress["scenarios"], key=lambda k: stress["scenarios"][k]["pnl_pct"])
        worst_contrib = min(holdings, key=lambda h: h["value"] * _shock(h, worst_s))["position_id"]
    seen: Dict[int, set] = {}
    for p in conn.execute("SELECT * FROM positions WHERE status='OPEN'").fetchall():
        out["open_positions"] += 1
        plan = conn.execute("SELECT * FROM position_forecast_plans WHERE position_id=? AND active=1 ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
        views = thesis_engine.views_for_ticker(conn, p["ticker"], regime.get("regime", "unknown"))
        tkey = p["thesis_key"]
        th = conn.execute("SELECT * FROM theses WHERE key=?", (tkey,)).fetchone() if tkey else None
        if not th and views:
            th = conn.execute("SELECT * FROM theses WHERE key=?", (max(views, key=lambda v: abs(v["tilted_edge"]))["key"],)).fetchone()
        since = plan["created"] if plan else p["opened"]
        sg = -1.0 if (p["side"] or "LONG") == "SHORT" else 1.0                           # everything below is judged from the POSITION's side: bearish news SUPPORTS a short
        ev_new = _evidence_since(conn, p["ticker"], views, since, now)
        if sg < 0:
            ev_new = dict(ev_new, dir=-ev_new["dir"], support=ev_new["contra"], contra=ev_new["support"])
        tev = evidence_engine.ticker_evidence(conn, p["ticker"], now)
        tnet = sg * (th["net_strength"] if th else 0.0)
        trend = sg * (th["trend"] if th else 0.0)
        state = th["state"] if th else "n/a"
        bonus = {"Strong": 10, "Active": 6, "Overcrowded": -5, "Weakening": -15, "Broken": -35} if sg > 0 else {"Strong": -10, "Active": -6, "Overcrowded": 5, "Weakening": 15, "Broken": 35}
        health = 50 + 25 * sg * tev["net"] + 20 * tnet + 150 * max(-0.1, min(0.1, trend)) + bonus.get(state, 0)
        health = max(0.0, min(100.0, health))
        signals = []
        if th and state == "Broken" and sg > 0:                                           # a broken bullish thesis is CONFIRMATION for a short, not a reason to leave
            signals.append(("THESIS_BROKEN", 0.9, "thesis %s is Broken (P=%.0f%%)" % (th["name"], th["probability"] * 100)))
        elif health < 25:
            signals.append(("THESIS_BROKEN", 0.75, "position health %.0f/100 (net ticker evidence %+.2f, thesis net %+.2f%s)" % (health, sg * tev["net"], tnet, ", seen from the short side" if sg < 0 else "")))
        if ev_new["dir"] <= -0.55 and ev_new["mass"] >= 0.15:
            signals.append(("RISK_ESCALATION", 0.8, "adverse evidence since plan: direction %+.2f over mass %.2f" % (ev_new["dir"], ev_new["mass"])))
        if worst_contrib == p["id"]:
            signals.append(("RISK_ESCALATION", 0.6, "largest contributor to worst stress loss %.0f%% (limit %.0f%%)" % (stress["worst_loss"] * 100, cfg["risk"]["max_stress_loss_pct"] * 100)))
        for v in views:
            if v["sign"] * sg < 0 and v["state"] == "Strong" and v["probability"] >= 0.8:
                signals.append(("RISK_ESCALATION", 0.6, "position is a victim of strong thesis %s" % v["name"]))
        rv = conn.execute("SELECT valuation_view FROM redteam_reviews WHERE ticker=? ORDER BY id DESC LIMIT 1", (p["ticker"],)).fetchone()
        revisions.log_confidence(conn, p["id"], now, float(th["confidence_quality"]) if th else 0.0, float(plan["confidence"]) / 100.0 if plan else 0.0,
                                 float(plan["target_probability"]) if plan else 0.0, health / 100.0, rv["valuation_view"] if rv else "unknown", {"health": health, "state": state})
        with db.tx(conn):
            conn.execute("INSERT INTO position_health(position_id,ts,thesis_key,thesis_p,thesis_trend,health,support,contradiction,signals_json) VALUES(?,?,?,?,?,?,?,?,?)",
                         (p["id"], iso(now), th["key"] if th else None, th["probability"] if th else None, trend, health, ev_new["support"], ev_new["contra"], json.dumps([s[0] for s in signals])))
            seen[p["id"]] = {s[0] for s in signals}
            for trig, sev, why in signals:
                conn.execute("INSERT OR IGNORE INTO exit_signals(position_id,created,trigger,severity,reason,status) VALUES(?,?,?,?,?,'OPEN')", (p["id"], iso(now), trig, sev, why))
                out["signals"].append({"ticker": p["ticker"], "trigger": trig, "reason": why})
            for sig in conn.execute("SELECT id,trigger,created FROM exit_signals WHERE position_id=? AND status='OPEN'", (p["id"],)).fetchall():          # (not `sg`: that is the position's side, +1 / -1)
                age_h = (now - parse_ts(sig["created"])).total_seconds() / 3600
                if sig["trigger"] in ("THESIS_BROKEN", "RISK_ESCALATION") and sig["trigger"] not in seen[p["id"]] and age_h > 0.2:
                    conn.execute("UPDATE exit_signals SET status='CLEARED' WHERE id=?", (sig["id"],))
                elif sig["trigger"] == "SUPERIOR_OPPORTUNITY" and age_h > 24:
                    conn.execute("UPDATE exit_signals SET status='EXPIRED' WHERE id=?", (sig["id"],))
        # bounded plan revision on material new evidence
        change = "UNCHANGED"
        if plan and ev_new["n"] and ev_new["mass"] >= 0.05:
            w = ev_new["dir"]
            pt, ps, pdays = float(plan["target_return"]), float(plan["stop_return"]), int(plan["max_hold_days"])
            nt = max(0.005, min(0.20, pt * (1 + max(-0.35, min(0.35, w * 0.35)))))
            ns = tighten_stop(ps, w)
            nd = swing_cap(cfg, max(1, min(15, pdays + (1 if w > 0.35 else -2 if w < -0.35 else -1 if w < -0.10 else 0))))      # swing: never longer than max hold + 1 session
            if abs(nt - pt) >= 0.0025 or abs(ns - ps) >= 0.0025 or nd != pdays:
                change = "EXPANDED" if w > 0.10 else "TIGHTENED" if w < -0.10 else "REVISED"
                entry = float(p["entry_price"])
                with db.tx(conn):
                    conn.execute("UPDATE position_forecast_plans SET active=0 WHERE id=?", (plan["id"],))
                    cols = [c[1] for c in conn.execute("PRAGMA table_info(position_forecast_plans)")]
                    vals = dict(plan)
                    vals.update(created=iso(now), plan_version=VERSION, active=1, target_return=nt, stop_return=ns, target_price=entry * (1 + sg * nt), stop_price=entry * (1 + sg * ns), max_hold_days=nd,
                                estimated_exit=iso(add_trading_days(p["opened"], nd)),
                                rationale="Live reassessment: %d new evidence items, net direction %+.2f. Target %+.2f%%->%+.2f%%, stop %+.2f%%->%+.2f%%, hold %d->%d." % (ev_new["n"], w, pt * 100, nt * 100, ps * 100, ns * 100, pdays, nd))
                    ins = [c for c in cols if c != "id"]
                    cur = conn.execute("INSERT INTO position_forecast_plans(%s) VALUES(%s)" % (",".join(ins), ",".join("?" * len(ins))), [vals[c] for c in ins])
                    for fld, oldv, newv in (("target_price", plan["target_price"], entry * (1 + sg * nt)), ("stop_price", plan["stop_price"], entry * (1 + sg * ns)), ("max_hold_days", pdays, nd)):
                        revisions.record(conn, now, "position", str(p["id"]), p["ticker"], fld, oldv, newv, vals["rationale"], float(plan["confidence"]) / 100.0, float(plan["confidence"]) / 100.0, {"direction": w, "n_evidence": ev_new["n"]})
                    conn.execute("INSERT INTO position_plan_changes(position_id,created,prior_plan_id,new_plan_id,change_type,reason,event_uids_json) VALUES(?,?,?,?,?,?,?)",
                                 (p["id"], iso(now), plan["id"], cur.lastrowid, change, vals["rationale"], "[]"))
                out["plans_changed"] += 1
        out["details"].append({"ticker": p["ticker"], "health": round(health, 1), "thesis": th["key"] if th else None, "state": state, "evidence_new": ev_new["n"], "change": change})
    with db.tx(conn):
        for tw in redteam.check_tripwires(conn, now):
            out["signals"].append({"ticker": tw["ticker"], "trigger": "THESIS_BROKEN", "reason": "tripwire: " + tw["why"]})
        conn.execute("INSERT INTO position_monitor_runs(created,status,open_positions,events_reviewed,plans_changed,detail_json) VALUES(?,?,?,?,?,?)",
                     (iso(now), "OK", out["open_positions"], sum(d["evidence_new"] for d in out["details"]), out["plans_changed"], json.dumps(out["details"])))
    return out
