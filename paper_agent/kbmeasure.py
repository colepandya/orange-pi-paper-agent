"""Did the red team help? EVERY idea that reaches the red team is recorded (allowed, reduced, held or blocked) with its objections, rule ids, multipliers and a hypothetical entry, stop and target; afterwards the price
path is replayed from stored bars (stop or target first, 1/3/5-day returns, best and worst excursion) and the intervention is scored: gain avoided or missed, loss avoided or added, net economic benefit.
Nothing here ever promotes or removes a rule: it produces evidence, labelled by how much of it exists. Only recorded facts are used: the price replay starts at the recorded reference price and never at a later one."""
from __future__ import annotations

import datetime as dt
import json
import math
from typing import Dict, List, Optional

from . import db, kbevent, pathways, timeframe
from .timeutil import iso, parse_ts, utcnow

HARD = {"denied", "stale_catalyst", "wrong_company", "duplicate_catalyst", "contradiction_factual", "contradiction_superseded"}
CONF_BANDS = ((0.40, "low"), (0.65, "medium"), (9.0, "high"))


def conf_band(c) -> str:
    c = float(c or 0.0)
    for hi, name in CONF_BANDS:
        if c < hi:
            return name
    return "high"


def ref_price(conn, ticker: str, now) -> Dict[str, object]:
    """The price the hypothetical entry assumes: a stored quote from the last two days, else the latest stored close. The basis is recorded; no price from after `now` is ever used."""
    if "quote_cache" in db.tables(conn):
        q = conn.execute("SELECT price,quoted_at FROM quote_cache WHERE ticker=?", (ticker,)).fetchone()
        if q and q["quoted_at"] <= iso(now) and (now - parse_ts(q["quoted_at"])).total_seconds() <= 2 * 86400:
            return {"price": float(q["price"]), "basis": "stored quote at %s" % q["quoted_at"]}
    r = conn.execute("SELECT close,day FROM market_series WHERE symbol=? AND day<=? ORDER BY day DESC LIMIT 1", (ticker, iso(now)[:10])).fetchone()
    if r and r["close"]:
        return {"price": float(r["close"]), "basis": "close of %s" % r["day"], "day": r["day"]}
    return {"price": None, "basis": "no stored price for %s" % ticker}


def record(conn, cfg: dict, decision_id: Optional[int], cand: dict, f: dict, size_before_pct: float, size_after_pct: float, rt: Optional[dict], gate: Optional[dict], final: str, now=None, regime: str = "") -> Optional[int]:
    """One row per reviewed idea. `final` = allowed | reduced | blocked. Never raises into the trading path."""
    try:
        now = now or utcnow()
        from . import skeptic
        side = "SHORT" if f.get("verdict") == "SHORT" else "BUY"
        objs = []
        for b in (rt or {}).get("bear_points", []):
            objs.append({"rule": b.get("rule"), "severity": b.get("severity"), "class": b.get("action"), "mult": b.get("mult"), "evidence": (b.get("evidence") or "")[:200], "hard_veto": b.get("rule") in HARD and b.get("action") in ("veto", "hold")})
        would = (gate or {}).get("would") or {}
        mult = float((gate or {}).get("size_mult", 1.0) or 1.0)
        et = cand.get("event_type") or (f.get("inputs") or {}).get("event_type") or "thesis_signal"
        origin = "derived" if cand.get("origin") in ("graph", "relationship", "derived") else "direct"
        rp = ref_price(conn, cand["ticker"], now)
        tgt, stp = f.get("target_return"), f.get("stop_return")
        hold = int(round(float(f.get("horizon_days") or (cfg.get("strategy") or {}).get("max_hold_days") or 3)))
        cur = conn.execute("INSERT INTO redteam_interventions(decision_id,ticker,created,side,event_type,event_family,origin,regime,conf_band,mode,original_action,original_size_pct,final_action,final_size_pct,combined_mult,hard_veto,block_reason,"
                           "objections_json,hyp_entry,hyp_stop,hyp_target,horizon_days,entry_basis) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (decision_id, cand["ticker"], iso(now), side, et, skeptic.FAMILY.get(et, et), origin, regime, conf_band(f.get("confidence")), (cfg.get("intelligence") or {}).get("influence", "guard"), side, size_before_pct, final, size_after_pct,
                            mult, int(any(o["hard_veto"] for o in objs)), "; ".join((gate or {}).get("reasons") or []) if final == "blocked" else None,
                            json.dumps({"objections": objs, "would_active": {"ok": would.get("ok"), "size_mult": would.get("size_mult"), "reasons": would.get("reasons")}, "net_conviction": (rt or {}).get("net_conviction")}, default=str),
                            rp["price"], stp, tgt, hold, rp["basis"]))
        return cur.lastrowid
    except Exception:  # noqa: BLE001 - measurement must never break a trade decision
        return None


def _benefit(side: int, final: str, size_before: float, size_after: float, pnl: float) -> Dict[str, float]:
    """The economics of the intervention on a hypothetical position that earned `pnl` (a return, already side-adjusted) at the recorded size, in fractions of equity."""
    removed = max(0.0, size_before - size_after) / 100.0 if final != "blocked" else size_before / 100.0
    added = max(0.0, size_after - size_before) / 100.0
    gm = removed * max(0.0, pnl)
    la = removed * max(0.0, -pnl)
    ga = added * max(0.0, pnl)
    lad = added * max(0.0, -pnl)
    return {"gain_missed": gm, "loss_avoided": la, "gain_added": ga, "loss_added": lad, "net_benefit": la - gm - lad + ga}


def measure(conn, row: dict) -> Optional[dict]:
    """Replay one recorded idea against stored bars. None until the shortest horizon has data."""
    entry = row["hyp_entry"]
    if not entry:
        return None
    sg = -1.0 if row["side"] == "SHORT" else 1.0
    day = row["created"][:10]
    bs = pathways.bars(conn, row["ticker"], day, 25)
    hold = max(1, min(10, int(row["horizon_days"] or 3)))
    tgt = row["hyp_target"]
    stp = row["hyp_stop"]
    p1, p3, p5 = (pathways.measure(entry, sg, bs, tgt, stp, h) for h in (1, 3, 5))
    ph = pathways.measure(entry, sg, bs, tgt, stp, hold)
    if p1 is None:
        return None
    out = {"r1": p1["ret"], "r3": p3["ret"] if p3 else None, "r5": p5["ret"] if p5 else None, "mfe": (p5 or p3 or p1)["mfe"], "mae": (p5 or p3 or p1)["mae"]}
    if ph:
        fh = ph["first_hit"]
        out["first_hit"] = fh
        out["pnl"] = (tgt if fh == "target" else stp if fh in ("stop", "ambiguous_stop_assumed") else ph["ret"])        # the plan's own exits decide the hypothetical result: target, stop, or the return at the horizon
        out["status"] = "measured"
    else:
        out["first_hit"], out["pnl"], out["status"] = None, (p5 or p3 or p1)["ret"], "partial"
    return out


def measure_due(conn, cfg: dict, now=None, max_n: int = 60) -> dict:
    now = now or utcnow()
    rows = conn.execute("SELECT i.* FROM redteam_interventions i LEFT JOIN intervention_outcomes o ON o.intervention_id=i.id WHERE (o.intervention_id IS NULL OR o.status='partial') AND i.hyp_entry IS NOT NULL AND i.created<=? ORDER BY i.id LIMIT ?",
                        (iso(now - dt.timedelta(days=1)), max_n)).fetchall()
    done = part = 0
    for r in rows:
        r = dict(r)
        m = measure(conn, r)
        if not m:
            continue
        sg = -1.0 if r["side"] == "SHORT" else 1.0
        b = _benefit(sg, r["final_action"], float(r["original_size_pct"] or 0.0), float(r["final_size_pct"] or 0.0), float(m["pnl"] or 0.0))
        helped = None if r["final_action"] == "allowed" else int(b["net_benefit"] > 0)
        conn.execute("INSERT OR REPLACE INTO intervention_outcomes(intervention_id,measured,r1,r3,r5,mfe,mae,first_hit,helped,gain_avoided,gain_missed,loss_avoided,loss_added,net_benefit,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (r["id"], iso(now), m["r1"], m["r3"], m["r5"], m["mfe"], m["mae"], m["first_hit"], helped, b["gain_added"], b["gain_missed"], b["loss_avoided"], b["loss_added"], b["net_benefit"], m["status"]))
        done += m["status"] == "measured"
        part += m["status"] == "partial"
    return {"candidates": len(rows), "measured": done, "partial": part}


def wilson(k: int, n: int, z: float = 1.64):
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def scorecard(conn) -> dict:
    """By rule, event family, side, direct vs relationship-derived, market regime and confidence band. Each cell: how many ideas, how many were actually changed, mean hypothetical result of the idea, mean net benefit of the
    intervention, the share of interventions that helped (with an interval), and the maturity of the evidence. NOTHING is promoted or removed from this."""
    rows = [dict(r) for r in conn.execute("SELECT i.*,o.r1,o.r3,o.r5,o.mfe,o.mae,o.first_hit,o.helped,o.net_benefit,o.gain_missed,o.loss_avoided,o.loss_added,o.status AS ostatus FROM redteam_interventions i LEFT JOIN intervention_outcomes o ON o.intervention_id=i.id")]
    dims: Dict[str, Dict[str, List[dict]]] = {k: {} for k in ("rule", "event_family", "side", "origin", "regime", "confidence_band", "timeframe", "final_action")}
    for r in rows:
        try:
            objs = json.loads(r["objections_json"] or "{}").get("objections", [])
        except ValueError:
            objs = []
        keys = {"event_family": r["event_family"], "side": r["side"], "origin": r["origin"], "regime": r["regime"] or "unknown", "confidence_band": r["conf_band"], "timeframe": timeframe.from_sessions(r["horizon_days"]), "final_action": r["final_action"]}
        for d, v in keys.items():
            dims[d].setdefault(str(v), []).append(r)
        for rule in {o.get("rule") for o in objs if o.get("rule") and o.get("rule") != "residual"}:
            dims["rule"].setdefault(rule, []).append(r)
    out: Dict[str, dict] = {}
    for d, cells in dims.items():
        out[d] = {}
        for k, rs in sorted(cells.items()):
            meas = [x for x in rs if x["ostatus"] == "measured"]
            chg = [x for x in meas if x["final_action"] != "allowed"]
            helped = sum(1 for x in chg if x["helped"])
            lo, hi = wilson(helped, len(chg))
            out[d][k] = {"ideas": len(rs), "measured": len(meas), "changed_and_measured": len(chg), "mean_idea_5d_return": (sum(x["r5"] for x in meas if x["r5"] is not None) / max(1, sum(1 for x in meas if x["r5"] is not None))) if meas else None,
                         "mean_net_benefit": (sum(x["net_benefit"] or 0.0 for x in chg) / len(chg)) if chg else None, "helped_rate": (helped / len(chg)) if chg else None, "helped_low": lo if chg else None, "helped_high": hi if chg else None,
                         "gain_missed": sum(x["gain_missed"] or 0.0 for x in chg), "loss_avoided": sum(x["loss_avoided"] or 0.0 for x in chg), "loss_added": sum(x["loss_added"] or 0.0 for x in chg), "maturity": kbevent.maturity(len(chg) if d != "final_action" else len(meas))}
    out["totals"] = {"recorded": len(rows), "measured": sum(1 for r in rows if r["ostatus"] == "measured"), "partial": sum(1 for r in rows if r["ostatus"] == "partial"), "pending": sum(1 for r in rows if r["ostatus"] is None)}
    return out
