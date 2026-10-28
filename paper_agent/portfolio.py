"""Portfolio intelligence: mark-to-market equity (the legacy code used cost), exposures, stress tests, survival, recovery time."""
from __future__ import annotations

import json
import math
from typing import Dict, List, Optional

from . import db, quotes
from .timeutil import iso, utcnow

AI_THESES = ("ai_infrastructure", "ai_datacentres", "ai_networking", "semiconductor_demand", "ai_power_demand", "ai_cooling")
CYCLICAL = {"Materials", "Industrials", "Energy", "Consumer Discretionary", "Semiconductors"}
DEFENSIVE = {"Consumer Staples", "Utilities", "Healthcare"}


def holdings(conn, now=None) -> List[dict]:
    out = []
    for p in conn.execute("SELECT p.*,l.remaining_shares,l.remaining_cost,l.phase FROM positions p LEFT JOIN position_lifecycle l ON l.position_id=p.id WHERE p.status='OPEN'"):
        shares = float(p["remaining_shares"] if p["remaining_shares"] is not None else p["shares"])
        cost = float(p["remaining_cost"] if p["remaining_cost"] is not None else p["cost"])
        q = quotes.valuation_price(conn, p["ticker"], now)
        px = q["price"] if q else (cost / shares if shares else 0.0)
        c = conn.execute("SELECT * FROM companies WHERE ticker=?", (p["ticker"],)).fetchone()
        tk = conn.execute("SELECT thesis_key FROM thesis_companies WHERE ticker=? LIMIT 1", (p["ticker"],)).fetchone()
        out.append({"position_id": p["id"], "ticker": p["ticker"], "shares": shares, "cost": cost, "price": px, "value": shares * px, "price_source": q["source"] if q else "COST",
                    "price_age": q["age_seconds"] if q else None, "sector": (c["sector"] if c and c["sector"] else p["sector"] or "Unclassified"), "country": c["country"] if c and c["country"] else "US",
                    "currency": c["currency"] if c and c["currency"] else "USD", "beta": (c["beta"] if c and c["beta"] is not None else 1.0), "adv_usd": c["adv_usd"] if c else None,
                    "market_cap": c["market_cap"] if c else None, "industry": (c["industry"] or "") if c else "", "dna": c["dna"] if c else None,
                    "thesis_key": p["thesis_key"] or (tk["thesis_key"] if tk else None), "phase": p["phase"] or "OPEN", "opened": p["opened"]})
    return out


def equity_of(conn, hs: Optional[List[dict]] = None, now=None) -> dict:
    hs = holdings(conn, now) if hs is None else hs
    c = db.cash(conn)
    mv = sum(h["value"] for h in hs)
    return {"cash": c, "market_value": mv, "equity": c + mv, "gross": mv / (c + mv) if c + mv > 0 else 0.0, "cost": sum(h["cost"] for h in hs)}


def exposures(hs: List[dict], equity: float) -> Dict[str, Dict[str, float]]:
    dims = {"sector": {}, "theme": {}, "country": {}, "currency": {}, "liquidity": {}, "factor": {}, "issuer": {}}
    for h in hs:
        w = h["value"] / equity if equity else 0
        for dim, key in (("sector", h["sector"]), ("theme", h["thesis_key"] or "none"), ("country", h["country"]), ("currency", h["currency"]), ("issuer", h["ticker"])):
            dims[dim][key] = dims[dim].get(key, 0.0) + w
        adv = h["adv_usd"]
        liq = "unknown" if not adv else "high" if adv >= 1e8 else "medium" if adv >= 1e7 else "low"
        dims["liquidity"][liq] = dims["liquidity"].get(liq, 0.0) + w
        beta = h["beta"] or 1.0
        for fac, cond in (("high_beta", beta > 1.3), ("low_beta", beta < 0.8), ("growth", h["dna"] in ("Hyper Growth", "Speculative")), ("quality", h["dna"] == "Quality Compounder"),
                          ("cyclical", h["sector"] in CYCLICAL), ("defensive", h["sector"] in DEFENSIVE), ("small_cap", bool(h["market_cap"] and h["market_cap"] < 2e9))):
            if cond:
                dims["factor"][fac] = dims["factor"].get(fac, 0.0) + w
    return dims


# ------------------------------------------------------------------------------ stress testing
def _shock(h: dict, scenario: str) -> float:
    sec, beta = h["sector"], (h["beta"] if h["beta"] is not None else 1.0)
    thesis_ai = h["thesis_key"] in AI_THESES
    if scenario == "ai_crash":
        return -0.40 if thesis_ai else -0.35 if sec == "Semiconductors" else -0.25 if sec in ("Technology", "Software") else -0.08 * max(beta, 0.5)
    if scenario == "market_crash":
        return -0.25 * max(beta, 0.3)
    if scenario == "recession":
        return -0.28 if sec == "Financials" else -0.22 if sec in CYCLICAL else -0.18 if sec in ("Technology", "Software") else -0.06 if sec in DEFENSIVE else -0.12
    if scenario == "rate_shock":
        return {"Technology": -.18, "Software": -.20, "Real Estate": -.15, "Utilities": -.10, "Consumer Discretionary": -.12, "Financials": -.06, "Energy": -.03}.get(sec, -0.08)
    if scenario == "china_taiwan":
        if h["country"] in ("TW", "CN"):
            return -0.50
        return {"Semiconductors": -.40, "Technology": -.22, "Consumer Discretionary": -.15, "Defense": 0.08, "Energy": 0.03}.get(sec, -0.12)
    if scenario == "credit_crisis":
        if sec == "Materials" and "gold" in (h["industry"] or "").lower():
            return 0.05
        return -0.35 if sec == "Financials" else -0.30 if sec == "Real Estate" or (h["market_cap"] and h["market_cap"] < 2e9) else -0.22 if sec == "Industrials" else -0.18 * max(beta, 0.4)
    return -0.1


SCENARIOS = ["ai_crash", "market_crash", "recession", "rate_shock", "china_taiwan", "credit_crisis"]
SCENARIO_NAMES = {"ai_crash": "AI Crash", "market_crash": "Market Crash", "recession": "Recession", "rate_shock": "Rate Shock", "china_taiwan": "China-Taiwan Conflict", "credit_crisis": "Credit Crisis"}


def scenario_probabilities(conn, regime: dict) -> Dict[str, float]:
    def tp(key, default):
        r = conn.execute("SELECT probability FROM theses WHERE key=?", (key,)).fetchone()
        return float(r[0]) if r else default
    crowd = conn.execute("SELECT crowding FROM narrative_states WHERE thesis_key='ai_infrastructure' ORDER BY ts DESC LIMIT 1").fetchone()
    probs = regime.get("probs") or {}
    return {"ai_crash": 0.08 + 0.10 * (float(crowd[0]) if crowd and crowd[0] is not None else 0.0),
            "market_crash": 0.06 * (2 - float(regime.get("risk_mult", 0.75))),
            "recession": 0.40 * tp("recession_risk", 0.28),
            "rate_shock": 0.10 * (1 + 2 * probs.get("liquidity_contraction", 0.0)),
            "china_taiwan": 0.40 * tp("china_taiwan_escalation", 0.08),
            "credit_crisis": 0.30 * tp("credit_stress", 0.15)}


def stress(hs: List[dict], equity: float, cfg: dict, probs: Dict[str, float]) -> dict:
    res = {}
    for s in SCENARIOS:
        loss = sum(h["value"] * _shock(h, s) for h in hs) / equity if equity else 0.0
        res[s] = {"name": SCENARIO_NAMES[s], "pnl_pct": loss, "p": probs.get(s, 0.0)}
    worst = min((v["pnl_pct"] for v in res.values()), default=0.0)
    ruin = float(cfg["risk"]["ruin_loss_pct"])
    p_ruin = sum(v["p"] for v in res.values() if -v["pnl_pct"] > ruin)
    survival = max(0.0, 1.0 - p_ruin)
    recovery = (math.log(1 / (1 + worst)) / math.log(1.08)) * 365 if worst < 0 else 0.0
    return {"scenarios": res, "worst_loss": -worst if worst < 0 else 0.0, "survival_probability": survival, "recovery_days": recovery, "ruin_threshold": ruin}


def drawdown(conn, equity_now: float) -> float:
    r = conn.execute("SELECT MAX(equity) FROM portfolio_snapshots").fetchone()
    peak = max(float(r[0] or 0), db.start_capital(conn), equity_now)
    return max(0.0, 1 - equity_now / peak) if peak else 0.0


def daily_return(conn, equity_now: float, now=None) -> float:
    day = (now or utcnow()).date().isoformat()
    r = conn.execute("SELECT equity FROM portfolio_snapshots WHERE ts<? ORDER BY ts DESC LIMIT 1", (day,)).fetchone()
    return equity_now / float(r[0]) - 1 if r and r[0] else 0.0


def snapshot(conn, cfg: dict, regime: dict, now=None) -> dict:
    now = now or utcnow()
    hs = holdings(conn, now)
    eq = equity_of(conn, hs, now)
    ex = exposures(hs, eq["equity"])
    st = stress(hs, eq["equity"], cfg, scenario_probabilities(conn, regime))
    dd = drawdown(conn, eq["equity"])
    conn.execute("INSERT INTO portfolio_snapshots(ts,equity,cash,gross,drawdown,regime,survival_probability,max_stress_loss,recovery_days,exposures_json,stress_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (iso(now), eq["equity"], eq["cash"], eq["gross"], dd, regime.get("regime"), st["survival_probability"], st["worst_loss"], st["recovery_days"], json.dumps(ex, sort_keys=True), json.dumps(st, sort_keys=True)))
    day = now.date().isoformat()
    conn.execute("INSERT OR REPLACE INTO daily_values(day,equity,cash,deployed,drawdown) VALUES(?,?,?,?,?)", (day, eq["equity"], eq["cash"], eq["market_value"], dd))
    conn.execute("DELETE FROM portfolio_snapshots WHERE id NOT IN (SELECT id FROM portfolio_snapshots ORDER BY id DESC LIMIT 5000)")
    return {**eq, "drawdown": dd, "exposures": ex, "stress": st, "holdings": hs}
