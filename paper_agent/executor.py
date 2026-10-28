"""Virtual exit executor: atomic, idempotent, stale-quote-refusing.

Trigger precedence: thesis/risk signals > stop (stop-first when ordering is unknown) > target (partial + runner) > time expiry.
A target hit unusually early triggers a bounded re-forecast and may extend the hold instead of selling."""
from __future__ import annotations

import hashlib
import json
from typing import Callable, Dict, Optional

from . import db, learning, quotes
from .timeutil import add_trading_days, iso, market_state, parse_ts, trading_days_between, utcnow

VERSION = "5.0.0"
SIGNAL_TRIGGERS = ("THESIS_BROKEN", "RISK_ESCALATION", "SUPERIOR_OPPORTUNITY")


def action_id(position: int, plan: int, trigger: str, phase: str, shares: float) -> str:
    return hashlib.sha256(("%s:%s:%s:%s:%.8f" % (position, plan, trigger, phase, float(shares))).encode()).hexdigest()


def choose_trigger(conn, p, plan, q: dict, held: int, phase: str) -> tuple:
    price = q["price"]
    sig = conn.execute("SELECT * FROM exit_signals WHERE position_id=? AND status='OPEN' ORDER BY severity DESC LIMIT 1", (p["id"],)).fetchone()
    if sig:
        return {"THESIS_BROKEN": "THESIS_INVALIDATED", "RISK_ESCALATION": "RISK_ESCALATION", "SUPERIOR_OPPORTUNITY": "SUPERIOR_OPPORTUNITY"}[sig["trigger"]], sig["reason"], sig["id"]
    stop, target = float(plan["stop_price"]), float(plan["target_price"])
    if price <= stop:
        return ("RUNNER_STOP" if phase == "RUNNER_OPEN" else "STOP_REACHED"), "price %.4f <= stop %.4f" % (price, stop), None
    if price >= target:
        return ("RUNNER_TARGET" if phase == "RUNNER_OPEN" else "TARGET_REACHED"), "price %.4f >= target %.4f" % (price, target), None
    if held >= int(plan["max_hold_days"]):
        return ("RUNNER_TIME" if phase == "RUNNER_OPEN" else "TIME_EXPIRED"), "held %d sessions >= maximum %d" % (held, plan["max_hold_days"]), None
    return None, "no trigger", None


def rapid_reforecast(conn, p, plan, q: dict, held: int, now) -> Optional[dict]:
    if held >= max(1, int(plan["max_hold_days"]) // 2 + 1):
        return None
    if conn.execute("SELECT 1 FROM target_reforecasts WHERE position_id=? AND prior_plan_id=?", (p["id"], plan["id"])).fetchone():
        return None
    observed = q["price"] / float(p["entry_price"]) - 1
    velocity = observed / max(0.001, float(plan["target_return"]))
    remaining_bull = float(plan["bull_return"]) - observed
    adverse = conn.execute("SELECT 1 FROM exit_signals WHERE position_id=? AND status='OPEN'", (p["id"],)).fetchone()
    hold = velocity >= 1 and remaining_bull >= max(0.012, float(plan["target_return"]) * 0.5) and float(plan["confidence"]) >= 65 and not adverse
    reason = "Target reached in %d session(s), %.2fx planned velocity; bull-case residual %+.2f%%; confidence %.0f%%. Decision: %s." % (held, velocity, remaining_bull * 100, float(plan["confidence"]), "extend and hold" if hold else "follow exit plan")
    newid = None
    if hold:
        ext = min(0.20, max(observed + 0.01, float(plan["runner_target_return"]), float(plan["bull_return"]) * 0.9))
        days = min(15, max(held + 1, int(plan["max_hold_days"]) + 1))
        entry = float(p["entry_price"])
        cols = [c[1] for c in conn.execute("PRAGMA table_info(position_forecast_plans)")]
        vals = dict(plan)
        vals.update(created=iso(now), plan_version=VERSION, active=1, target_return=ext, target_price=entry * (1 + ext), max_hold_days=days, estimated_exit=iso(add_trading_days(p["opened"], days)), rationale=reason)
        conn.execute("UPDATE position_forecast_plans SET active=0 WHERE id=?", (plan["id"],))
        ins = [c for c in cols if c != "id"]
        newid = conn.execute("INSERT INTO position_forecast_plans(%s) VALUES(%s)" % (",".join(ins), ",".join("?" * len(ins))), [vals[c] for c in ins]).lastrowid
    conn.execute("INSERT INTO target_reforecasts(position_id,prior_plan_id,new_plan_id,created,decision,observed_return,elapsed_sessions,velocity_ratio,remaining_bull,confidence,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (p["id"], plan["id"], newid, iso(now), "HOLD_EXTENDED" if hold else "EXIT_PLAN", observed, held, velocity, remaining_bull, float(plan["confidence"]), reason))
    return {"hold": hold, "reason": reason}


def _create_runner_plan(conn, p, plan, now) -> int:
    entry = float(p["entry_price"])
    t, s = float(plan["runner_target_return"]), float(plan["runner_stop_return"])
    days = min(15, max(1, int(plan["max_hold_days"]) + 2))
    cols = [c[1] for c in conn.execute("PRAGMA table_info(position_forecast_plans)")]
    vals = dict(plan)
    vals.update(created=iso(now), plan_version=VERSION, active=1, target_return=t, stop_return=s, target_price=entry * (1 + t), stop_price=entry * (1 + s), max_hold_days=days,
                estimated_exit=iso(add_trading_days(p["opened"], days)), partial_exit_pct=1.0, rationale="Runner plan after partial target exit: target %+.2f%%, protected stop %+.2f%%, max hold %d sessions." % (t * 100, s * 100, days))
    conn.execute("UPDATE position_forecast_plans SET active=0 WHERE id=?", (plan["id"],))
    ins = [c for c in cols if c != "id"]
    return conn.execute("INSERT INTO position_forecast_plans(%s) VALUES(%s)" % (",".join(ins), ",".join("?" * len(ins))), [vals[c] for c in ins]).lastrowid


def commit_action(conn, cfg: dict, p, life, plan, trigger: str, reason: str, q: dict, shares: float, aid: str, signal_id: Optional[int], now) -> dict:
    slip = cfg["portfolio"]["slippage_bps"]
    fill = max(0.0001, q["price"] * (1 - slip / 1e4))
    with db.tx(conn):
        existing = conn.execute("SELECT status FROM virtual_exit_actions WHERE action_id=?", (aid,)).fetchone()
        if existing and existing["status"] == "COMMITTED":
            return {"result": "ALREADY_COMMITTED", "action_id": aid}
        rem_sh, rem_cost = float(life["remaining_shares"]), float(life["remaining_cost"])
        shares = min(float(shares), rem_sh)
        if shares <= 0 or rem_sh <= 0:
            return {"result": "NOTHING_TO_SELL"}
        frac = shares / rem_sh
        released, proceeds = round(rem_cost * frac, 2), round(fill * shares, 2)
        pnl = round(proceeds - released, 2)
        left_sh, left_cost = round(rem_sh - shares, 8), round(rem_cost - released, 2)
        partial = left_sh > 1e-8
        prior = db.cash(conn)
        new_cash = round(prior + proceeds, 2)
        if not existing:
            conn.execute("INSERT INTO virtual_exit_actions(action_id,position_id,decision_id,plan_id,created,status,trigger,phase_before,shares_requested,observed_price,quote_source,quote_time,quote_age_seconds,reason,plan_json,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (aid, p["id"], p["decision_id"] or 0, plan["id"], iso(now), "RESERVED", trigger, life["phase"], shares, q["price"], "+".join(q.get("providers_used") or []), q.get("quoted_at"), q.get("age_seconds"), reason,
                          json.dumps(dict(plan), sort_keys=True, default=str), json.dumps({"quote": {k: q.get(k) for k in ("price", "providers_used", "quoted_at", "age_seconds")}}, default=str)))
        conn.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)", (iso(now), "VIRTUAL_PARTIAL_SELL" if partial else "VIRTUAL_SELL_V5", proceeds, "%s:%d:%s" % (p["ticker"], p["id"], aid[:16]), new_cash))
        total_real = round(float(life["realized_pnl"]) + pnl, 2)
        if partial:
            rid = _create_runner_plan(conn, p, plan, now)
            conn.execute("UPDATE positions SET shares=?,cost=?,review_code='RUNNER_OPEN' WHERE id=?", (left_sh, left_cost, p["id"]))
            conn.execute("UPDATE position_lifecycle SET phase='RUNNER_OPEN',remaining_shares=?,remaining_cost=?,realized_pnl=?,last_action_at=?,last_action_id=?,runner_plan_id=?,updated=? WHERE position_id=?",
                         (left_sh, left_cost, total_real, iso(now), aid, rid, iso(now), p["id"]))
            phase_after = "RUNNER_OPEN"
        else:
            conn.execute("UPDATE positions SET shares=0,cost=0,status='CLOSED',closed=?,exit_price=?,pnl=?,review_code=? WHERE id=?", (iso(now), fill, total_real, trigger, p["id"]))
            conn.execute("UPDATE position_forecast_plans SET active=0 WHERE position_id=?", (p["id"],))
            conn.execute("UPDATE position_lifecycle SET phase='CLOSED',remaining_shares=0,remaining_cost=0,realized_pnl=?,last_action_at=?,last_action_id=?,updated=? WHERE position_id=?", (total_real, iso(now), aid, iso(now), p["id"]))
            phase_after = "CLOSED"
            orig_cost = conn.execute("SELECT SUM(cost_released) c FROM virtual_exit_actions WHERE position_id=? AND status='COMMITTED'", (p["id"],)).fetchone()["c"] or 0.0
            learning.position_closed(conn, p["id"], total_real, orig_cost + released, trigger, now)
        conn.execute("UPDATE virtual_exit_actions SET status='COMMITTED',phase_after=?,shares_filled=?,fill_price=?,slippage_bps=?,proceeds=?,cost_released=?,realized_pnl=?,prior_cash=?,new_cash=?,committed_at=? WHERE action_id=?",
                     (phase_after, shares, fill, slip, proceeds, released, pnl, prior, new_cash, iso(now), aid))
        if signal_id:
            conn.execute("UPDATE exit_signals SET status='CONSUMED',consumed_at=? WHERE id=?", (iso(now), signal_id))
        db.audit(conn, "executor", "virtual_exit", p["ticker"], {"trigger": trigger, "pnl": pnl, "phase_after": phase_after})
    return {"result": phase_after, "trigger": trigger, "pnl": pnl, "action_id": aid}


def run(conn, cfg: dict, now=None, quote_fn: Optional[Callable] = None) -> dict:
    now = now or utcnow()
    quote_fn = quote_fn or (lambda c, t, purpose: quotes.get_quote(c, t, cfg, purpose))
    checked = committed = extended = blocked = errors = 0
    details = []
    rows = conn.execute("SELECT p.*,l.phase lphase,l.remaining_shares,l.remaining_cost,l.realized_pnl,l.last_action_at FROM positions p JOIN position_lifecycle l ON l.position_id=p.id WHERE p.status='OPEN'").fetchall()
    session = market_state(now)
    for p in rows:
        checked += 1
        try:
            plan = conn.execute("SELECT * FROM position_forecast_plans WHERE position_id=? AND active=1 ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
            if not plan:
                details.append({"ticker": p["ticker"], "result": "NO_ACTIVE_PLAN"})
                continue
            if not session["execution"]:
                blocked += 1
                details.append({"ticker": p["ticker"], "result": "BLOCKED_MARKET_%s" % session["session"]})
                continue
            q = quote_fn(conn, p["ticker"], "exit")
            if not q or q.get("status") != "PASS" or not q.get("execution_eligible") or q.get("price") is None or (q.get("age_seconds") is not None and q["age_seconds"] > cfg["quotes"]["max_age_seconds"]):
                blocked += 1
                details.append({"ticker": p["ticker"], "result": "BLOCKED_NO_FRESH_QUOTE"})
                continue
            held = trading_days_between(p["opened"], now)
            life = {"phase": p["lphase"], "remaining_shares": p["remaining_shares"], "remaining_cost": p["remaining_cost"], "realized_pnl": p["realized_pnl"]}
            trigger, reason, sig_id = choose_trigger(conn, p, plan, q, held, p["lphase"])
            if not trigger:
                details.append({"ticker": p["ticker"], "result": "HOLD"})
                continue
            if trigger == "TARGET_REACHED" and p["lphase"] == "OPEN":
                with db.tx(conn):
                    rf = rapid_reforecast(conn, p, plan, q, held, now)
                if rf and rf["hold"]:
                    extended += 1
                    details.append({"ticker": p["ticker"], "result": "HOLD_EXTENDED", "reason": rf["reason"]})
                    continue
            frac = float(plan["partial_exit_pct"]) if trigger == "TARGET_REACHED" else 1.0
            shares = float(p["remaining_shares"]) * frac
            aid = action_id(p["id"], plan["id"], trigger, p["lphase"], shares)
            r = commit_action(conn, cfg, p, life, plan, trigger, reason, q, shares, aid, sig_id, now)
            if r["result"] in ("RUNNER_OPEN", "CLOSED"):
                committed += 1
            details.append({"ticker": p["ticker"], **r})
        except Exception as e:  # noqa: BLE001 - one position's failure must not stop the others
            errors += 1
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            details.append({"ticker": p["ticker"], "result": "ERROR", "error": "%s: %s" % (type(e).__name__, e)})
    with db.tx(conn):
        conn.execute("INSERT INTO executor_runs(created,status,positions_checked,actions_committed,holds_extended,blocked_stale,errors,detail_json) VALUES(?,?,?,?,?,?,?,?)",
                     (iso(now), "OK" if not errors else "ERROR", checked, committed, extended, blocked, errors, json.dumps(details, sort_keys=True, default=str)))
    return {"checked": checked, "committed": committed, "extended": extended, "blocked": blocked, "errors": errors, "details": details}
