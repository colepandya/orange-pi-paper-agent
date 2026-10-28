"""Position sizing. Never from conviction alone: EV, thesis strength, risk, portfolio fit, liquidity and regime all bind."""
from __future__ import annotations

from typing import Dict, List, Optional

from . import portfolio as pf


def size_position(forecast: dict, ticker_meta: dict, state: dict, cfg: dict, regime: dict, thesis_info: dict) -> dict:
    """state: {'equity','cash','holdings':[...]}. Returns {'pct','amount','reasons':[...],'binding':str}."""
    r = cfg["risk"]
    eq = state["equity"]
    reasons: List[str] = []
    sigma_h = max(forecast["sigma_h"], 0.01)
    kelly = max(0.0, forecast["ev_net"] / (sigma_h ** 2))
    base = min(0.25 * kelly, 1.0)
    reasons.append("quarter-Kelly %.1f%% from EV %.2f%% / var %.4f" % (base * 100, forecast["ev_net"] * 100, sigma_h ** 2))
    pct, binding = base, "kelly"

    def cap(val: float, label: str):
        nonlocal pct, binding
        if val < pct:
            pct, binding = val, label
            reasons.append("capped by %s at %.1f%%" % (label, val * 100))

    cap(r["max_position_pct"], "max position")
    stop = abs(forecast["stop_return"]) or 0.05
    cap(r["risk_per_trade_pct"] / stop, "risk-per-trade (stop %.1f%%)" % (stop * 100))
    state_mult = {"Strong": 1.0, "Active": 0.9, "Forming": 0.7, "Emerging": 0.5, "Overcrowded": 0.5, "Weakening": 0.4, "Broken": 0.0}.get(thesis_info.get("state") or "", 0.8 if not thesis_info.get("state") else 0.6)
    thesis_mult = state_mult * (0.85 + 0.15 * (0.5 + 0.5 * thesis_info.get("score", 0.0)))
    mults = {"thesis": thesis_mult, "regime": float(regime.get("risk_mult", 0.75)), "confidence": 0.5 + 0.5 * forecast["confidence"], "risk": 1.0 - 0.4 * forecast["risk_score"] / 100.0}
    for k, m in mults.items():
        pct *= m
    reasons.append("multipliers " + ", ".join("%s x%.2f" % (k, v) for k, v in mults.items()))
    adv = ticker_meta.get("adv_usd")
    if adv:
        cap(0.02 * adv / eq, "liquidity (2% of ADV)")
    ticker = forecast["ticker"]
    hs = state["holdings"]
    issuer = sum(h["value"] for h in hs if h["ticker"] == ticker) / eq
    cap(max(0.0, r["max_issuer_pct"] - issuer), "issuer limit")
    if thesis_info.get("primary"):
        theme = sum(h["value"] for h in hs if h["thesis_key"] == thesis_info["primary"]) / eq
        cap(max(0.0, r["max_theme_pct"] - theme), "theme limit")
    gross = sum(h["value"] for h in hs) / eq
    cap(max(0.0, r["max_gross_pct"] - gross), "gross limit")
    cap(max(0.0, (state["cash"] / eq) - r["min_cash_pct"]), "cash reserve")
    # survival: shrink until the worst stress loss fits the budget
    probs = state.get("scenario_probs") or {}
    for _ in range(6):
        trial = hs + [{"ticker": ticker, "value": pct * eq, "sector": ticker_meta.get("sector", "Unclassified"), "beta": ticker_meta.get("beta") or 1.0, "country": ticker_meta.get("country", "US"),
                       "industry": ticker_meta.get("industry", ""), "market_cap": ticker_meta.get("market_cap"), "thesis_key": thesis_info.get("primary")}]
        st = pf.stress(trial, eq, cfg, probs)
        if st["worst_loss"] <= r.get("max_stress_loss_pct", 0.20) or pct <= 0.0:
            break
        pct *= 0.75
        binding = "stress survival"
    if binding == "stress survival":
        reasons.append("scaled to keep worst stress loss <= %.0f%%" % (r.get("max_stress_loss_pct", 0.20) * 100))
    pct = max(0.0, pct)
    return {"pct": pct, "amount": pct * eq, "reasons": reasons, "binding": binding, "ok": pct >= r["min_position_pct"]}
