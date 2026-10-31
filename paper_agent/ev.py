"""Expected Value engine: EV = sum(P(scenario) x outcome) over Bull / Base / Bear / Tail / Black Swan, net of costs.

Negative-EV opportunities are rejected (OMEGA principle 5). Long-only: bearish events produce an AVOID verdict, not a short."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Dict, Optional

from . import earnings as earnings_mod, db, flows, marketdata as md, thesis as thesis_engine
from .timeutil import iso, utcnow

# mean absolute 1-session..horizon abnormal move priors per event type (before learning scales them)
MOVE_PRIOR = {"earnings_beat": .045, "earnings_miss": .050, "guidance_raise": .055, "guidance_cut": .060, "contract_win": .030, "contract_loss": .035,
              "regulatory_approval": .070, "regulatory_action": .060, "merger_acquisition": .180, "capital_raise": .045, "buyback_dividend": .018,
              "cyber_incident": .040, "litigation": .030, "production_disruption": .040, "partnership": .020, "product_launch": .020, "analyst_action": .015,
              "leadership_change": .020, "insider_filing": .012, "structural_theme": .030, "bankruptcy_distress": .250, "technology_shift": .030, "thesis_signal": .025}
Z = {"bull": 1.0, "base": 0.0, "bear": -1.0, "tail": -2.5, "black_swan": -5.0}


def move_scale(conn, event_type: str) -> float:
    v = db.param(conn, "move_scale:" + event_type)
    return float(v) if isinstance(v, (int, float)) else 1.0


def calibrate_p(conn, p: float) -> float:
    cal = db.param(conn, "calibration")
    if isinstance(cal, dict) and "a" in cal and "b" in cal:
        return max(0.02, min(0.98, cal["a"] + cal["b"] * p))
    return p


def materiality_ratio(conn, ticker: str, amount: Optional[float]) -> Optional[float]:
    if not amount:
        return None
    r = conn.execute("SELECT revenue,market_cap,backlog FROM company_fundamentals WHERE ticker=?", (ticker,)).fetchone()
    c = conn.execute("SELECT market_cap FROM companies WHERE ticker=?", (ticker,)).fetchone()
    vals = []
    if r:
        vals += [amount / float(r[k]) for k in ("revenue", "market_cap", "backlog") if r[k]]
    if c and c["market_cap"]:
        vals.append(amount / float(c["market_cap"]))
    return max(vals) if vals else None


def scenario_table(e: float, sigma_h: float, p_dir: float, risk_mult: float) -> Dict[str, dict]:
    tau = max(-0.2, min(0.2, (p_dir - 0.5) * 0.8))
    p_tail, p_black = 0.04 * (2 - risk_mult), 0.008 * (2 - risk_mult)
    m = 1 - p_tail - p_black
    w = {"bull": 0.22 + tau, "base": 0.46, "bear": max(0.05, 0.22 - tau)}
    s = sum(w.values())
    probs = {k: v / s * m for k, v in w.items()}
    probs["tail"], probs["black_swan"] = p_tail, p_black
    out = {}
    for k, z in Z.items():
        r = e + z * sigma_h
        if k == "black_swan":
            r = max(r, -0.45)
        out[k] = {"p": probs[k], "ret": r}
    return out


def build(conn, cand: dict, cfg: dict, regime: dict, now=None, universe=None) -> dict:
    now = now or utcnow()
    t = cand["ticker"]
    etype = cand.get("event_type", "thesis_signal")
    direction = cand["direction"]
    sign = 1.0 if direction >= 0 else -1.0
    closes = md.closes(conn, t, 260)
    comp = conn.execute("SELECT * FROM companies WHERE ticker=?", (t,)).fetchone()
    vol_d = md.daily_vol(conn, t, 60, default=(comp["vol_daily"] if comp and comp["vol_daily"] else 0.025))
    sw = cfg.get("strategy") or {}
    swing = bool(sw.get("swing_enabled"))
    h0 = int(max(2, min(15, round(cand.get("horizon", 8) or 8))))                       # how long the NEWS is expected to keep working
    h = int(max(1, min(h0, int(sw.get("max_hold_days", 3))))) if swing else h0          # swing: we only stay for the first part of it
    capture = min(1.0, math.sqrt(h / float(h0))) if swing else 1.0                     # share of the expected move that plays out inside our shorter window
    sigma_h = vol_d * math.sqrt(h)
    prior = MOVE_PRIOR.get(etype, 0.02) * move_scale(conn, etype)
    mag = max(0.05, min(1.0, cand.get("magnitude", 0.4)))
    ratio = materiality_ratio(conn, t, cand.get("amount_usd"))
    econ = prior * (0.4 + 0.6 * mag)
    if ratio is not None:
        econ = 0.5 * econ + 0.5 * max(0.012, min(0.18, 0.012 + ratio * 0.35))
    econ *= abs(direction) ** 0.5
    econ *= capture
    tt = thesis_engine.ticker_thesis_score(conn, t, regime.get("regime", "unknown"))
    align = tt["score"] * sign
    fl = flows.flow_score(conn, t, comp["sector"] if comp else None, now)
    edge_mult = max(0.4, min(1.6, 1.0 + 0.30 * align + 0.12 * fl["score"] * sign))
    r5 = md.ret_n(closes, 5)
    priced_in = max(0.0, min(0.20, (r5 or 0.0) * sign)) * 0.8
    e = sign * (econ * edge_mult - priced_in)   # signed residual edge after subtracting what the price already moved in the event direction
    # confidence: source, entity, thesis quality, data completeness, learned history
    n_hist = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
    completeness = (comp["completeness"] if comp else 0.0)
    conf = max(0.1, min(0.95, 0.20 + 0.28 * cand.get("credibility", 0.5) + 0.15 * cand.get("conf", 0.7) + 0.12 * (1 - min(1, cand.get("age_hours", 0) / 24.0))
                              + 0.12 * completeness + 0.08 * (tt["views"][0]["confidence_quality"] if tt["views"] else 0.0) + 0.05 * min(1.0, n_hist / 100.0)))
    p_dir = calibrate_p(conn, max(0.05, min(0.95, 0.5 + 0.30 * math.tanh(e / max(sigma_h, 1e-3)) * (0.5 + conf) + 0.08 * align)))
    scen = scenario_table(e, sigma_h, p_dir, float(regime.get("risk_mult", 0.75)))
    ev_gross = sum(v["p"] * v["ret"] for v in scen.values())
    cost = 2 * cfg["portfolio"]["slippage_bps"] / 1e4 + 0.0005
    ev_net = ev_gross - cost
    p_pos = sum(v["p"] for v in scen.values() if v["ret"] > cost)
    adv = comp["adv_usd"] if comp and comp["adv_usd"] else md.adv_usd(conn, t)
    if swing:
        stop = max(float(sw["stop_min_pct"]), min(float(sw["stop_max_pct"]), float(sw["stop_vol_mult"]) * vol_d))
        target = max(float(sw["target_min_pct"]), min(float(sw["target_max_pct"]), e - cost)) if e > cost else 0.0
    else:
        stop = max(0.02, min(0.10, 1.25 * vol_d * math.sqrt(min(h, 5))))
        target = max(0.012, min(0.20, e - cost)) if e > cost else 0.0
    p_hit = max(0.05, min(0.90, stop / (target + stop) + max(-0.15, min(0.15, ev_net / max(sigma_h, 1e-3) * 0.15)))) if target else 0.0
    p_tail = scen["tail"]["p"] + scen["black_swan"]["p"]
    liq_risk = 0.0 if (adv or 0) >= cfg["risk"]["min_adv_usd"] * 5 else (0.5 if adv and adv >= cfg["risk"]["min_adv_usd"] else 1.0)
    risk_score = 100 * max(0.0, min(1.0, 0.35 * min(sigma_h / 0.15, 1) + 0.25 * min(1, p_tail / 0.15) + 0.20 * (1 - conf) + 0.20 * liq_risk))
    verdict, why = "BUY", []
    if sign < 0:
        verdict, why = "AVOID_BEARISH_LONG_ONLY", ["bearish event; long-only book"]
    elif ev_net <= 0:
        verdict, why = "REJECT_NEGATIVE_EV", ["EV net %.2f%% <= 0" % (ev_net * 100)]
    elif ev_net < cfg["risk"]["min_ev_net"]:
        verdict, why = "WATCH", ["EV net %.2f%% below minimum %.2f%%" % (ev_net * 100, cfg["risk"]["min_ev_net"] * 100)]
    elif conf < cfg["risk"]["min_confidence"]:
        verdict, why = "WATCH", ["confidence %.0f%% below minimum" % (conf * 100)]
    elif p_pos < 0.45:
        verdict, why = "WATCH", ["P(positive) %.0f%% too low" % (p_pos * 100)]
    elif swing and sw.get("avoid_earnings", True) and etype not in earnings_mod.EARNINGS_EVENT_TYPES and earnings_mod.inside_hold(conn, t, now, h):
        er = earnings_mod.inside_hold(conn, t, now, h)
        verdict, why = "WATCH", ["earnings report due %s, inside the %d-session hold: a gap can blow straight through a 1-3%% stop" % (er["label"], h)]
    elif swing and target and target / stop < float(sw["min_reward_risk"]):
        verdict, why = "WATCH", ["swing reward:risk %.2f below minimum %.2f (target %.1f%% vs stop %.1f%%)" % (target / stop, float(sw["min_reward_risk"]), target * 100, stop * 100)]
    elif swing and vol_d < float(sw["min_daily_vol_pct"]):
        verdict, why = "WATCH", ["too quiet for a swing: it moves %.2f%% a day, minimum %.2f%%" % (vol_d * 100, float(sw["min_daily_vol_pct"]) * 100)]
    elif adv is not None and adv < cfg["risk"]["min_adv_usd"]:
        verdict, why = "REJECT_ILLIQUID", ["ADV $%.1fM below minimum" % (adv / 1e6)]
    runner = max(target * 1.6, scen["bull"]["ret"]) if target else 0.0
    inputs = {"event_type": etype, "direction": direction, "magnitude": mag, "amount_usd": cand.get("amount_usd"), "materiality_ratio": ratio, "vol_daily": vol_d, "horizon_days": h, "swing": swing, "partial_exit_pct": float(sw.get("partial_exit_pct", 0.6)) if swing else None, "event_horizon_days": h0, "capture": capture, "stop": stop,
              "move_prior": prior, "priced_in": priced_in, "thesis_score": tt["score"], "thesis_primary": tt["primary"], "flow": fl, "edge_mult": edge_mult, "confidence": conf,
              "p_dir": p_dir, "cost": cost, "adv_usd": adv, "regime": regime.get("regime"), "risk_mult": regime.get("risk_mult")}
    out = {"ticker": t, "direction": "LONG" if sign > 0 else "SHORT", "horizon_days": h, "p_up": p_dir, "p_positive": p_pos, "ev_gross": ev_gross, "ev_net": ev_net, "risk_score": risk_score,
           "confidence": conf, "target_return": target, "stop_return": -stop, "target_probability": p_hit, "runner_target": min(float(sw.get("runner_cap_pct", 0.35)) if swing else 0.35, runner), "scenarios": scen, "verdict": verdict, "why": why,
           "inputs": inputs, "thesis_key": tt["primary"], "sigma_h": sigma_h, "edge": e, "regime": regime.get("regime"), "completeness": completeness}
    out["hash"] = hashlib.sha256(json.dumps({"i": inputs, "s": scen}, sort_keys=True, default=str).encode()).hexdigest()
    return out


def materially_same(prev, f: dict) -> bool:
    """Is a re-made forecast for the same decision the same forecast for every practical purpose? (same verdict, edge within 0.3 points, same target / stop / horizon)."""
    try:
        return (prev["verdict"] == f["verdict"] and int(prev["horizon_days"]) == int(f["horizon_days"]) and abs(float(prev["ev_net"]) - float(f["ev_net"])) < 0.003
                and abs(float(prev["target_return"]) - float(f["target_return"])) < 0.0005 and abs(float(prev["stop_return"]) - float(f["stop_return"])) < 0.0005)
    except (TypeError, ValueError, KeyError):
        return False


def refresh(conn, fid: int, f: dict, now=None) -> int:
    """Bring an existing forecast row up to date in place (a retry of a waiting BUY that has not materially changed) instead of writing another near-identical row every cycle."""
    conn.execute("UPDATE forecasts SET created=?,thesis_key=?,direction=?,horizon_days=?,p_up=?,ev_gross=?,ev_net=?,risk_score=?,confidence=?,target_return=?,stop_return=?,regime=?,verdict=?,scenarios_json=?,inputs_json=?,hash=? WHERE id=?",
                 (iso(now), f["thesis_key"], f["direction"], f["horizon_days"], f["p_up"], f["ev_gross"], f["ev_net"], f["risk_score"], f["confidence"], f["target_return"], f["stop_return"], f["regime"], f["verdict"],
                  json.dumps(f["scenarios"], sort_keys=True), json.dumps(f["inputs"], sort_keys=True, default=str), f["hash"], fid))
    return fid


def persist(conn, f: dict, decision_id: Optional[int], event_id: Optional[int], now=None) -> int:
    cur = conn.execute("INSERT INTO forecasts(created,ticker,decision_id,event_id,thesis_key,direction,horizon_days,p_up,ev_gross,ev_net,risk_score,confidence,target_return,stop_return,regime,verdict,scenarios_json,inputs_json,hash) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (iso(now), f["ticker"], decision_id, event_id, f["thesis_key"], f["direction"], f["horizon_days"], f["p_up"], f["ev_gross"], f["ev_net"], f["risk_score"], f["confidence"],
                        f["target_return"], f["stop_return"], f["regime"], f["verdict"], json.dumps(f["scenarios"], sort_keys=True), json.dumps(f["inputs"], sort_keys=True, default=str), f["hash"]))
    return cur.lastrowid
