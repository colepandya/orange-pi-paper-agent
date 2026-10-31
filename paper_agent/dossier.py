"""Intelligence dossier: one auditable page per company - thesis, confidence components, forecast, evidence for AND against, red-team case,
tripwires, causal chain, and the full forecast-revision history. Built deterministically from stored data (no LLM call at render time)."""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from . import quotes, revisions, thesis as thesis_engine
from .timeutil import iso, utcnow


def _evidence(conn, ticker: str, views: List[dict], now) -> Dict[str, List[dict]]:
    keys = {v["key"]: v["sign"] for v in views}
    q = ("SELECT ev.ticker,ev.thesis_key,ev.direction,ev.credibility*ev.strength*ev.novelty*ev.predictive_power AS w,ev.source,ev.note,e.headline,e.published,e.event_type "
         "FROM evidence ev JOIN events e ON e.id=ev.event_id WHERE ev.expiry_date>? AND (ev.ticker=?" + (" OR ev.thesis_key IN (%s)" % ",".join("?" * len(keys)) if keys else "") + ") ORDER BY w DESC LIMIT 60")
    rows = conn.execute(q, [iso(now), ticker] + list(keys)).fetchall()
    sup, con = [], []
    for r in rows:
        d = r["direction"] * (1.0 if r["ticker"] else keys.get(r["thesis_key"], 1.0))
        item = {"headline": r["headline"], "source": r["source"], "type": r["event_type"], "weight": r["w"], "published": r["published"], "note": r["note"], "scope": r["ticker"] or ("thesis:" + (r["thesis_key"] or ""))}
        (sup if d > 0 else con).append(item)
    return {"supporting": sup[:6], "contradicting": con[:6]}


def build(conn, cfg: dict, ticker: str, now=None) -> Optional[dict]:
    now = now or utcnow()
    comp = conn.execute("SELECT * FROM companies WHERE ticker=?", (ticker,)).fetchone()
    pos = conn.execute("SELECT * FROM positions WHERE ticker=? ORDER BY (status='OPEN') DESC,id DESC LIMIT 1", (ticker,)).fetchone()
    reg = conn.execute("SELECT regime FROM regimes ORDER BY id DESC LIMIT 1").fetchone()
    regime = reg["regime"] if reg else "unknown"
    ts = thesis_engine.ticker_thesis_score(conn, ticker, regime)
    if not (comp or pos or ts["views"]):
        return None
    d: Dict = {"ticker": ticker, "generated": iso(now), "regime": regime, "company": dict(comp) if comp else {}, "position": None, "thesis": None}
    if ts["primary"]:
        th = conn.execute("SELECT * FROM theses WHERE key=?", (ts["primary"],)).fetchone()
        d["thesis"] = {"key": th["key"], "name": th["name"], "probability": th["probability"], "prior": th["prior"], "state": th["state"], "trend": th["trend"], "uncertainty": th["uncertainty"],
                       "quality": th["confidence_quality"], "support": th["support_score"], "contradiction": th["contradiction_score"], "role": next((v["sign"] for v in ts["views"] if v["key"] == th["key"]), 1.0)}
    d["other_theses"] = [{"name": v["name"], "p": v["probability"], "state": v["state"], "role": "beneficiary" if v["sign"] > 0 else "victim"} for v in ts["views"] if not d["thesis"] or v["key"] != d["thesis"]["key"]][:5]
    if pos:
        plan = conn.execute("SELECT * FROM position_forecast_plans WHERE position_id=? ORDER BY active DESC,id DESC LIMIT 1", (pos["id"],)).fetchone()
        q = quotes.valuation_price(conn, ticker, now)
        entry = float(pos["entry_price"])
        d["position"] = {"id": pos["id"], "status": pos["status"], "opened": pos["opened"], "entry": entry, "price": q["price"] if q else None, "side": pos["side"] or "LONG", "pnl_pct": ((-1 if (pos["side"] or "LONG") == "SHORT" else 1) * (q["price"] / entry - 1)) if q and entry else None,
                         "shares": pos["shares"], "closed": pos["closed"], "exit_reason": pos["review_code"]}
        if plan:
            d["forecast"] = {"target_price": plan["target_price"], "stop_price": plan["stop_price"], "target_return": plan["target_return"], "stop_return": plan["stop_return"], "probability": plan["target_probability"],
                             "expected_return": plan["expected_return"], "review_date": plan["estimated_exit"], "max_hold_days": plan["max_hold_days"], "bear": plan["bear_return"], "base": plan["base_return"], "bull": plan["bull_return"],
                             "rationale": plan["rationale"], "plan_created": plan["created"]}
        cl = conn.execute("SELECT * FROM confidence_log WHERE position_id=? ORDER BY id DESC LIMIT 12", (pos["id"],)).fetchall()
        d["confidence"] = {"latest": dict(cl[0]) if cl else None, "history": [dict(r) for r in cl][::-1]}
        d["tripwires"] = [dict(r) for r in conn.execute("SELECT kind,threshold,event_type,description,status,triggered_at FROM tripwires WHERE position_id=?", (pos["id"],))]
        d["signals"] = [dict(r) for r in conn.execute("SELECT trigger,severity,reason,status,created FROM exit_signals WHERE position_id=? ORDER BY id DESC LIMIT 5", (pos["id"],))]
        ph = conn.execute("SELECT * FROM position_health WHERE position_id=? ORDER BY id DESC LIMIT 1", (pos["id"],)).fetchone()
        d["health"] = dict(ph) if ph else None
    rt = conn.execute("SELECT * FROM redteam_reviews WHERE ticker=? ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
    d["redteam"] = {**json.loads(rt["review_json"]), "created": rt["created"], "gate": rt["gate"]} if rt else None
    d["evidence"] = _evidence(conn, ticker, ts["views"], now)
    ie = conn.execute("SELECT i.analysis_json,e.headline,e.published FROM intel_events i JOIN events e ON e.id=i.event_id WHERE e.tickers_json LIKE ? ORDER BY i.event_id DESC LIMIT 1", ('%"' + ticker + '"%',)).fetchone()
    d["intel"] = {**json.loads(ie["analysis_json"]), "headline": ie["headline"], "published": ie["published"]} if ie else None
    d["revisions"] = revisions.history(conn, ticker, 15)
    kb = conn.execute("SELECT * FROM company_knowledge WHERE ticker=?", (ticker,)).fetchone() if conn.execute("SELECT 1 FROM sqlite_master WHERE name='company_knowledge'").fetchone() else None
    d["knowledge"] = json.loads(kb["summary_json"]) if kb else None
    return d
