"""The deterministic red team (v2): the strongest honest case AGAINST a proposed trade, with every objection tagged by the RULE that raised it and graded by what it should be allowed to DO.

Severity classes (what an objection may do): info (explain only) < confidence (lower conviction) < size (make the trade smaller) < hold (not now) < veto (do not trade).
Influence modes decide which classes act (reasoning engine setting 'influence'):
  observe  nothing acts; the review and what it WOULD have done are recorded
  guard    only HARD-VETO rules (the evidence is factually broken: denied, stale, the wrong company, a duplicate, superseded by a better source) may veto; size reductions apply; nothing enlarges a trade
  active   every class acts, plus the conviction floor
EVERY review also records what `active` would have done (`would`), and the scorecard follows what happened to the ideas it would have blocked, so each rule earns (or loses) influence from evidence, not from belief.
Contradictions are only counted when they ALIGN: same company, same kind of event, overlapping time, opposite implication, not a re-run of the same story, not already in the news the idea was built on."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from typing import Dict, List, Optional, Tuple

from . import db, facts as facts_mod, nlp, redteam
from .timeutil import iso, parse_ts

CLASSES = ("info", "confidence", "size", "hold", "veto")
HARD_VETO = {"denied", "stale_catalyst", "wrong_company", "duplicate_catalyst", "contradiction_factual", "contradiction_superseded"}
RULES: Dict[str, dict] = {
    "priced_in": {"cls": "size", "mult": 0.8, "label": "a good part of the move has already happened"},
    "fully_priced_in": {"cls": "hold", "mult": 1.0, "label": "the news is fully priced in"},
    "contradiction_factual": {"cls": "veto", "mult": 1.0, "label": "a credible source says the event did not happen"},
    "contradiction_superseded": {"cls": "veto", "mult": 1.0, "label": "a newer, better-sourced report replaces the news this idea rests on"},
    "contradiction_newer": {"cls": "size", "mult": 0.75, "label": "newer news on the same subject points the other way"},
    "contradiction_tension": {"cls": "confidence", "mult": 1.0, "label": "good and bad news on the same subject"},
    "contradiction_scope": {"cls": "info", "mult": 1.0, "label": "news about a different part of the business points the other way"},
    "rumor": {"cls": "size", "mult": 0.5, "label": "the report is unconfirmed"},
    "denied": {"cls": "veto", "mult": 1.0, "label": "the report is denied"},
    "stale_catalyst": {"cls": "veto", "mult": 1.0, "label": "the news is too old to trade"},
    "aging_news": {"cls": "confidence", "mult": 1.0, "label": "the news is hours old"},
    "wrong_company": {"cls": "veto", "mult": 1.0, "label": "the company is only mentioned, it is not the subject"},
    "duplicate_catalyst": {"cls": "veto", "mult": 1.0, "label": "the same story already produced an idea for this company"},
    "crowded_theme": {"cls": "size", "mult": 0.85, "label": "the theme is crowded or fading"},
    "against_strong_theme": {"cls": "size", "mult": 0.85, "label": "shorting against a strong theme"},
    "weak_source": {"cls": "size", "mult": 0.8, "label": "the source is not very credible"},
    "single_source": {"cls": "info", "mult": 1.0, "label": "only one independent source"},
    "regime": {"cls": "size", "mult": 0.85, "label": "the market regime works against the position"},
    "noisy_stop": {"cls": "size", "mult": 0.85, "label": "the stop is inside a normal day's move"},
    "thin_edge": {"cls": "confidence", "mult": 1.0, "label": "the edge after costs is thin"},
    "thin_volume": {"cls": "size", "mult": 0.85, "label": "trading volume is thin"},
    "horizon_mismatch": {"cls": "size", "mult": 0.8, "label": "most of the event's effect falls outside the trade window"},
    "fundamentals": {"cls": "size", "mult": 0.9, "label": "the company's reported numbers argue against it"},
    "residual": {"cls": "info", "mult": 1.0, "label": "no specific objection found"},
}
FAMILY = {"earnings_beat": "results", "earnings_miss": "results", "guidance_raise": "results", "guidance_cut": "results", "contract_win": "contracts", "contract_loss": "contracts", "regulatory_action": "regulatory",
          "regulatory_approval": "regulatory", "litigation": "legal", "production_disruption": "operations", "recall": "operations", "product_launch": "operations", "cyber_incident": "operations",
          "merger_acquisition": "corporate", "partnership": "corporate", "bankruptcy_distress": "distress", "buyback_dividend": "capital", "capital_raise": "capital", "leadership_change": "people"}
SEGMENT = re.compile(r"\b(segment|division|unit|business|cloud|data ?cent(?:er|re)|automotive|gaming|advertising|china|europe|asia|international|services|hardware)\b", re.I)
DENY = re.compile(r"\b(denies|denied|dismiss(?:es|ed)|no plans to|not planning|rejects?|false report|unfounded|walks? back|retracts?|withdraws?)\b", re.I)
RUMOR = re.compile(r"\b(reportedly|according to (?:people|sources)|people familiar|sources (?:say|said)|rumou?rs?|in talks|exploring|weighing)\b", re.I)


def _tokens(s: str) -> set:
    return set(re.findall(r"[a-z0-9]{3,}", (s or "").lower()))


def same_story(a: str, b: str, thr: float = 0.6) -> bool:
    x, y = _tokens(a), _tokens(b)
    return bool(x and y) and len(x & y) / float(len(x | y)) >= thr


def classify_contradiction(cand: dict, other, side: float, event_time: dt.datetime) -> Optional[dict]:
    """Is `other` (an event about the same company) a REAL contradiction of the idea? Returns {rule, severity, why} or None. Aligns family, time, implication and story identity."""
    if other["direction"] * side >= -0.3:
        return None                                                                                      # not the other way
    if same_story(cand.get("headline") or "", other["headline"] or ""):
        return None                                                                                      # the same story, re-run or syndicated
    fam_c, fam_o = FAMILY.get(cand.get("event_type")), FAMILY.get(other["event_type"])
    t_o = parse_ts(other["created"])
    later = t_o > event_time
    strong = other["credibility"] >= 0.85 and other["magnitude"] >= 0.7
    if DENY.search(other["headline"] or "") and (fam_c is None or fam_c == fam_o or RUMOR.search(cand.get("headline") or "")):
        return {"rule": "contradiction_factual", "severity": 0.9, "why": "a denial or withdrawal: " + (other["headline"] or "")[:100]}
    if fam_c is None or fam_o is None or fam_c != fam_o:
        sev = 0.25 if SEGMENT.search(other["headline"] or "") else 0.2
        return {"rule": "contradiction_scope", "severity": sev, "why": "different kind of news (%s vs %s): %s" % (fam_o or "?", fam_c or "?", (other["headline"] or "")[:80])}
    if later and strong:
        return {"rule": "contradiction_superseded", "severity": 0.85, "why": "newer and stronger on the same subject: " + (other["headline"] or "")[:100]}
    if later:
        return {"rule": "contradiction_newer", "severity": min(0.7, 0.3 + 0.4 * other["magnitude"] * other["credibility"]), "why": "newer on the same subject: " + (other["headline"] or "")[:100]}
    return {"rule": "contradiction_tension", "severity": min(0.5, 0.15 + 0.3 * other["magnitude"]), "why": "earlier mixed news on the same subject, probably already in the price: " + (other["headline"] or "")[:90]}


def contradictions(conn, cand: dict, side: float, now, hours: int = 48) -> List[dict]:
    event_time = now - dt.timedelta(hours=float(cand.get("age_hours") or 0))
    rows = conn.execute("SELECT headline,direction,magnitude,credibility,event_type,created,cluster_id FROM events WHERE created>=? AND created<=? AND stale=0 AND tickers_json LIKE ? AND magnitude>=0.35 AND credibility>=0.5 "
                        "AND gate IS NOT 'stale' AND gate IS NOT 'too_many_companies' AND event_type NOT IN ('price_recap','opinion_piece','general_news','sec_filing') ORDER BY id DESC LIMIT 40",
                        (iso(now - dt.timedelta(hours=hours)), iso(now), '%"' + cand["ticker"] + '"%')).fetchall()
    own = conn.execute("SELECT cluster_id FROM events WHERE id=?", (cand.get("event_id"),)).fetchone() if cand.get("event_id") else None
    out = []
    for r in rows:
        if own and own["cluster_id"] and r["cluster_id"] == own["cluster_id"]:
            continue                                                                                     # the same cluster of articles
        c = classify_contradiction(cand, r, side, event_time)
        if c:
            out.append(c)
    return sorted(out, key=lambda c: -c["severity"])


def _band(h: float) -> str:
    return "immediate" if h <= 1 else "short" if h <= 5 else "medium" if h <= 20 else "long"


def window_share(profile: Dict[str, float], horizon_days: float) -> float:
    """How much of an event's effect falls INSIDE a trade held for `horizon_days` sessions: the bands that end before the trade ends count in full, the band it ends inside counts in proportion."""
    h = float(horizon_days)
    return (profile.get("immediate", 0.0) * (1.0 if h >= 1 else 0.0) + profile.get("short", 0.0) * min(1.0, max(0.0, (h - 1) / 4.0))
            + profile.get("medium", 0.0) * min(1.0, max(0.0, (h - 5) / 15.0)) + profile.get("long", 0.0) * min(1.0, max(0.0, (h - 20) / 70.0)))


def review(conn, cfg: dict, cand: dict, f: dict, comp: dict, th: dict, regime: dict, now, decision_id: Optional[int] = None) -> Optional[dict]:
    from . import reasoning as R
    if decision_id:
        r = conn.execute("SELECT review_json,created FROM redteam_reviews WHERE decision_id=? ORDER BY id DESC LIMIT 1", (decision_id,)).fetchone()
        if r and (now - parse_ts(r["created"])).total_seconds() < 12 * 3600:
            newer = conn.execute("SELECT 1 FROM events WHERE created>? AND created<=? AND tickers_json LIKE ? LIMIT 1", (r["created"], iso(now), '%"' + cand["ticker"] + '"%')).fetchone()
            newer = newer or (conn.execute("SELECT 1 FROM reasoning_events WHERE created>? AND tickers_json LIKE ? LIMIT 1", (r["created"], '%"' + cand["ticker"] + '"%')).fetchone() if _has(conn, "reasoning_events") else None)
            if not newer:                                                                                  # a review is reused for retries ONLY while nothing new about the company has arrived since it was made
                return json.loads(r["review_json"])
    side = -1.0 if f.get("verdict") == "SHORT" else 1.0
    inp = f.get("inputs") or {}
    bears: List[dict] = []

    def bear(rule: str, sev: float, evidence: str = "", mult: Optional[float] = None, point: Optional[str] = None):
        spec = RULES[rule]
        bears.append({"point": point or spec["label"], "severity": round(max(0.0, min(1.0, sev)), 2), "evidence": evidence[:300], "rule": rule, "action": spec["cls"], "mult": spec["mult"] if mult is None else mult})

    pi = float(inp.get("priced_in") or 0.0)
    if pi >= 0.10:
        bear("fully_priced_in", 0.85, "the stock already moved %.0f%% in the event's direction over 5 days" % (pi / 0.8 * 100))
    elif pi >= 0.03:
        bear("priced_in", min(0.8, pi * 6), "the stock already moved %.1f%% in the event's direction over 5 days" % (pi / 0.8 * 100))
    for c in contradictions(conn, cand, side, now)[:3]:
        bear(c["rule"], c["severity"], c["why"])
    text = cand.get("headline") or ""
    st, _, hits = R.report_status(text)
    if st == "rumor":
        bear("rumor", 0.55, hits[0] if hits else "unconfirmed-source wording")
    elif st == "denied":
        bear("denied", 0.9, hits[0] if hits else "denial wording")
    age = float(cand.get("age_hours") or 0)
    if age > 36:
        bear("stale_catalyst", 0.9, "the news is %.0f hours old" % age)
    elif age > 12:
        bear("aging_news", min(0.6, 0.2 + age / 80.0), "the news is %.0f hours old" % age)
    if cand.get("event_id") and cand.get("origin") in (None, "event"):
        rec = conn.execute("SELECT roles_json FROM reasoning_events WHERE event_ref=? ORDER BY id DESC LIMIT 1", (cand["event_id"],)).fetchone() if _has(conn, "reasoning_events") else None
        roles = json.loads(rec[0]) if rec and rec[0] else {}
        if cand["ticker"] in roles and roles[cand["ticker"]] < 0.3:
            bear("wrong_company", 0.85, "role confidence %.2f: the company is only mentioned" % roles[cand["ticker"]])
        own = conn.execute("SELECT cluster_id FROM events WHERE id=?", (cand["event_id"],)).fetchone()
        if own and own["cluster_id"]:
            dup = conn.execute("SELECT d.id FROM decisions d JOIN events e ON e.id=d.event_id WHERE d.ticker=? AND e.cluster_id=? AND d.event_id!=? AND d.created>=? AND d.status IN ('EXECUTED','AWAITING_MARKET','VETOED','RISK_REJECTED') LIMIT 1",
                               (cand["ticker"], own["cluster_id"], cand["event_id"], iso(now - dt.timedelta(hours=24)))).fetchone()
            if dup:
                bear("duplicate_catalyst", 0.8, "decision %d already came from this story" % dup[0])
    state = (th or {}).get("state")
    if side > 0 and state in ("Overcrowded", "Weakening"):
        bear("crowded_theme", 0.5 if state == "Overcrowded" else 0.4, "thesis state %s" % state, point="the theme is %s" % state.lower())
    if side < 0 and state in ("Strong", "Active"):
        bear("against_strong_theme", 0.5, "thesis state %s" % state, point="shorting against a %s theme" % state.lower())
    if (cand.get("credibility") or 1.0) < 0.5:
        bear("weak_source", 0.4, "credibility %.2f" % cand["credibility"])
    if cand.get("independent_sources") == 1 and not cand.get("primary_source"):
        bear("single_source", 0.2, "no other independent outlet carries it")
    reg = (regime or {}).get("regime")
    if (reg == "risk_off" and side > 0) or (reg == "risk_on" and side < 0):
        bear("regime", 0.35, "regime %s" % reg)
    stop, vol = abs(float(f.get("stop_return") or 0)), float(inp.get("vol_daily") or 0)
    if vol and stop and stop < 1.2 * vol:
        bear("noisy_stop", 0.45, "stop %.1f%% vs a typical daily move of %.1f%%" % (stop * 100, vol * 100))
    if float(f.get("ev_net") or 0) < 0.012:
        bear("thin_edge", 0.3, "expected %.1f%%" % (float(f.get("ev_net") or 0) * 100))
    adv = (comp or {}).get("adv_usd")
    if adv and adv < 5e7:
        bear("thin_volume", 0.3, "$%.0fM a day" % (adv / 1e6))
    prof = R.HORIZON_PROFILE.get(cand.get("event_type"))
    if prof and f.get("horizon_days"):
        w = window_share(prof, float(f["horizon_days"]))
        if w < 0.35:
            bear("horizon_mismatch", 0.45, "%s news acts mostly over %s; this trade is held %d sessions, so only %.0f%% of its effect falls inside the window" % ((cand.get("event_type") or "").replace("_", " "), max(prof, key=prof.get), float(f["horizon_days"]), w * 100))
    fx = facts_mod.get(conn, cand["ticker"]) if _has(conn, "company_facts") else None
    if fx:
        for fl in fx.get("flags", []):
            if side > 0 and fl["severity"] > 0 and fl["flag"] != "fast_growth":
                bear("fundamentals", fl["severity"], "SEC data: " + fl["text"], point="the numbers it reports: " + fl["text"])
            elif side < 0 and fl["flag"] == "fast_growth":
                bear("fundamentals", 0.35, "SEC data: " + fl["text"], point="shorting a fast-growing company: " + fl["text"])
        if side < 0 and fx.get("net_income_ttm", -1) > 0 and (fx.get("net_debt") is not None and fx["net_debt"] < 0):
            bear("fundamentals", 0.3, "SEC data: profitable with more cash than debt", point="shorting a profitable company with net cash")
    if not bears:
        bear("residual", 0.15, "the residual risk is the model being wrong")
    bulls = []
    sc = f.get("scenarios") or {}
    if sc and sc.get("bull", {}).get("ret", 0) > 2.0 * abs(sc.get("bear", {}).get("ret", 0) or 1e-9):
        bulls.append({"point": "the upside case is more than twice the downside case", "strength": 0.5})
    if state in ("Strong", "Active") and side > 0:
        bulls.append({"point": "the theme is %s" % state.lower(), "strength": 0.45})
    if (cand.get("credibility") or 0) >= 0.75:
        bulls.append({"point": "the source is credible", "strength": 0.4})
    if st == "confirmed":
        bulls.append({"point": "the event is confirmed, not a rumour", "strength": 0.45})
    if cand.get("independent_sources", 0) >= 2:
        bulls.append({"point": "%d independent outlets carry it" % cand["independent_sources"], "strength": 0.4})
    falsifiers = []
    if stop:
        falsifiers.append({"kind": "price_above" if side < 0 else "price_below", "level_pct": (1 if side < 0 else -1) * round(max(1.0, min(25.0, stop * 100)), 1), "description": "the stock moves %.1f%% against the position: the idea is wrong" % (stop * 100)})
    wanted = R.SHORT_FALSIFIERS if side < 0 else (R.OPPOSITE_EVENTS.get(cand.get("event_type"), []) + R.LONG_FALSIFIERS)
    for et in [e for e in dict.fromkeys(wanted) if e in nlp.BY_NAME][:3]:
        falsifiers.append({"kind": "event", "event_type": et, "against": "up" if side < 0 else "down", "description": "a %s event on this company contradicts the idea" % et.replace("_", " ")})
    vetoing = [b for b in bears if b["action"] == "veto"]
    holding = [b for b in bears if b["action"] == "hold"]
    worst = max((b["severity"] for b in bears), default=0.0)
    conviction = max(0.0, min(1.0, float(f.get("confidence") or 0.5) * (1.0 - 0.5 * worst) + 0.1 * len(bulls)))
    fatal = bool(vetoing or holding)
    obj = {"bear_points": bears, "bull_points": bulls, "fatal_flaw": fatal, "fatal_reason": (vetoing or holding)[0]["evidence"] or (vetoing or holding)[0]["point"] if fatal else "", "valuation_view": "unknown",
           "conviction": conviction, "falsifiers": falsifiers, "review_in_days": int(f.get("horizon_days") or 10)}
    rv = redteam.validate(obj)
    if rv:
        conn.execute("INSERT INTO redteam_reviews(created,ticker,decision_id,net_conviction,fatal_flaw,valuation_view,gate,review_json) VALUES(?,?,?,?,?,?,?,?)",
                     (iso(now), cand["ticker"], decision_id, rv["net_conviction"], int(rv["fatal_flaw"]), rv["valuation_view"], "", json.dumps(rv, sort_keys=True)))
    return rv


def _has(conn, t: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone())


def _evaluate(rv: dict, cfg: dict, mode: str) -> dict:
    ok, mult, reasons = True, 1.0, []
    floor = float(cfg["intelligence"].get("redteam_min_conviction", 0.45))
    for b in rv["bear_points"]:
        rule, cls = b.get("rule", "residual"), b.get("action", "info")
        if cls in ("veto", "hold") and (mode == "active" or (mode == "guard" and rule in HARD_VETO)):
            ok = False
            reasons.append("%s: %s" % (rule.replace("_", " "), b["evidence"] or b["point"]))
        elif cls in ("size", ) and mode in ("guard", "active"):
            m = float(b.get("mult", 1.0))
            if m < 1.0:
                mult *= m
                reasons.append("%s: size x%.2f" % (rule.replace("_", " "), m))
    if mode == "active" and rv["net_conviction"] < floor:
        ok = False
        reasons.append("net conviction %.2f below %.2f" % (rv["net_conviction"], floor))
    return {"ok": ok, "size_mult": max(0.5, mult) if ok else 1.0, "reasons": reasons}


def gate(rv: dict, cfg: dict, mode: str = "guard") -> dict:
    """The decision on a review. `would` is ALWAYS what active mode would have done, whatever the mode, so the scorecard can judge each rule before it is given the power to act."""
    would = _evaluate(rv, cfg, "active")
    acting = _evaluate(rv, cfg, mode) if mode in ("guard", "active") else {"ok": True, "size_mult": 1.0, "reasons": []}
    acting["would"] = would
    acting["mode"] = mode
    return acting


# ------------------------------------------------------------------------------------------------ did the red team help?
def wilson(k: int, n: int, z: float = 1.64) -> Tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - m) / d), min(1.0, (c + m) / d)


def scorecard(conn) -> dict:
    """What happened to the ideas the red team would have blocked, compared with the ones it would have allowed, and per rule: the evidence that decides each rule's influence. Position return = abnormal
    return (vs the market) x the position's side; a 'win' beats the market in the position's direction."""
    rows = conn.execute("SELECT r.review_json,r.gate,d.action,o.abnormal_return FROM redteam_reviews r JOIN decisions d ON d.id=r.decision_id JOIN predictions p ON p.forecast_id=d.forecast_id AND p.kind='forecast' "
                        "JOIN outcomes o ON o.prediction_id=p.id WHERE o.abnormal_return IS NOT NULL").fetchall()
    cohorts: Dict[str, List[float]] = {}
    for r in rows:
        try:
            rv, g = json.loads(r["review_json"]), json.loads(r["gate"] or "{}")
        except ValueError:
            continue
        sg = -1.0 if r["action"] == "SHORT" else 1.0
        x = sg * float(r["abnormal_return"])
        would_ok = (g.get("would") or {}).get("ok", True)
        cohorts.setdefault("would block" if not would_ok else "would allow", []).append(x)
        for rule in {b.get("rule") for b in rv.get("bear_points", []) if b.get("rule") and b.get("rule") != "residual"}:
            cohorts.setdefault("rule:" + rule, []).append(x)
    out = {}
    base = [x for k, v in cohorts.items() if k in ("would block", "would allow") for x in v]
    p0 = (sum(1 for x in base if x > 0) / len(base)) if base else 0.5
    for k, v in sorted(cohorts.items()):
        n, w = len(v), sum(1 for x in v if x > 0)
        lo, hi = wilson(w, n)
        shrunk = (w + 20 * p0) / (n + 20)                                                                  # small groups inherit from the whole
        out[k] = {"n": n, "mean": sum(v) / n, "win": w / n, "win_low": lo, "win_high": hi, "win_shrunk": shrunk}
    return out
