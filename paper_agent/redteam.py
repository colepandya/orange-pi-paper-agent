"""Red team: before capital is committed, a SEPARATE LLM pass tries to prove the idea wrong.
Its output is data for the deterministic gate; its falsifiers become tripwires the monitor checks every cycle."""
from __future__ import annotations

import json
from typing import Callable, List, Optional

from . import db, marketdata as md, nlp, quotes
from .llm_client import LLMClient, wrap
from .timeutil import iso, parse_ts

SYSTEM = """You are the firm's risk manager and professional sceptic. A colleague wants to BUY a stock. Try hard to prove them WRONG, then reply with ONE JSON object:
{
 "bear_points": [{"point":"<=200 chars","severity":0..1,"evidence":"verbatim quote from the material provided, or n/a"}],
 "bull_points": [{"point":"<=200 chars","strength":0..1}],
 "fatal_flaw": true|false, "fatal_reason": "<=200 chars",
 "valuation_view": "cheap"|"fair"|"rich"|"unknown",
 "conviction": 0..1  (your honest remaining conviction AFTER the best bear case),
 "falsifiers": [{"kind":"price_below","level_pct":-3..-25,"description":"..."} or {"kind":"event","event_type":"<one of %(types)s>","description":"..."}],
 "review_in_days": 1..30
}
Give at least two bear points. fatal_flaw=true ONLY if the premise is factually wrong, clearly already priced in, or the company cannot benefit as claimed.
Falsifiers must be objectively checkable (a price level or a specific kind of future event), not opinions."""


def _clip(x, lo, hi, d=0.0):
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return d


def validate(obj: dict) -> Optional[dict]:
    try:
        bears = [{"point": str(b.get("point", ""))[:240], "severity": _clip(b.get("severity"), 0, 1, 0.3), "evidence": str(b.get("evidence", ""))[:300]} for b in (obj.get("bear_points") or [])[:6] if isinstance(b, dict)]
        bulls = [{"point": str(b.get("point", ""))[:240], "strength": _clip(b.get("strength"), 0, 1, 0.3)} for b in (obj.get("bull_points") or [])[:6] if isinstance(b, dict)]
        if not bears:
            return None
        fal = []
        for f in (obj.get("falsifiers") or [])[:5]:
            if not isinstance(f, dict):
                continue
            if f.get("kind") == "price_below":
                lv = _clip(f.get("level_pct"), -25, -1, 0)
                if lv < 0:
                    fal.append({"kind": "price_below", "level_pct": lv, "description": str(f.get("description", ""))[:200]})
            elif f.get("kind") == "price_above":                                                          # a SHORT is proven wrong by a rise
                lv = _clip(f.get("level_pct"), 1, 25, 0)
                if lv > 0:
                    fal.append({"kind": "price_above", "level_pct": lv, "description": str(f.get("description", ""))[:200]})
            elif f.get("kind") == "event" and f.get("event_type") in nlp.BY_NAME:
                fal.append({"kind": "event", "event_type": f["event_type"], "against": "up" if f.get("against") == "up" else "down", "description": str(f.get("description", ""))[:200]})
        view = obj.get("valuation_view") if obj.get("valuation_view") in ("cheap", "fair", "rich", "unknown") else "unknown"
        conviction = _clip(obj.get("conviction"), 0, 1, 0.5)
        fatal = bool(obj.get("fatal_flaw"))
        max_sev = max((b["severity"] for b in bears), default=0.0)
        net = max(0.0, min(1.0, 0.6 * conviction + 0.4 * (1 - max_sev)))
        if fatal:
            net = min(net, 0.2)
        return {"bear_points": bears, "bull_points": bulls, "fatal_flaw": fatal, "fatal_reason": str(obj.get("fatal_reason", ""))[:240], "valuation_view": view,
                "conviction": conviction, "net_conviction": net, "falsifiers": fal, "review_in_days": int(_clip(obj.get("review_in_days"), 1, 30, 10))}
    except (TypeError, AttributeError):
        return None


def build_context(conn, cand: dict, f: dict, comp: dict, th: dict, regime: dict) -> str:
    c = md.closes(conn, cand["ticker"], 260)
    r5, r60 = md.ret_n(c, 5), md.ret_n(c, 60)
    dd = (c[-1] / max(c) - 1) if c else None
    lines = ["Ticker: %s (%s) sector=%s dna=%s" % (cand["ticker"], comp.get("name") or "", comp.get("sector"), comp.get("dna")),
             "Trigger event (%s, source %s): %s" % (cand.get("event_type"), cand.get("source"), cand.get("headline")),
             "Claimed mechanism: %s" % (cand.get("mechanism") or "n/a"),
             "Proposed: LONG, target %+.1f%%, stop %.1f%%, horizon %d sessions, EV net %+.2f%%, model confidence %.0f%%" % (f["target_return"] * 100, f["stop_return"] * 100, f["horizon_days"], f["ev_net"] * 100, f["confidence"] * 100),
             "Price context: 5d %s, 60d %s, from 52w high %s, daily vol %.1f%%" % (_p(r5), _p(r60), _p(dd), f["inputs"]["vol_daily"] * 100),
             "Thesis: %s state=%s P=%s | regime=%s" % (th.get("primary"), th.get("state"), _p(th.get("p")), regime.get("regime")),
             "Fundamentals: financial_strength=%s moat=%s market_cap=%s" % (_n(comp.get("financial_strength")), _n(comp.get("competitive_moat")), comp.get("market_cap"))]
    k = conn.execute("SELECT summary_json FROM company_knowledge WHERE ticker=?", (cand["ticker"],)).fetchone() if "company_knowledge" in db.tables(conn) else None
    if k:
        lines.append("Company knowledge base: " + k[0][:1500])
    return "\n".join(lines)


def _p(x):
    return "n/a" if x is None else "%+.1f%%" % (x * 100) if abs(x) < 5 else str(x)


def _n(x):
    return "n/a" if x is None else "%.0f" % x


def review(conn, cfg: dict, client: Optional[LLMClient], cand: dict, f: dict, comp: dict, th: dict, regime: dict, now, decision_id: Optional[int] = None) -> Optional[dict]:
    if client is None or not client.enabled("analyst"):
        return None
    if decision_id:   # reuse a recent review for the same decision (retries must not re-spend)
        r = conn.execute("SELECT review_json,created FROM redteam_reviews WHERE decision_id=? ORDER BY id DESC LIMIT 1", (decision_id,)).fetchone()
        if r and (now - parse_ts(r["created"])).total_seconds() < 12 * 3600:
            return json.loads(r["review_json"])
    system = SYSTEM % {"types": json.dumps(sorted(nlp.BY_NAME))}
    user = "Challenge this proposed trade.\n" + wrap(build_context(conn, cand, f, comp, th, regime), 4000)
    out = client.complete_json("analyst", "redteam", system, user, validate, max_tokens=1000)
    if out:
        conn.execute("INSERT INTO redteam_reviews(created,ticker,decision_id,net_conviction,fatal_flaw,valuation_view,gate,review_json) VALUES(?,?,?,?,?,?,?,?)",
                     (iso(now), cand["ticker"], decision_id, out["net_conviction"], int(out["fatal_flaw"]), out["valuation_view"], "", json.dumps(out, sort_keys=True)))
    return out


def gate(rv: dict, cfg: dict) -> dict:
    """Deterministic decision on the red-team output. The LLM informs; this function decides."""
    reasons, mult, ok = [], 1.0, True
    if rv["fatal_flaw"]:
        ok = False
        reasons.append("fatal flaw: %s" % rv["fatal_reason"])
    if rv["net_conviction"] < float(cfg["intelligence"]["redteam_min_conviction"]):
        ok = False
        reasons.append("net conviction %.2f below %.2f" % (rv["net_conviction"], cfg["intelligence"]["redteam_min_conviction"]))
    if rv["valuation_view"] == "rich":
        mult *= 0.7
        reasons.append("valuation rich: size x0.7")
    if rv["net_conviction"] < 0.6 and ok:
        mult *= 0.85
        reasons.append("moderate conviction: size x0.85")
    return {"ok": ok, "size_mult": mult, "reasons": reasons}


def store_tripwires(conn, position_id: int, entry: float, rv: Optional[dict], now) -> int:
    if not rv:
        return 0
    conn.execute("UPDATE redteam_reviews SET position_id=? WHERE id=(SELECT id FROM redteam_reviews WHERE ticker=(SELECT ticker FROM positions WHERE id=?) ORDER BY id DESC LIMIT 1)", (position_id, position_id))
    n = 0
    for fa in rv["falsifiers"]:
        if fa["kind"] == "price_above":
            conn.execute("INSERT INTO tripwires(position_id,created,kind,threshold,description) VALUES(?,?,?,?,?)", (position_id, iso(now), "price_above", entry * (1 + fa["level_pct"] / 100.0), fa["description"] or "price above"))
        elif fa["kind"] == "price_below":
            conn.execute("INSERT INTO tripwires(position_id,created,kind,threshold,description) VALUES(?,?,?,?,?)", (position_id, iso(now), "price", entry * (1 + fa["level_pct"] / 100.0), fa["description"] or "price below %.1f%%" % fa["level_pct"]))
        else:
            conn.execute("INSERT INTO tripwires(position_id,created,kind,event_type,description) VALUES(?,?,?,?,?)", (position_id, iso(now), "event_up" if fa.get("against") == "up" else "event", fa["event_type"], fa["description"] or fa["event_type"]))
        n += 1
    return n


def check_tripwires(conn, now) -> List[dict]:
    fired = []
    for tw in conn.execute("SELECT t.*,p.ticker,p.status pstatus FROM tripwires t JOIN positions p ON p.id=t.position_id WHERE t.status='OPEN'").fetchall():
        if tw["pstatus"] != "OPEN":
            conn.execute("UPDATE tripwires SET status='CLEARED' WHERE id=?", (tw["id"],))
            continue
        hit, why = False, ""
        if tw["kind"] in ("price", "price_above"):
            q = quotes.valuation_price(conn, tw["ticker"], now, max_age_hours=24)
            if q and tw["kind"] == "price" and q["price"] <= tw["threshold"]:
                hit, why = True, "price %.2f <= tripwire %.2f" % (q["price"], tw["threshold"])
            elif q and tw["kind"] == "price_above" and q["price"] >= tw["threshold"]:
                hit, why = True, "price %.2f >= tripwire %.2f" % (q["price"], tw["threshold"])
        else:
            e = conn.execute("SELECT headline FROM events WHERE event_type=? AND direction" + (">0" if tw["kind"] == "event_up" else "<0") + " AND stale=0 AND novelty>=0.9 AND created>? AND tickers_json LIKE ? ORDER BY id DESC LIMIT 1",
                             (tw["event_type"], tw["created"], '%"' + tw["ticker"] + '"%')).fetchone()
            if e:
                hit, why = True, "event %s: %s" % (tw["event_type"], e["headline"][:100])
        if hit:
            conn.execute("UPDATE tripwires SET status='TRIGGERED',triggered_at=? WHERE id=?", (iso(now), tw["id"]))
            conn.execute("INSERT OR IGNORE INTO exit_signals(position_id,created,trigger,severity,reason,status) VALUES(?,?,?,?,?,'OPEN')",
                         (tw["position_id"], iso(now), "THESIS_BROKEN", 0.85, "tripwire: %s (%s)" % (tw["description"][:100], why)))
            fired.append({"ticker": tw["ticker"], "tripwire": tw["description"], "why": why})
    return fired
