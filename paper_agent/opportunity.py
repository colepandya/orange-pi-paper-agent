"""Opportunity-cost engine: every new idea competes against existing positions, the watchlist and cash."""
from __future__ import annotations

import math
from typing import List, Optional

from . import marketdata as md, quotes
from .timeutil import add_trading_days, parse_ts, trading_days_between, utcnow

CASH_RATE = 0.04          # annual hurdle for sitting in cash


def cash_hurdle(horizon_days: int) -> float:
    return CASH_RATE * horizon_days / 252.0 + 0.002


def position_remaining(conn, h: dict, now=None) -> Optional[dict]:
    """Remaining expected value of an open position from its active plan, measured from the CURRENT price."""
    plan = conn.execute("SELECT * FROM position_forecast_plans WHERE position_id=? AND active=1 ORDER BY id DESC LIMIT 1", (h["position_id"],)).fetchone()
    pos = conn.execute("SELECT entry_price,opened FROM positions WHERE id=?", (h["position_id"],)).fetchone()
    if not plan or not pos or not h["price"]:
        return None
    entry = float(pos["entry_price"])
    r = h["price"] / entry - 1
    T, S, p = float(plan["target_return"]), float(plan["stop_return"]), float(plan["target_probability"])
    rem = p * (T - r) + (1 - p) * (S - r)
    left = max(1, int(plan["max_hold_days"]) - trading_days_between(pos["opened"], now or utcnow()))
    vol = md.daily_vol(conn, h["ticker"], 60, 0.025)
    return {"position_id": h["position_id"], "ticker": h["ticker"], "remaining_ev": rem, "per_risk": rem / max(vol * math.sqrt(left), 1e-3), "days_left": left, "return": r}


def evaluate(conn, forecast: dict, sizing_ok: bool, state: dict, cfg: dict, now=None, headroom_pct: float = 1.0) -> dict:
    """-> {'ok':bool,'reason':str,'rotate_out':position_id|None}.  headroom_pct = free capacity as a fraction of equity."""
    hurdle = cash_hurdle(forecast["horizon_days"])
    if forecast["ev_net"] < hurdle:
        return {"ok": False, "reason": "EV %.2f%% does not beat cash hurdle %.2f%%" % (forecast["ev_net"] * 100, hurdle * 100), "rotate_out": None}
    cand_score = forecast["ev_net"] / max(forecast["sigma_h"], 1e-3)
    if sizing_ok and headroom_pct >= cfg["risk"]["min_position_pct"]:
        return {"ok": True, "reason": "capacity available; beats cash hurdle", "rotate_out": None}
    rems = [x for x in (position_remaining(conn, h, now) for h in state["holdings"]) if x]
    if not rems:
        return {"ok": False, "reason": "no capacity and no rotation candidate", "rotate_out": None}
    weakest = min(rems, key=lambda x: x["per_risk"])
    if cand_score >= 1.5 * max(weakest["per_risk"], 0.0) + 0.05 and forecast["ev_net"] > weakest["remaining_ev"]:
        return {"ok": False, "reason": "capacity full: rotate out %s (remaining EV/risk %.2f vs candidate %.2f)" % (weakest["ticker"], weakest["per_risk"], cand_score),
                "rotate_out": weakest["position_id"], "rotate_ticker": weakest["ticker"], "superior": True}
    return {"ok": False, "reason": "capacity full and candidate not superior to weakest holding %s" % weakest["ticker"], "rotate_out": None}
