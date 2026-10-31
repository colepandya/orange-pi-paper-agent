"""Event Intelligence: the LLM reads an item and returns a STRUCTURED, GROUNDED analysis (what happened, why, who benefits/loses,
causal chain by order, narratives, bull/bear). This module validates it and turns it into decaying evidence.

Hard rules: the LLM never sizes or places trades. Each causal hop gets  confidence x decay^(order-1) x certainty, and the reliability of
first/second/third-order calls is LEARNED from outcomes (source_stats 'LLM-analyst:oN'), so unreliable hops fade automatically."""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Callable, Dict, List, Optional

from . import db, learning, nlp
from .llm_client import LLMClient, ground_quotes, wrap
from .theses_seed import SEED_THESES
from .timeutil import iso, parse_ts

MAG = {"low": 0.25, "medium": 0.55, "high": 0.85}
HORIZON_TRADING_DAYS = {"days": 5, "weeks": 15, "1-3m": 40, "3-12m": 120, ">12m": 250}
HORIZON_DECAY_DAYS = {"days": 14, "weeks": 45, "1-3m": 120, "3-12m": 365, ">12m": 730}
THESIS_NAMES = {t["key"]: t["name"] for t in SEED_THESES}
EVENT_TYPES = sorted(nlp.BY_NAME)

SYSTEM = """You are a senior buy-side equity analyst. Analyse the news item and reply with ONE JSON object:
{
 "event_type": one of %(types)s or "other",
 "summary": "<=280 chars, what happened",
 "why": "<=280 chars, why it happened / what drives it",
 "certainty": 0..1  (probability the reported event is real and accurately described),
 "surprise": -1..1 or null (vs market expectations; null if unknown),
 "horizon": "days"|"weeks"|"1-3m"|"3-12m"|">12m"  (how long the impact lasts),
 "magnitude": "low"|"medium"|"high"  (economic significance),
 "direction_market": -1..1  (effect on broad US equities; 0 if company-specific),
 "beneficiaries": [{"ticker":"US ticker","mechanism":"<=160 chars: revenue/margin/valuation channel","confidence":0..1,"order":1|2|3}],
 "losers": [same shape],
 "chain": [{"order":1|2|3,"effect":"<=160 chars","confidence":0..1,"tickers":["..."],"sector":"..."}],
 "narratives": [{"key": one of %(keys)s or "new:<short_name>","stance":"support"|"contradict","strength":0..1,"why":"<=160 chars"}],
 "bull_case": "<=300 chars", "bear_case": "<=300 chars",
 "evidence_quotes": [{"claim":"...","quote":"EXACT verbatim text copied from the document, >=12 chars"}]
}
Rules: order 1 = directly affected company/sector; 2 = a customer/supplier/competitor effect; 3 = indirect macro or policy consequence.
Confidence must FALL with order. Only name tickers you are genuinely sure are affected through a stated mechanism; empty lists are fine.
Base everything on the document; do not use hindsight. Every material claim needs a verbatim quote."""


def _clip(x, lo, hi, default=0.0):
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return default


def slug(name: str) -> str:
    return "new:" + re.sub(r"[^a-z0-9]+", "_", str(name).lower().replace("new:", "")).strip("_")[:40]


def make_validator(universe, thesis_keys, source_text: str) -> Callable[[dict], Optional[dict]]:
    def validate(obj: dict) -> Optional[dict]:
        try:
            summary = str(obj["summary"])[:300]
            certainty = _clip(obj["certainty"], 0, 1, 0.5)
        except (KeyError, TypeError):
            return None
        if obj.get("magnitude") not in MAG:
            return None
        quotes, ratio = ground_quotes(obj.get("evidence_quotes"), source_text)
        grounded = ratio is not None and ratio >= 0.5
        if ratio is not None and ratio < 0.5:
            return None                      # most quotes are not in the source: treat as hallucination
        if ratio is None:
            certainty *= 0.7                 # no verbatim support offered
        def effects(key):
            out = []
            for e in (obj.get(key) or [])[:6]:
                if not isinstance(e, dict):
                    continue
                t = str(e.get("ticker", "")).upper().strip()
                if not re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", t) or (universe is not None and t not in universe):
                    continue
                order = int(_clip(e.get("order", 1), 1, 3, 1))
                out.append({"ticker": t, "mechanism": str(e.get("mechanism", ""))[:200], "confidence": _clip(e.get("confidence"), 0, 1, 0.3), "order": order})
            return out
        narratives, proposed = [], []
        for nrow in (obj.get("narratives") or [])[:5]:
            if not isinstance(nrow, dict) or nrow.get("stance") not in ("support", "contradict"):
                continue
            k = str(nrow.get("key", ""))
            if k in thesis_keys:
                narratives.append({"key": k, "stance": nrow["stance"], "strength": _clip(nrow.get("strength"), 0, 1, 0.3), "why": str(nrow.get("why", ""))[:200]})
            elif k.startswith("new:") and len(k) > 6:
                proposed.append({"key": slug(k), "name": k[4:].replace("_", " ")[:60], "stance": nrow["stance"]})
        chain = []
        for c in (obj.get("chain") or [])[:6]:
            if isinstance(c, dict):
                chain.append({"order": int(_clip(c.get("order", 1), 1, 3, 1)), "effect": str(c.get("effect", ""))[:200], "confidence": _clip(c.get("confidence"), 0, 1, 0.3),
                              "tickers": [str(t).upper() for t in (c.get("tickers") or [])[:5] if re.fullmatch(r"[A-Z]{1,5}", str(t).upper())], "sector": str(c.get("sector", ""))[:40]})
        et = obj.get("event_type") if obj.get("event_type") in nlp.BY_NAME else "other"
        hz = obj.get("horizon") if obj.get("horizon") in HORIZON_TRADING_DAYS else "weeks"
        surprise = obj.get("surprise")
        return {"event_type": et, "summary": summary, "why": str(obj.get("why", ""))[:300], "certainty": certainty, "grounded": grounded,
                "surprise": _clip(surprise, -1, 1) if surprise is not None else None, "horizon": hz, "magnitude": MAG[obj["magnitude"]], "magnitude_label": obj["magnitude"],
                "direction_market": _clip(obj.get("direction_market"), -1, 1, 0.0), "beneficiaries": effects("beneficiaries"), "losers": effects("losers"), "chain": chain,
                "narratives": narratives, "proposed_narratives": proposed, "bull_case": str(obj.get("bull_case", ""))[:400], "bear_case": str(obj.get("bear_case", ""))[:400], "quotes": quotes}
    return validate


def analyze(client: LLMClient, universe, item: dict, tier: str = "triage") -> Optional[dict]:
    keys = list(THESIS_NAMES)
    system = SYSTEM % {"types": json.dumps(EVENT_TYPES), "keys": json.dumps(keys)}
    text = "%s\n\n%s" % (item["title"], item.get("summary") or "")
    hint = ""
    if item.get("tickers"):
        hint = "Companies the deterministic matcher linked to this item (may be wrong): %s\n" % ", ".join(item["tickers"])
    user = hint + "Analyse this item.\n" + wrap(text)
    v = make_validator(universe, set(keys), text)
    return client.complete_json(tier, "event_intel", system, user, v, max_tokens=1100)


def make_analyst(conn, cfg: dict, universe=None, transport: Optional[Callable] = None) -> Optional[Callable]:
    client = LLMClient(conn, cfg, transport)
    if not client.enabled("triage"):
        return None

    def analyst(item: dict) -> Optional[dict]:
        tier = item.get("tier", "triage")
        if not client.enabled(tier):
            tier = "triage"
        intel = analyze(client, universe, item, tier)
        if not intel:
            return None
        a = item.get("analysis") or {}
        first = [e for e in intel["beneficiaries"] + intel["losers"] if e["order"] == 1]
        if a.get("scope") == "macro" or not first:
            direction = intel["direction_market"]
        else:
            top = max(first, key=lambda e: e["confidence"])
            direction = (1.0 if top in intel["beneficiaries"] else -1.0) * top["confidence"]
        return {"direction": direction * intel["certainty"], "magnitude": intel["magnitude"], "tickers": [e["ticker"] for e in first], "confidence": intel["certainty"],
                "rationale": intel["summary"], "intel": intel, "tier": tier}
    return analyst


def pick_for_llm(conn, pre: list, icfg: dict, analyst) -> Dict[str, str]:
    """Choose which items deserve an LLM read this cycle: ambiguous or material, ranked by importance; returns {uid: tier}."""
    if not analyst:
        return {}
    budget = int(icfg.get("engine_max_per_cycle" if icfg.get("engine", "rules") == "rules" else "llm_max_per_cycle", 0))      # the rules engine costs nothing, so it can read far more items than an LLM budget allows
    if budget <= 0:
        return {}
    held = {r[0] for r in conn.execute("SELECT ticker FROM positions WHERE status='OPEN'")}
    scored = []
    for (n, a, pub, age_h, stale, matches) in pre:
        if stale:
            continue
        tickers = [t for t, c, h in matches if c >= 0.5]
        eligible = (a["scope"] in ("macro", "sector") and a["magnitude"] >= 0.3) or a["event_type"] == "general_news" and bool(tickers) or (a["scope"] == "company" and tickers and a["magnitude"] >= 0.3) \
            or (not tickers and a["magnitude"] >= 0.4)
        if not eligible:
            continue
        hold = any(t in held for t in tickers)
        pr = a["magnitude"] * float(n["trust"] or 0.5) + (0.4 if hold else 0) + (0.2 if a["scope"] == "macro" else 0)
        tier = "analyst" if (a["magnitude"] >= 0.6 or hold or (a["scope"] == "macro" and a["magnitude"] >= 0.5)) else "triage"
        scored.append((pr, n["uid"], tier))
    scored.sort(reverse=True)
    return {uid: tier for _, uid, tier in scored[:budget]}


def apply(conn, cfg: dict, eid: int, a: dict, intel: dict, pub, cluster: int, novelty: float, now, stats: dict, source_cred: Callable, power: float) -> List[dict]:
    """Persist the analysis and its evidence. Returns the ticker effects (for trading candidates / suppressing duplicate deterministic evidence)."""
    icfg = cfg["intelligence"]
    tag = intel.get("tag") or "LLM-analyst"                                                       # whose analysis this is: the rules engine's learned reliability is kept apart from the LLM's
    decay = float(icfg["chain_decay"])
    prior = icfg["chain_order_prior"]
    dd = HORIZON_DECAY_DAYS[intel["horizon"]]
    expiry = iso(pub + dt.timedelta(days=dd))
    etype = intel["event_type"] if intel["event_type"] != "other" else a["event_type"]
    conn.execute("INSERT OR REPLACE INTO intel_events(event_id,created,tier,model,analysis_json) VALUES(?,?,?,?,?)", (eid, iso(now), "", "", json.dumps(intel, sort_keys=True)))
    out = []
    for role, sign in (("beneficiaries", 1.0), ("losers", -1.0)):
        for e in intel[role]:
            o = e["order"]
            eff = e["confidence"] * (decay ** (o - 1)) * intel["certainty"]
            cred = source_cred("%s:o%d" % (tag, o), etype, float(prior.get(str(o), 0.3)))
            strength = max(0.02, min(1.0, intel["magnitude"] * eff))
            conn.execute("INSERT INTO evidence(event_id,thesis_key,ticker,created,source,credibility,strength,novelty,predictive_power,direction,cluster_id,event_type,decay_days,expiry_date,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (eid, None, e["ticker"], iso(pub), "%s:o%d" % (tag, o), cred, strength, novelty, power, sign, cluster, etype, dd, expiry, ("o%d %s" % (o, e["mechanism"]))[:180]))
            stats["evidence"] += 1
            if o <= 2 and novelty >= 0.9:
                learning.record_chain_prediction(conn, e["ticker"], "abnormal_up" if sign > 0 else "abnormal_down", min(0.9, 0.5 + 0.35 * eff), HORIZON_TRADING_DAYS[intel["horizon"]],
                                                 etype, "%s:o%d" % (tag, o), now)
            out.append({"ticker": e["ticker"], "sign": sign, "order": o, "confidence": e["confidence"], "eff": eff, "mechanism": e["mechanism"]})
    for nr in intel["narratives"]:
        d = 1.0 if nr["stance"] == "support" else -1.0
        cred = source_cred(tag + ":narr", etype, 0.55)
        conn.execute("INSERT OR IGNORE INTO evidence(event_id,thesis_key,ticker,created,source,credibility,strength,novelty,predictive_power,direction,cluster_id,event_type,decay_days,expiry_date,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (eid, nr["key"], None, iso(pub), tag + ":narr", cred, max(0.05, min(1.0, intel["magnitude"] * nr["strength"] * intel["certainty"])), novelty, power, d, cluster, etype, dd, expiry, nr["why"][:180]))
        stats["evidence"] += 1
    for pn in intel["proposed_narratives"]:
        conn.execute("INSERT INTO proposed_narratives(key,name,first_seen,last_seen,mentions,sample_event_id) VALUES(?,?,?,?,1,?) ON CONFLICT(key) DO UPDATE SET mentions=mentions+1,last_seen=excluded.last_seen",
                     (pn["key"], pn["name"], iso(now), iso(now), eid))
    return out


def candidates_from(effects: List[dict], intel: dict, a: dict, n, eid: int, age_h: float, cred: float, cfg: dict, already: set) -> List[dict]:
    """Order-1 (configurable) beneficiaries the LLM is confident about become trade CANDIDATES; the deterministic engine still decides."""
    icfg = cfg["intelligence"]
    out = []
    if intel["certainty"] < 0.6 or intel["magnitude"] < 0.3 or age_h > float(icfg["trade_event_max_age_hours"]):
        return out
    for e in effects:
        if e["sign"] > 0 and e["order"] <= int(icfg["max_trade_order"]) and e["confidence"] >= 0.6 and e["ticker"] not in already:
            et = intel["event_type"] if intel["event_type"] in nlp.BY_NAME else a["event_type"]
            out.append({"event_id": eid, "ticker": e["ticker"], "conf": 0.7, "direction": min(1.0, 0.3 + 0.7 * e["confidence"] * intel["certainty"]), "magnitude": intel["magnitude"],
                        "event_type": et, "age_hours": age_h, "headline": n["title"], "source": "%s:o%d" % (intel.get("tag") or "LLM-analyst", e["order"]), "news_uid": n["uid"], "amount_usd": a.get("amount_usd"),
                        "credibility": cred, "horizon": min(15, max(3, HORIZON_TRADING_DAYS[intel["horizon"]] // 3)), "decay_days": HORIZON_DECAY_DAYS[intel["horizon"]],
                        "origin": "intel", "mechanism": e["mechanism"], "chain_order": e["order"]})
    return out
