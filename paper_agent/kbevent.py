"""Event-aware reasoning: (1) how long a catalyst stays tradable, by event kind, materiality, resolution status, corroboration and how much of the move already happened; (2) economic materiality computed from real
numerators and denominators, `unknown` (never zero, never invented) when an input is missing; (3) a company's active event STATE built from its whole recent event history, not article by article.
Everything here records and explains; with the default observe modes nothing it computes changes a trade."""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Dict, List, Optional, Tuple

from . import db, timeframe
from .timeutil import iso, parse_ts, utcnow

GENERIC_STALE_HOURS = 36.0                      # the red team's current single threshold (skeptic.review: age > 36 hours -> stale_catalyst)
RULES_VERSION = 1
# rule_id, family, event types ('*' = any), onset_h, peak_h, decay_h, expiry_h, unresolved multiplier, notes
EXPIRY_ROWS = [
    ("EXP_RUMOR", "rumour", ["*"], 0, 2, 24, 36, 1.5, "an unconfirmed report fades or resolves within about a day and a half"),
    ("EXP_ANALYST", "analyst_rating", ["analyst_action"], 0, 1, 12, 24, 1.0, "a rating change is absorbed within a day"),
    ("EXP_EARNINGS", "earnings_result", ["earnings_beat", "earnings_miss", "earnings_report"], 0, 4, 72, 120, 1.0, "post-earnings drift fades within about a week"),
    ("EXP_GUIDANCE", "guidance_change", ["guidance_raise", "guidance_cut"], 0, 4, 120, 168, 1.0, "guidance resets expectations for about a week"),
    ("EXP_CONTRACT_WIN", "contract_award", ["contract_win"], 0, 8, 48, 96, 2.0, "an award stays live longer if it is being protested"),
    ("EXP_CONTRACT_LOSS", "contract_loss", ["contract_loss"], 0, 8, 48, 96, 1.5, ""),
    ("EXP_REG_APPROVAL", "regulatory_approval", ["regulatory_approval"], 0, 8, 72, 120, 1.0, ""),
    ("EXP_REG_ACTION", "regulatory_rejection", ["regulatory_action"], 0, 8, 96, 120, 2.0, "an unresolved enforcement action lingers"),
    ("EXP_ACQUISITION", "acquisition", ["merger_acquisition"], 0, 12, 120, 240, 3.0, "a pending deal keeps the catalyst alive until it closes or fails"),
    ("EXP_OFFERING", "offering", ["capital_raise"], 0, 4, 48, 72, 1.0, "the share-supply overhang is priced within days"),
    ("EXP_INSIDER", "insider_transaction", ["insider_filing"], 0, 12, 36, 72, 1.0, ""),
    ("EXP_LAUNCH", "product_launch", ["product_launch"], 0, 12, 72, 96, 1.0, ""),
    ("EXP_RECALL", "recall", ["recall"], 0, 8, 96, 120, 2.0, ""),
    ("EXP_OUTAGE", "production_outage", ["production_disruption"], 0, 6, 120, 240, 2.0, "lasts until production is restored"),
    ("EXP_COMMODITY", "commodity_disruption", ["energy_supply", "supply_chain_macro"], 0, 12, 120, 168, 2.0, ""),
    ("EXP_SANCTIONS", "sanctions", ["sanctions"], 0, 12, 168, 240, 1.5, ""),
    ("EXP_TARIFF", "tariffs", ["trade_tariff"], 0, 12, 168, 240, 2.0, ""),
    ("EXP_EXPORT_CONTROL", "export_controls", ["regulatory_action", "sanctions", "trade_tariff"], 0, 12, 168, 240, 2.0, "chosen when the text names export controls, restrictions or the entity list"),
    ("EXP_LITIGATION", "litigation", ["litigation"], 0, 8, 96, 168, 2.5, "an unresolved case lingers"),
    ("EXP_SETTLEMENT", "settlement", ["litigation"], 0, 8, 48, 96, 1.0, "chosen when the text reports a settlement"),
    ("EXP_CLINICAL", "clinical_fda", ["regulatory_approval", "regulatory_action", "product_launch", "general_news"], 0, 12, 168, 240, 2.0, "chosen when the text names the FDA or a clinical trial"),
    ("EXP_DISTRESS", "distress", ["bankruptcy_distress", "credit_stress", "financial_stress_event"], 0, 12, 168, 336, 3.0, ""),
    ("EXP_BUYBACK", "buyback_dividend", ["buyback_dividend"], 0, 8, 48, 96, 1.0, ""),
    ("EXP_LEADERSHIP", "leadership_change", ["leadership_change"], 0, 8, 72, 120, 1.0, ""),
    ("EXP_PARTNERSHIP", "partnership", ["partnership"], 0, 8, 48, 96, 1.0, ""),
    ("EXP_CYBER", "cyber_incident", ["cyber_incident"], 0, 8, 96, 120, 2.0, ""),
    ("EXP_DEFAULT", "default", ["*"], 0, 6, 24, 36, 1.0, "no specific rule: the existing generic 36-hour limit"),
]
_EXPORT = re.compile(r"\bexport (?:control|restriction|ban|licen[cs]e|curb)s?\b|\bentity list\b", re.I)
_SETTLE = re.compile(r"\bsettle(?:s|d|ment|ments)?\b", re.I)
_CLINICAL = re.compile(r"\bFDA\b|\bclinical trial|\bphase [123I]+\b|\bPDUFA\b|\bcomplete response letter\b", re.I)
_RESOLVED = re.compile(r"\b(completed?|closes?|closed|restored|resumed|resolved|settled|dismissed|approved)\b", re.I)
_PROTEST = re.compile(r"\b(protest(?:s|ed)?|challenge[sd]?|GAO|appeal(?:s|ed)?)\b", re.I)


def seed_rules(conn) -> int:
    n = 0
    for r in EXPIRY_ROWS:
        n += conn.execute("INSERT OR IGNORE INTO event_expiry_rules(rule_id,event_family,event_types_json,onset_h,peak_h,decay_h,expiry_h,unresolved_mult,notes,version,active) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                          (r[0], r[1], json.dumps(r[2]), r[3], r[4], r[5], r[6], r[7], r[8], RULES_VERSION)).rowcount
    return n


def pick_rule(etype: str, state: Optional[str], text: str) -> dict:
    """The most specific rule for this event; the order is: rumour state, then text-defined kinds (export controls, settlement, FDA), then the event type, then the default."""
    byid = {r[0]: r for r in EXPIRY_ROWS}
    chosen = None
    if state == "rumored":
        chosen = "EXP_RUMOR"
    elif etype in byid["EXP_EXPORT_CONTROL"][2] and _EXPORT.search(text or ""):
        chosen = "EXP_EXPORT_CONTROL"
    elif etype == "litigation" and _SETTLE.search(text or ""):
        chosen = "EXP_SETTLEMENT"
    elif etype in byid["EXP_CLINICAL"][2] and _CLINICAL.search(text or ""):
        chosen = "EXP_CLINICAL"
    else:
        for r in EXPIRY_ROWS:
            if r[0] in ("EXP_RUMOR", "EXP_DEFAULT", "EXP_EXPORT_CONTROL", "EXP_SETTLEMENT", "EXP_CLINICAL"):
                continue
            if etype in r[2]:
                chosen = r[0]
                break
    r = byid[chosen or "EXP_DEFAULT"]
    return {"rule_id": r[0], "family": r[1], "onset_h": r[3], "peak_h": r[4], "decay_h": r[5], "expiry_h": r[6], "unresolved_mult": r[7], "notes": r[8], "timeframe": timeframe.from_hours(r[6])}


def _ts(x) -> Optional[dt.datetime]:
    try:
        return parse_ts(x) if x else None
    except (ValueError, TypeError):
        return None


def lifecycle(conn, cfg: dict, ev: dict, now=None, persist: bool = True) -> dict:
    """ev = {event_id, ticker, event_type, state, text, published, created, materiality, independent_sources, novelty, superseded_at, denied_at, priced_in, resolved}. Returns every time and every modifier, so the red team can
    explain a stale call. `generic_stale` is what Build 44 decides today (age > 36 hours); `eventaware_stale` is what this rule decides."""
    now = now or utcnow()
    k = cfg.get("kb") or {}
    rule = pick_rule(ev.get("event_type") or "", ev.get("state"), ev.get("text") or "")
    occurred = _ts(ev.get("published")) or _ts(ev.get("created")) or now
    first_seen = _ts(ev.get("created")) or occurred
    mods: List[dict] = []
    hours = float(rule["expiry_h"])
    band = ev.get("materiality") or "unknown"
    f = {"high": 1.5, "very_high": 1.5, "low": 0.6, "negligible": 0.6}.get(band, 1.0)
    mods.append({"name": "materiality", "band": band, "factor": f})
    hours *= f
    text = ev.get("text") or ""
    unresolved = bool(ev.get("state") == "rumored" or (ev.get("event_type") in ("merger_acquisition", "litigation", "production_disruption", "bankruptcy_distress", "regulatory_action") and not (ev.get("resolved") or _RESOLVED.search(text)))
                      or (ev.get("event_type") == "contract_win" and _PROTEST.search(text)))
    if unresolved and rule["unresolved_mult"] != 1.0:
        mods.append({"name": "unresolved", "factor": rule["unresolved_mult"], "why": "the matter is not settled, so the catalyst stays live"})
        hours *= rule["unresolved_mult"]
    if int(ev.get("independent_sources") or 0) >= 2:
        mods.append({"name": "corroborated", "factor": 1.25, "why": "%d independent sources" % int(ev["independent_sources"])})
        hours *= 1.25
    pi = float(ev.get("priced_in") or 0.0)
    if pi >= 0.10:
        mods.append({"name": "priced_in", "factor": 0.5, "why": "the stock already moved %.0f%% of the way" % (pi / 0.8 * 100)})
        hours *= 0.5
    elif pi >= 0.03:
        mods.append({"name": "priced_in", "factor": 0.8, "why": "part of the move has already happened"})
        hours *= 0.8
    hold = float(((cfg.get("strategy") or {}).get("max_hold_days")) or 3)
    cap = max(48.0, 2.0 * hold * 24.0)
    if hours > cap:
        mods.append({"name": "holding_horizon_cap", "factor": round(cap / hours, 3), "why": "the strategy holds at most %.0f days: a catalyst more than twice that old is not actionable for it" % hold})
        hours = cap
    effective = occurred + dt.timedelta(hours=hours)
    cause = None
    den = _ts(ev.get("denied_at"))
    sup = _ts(ev.get("superseded_at"))
    if ev.get("state") in ("denied", "rejected") or den:
        effective, cause = min(effective, den or occurred), "denied"
        mods.append({"name": "denied", "why": "a denial ends the catalyst at once"})
    if sup and sup < effective:
        effective, cause = sup, "superseded"
        mods.append({"name": "superseded", "why": "a newer event replaced it"})
    age_h = max(0.0, (now - occurred).total_seconds() / 3600.0)
    out = {"event_id": ev.get("event_id"), "ticker": ev.get("ticker"), "event_type": ev.get("event_type"), "rule_id": rule["rule_id"], "family": rule["family"], "occurred": iso(occurred), "first_seen": iso(first_seen),
           "published": ev.get("published"), "onset": iso(occurred + dt.timedelta(hours=rule["onset_h"])), "peak": iso(occurred + dt.timedelta(hours=rule["peak_h"])), "decay_end": iso(occurred + dt.timedelta(hours=rule["decay_h"])),
           "default_expiry": iso(occurred + dt.timedelta(hours=rule["expiry_h"])), "effective_expiry": iso(effective), "unresolved": unresolved, "modifiers": mods, "age_hours": round(age_h, 1), "ended_by": cause,
           "timeframe": timeframe.from_hours(hours), "generic_stale": age_h > GENERIC_STALE_HOURS, "eventaware_stale": now > effective, "hours_allowed": round((effective - occurred).total_seconds() / 3600.0, 1)}
    out["explanation"] = ("%s (rule %s: %s) is allowed %.0f hours%s; it is %.0f hours old, so it is %s." % (rule["family"].replace("_", " "), rule["rule_id"], rule["notes"] or "default timing", out["hours_allowed"],
                          (" after adjustments: " + "; ".join("%s x%s" % (m["name"].replace("_", " "), m["factor"]) for m in mods if "factor" in m and m["factor"] != 1.0)) if any(m.get("factor", 1.0) != 1.0 for m in mods) else "",
                          age_h, "EXPIRED" if out["eventaware_stale"] else "still live"))
    if persist:
        conn.execute("INSERT INTO event_lifecycle(event_id,ticker,event_type,occurred,first_seen,published,onset,peak,decay_end,default_expiry,unresolved,superseded_by,effective_expiry,rule_id,modifiers_json,generic_stale,eventaware_stale,computed) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (out["event_id"], out["ticker"], out["event_type"], out["occurred"], out["first_seen"], out["published"], out["onset"], out["peak"], out["decay_end"], out["default_expiry"], int(unresolved),
                                                                    None, out["effective_expiry"], rule["rule_id"], json.dumps(mods), int(out["generic_stale"]), int(out["eventaware_stale"]), iso(now)))
    return out


def expiry_scorecard(conn) -> dict:
    """For each event family: how often the generic 36-hour limit and the event-aware rule disagreed, and what happened to those ideas. 'generic too short' = the generic limit blocked an idea the event-aware rule would
    have kept; 'generic too long' = the generic limit kept one the event-aware rule would have dropped. Outcomes come from the red-team intervention records (measured after the fact); no rule is changed automatically."""
    rows = conn.execute("SELECT l.rule_id,l.generic_stale,l.eventaware_stale,i.id AS iid,o.r5,o.net_benefit,i.side FROM event_lifecycle l JOIN redteam_interventions i ON i.decision_id IN (SELECT id FROM decisions WHERE event_id=l.event_id) "
                        "LEFT JOIN intervention_outcomes o ON o.intervention_id=i.id").fetchall()
    out: Dict[str, dict] = {}
    for r in rows:
        k = r["rule_id"]
        d = out.setdefault(k, {"n": 0, "agree": 0, "generic_too_short": [], "generic_too_long": []})
        d["n"] += 1
        if bool(r["generic_stale"]) == bool(r["eventaware_stale"]):
            d["agree"] += 1
        elif r["generic_stale"] and not r["eventaware_stale"]:
            d["generic_too_short"].append(r["r5"])
        else:
            d["generic_too_long"].append(r["r5"])
    for k, d in out.items():
        for name in ("generic_too_short", "generic_too_long"):
            xs = [x for x in d[name] if x is not None]
            d[name] = {"cases": len(d[name]), "measured": len(xs), "mean_5d_return": (sum(xs) / len(xs)) if xs else None, "maturity": maturity(len(xs))}
    return out


def maturity(n: int) -> str:
    return "insufficient" if n < 20 else "informational" if n < 50 else "preliminary" if n < 100 else "review candidate"


# ================================================================================================================================ economic materiality
BANDS = ((0.01, "negligible"), (0.03, "low"), (0.10, "medium"), (0.25, "high"), (9e9, "very_high"))
_AMT = re.compile(r"\$\s?([\d][\d,]*\.?\d*)\s*(trillion|tn|billion|bn|b|million|mm|m|thousand|k)?\b", re.I)
_SHARES = re.compile(r"([\d][\d,]*\.?\d*)\s*(million|billion|thousand|m|b|k)?\s+(?:common |ordinary |new )?shares\b", re.I)
_PCT = re.compile(r"([\d]+\.?\d*)\s?%")
_MULT = {"trillion": 1e12, "tn": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mm": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3, None: 1.0}


def parse_amounts(text: str) -> dict:
    """Dollar amounts, share counts and percentages stated in a headline or summary. Every number is returned with the words it came from."""
    out = {"usd": [], "shares": [], "pct": []}
    for m in _AMT.finditer(text or ""):
        try:
            out["usd"].append({"value": float(m.group(1).replace(",", "")) * _MULT[(m.group(2) or "").lower() or None], "text": m.group(0)})
        except (ValueError, KeyError):
            pass
    for m in _SHARES.finditer(text or ""):
        try:
            out["shares"].append({"value": float(m.group(1).replace(",", "")) * _MULT[(m.group(2) or "").lower() or None], "text": m.group(0)})
        except (ValueError, KeyError):
            pass
    for m in _PCT.finditer(text or ""):
        out["pct"].append({"value": float(m.group(1)) / 100.0, "text": m.group(0)})
    return out


def band_of(r: Optional[float]) -> str:
    if r is None:
        return "unknown"
    for hi, name in BANDS:
        if abs(r) < hi:
            return name
    return "very_high"


def company_inputs(conn, ticker: str, as_of: Optional[str] = None, max_fin_days: int = 150) -> dict:
    """What the knowledge base KNEW about the company at `as_of` (financial snapshot first known by then; market snapshot observed by then and still current)."""
    from . import kbsnap
    eid = kbsnap.entity_of(conn, ticker)
    out = {"entity_id": eid, "financial": None, "market": None, "missing": []}
    if not eid:
        out["missing"].append("company is not in the knowledge base")
        return out
    as_of = as_of or iso(utcnow())
    snap = kbsnap.financial_as_of(conn, eid, as_of)
    if snap:
        m = json.loads(snap["metrics_json"] or "{}")
        out["financial"] = {"reported": m.get("reported") or {}, "derived": m.get("derived") or {}, "taken": snap["taken"], "first_known": snap["first_known"], "status": snap["status"]}
    else:
        out["missing"].append("no financial snapshot known by then")
    r = conn.execute("SELECT * FROM kb_market_snapshots WHERE entity_id=? AND observed<=? ORDER BY observed DESC LIMIT 1", (eid, as_of)).fetchone()
    if r:
        out["market"] = dict(r)
        if r["status"] != "ok":
            out["missing"].append("the latest market observation is %s" % r["status"])
    else:
        out["missing"].append("no market snapshot known by then")
    return out


def _rec(kind: str, num: Optional[dict], den: Optional[dict], calc: str, result: Optional[float], conf: float, missing: List[str], fresh: bool = True) -> dict:
    return {"kind": kind, "numerator": num, "denominator": den, "calc": calc, "result": result, "band": band_of(result), "confidence": conf if result is not None else 0.0, "missing": missing,
            "may_influence": bool(result is not None and conf >= 0.6 and fresh and not missing)}


def ratio(kind: str, label_n: str, n: Optional[float], n_src: str, label_d: str, d: Optional[float], d_src: str, conf: float = 0.7, fresh: bool = True) -> dict:
    missing = []
    if n is None:
        missing.append("%s not stated" % label_n)
    if d is None or d == 0:
        missing.append("%s unavailable" % label_d)
    if missing:
        return _rec(kind, {"label": label_n, "value": n, "source": n_src}, {"label": label_d, "value": d, "source": d_src}, "%s / %s" % (label_n, label_d), None, 0.0, missing)
    return _rec(kind, {"label": label_n, "value": n, "source": n_src}, {"label": label_d, "value": d, "source": d_src}, "%s / %s = %.4g / %.4g" % (label_n, label_d, n, d), n / d, conf, [], fresh)


def materiality_for(conn, cfg: dict, ev: dict, now=None, as_of: Optional[str] = None, persist: bool = True) -> List[dict]:
    """The calculations that apply to this event, each recorded with its inputs. `ev` = {event_id, ticker, event_type, text, amount_usd, roles(optional {ticker: role}), counterparty}."""
    now = now or utcnow()
    as_of = as_of or iso(now)
    t, et, text = ev["ticker"], ev.get("event_type"), ev.get("text") or ""
    amts = parse_amounts(text)
    usd = ev.get("amount_usd") or (amts["usd"][0]["value"] if amts["usd"] else None)
    usd_src = "events.amount_usd" if ev.get("amount_usd") else ("headline: " + amts["usd"][0]["text"] if amts["usd"] else "not stated")
    ci = company_inputs(conn, t, as_of)
    fin, mk = (ci["financial"] or {}), (ci["market"] or {})
    rep, der = fin.get("reported") or {}, fin.get("derived") or {}
    val = lambda d, k: (d.get(k) or {}).get("value") if isinstance(d.get(k), dict) else d.get(k)  # noqa: E731
    rev, ni, cash, debt = val(rep, "revenue_ttm"), val(rep, "net_income_ttm"), val(rep, "cash"), der.get("debt")
    shares = val(rep, "shares_diluted")
    mcap = mk.get("mcap") if mk.get("status") == "ok" else None
    fsrc = "kb_financial_snapshots first known %s" % fin.get("first_known") if fin else "unavailable"
    msrc = "kb_market_snapshots observed %s" % mk.get("observed") if mk else "unavailable"
    fresh = bool(fin) and fin.get("status") in ("ok", "partial")
    recs: List[dict] = []
    if et in ("contract_win", "contract_loss"):
        recs.append(ratio("contract_vs_revenue", "contract value", usd, usd_src, "annual revenue (TTM)", rev, fsrc, 0.7, fresh))
        bl = conn.execute("SELECT backlog FROM company_fundamentals WHERE ticker=?", (t,)).fetchone() if "company_fundamentals" in db.tables(conn) else None
        recs.append(ratio("contract_vs_backlog", "contract value", usd, usd_src, "backlog", bl[0] if bl and bl[0] else None, "company_fundamentals.backlog", 0.6, True))
    elif et == "capital_raise":
        sh = amts["shares"][0]["value"] if amts["shares"] else None
        recs.append(ratio("offering_vs_market_cap", "offering size", usd, usd_src, "market capitalisation", mcap, msrc, 0.7, bool(mk)))
        recs.append(ratio("offering_shares_vs_outstanding", "shares offered", sh, "headline" if sh else "not stated", "diluted shares outstanding", shares, fsrc, 0.65, fresh))
        if re.search(r"\b(notes?|debt|debentures?|bonds?|term loan)\b", text, re.I):
            recs.append(ratio("debt_issuance_vs_debt", "debt issued", usd, usd_src, "existing debt", debt, fsrc, 0.6, fresh))
            recs.append(ratio("debt_issuance_vs_cash", "debt issued", usd, usd_src, "cash", cash, fsrc, 0.6, fresh))
    elif et == "litigation":
        for lab, d, src in (("cash", cash, fsrc), ("annual revenue (TTM)", rev, fsrc), ("annual net income (TTM)", ni, fsrc)):
            recs.append(ratio("fine_or_settlement_vs_" + lab.split()[0] if lab != "annual net income (TTM)" else "fine_or_settlement_vs_earnings", "fine or settlement", usd, usd_src, lab, d if (lab != "annual net income (TTM)" or (d or 0) > 0) else None, src, 0.65, fresh))
    elif et == "buyback_dividend":
        if re.search(r"\bdividend\b", text, re.I) and amts["pct"]:
            recs.append(_rec("dividend_change", {"label": "stated change", "value": amts["pct"][0]["value"], "source": "headline: " + amts["pct"][0]["text"]}, None, "stated percentage change in the dividend", amts["pct"][0]["value"], 0.6, [], True))
        else:
            recs.append(ratio("buyback_vs_market_cap", "buyback size", usd, usd_src, "market capitalisation", mcap, msrc, 0.7, bool(mk)))
            sh = amts["shares"][0]["value"] if amts["shares"] else None
            recs.append(ratio("buyback_shares_vs_outstanding", "shares to buy back", sh, "headline" if sh else "not stated", "diluted shares outstanding", shares, fsrc, 0.65, fresh))
    elif et == "merger_acquisition":
        tgt_mcap = None
        cp = ev.get("counterparty")
        if cp:
            cm = conn.execute("SELECT mcap,status FROM kb_market_snapshots WHERE entity_id=(SELECT entity_id FROM kb_identifiers WHERE kind='ticker' AND value=? AND valid_to IS NULL) ORDER BY observed DESC LIMIT 1", (cp,)).fetchone()
            tgt_mcap = cm["mcap"] if cm and cm["status"] == "ok" else None
        recs.append(ratio("deal_vs_acquirer_market_cap", "deal value", usd, usd_src, "acquirer market capitalisation", mcap, msrc, 0.65, bool(mk)))
        recs.append(ratio("deal_vs_target_market_cap", "deal value", usd, usd_src, "target market capitalisation", tgt_mcap, "counterparty %s" % cp if cp else "counterparty not identified", 0.6, True))
    elif et == "insider_filing":
        recs.append(ratio("insider_trade_vs_market_value", "transaction value", usd, usd_src, "market capitalisation", mcap, msrc, 0.6, bool(mk)))
        recs.append(_rec("insider_trade_vs_holding", {"label": "transaction", "value": usd, "source": usd_src}, None, "the insider's post-transaction holding is needed (Form 4 detail)", None, 0.0, ["post-transaction holding not available in the headline"]))
    elif et in ("guidance_raise", "guidance_cut"):
        pcts = amts["pct"]
        prior = re.search(r"from\s+\$?([\d.,]+)\s*(billion|million|bn|mm|b|m)?.{0,40}?to\s+\$?([\d.,]+)\s*(billion|million|bn|mm|b|m)?", text, re.I)
        if prior:
            try:
                a = float(prior.group(1).replace(",", "")) * _MULT[(prior.group(2) or "").lower() or None]
                b = float(prior.group(3).replace(",", "")) * _MULT[(prior.group(4) or "").lower() or None]
                recs.append(_rec("guidance_change_vs_prior", {"label": "new guidance", "value": b, "source": "headline"}, {"label": "prior guidance", "value": a, "source": "headline"}, "new / prior - 1 = %.4g / %.4g - 1" % (b, a), (b / a - 1.0) if a else None, 0.6, [], True))
            except (ValueError, KeyError, ZeroDivisionError):
                pass
        if not prior:
            recs.append(_rec("guidance_change_vs_prior", None, None, "needs the prior and new guidance figures", None, 0.0, ["prior and new guidance figures not both stated"]))
    elif et == "production_disruption":
        m = re.search(r"(\d+\.?\d*)\s?%\s+of\s+(?:its\s+|the\s+|global\s+|total\s+)?(?:production|output|capacity|supply)", text, re.I)
        recs.append(_rec("outage_vs_normal_production", {"label": "share of production affected", "value": float(m.group(1)) / 100.0, "source": "headline"} if m else None, None, "stated share of normal production", float(m.group(1)) / 100.0 if m else None, 0.6 if m else 0.0,
                         [] if m else ["the share of production affected is not stated"], True))
    elif et == "regulatory_approval" and re.search(r"\bFDA\b|clinical", text, re.I):
        recs.append(_rec("clinical_vs_pipeline_concentration", None, None, "needs the company's pipeline revenue concentration (not stored)", None, 0.0, ["pipeline concentration data is not available"]))
    if not recs:
        recs.append(_rec("not_applicable", None, None, "no materiality calculation exists for event type %s" % et, None, 0.0, ["no calculator for this event type"]))
    if persist:
        for r in recs:
            conn.execute("INSERT INTO event_materiality(event_id,ticker,kind,numerator_json,denominator_json,calc,result,band,confidence,missing_json,may_influence,computed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (ev.get("event_id"), t, r["kind"], json.dumps(r["numerator"]), json.dumps(r["denominator"]), r["calc"], r["result"], r["band"], r["confidence"], json.dumps(r["missing"]), int(r["may_influence"]), iso(now)))
    return recs


# ================================================================================================================================ multi-event company state
_QUALIFIER = re.compile(r"\b(temporary|one[- ]time|one[- ]off|transitory|non[- ]recurring|short[- ]lived|timing)\b", re.I)
_RESTRICT = re.compile(r"\b(restricted|limited to|with conditions|boxed warning|narrower|conditional(?:ly)?)\b", re.I)
HORIZON_DAYS = {"immediate": 1, "days": 3, "short": 5, "weeks": 15, "medium": 20, "months": 60, "long": 90}


def _near(h: str) -> bool:
    return HORIZON_DAYS.get(h or "weeks", 15) <= 5


_CANON = {"reported": "confirmed", "agreed": "confirmed", "completed": "confirmed", "rejected": "denied"}


def relate(new: dict, prior: dict) -> str:
    """How a new event stands to an earlier one about the same company: confirms | denies | corrects | updates | contradicts | duplicates | unrelated."""
    new = dict(new, state=_CANON.get(new.get("state"), new.get("state")))                      # the analyst says reported / agreed / completed: all of them are "confirmed" here
    prior = dict(prior, state=_CANON.get(prior.get("state"), prior.get("state")))
    same_chain = new.get("chain") and new.get("chain") == prior.get("chain")
    if not same_chain and new.get("family") != prior.get("family"):
        if (new.get("direction") or 0) * (prior.get("direction") or 0) < -0.1:
            return "contradicts"
        return "unrelated"
    if new.get("state") in ("denied",) and prior.get("state") in ("rumored", "confirmed"):
        return "denies"
    if new.get("novelty") == "correction":
        return "corrects"
    if prior.get("state") == "rumored" and new.get("state") == "confirmed":
        return "confirms"
    if prior.get("state") == "denied" and new.get("state") == "confirmed":
        return "supersedes"
    if (new.get("direction") or 0) * (prior.get("direction") or 0) < -0.1:
        return "contradicts"
    if new.get("novelty") in ("update", "new_facts"):
        return "updates"
    if int(new.get("independent_sources") or 0) > int(prior.get("independent_sources") or 0) or new.get("source") != prior.get("source"):
        return "confirms"
    return "duplicates"


NON_CATALYST = {"general_news", "opinion_piece", "price_recap", "structural_theme", "sec_filing", "market_crash", "central_bank", "jobs_data", "inflation_data", "growth_recession", "election_policy", "fiscal_policy", "emergency_policy", "war_escalation", "geopolitical_conflict", "disaster_pandemic", "market_infrastructure_outage", "energy_supply", "supply_chain_macro"}


def _load_events(conn, ticker: str, since: str, until: str) -> List[dict]:
    rows = conn.execute("SELECT e.id,e.event_type,e.direction,e.magnitude,e.headline,e.published,e.created,e.cluster_id,e.novelty,e.credibility,e.source,e.stale FROM events e WHERE e.tickers_json LIKE ? AND e.created>=? AND e.created<=? ORDER BY e.created,e.id",
                        ('%"' + ticker + '"%', since, until)).fetchall()
    out = []
    has_r = "reasoning_events" in db.tables(conn)
    from . import skeptic
    from . import reasoning as R
    for r in rows:
        d = dict(r)
        if d["event_type"] in NON_CATALYST:                                                                   # filler and market-wide items are not company catalysts
            continue
        d["family"] = skeptic.FAMILY.get(d["event_type"], d["event_type"])
        d["chain"] = d.get("cluster_id") or d["id"]
        d["state"], d["independent_sources"], d["materiality"], d["horizon"] = R.event_state(d.get("headline") or ""), 1, None, None      # the deterministic reading of the headline, until a stored analyst reading says otherwise
        if not d.get("novelty") or isinstance(d.get("novelty"), (int, float)):
            d["novelty"] = R.novelty_of(d.get("headline") or "")
        if has_r:
            rr = conn.execute("SELECT state,novelty,independent_sources,materiality,horizons_json,roles_json,abstained FROM reasoning_events WHERE event_ref=? ORDER BY id DESC LIMIT 1", (d["id"],)).fetchone()
            if rr:
                d["state"], d["independent_sources"], d["materiality"] = rr["state"], rr["independent_sources"] or 1, rr["materiality"]
                d["novelty"] = rr["novelty"] or d.get("novelty")
                try:
                    roles = json.loads(rr["roles_json"] or "{}")
                    if ticker in roles and isinstance(roles[ticker], (list, tuple)) and roles[ticker]:
                        d["direction"] = float(roles[ticker][0])                                         # the direction FOR THIS company, from the analyst's role reading
                except (ValueError, TypeError):
                    pass
        out.append(d)
    return out


def _weight(it: dict, now: dt.datetime, lc: Optional[dict]) -> float:
    mag = float(it.get("magnitude") or 0.0)
    cred = float(it.get("credibility") or 0.6)
    corr = 1.0 + 0.15 * (max(1, int(it.get("independent_sources") or 1)) - 1)
    mat = {"high": 1.2, "very_high": 1.3, "medium": 1.0, "low": 0.7, "negligible": 0.5}.get(it.get("materiality"), 0.9)
    fresh = 1.0
    if lc:
        exp, occ = _ts(lc["effective_expiry"]), _ts(lc["occurred"])
        if exp and occ and exp > occ:
            fresh = max(0.0, min(1.0, 1.0 - max(0.0, (now - occ).total_seconds()) / (exp - occ).total_seconds()))
    return round(mag * cred * min(corr, 1.4) * mat * (0.25 + 0.75 * fresh), 4)


def build_state(conn, cfg: dict, ticker: str, now=None, until: Optional[str] = None, days: int = 30) -> dict:
    """The company's event state AS KNOWN at `until` (default now). Chains of related events, each resolved by its own history (rumour -> denial, denial -> confirmation, award -> protest, approval -> restrictions,
    guidance cut -> 'temporary' explanation); then ONE thesis per timeframe from the best surviving evidence per direction. Not a sum of positive and negative scores."""
    now = now or utcnow()
    until = until or iso(now)
    since = iso(_ts(until) - dt.timedelta(days=days))
    evs = _load_events(conn, ticker, since, until)
    chains: Dict[object, List[dict]] = {}
    for e in evs:
        chains.setdefault(e["chain"], []).append(e)
    items: List[dict] = []
    relations: List[dict] = []
    t_until = _ts(until)
    for ck, es in chains.items():
        es = sorted(es, key=lambda x: (x["created"], x["id"]))
        last = es[-1]
        status, note = "open", ""
        first = es[0]
        cur = last
        for prev, nxt in zip(es, es[1:]):
            rel = relate(nxt, prev)
            relations.append({"event": nxt["id"], "prior": prev["id"], "relation": rel})
            if rel == "denies":
                status, note = "denied", "a later report denies it"
            elif rel == "confirms" and prev.get("state") == "rumored":
                status, note = "confirmed", "the rumour was later confirmed"
            elif rel == "supersedes":
                status, note = "confirmed", "a denial was itself superseded by confirmation"
            elif rel == "corrects":
                cur, note = nxt, "corrected by a later report"
        if cur.get("state") == "denied" and status == "open":
            status = "denied"
        qual = 1.0
        text = " ".join((x.get("headline") or "") for x in es[1:])
        if _QUALIFIER.search(text) and (first.get("direction") or 0) < 0:
            qual, note = 0.7, (note + "; " if note else "") + "a later report calls the cause temporary or one-off"
        if first["event_type"] == "contract_win" and _PROTEST.search(text):
            qual, status, note = 0.6, "contested", "the award is being protested"
        if first["event_type"] in ("regulatory_approval",) and _RESTRICT.search(" ".join((x.get("headline") or "") for x in es)):
            qual, note = 0.7, (note + "; " if note else "") + "approved with restrictions or conditions"
        lc = lifecycle(conn, cfg, {"event_id": cur["id"], "ticker": ticker, "event_type": cur["event_type"], "state": cur.get("state"), "text": cur.get("headline"), "published": cur.get("published"), "created": cur.get("created"),
                                   "materiality": cur.get("materiality"), "independent_sources": cur.get("independent_sources"), "denied_at": last["created"] if status == "denied" else None}, t_until, persist=False)
        w = _weight(cur, t_until, lc) * qual * (0.0 if status == "denied" else 1.0)
        hz = (__import__("paper_agent.reasoning", fromlist=["HORIZON"]).HORIZON.get(cur["event_type"]) or "weeks")
        items.append({"chain": ck, "events": [x["id"] for x in es], "event_type": cur["event_type"], "family": cur["family"], "headline": (cur.get("headline") or "")[:140], "direction": 0.0 if status == "denied" else (1.0 if (cur.get("direction") or 0) > 0 else -1.0 if (cur.get("direction") or 0) < 0 else 0.0),
                      "weight": w, "status": status, "note": note, "timeframe": "near" if _near(hz) else "medium", "horizon": hz, "state": cur.get("state"), "expiry": lc["effective_expiry"], "expired": lc["eventaware_stale"], "rule_id": lc["rule_id"],
                      "materiality": cur.get("materiality"), "independent_sources": cur.get("independent_sources"), "unresolved": lc["unresolved"] or status in ("open",) and cur.get("state") == "rumored"})

    def thesis(frame: str) -> dict:
        live = [i for i in items if i["timeframe"] == frame and not i["expired"] and i["status"] != "denied" and i["direction"] != 0]
        pos = max((i for i in live if i["direction"] > 0), key=lambda i: i["weight"], default=None)
        neg = max((i for i in live if i["direction"] < 0), key=lambda i: i["weight"], default=None)
        wp, wn = (pos["weight"] if pos else 0.0), (neg["weight"] if neg else 0.0)
        if not live or max(wp, wn) < 0.05:
            return {"direction": 0.0, "label": "no live catalyst", "confidence": 0.0, "driver": None, "opposed_by": None}
        lead, other = (pos, neg) if wp >= wn else (neg, pos)
        margin = abs(wp - wn) / max(wp, wn)
        if other and margin < 0.25:
            return {"direction": 0.0, "label": "contested", "confidence": round(0.3 * max(wp, wn), 3), "driver": lead["chain"], "opposed_by": other["chain"], "why": "strong evidence on both sides within 25% of each other"}
        conf = max(0.0, min(1.0, lead["weight"] * (1.0 - 0.5 * (other["weight"] / lead["weight"] if other else 0.0)) * (0.8 if lead["unresolved"] else 1.0)))
        return {"direction": lead["direction"], "label": "positive" if lead["direction"] > 0 else "negative", "confidence": round(conf, 3), "driver": lead["chain"], "opposed_by": other["chain"] if other else None}
    near, medium = thesis("near"), thesis("medium")
    both = [d for d in (near, medium) if d["direction"] != 0]
    net = 0.0
    if both:
        net = max(both, key=lambda d: d["confidence"])["direction"] if not (near["direction"] and medium["direction"] and near["direction"] != medium["direction"]) else 0.0
    exp = max((i["expiry"] for i in items if not i["expired"] and i["status"] != "denied"), default=None)
    ctx = []                                                                                                   # sector-wide pressure that reached this company through the relationship engine (recorded, never a catalyst by itself)
    if "kb_derived_impacts" in db.tables(conn):
        for r in conn.execute("SELECT src_ticker,rule_id,direction,direction_conf,mechanism,created FROM kb_derived_impacts WHERE dst_ticker=? AND created>=? AND created<=? AND direction!=0 AND rule_id LIKE '%INDUSTRY_WIDE%' ORDER BY created DESC LIMIT 5", (ticker, since, until)):
            ctx.append({"from": r["src_ticker"], "rule": r["rule_id"], "direction": r["direction"], "confidence": r["direction_conf"], "mechanism": r["mechanism"], "at": r["created"]})
    state = {"ticker": ticker, "as_of": until, "sector_context": ctx, "near_term": near, "medium_term": medium, "net_direction": net, "diverging": bool(near["direction"] and medium["direction"] and near["direction"] != medium["direction"]),
             "supporting": [i["chain"] for i in items if i["direction"] > 0 and not i["expired"] and i["status"] != "denied"], "opposing": [i["chain"] for i in items if i["direction"] < 0 and not i["expired"] and i["status"] != "denied"],
             "superseded": [i["chain"] for i in items if i["status"] == "denied" or any(r["relation"] in ("supersedes", "corrects") and r["prior"] in i["events"] for r in relations)],
             "unresolved": [i["chain"] for i in items if i["unresolved"]], "confirmations": [r for r in relations if r["relation"] == "confirms"], "denials": [r for r in relations if r["relation"] == "denies"],
             "corrections": [r for r in relations if r["relation"] == "corrects"], "items": items, "relations": relations, "catalyst_expiry": exp,
             "confidence_components": {"near": near["confidence"], "medium": medium["confidence"], "evidence_items": len(items), "live_items": sum(1 for i in items if not i["expired"] and i["status"] != "denied")},
             "invalidation": [], "last_material": max((i["events"][-1] for i in items), default=None)}
    for i in sorted([i for i in items if not i["expired"] and i["status"] != "denied" and i["direction"] != 0], key=lambda i: -i["weight"])[:3]:
        state["invalidation"].append("a denial or correction of: %s" % i["headline"][:70] if i["unresolved"] else "a %s event on this company pointing the other way" % ("negative" if i["direction"] > 0 else "positive"))
    return state


def save_state(conn, ticker: str, state: dict, trigger_event: Optional[int], now=None) -> dict:
    """Store the current state and keep EVERY prior version: event-time history is never rewritten. A new version is written only when something material changed."""
    now = now or utcnow()
    from . import kbsnap
    prev = conn.execute("SELECT state_version,state_json FROM company_event_states WHERE ticker=?", (ticker,)).fetchone()
    key = lambda s: json.dumps({k: s[k] for k in ("near_term", "medium_term", "net_direction", "supporting", "opposing", "unresolved", "catalyst_expiry")}, sort_keys=True, default=str)  # noqa: E731
    if prev:
        try:
            if key(json.loads(prev["state_json"])) == key(state):
                return {"version": prev["state_version"], "changed": False}
        except ValueError:
            pass
    ver = (prev["state_version"] if prev else 0) + 1
    blob = json.dumps(state, sort_keys=True, default=str)
    rel = next((r["relation"] for r in state["relations"] if r["event"] == trigger_event), "new" if not prev else "update")
    conn.execute("INSERT OR REPLACE INTO company_event_states(ticker,entity_id,state_version,state_json,near_dir,medium_dir,net_dir,confidence,expiry,last_material,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (ticker, kbsnap.entity_of(conn, ticker), ver, blob, state["near_term"]["direction"], state["medium_term"]["direction"], state["net_direction"], max(state["near_term"]["confidence"], state["medium_term"]["confidence"]),
                  state["catalyst_expiry"], str(state["last_material"]) if state["last_material"] else None, iso(now)))
    conn.execute("INSERT INTO company_event_state_history(ticker,state_version,as_of,trigger_event_id,relation,state_json,created) VALUES(?,?,?,?,?,?,?)", (ticker, ver, state["as_of"], trigger_event, rel, blob, iso(now)))
    return {"version": ver, "changed": True, "relation": rel}


def state_as_of(conn, ticker: str, when) -> Optional[dict]:
    """The state the bot held at `when`: the last stored version created at or before then. Never a later one."""
    r = conn.execute("SELECT state_json FROM company_event_state_history WHERE ticker=? AND created<=? ORDER BY created DESC,id DESC LIMIT 1", (ticker, iso(when) if hasattr(when, "isoformat") else when)).fetchone()
    return json.loads(r[0]) if r else None
