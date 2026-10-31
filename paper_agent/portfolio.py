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


def sign_of(side) -> int:
    """+1 for a long, -1 for a short: multiply a price change by this to get the POSITION's return."""
    return -1 if str(side or "LONG").upper() == "SHORT" else 1


def short_reserve(hs: List[dict], cfg: dict) -> float:
    """Cash held back against open shorts. Short proceeds are credited to cash, but they are not yours to spend: a broker holds them as collateral and wants a margin on top."""
    coll = float(((cfg or {}).get("instruments") or {}).get("short_collateral_pct", 1.5))
    return coll * sum(abs(h["value"]) for h in hs if h.get("side") == "SHORT")


def option_value(conn) -> float:
    """What the open options are worth at their last marks (a bought option is worth its premium at most as a loss, but it is an asset until closed)."""
    r = conn.execute("SELECT SUM(COALESCE(last_mark, entry_premium) * contracts * multiplier) FROM option_positions WHERE status='OPEN'").fetchone()
    return float(r[0] or 0.0)


def holdings(conn, now=None) -> List[dict]:
    out = []
    for p in conn.execute("SELECT p.*,l.remaining_shares,l.remaining_cost,l.phase FROM positions p LEFT JOIN position_lifecycle l ON l.position_id=p.id WHERE p.status='OPEN'"):
        shares = float(p["remaining_shares"] if p["remaining_shares"] is not None else p["shares"])
        cost = float(p["remaining_cost"] if p["remaining_cost"] is not None else p["cost"])
        q = quotes.valuation_price(conn, p["ticker"], now)
        px = q["price"] if q else (cost / shares if shares else 0.0)
        c = conn.execute("SELECT * FROM companies WHERE ticker=?", (p["ticker"],)).fetchone()
        tk = conn.execute("SELECT thesis_key FROM thesis_companies WHERE ticker=? LIMIT 1", (p["ticker"],)).fetchone()
        sg = sign_of(p["side"])
        out.append({"position_id": p["id"], "ticker": p["ticker"], "side": "SHORT" if sg < 0 else "LONG", "shares": shares, "cost": cost, "price": px,
                    "value": sg * shares * px,                                       # a short is a LIABILITY: negative, so cash + sum(value) is still the equity
                    "unrealized": sg * (shares * px - cost), "price_source": q["source"] if q else "COST",
                    "price_age": q["age_seconds"] if q else None, "sector": (c["sector"] if c and c["sector"] else p["sector"] or "Unclassified"), "country": c["country"] if c and c["country"] else "US",
                    "currency": c["currency"] if c and c["currency"] else "USD", "beta": (c["beta"] if c and c["beta"] is not None else 1.0), "adv_usd": c["adv_usd"] if c else None,
                    "market_cap": c["market_cap"] if c else None, "industry": (c["industry"] or "") if c else "", "dna": c["dna"] if c else None,
                    "thesis_key": p["thesis_key"] or (tk["thesis_key"] if tk else None), "phase": p["phase"] or "OPEN", "opened": p["opened"]})
    return out


def equity_of(conn, hs: Optional[List[dict]] = None, now=None) -> dict:
    hs = holdings(conn, now) if hs is None else hs
    c = db.cash(conn)
    mv = sum(h["value"] for h in hs)
    opt = option_value(conn)
    eq = c + mv + opt
    longs = sum(h["value"] for h in hs if h["value"] > 0)
    shorts = -sum(h["value"] for h in hs if h["value"] < 0)
    return {"cash": c, "market_value": mv, "equity": eq, "gross": (longs + shorts) / eq if eq > 0 else 0.0, "net": mv / eq if eq > 0 else 0.0, "long_value": longs, "short_value": shorts,
            "option_value": opt, "cost": sum(h["cost"] for h in hs)}


def exposures(hs: List[dict], equity: float) -> Dict[str, Dict[str, float]]:
    dims = {"sector": {}, "theme": {}, "country": {}, "currency": {}, "liquidity": {}, "factor": {}, "issuer": {}}
    for h in hs:
        w = abs(h["value"]) / equity if equity else 0                             # exposure is how much is at risk in a group, whichever way it points (net is reported separately)
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


def stress(hs: List[dict], equity: float, cfg: dict, probs: Dict[str, float], option_value_now: float = 0.0) -> dict:
    res = {}
    for s in SCENARIOS:
        loss = (sum(h["value"] * _shock(h, s) for h in hs) - option_value_now) / equity if equity else 0.0     # shorts are negative, so a crash is a GAIN for them; open options are assumed worthless in every stress
        res[s] = {"name": SCENARIO_NAMES[s], "pnl_pct": loss, "p": probs.get(s, 0.0)}
    worst = min((v["pnl_pct"] for v in res.values()), default=0.0)
    ruin = float(cfg["risk"]["ruin_loss_pct"])
    p_ruin = sum(v["p"] for v in res.values() if -v["pnl_pct"] > ruin)
    survival = max(0.0, 1.0 - p_ruin)
    recovery = (math.log(1 / (1 + worst)) / math.log(1.08)) * 365 if worst < 0 else 0.0
    return {"scenarios": res, "worst_loss": -worst if worst < 0 else 0.0, "survival_probability": survival, "recovery_days": recovery, "ruin_threshold": ruin}


def accrue_borrow_fees(conn, cfg: dict, hs: List[dict], now=None) -> dict:
    """Charge the borrow fee on every open short, at most once a day each (a ledger row, so it is auditable and idempotent): value x annual rate x days held since the last charge / 365."""
    from .timeutil import parse_ts
    now = now or utcnow()
    rate = float(((cfg or {}).get("instruments") or {}).get("short_borrow_fee_pct", 0.5)) / 100.0
    charged, total = 0, 0.0
    if rate <= 0:
        return {"charged": 0, "total": 0.0}
    for h in hs:
        if h.get("side") != "SHORT":
            continue
        last = conn.execute("SELECT MAX(created) FROM ledger WHERE kind='SHORT_BORROW_FEE' AND ref=?", ("fee:%d" % h["position_id"],)).fetchone()[0]
        since = parse_ts(last) if last else parse_ts(h["opened"])
        days = (now - since).total_seconds() / 86400.0 if since else 0.0
        if days < 0.9:
            continue
        fee = round(abs(h["value"]) * rate * days / 365.0, 2)
        if fee < 0.01:
            continue
        with db.tx(conn):
            cash = db.cash(conn)
            conn.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)", (iso(now), "SHORT_BORROW_FEE", -fee, "fee:%d" % h["position_id"], round(cash - fee, 2)))
        charged += 1
        total += fee
    return {"charged": charged, "total": round(total, 2)}


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
    st = stress(hs, eq["equity"], cfg, scenario_probabilities(conn, regime), eq.get("option_value", 0.0))
    dd = drawdown(conn, eq["equity"])
    conn.execute("INSERT INTO portfolio_snapshots(ts,equity,cash,gross,drawdown,regime,survival_probability,max_stress_loss,recovery_days,exposures_json,stress_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (iso(now), eq["equity"], eq["cash"], eq["gross"], dd, regime.get("regime"), st["survival_probability"], st["worst_loss"], st["recovery_days"], json.dumps(ex, sort_keys=True), json.dumps(st, sort_keys=True)))
    day = now.date().isoformat()
    conn.execute("INSERT OR REPLACE INTO daily_values(day,equity,cash,deployed,drawdown) VALUES(?,?,?,?,?)", (day, eq["equity"], eq["cash"], eq["market_value"], dd))
    conn.execute("DELETE FROM portfolio_snapshots WHERE id NOT IN (SELECT id FROM portfolio_snapshots ORDER BY id DESC LIMIT 5000)")
    return {**eq, "drawdown": dd, "exposures": ex, "stress": st, "holdings": hs}
