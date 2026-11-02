"""Event-conditioned relationship reasoning. The sign of an effect is NEVER a property of the relationship type alone. It is the result of a named rule keyed on the event (family, subtype, state, how the company figures
in it), the neighbour's role, the economic mechanism and the event's scope. When no rule gives a clear net effect the engine ABSTAINS and records why.
Influence policy: direct company effects are the existing engine's business; first-hop effects start in OBSERVE (recorded and measured, trading unchanged) and become trade-capable only if the owner sets
kb.relationship_mode to active; second-hop effects are research ideas only; third-hop are explanation only; correlation can never create, approve, size or block anything."""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

from . import db
from .timeutil import iso, parse_ts, utcnow

NEGATIVE_EVENTS = {"earnings_miss", "guidance_cut", "contract_loss", "bankruptcy_distress", "production_disruption", "cyber_incident", "litigation", "regulatory_action", "credit_stress", "financial_stress_event"}
POSITIVE_EVENTS = {"earnings_beat", "guidance_raise", "contract_win", "regulatory_approval", "product_launch", "partnership", "buyback_dividend"}
DEMAND_EVENTS = {"earnings_beat", "earnings_miss", "guidance_raise", "guidance_cut", "contract_win", "contract_loss"}
OPERATIONAL_COMPANY_SPECIFIC = {"production_disruption", "cyber_incident", "litigation", "regulatory_action", "contract_loss", "recall"}
HOP_DECAY = {1: 1.0, 2: 0.6, 3: 0.4}
INFLUENCE = {1: "observe", 2: "research", 3: "explain"}
CORR_TYPES = {"corr", "correlation", "comovement"}

# rule: id, event types, neighbour role (relative to the SOURCE company), source roles allowed (None = any), conditions, sign (+1 same direction as the effect on the source, -1 opposite), base confidence, horizon, mechanism, applicability
RULES: List[dict] = [
    {"id": "SUP_DEMAND_UP", "events": {"earnings_beat", "guidance_raise", "contract_win"}, "role": "supplier", "need": {"positive"}, "sign": 1, "conf": 0.55, "horizon": "medium",
     "mech": "{src}'s demand is strengthening, which raises its orders for what {dst} supplies", "why": "a positive demand event at a customer reaches the supplier through order volumes"},
    {"id": "SUP_DEMAND_DOWN", "events": {"earnings_miss", "guidance_cut", "contract_loss"}, "role": "supplier", "need": {"negative"}, "sign": 1, "conf": 0.50, "horizon": "medium",
     "mech": "{src}'s demand is weakening, which cuts its orders for what {dst} supplies", "why": "a negative demand event at a customer reaches the supplier through order volumes"},
    {"id": "SUP_CUSTOMER_DISTRESS", "events": {"bankruptcy_distress", "credit_stress", "financial_stress_event"}, "role": "supplier", "need": {"negative"}, "sign": 1, "conf": 0.70, "horizon": "short",
     "mech": "{src} is in financial distress, which threatens its orders from, and payments to, {dst}", "why": "customer distress endangers receivables and future orders"},
    {"id": "SUP_CUSTOMER_OUTAGE", "events": {"production_disruption"}, "role": "supplier", "need": {"negative"}, "sign": 1, "conf": 0.45, "horizon": "medium",
     "mech": "{src}'s production is disrupted, so it needs less of what {dst} supplies for as long as the outage lasts", "why": "a production outage at a customer pauses its purchases"},
    {"id": "CUST_SUPPLY_OUTAGE", "events": {"production_disruption", "cyber_incident"}, "role": "customer", "need": {"negative"}, "sign": 1, "conf": 0.45, "horizon": "medium",
     "mech": "{src} supplies {dst} and its output is disrupted, which may constrain what {dst} can sell", "why": "a supplier's outage constrains the customer's own supply"},
    {"id": "CUST_SUPPLIER_DISTRESS", "events": {"bankruptcy_distress", "credit_stress"}, "role": "customer", "need": {"negative"}, "sign": 1, "conf": 0.50, "horizon": "medium",
     "mech": "{src} supplies {dst} and is in distress, which puts {dst}'s supply at risk", "why": "supplier failure threatens continuity of supply"},
    {"id": "TECH_PROVIDER_FAILURE", "events": {"cyber_incident", "production_disruption", "regulatory_action"}, "role": "technology_provider", "need": {"negative"}, "sign": 1, "conf": 0.40, "horizon": "short",
     "mech": "{dst} depends on technology from {src}, which has suffered a setback", "why": "a technology dependency transmits the provider's failure"},
    {"id": "COMP_SPECIFIC_NEGATIVE", "events": OPERATIONAL_COMPANY_SPECIFIC, "role": "competitor", "need": {"negative", "company_specific"}, "sign": -1, "conf": 0.40, "horizon": "medium",
     "mech": "{src} has a company-specific setback, which may shift customers or share to rivals such as {dst}", "why": "a setback at one company can move demand to its competitors"},
    {"id": "COMP_LOST_CONTRACT", "events": {"contract_win"}, "role": "competitor", "need": {"positive", "company_specific"}, "sign": -1, "conf": 0.35, "horizon": "medium",
     "mech": "{src} won business that {dst} may have competed for", "why": "a contract won by one bidder is lost by the others, if {dst} bid; that is not known, hence the low confidence"},
    {"id": "COMP_PRODUCT_LAUNCH", "events": {"product_launch"}, "role": "competitor", "need": {"positive", "company_specific"}, "sign": -1, "conf": 0.25, "horizon": "long",
     "mech": "{src} launched a product that competes with {dst}'s", "why": "a competing launch can take share"},
    {"id": "INDUSTRY_WIDE_SAME_SIGN", "events": None, "role": "competitor", "need": {"industry_wide"}, "sign": 1, "conf": 0.60, "horizon": "medium",
     "mech": "the event affects the whole industry, so {dst} is hit in the same direction as {src}", "why": "an industry-wide development moves competitors together"},
    {"id": "PEER_INDUSTRY_WIDE", "events": None, "role": "sector_peer", "need": {"industry_wide"}, "sign": 1, "conf": 0.50, "horizon": "medium",
     "mech": "the event affects the whole sector, so {dst} moves with {src}", "why": "a sector-wide development moves peers together"},
    {"id": "PEER_INDUSTRY_WIDE_B", "events": None, "role": "industry_peer", "need": {"industry_wide"}, "sign": 1, "conf": 0.50, "horizon": "medium",
     "mech": "the event affects the whole industry, so {dst} moves with {src}", "why": "an industry-wide development moves peers together"},
    {"id": "PEER_REGULATORY_PRECEDENT", "events": {"regulatory_action", "regulatory_approval"}, "role": "sector_peer", "need": set(), "sign": 1, "conf": 0.40, "horizon": "medium",
     "mech": "a regulatory decision about {src} sets a precedent for peers such as {dst}", "why": "regulators tend to apply the same standard to similar companies"},
    {"id": "PEER_RESULTS_READ_ACROSS", "events": {"earnings_beat", "earnings_miss", "guidance_raise", "guidance_cut"}, "role": "sector_peer", "need": {"company_specific"}, "sign": 1, "conf": 0.28, "horizon": "short",
     "mech": "{src}'s results may say something about conditions that {dst} also faces", "why": "results at one company are read across to similar companies, weakly"},
    {"id": "PEER_TAKEOVER_REPRICE", "events": {"merger_acquisition"}, "role": "sector_peer", "need": {"source_target"}, "sign": 1, "conf": 0.30, "horizon": "short",
     "mech": "{src} is the target of a takeover, which can re-rate similar companies such as {dst}", "why": "a takeover premium re-prices comparable companies"},
    {"id": "PARENT_FROM_SUBSIDIARY", "events": None, "role": "parent", "need": set(), "sign": 1, "conf": 0.45, "horizon": "short",
     "mech": "{src} is a subsidiary of {dst}; what happens to it flows to the parent in proportion to its size", "why": "a parent consolidates its subsidiary's results"},
    {"id": "SUBSIDIARY_FROM_PARENT", "events": None, "role": "subsidiary", "need": set(), "sign": 1, "conf": 0.30, "horizon": "short",
     "mech": "{dst} is a subsidiary of {src}; parent-level news can affect it", "why": "parent decisions can affect a subsidiary"},
    {"id": "PARTNER_SAME_SIGN", "events": {"partnership", "contract_win", "contract_loss", "product_launch", "production_disruption", "regulatory_action"}, "role": "partner", "need": set(), "sign": 1, "conf": 0.35, "horizon": "medium",
     "mech": "{src} and {dst} are partners, so a development at one is shared by the other", "why": "a partnership shares outcomes"},
    {"id": "JV_SAME_SIGN", "events": None, "role": "joint_venture", "need": set(), "sign": 1, "conf": 0.40, "horizon": "medium",
     "mech": "{src} and {dst} share a joint venture, so a development at one reaches the other", "why": "a joint venture shares outcomes"},
    {"id": "GOV_CUSTOMER", "events": {"contract_win", "contract_loss", "fiscal_policy"}, "role": "government_customer", "need": set(), "sign": 1, "conf": 0.35, "horizon": "long",
     "mech": "{dst} depends on government customers, so government spending decisions reach it", "why": "a government-dependent company follows its customer's budget"},
]
# which role the NEIGHBOUR plays relative to the source, from (stored rel_type, whether the source is the src side)
NEIGHBOUR_ROLE = {("supplier", True): "customer", ("supplier", False): "supplier", ("customer", True): "supplier", ("customer", False): "customer", ("competitor", True): "competitor", ("competitor", False): "competitor",
                  ("sector_peer", True): "sector_peer", ("sector_peer", False): "sector_peer", ("industry_peer", True): "industry_peer", ("industry_peer", False): "industry_peer", ("parent", True): "subsidiary", ("parent", False): "parent",
                  ("subsidiary", True): "parent", ("subsidiary", False): "subsidiary", ("partner", True): "partner", ("partner", False): "partner", ("joint_venture", True): "joint_venture", ("joint_venture", False): "joint_venture",
                  ("technology_dependency", True): "technology_provider", ("technology_dependency", False): "technology_dependent", ("distributor", True): "distributor", ("distributor", False): "supplier",
                  ("contract_manufacturer", True): "customer", ("contract_manufacturer", False): "contract_manufacturer", ("government_customer", True): "government_customer", ("government_customer", False): "government_customer",
                  ("licensing", True): "licensee", ("licensing", False): "licensor", ("brand_owner", True): "brand_licensee", ("brand_owner", False): "brand_owner"}


def source_role(why: str, etype: str) -> str:
    w = (why or "").lower()
    for key, role in (("acquirer", "acquirer"), ("target", "target"), ("plaintiff", "plaintiff"), ("sued", "defendant"), ("defendant", "defendant"), ("wins", "winner"), ("won", "winner"), ("loses", "loser"), ("lost", "loser")):
        if key in w:
            return role
    return "subject"


def conditions(ev: dict) -> set:
    c = set()
    d = ev.get("direction") or 0.0
    if d > 0.05:
        c.add("positive")
    elif d < -0.05:
        c.add("negative")
    if ev.get("industry_wide"):
        c.add("industry_wide")
    else:
        c.add("company_specific")
    if ev.get("source_role") == "target":
        c.add("source_target")
    if ev.get("state") in ("confirmed", None, ""):
        c.add("confirmed")
    return c


def match_rules(ev: dict, role: str) -> List[dict]:
    have = conditions(ev)
    out = []
    for r in RULES:
        if r["role"] != role:
            continue
        if r["events"] is not None and ev["event_type"] not in r["events"]:
            continue
        if not r["need"] <= have:
            continue
        out.append(r)
    return out


def _fmt(s: str, src: str, dst: str) -> str:
    return s.format(src=src, dst=dst)


def neighbours(conn, eid: str, as_of: str, cap: int = 25) -> List[dict]:
    """Active relationships of an entity valid at `as_of` AND already known to the bot then (first_known <= as_of). Correlation-typed rows are never returned."""
    rows = conn.execute(
        "SELECT * FROM kb_relationships WHERE status='active' AND (src_entity=? OR dst_entity=?) AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR valid_to>?) AND (first_known IS NULL OR first_known<=?) "
        "ORDER BY confidence DESC, strength DESC LIMIT ?", (eid, eid, as_of, as_of, as_of, cap * 3)).fetchall()
    out = []
    for r in rows:
        if (r["rel_type"] or "").lower() in CORR_TYPES:
            continue
        is_src = r["src_entity"] == eid
        role = NEIGHBOUR_ROLE.get((r["rel_type"], is_src))
        other = r["dst_entity"] if is_src else r["src_entity"]
        if role and other != eid:
            out.append(dict(r, neighbour=other, role=role))
        if len(out) >= cap:
            break
    return out


def _ticker(conn, eid: str) -> str:
    r = conn.execute("SELECT value FROM kb_identifiers WHERE entity_id=? AND kind='ticker' AND valid_to IS NULL ORDER BY is_primary DESC LIMIT 1", (eid,)).fetchone()
    if not r:
        r = conn.execute("SELECT value FROM kb_identifiers WHERE entity_id=? AND kind='ticker' ORDER BY id DESC LIMIT 1", (eid,)).fetchone()
    return r[0] if r else eid


def propagate(conn, cfg: dict, ev: dict, now=None, as_of: Optional[str] = None, persist: bool = True, max_hops: Optional[int] = None) -> List[dict]:
    """ev = {event_id, ticker, event_type, direction (effect on the source, -1..1), magnitude, state, headline, source_role, industry_wide, confidence}. Returns every derived impact (including abstentions)."""
    k = cfg.get("kb") or {}
    mode = k.get("relationship_mode", "observe")
    if mode == "off":
        return []
    now = now or utcnow()
    as_of = as_of or iso(now)
    hops_max = int(max_hops if max_hops is not None else k.get("max_hops", 3))
    from . import kbsnap
    src_e = kbsnap.entity_of(conn, ev["ticker"])
    if not src_e:
        return []
    out: List[dict] = []
    seen = {src_e}
    frontier = [(src_e, ev["ticker"], 1.0, 1.0, [ev["ticker"]], None)]                   # entity, ticker, signed direction multiplier, confidence so far, path
    cap_total = 120
    for hop in range(1, hops_max + 1):
        nxt = []
        for e, t, sgn, conf0, path, _ in frontier:
            for nb in neighbours(conn, e, as_of):
                if len(out) >= cap_total:
                    break
                dst_e = nb["neighbour"]
                dst_t = _ticker(conn, dst_e)
                if dst_e in seen:
                    continue
                rules = match_rules(ev if hop == 1 else dict(ev, direction=ev["direction"] * sgn, industry_wide=ev.get("industry_wide")), nb["role"])
                base = {"event_id": ev.get("event_id"), "created": iso(now), "mode": mode, "src_ticker": t, "dst_ticker": dst_t, "hops": hop, "path": path + [dst_t], "rel_id": nb["rel_id"], "rel_type": nb["rel_type"], "role": nb["role"],
                        "knowledge": {"relationship_known_since": nb.get("first_known"), "relationship_verified": nb.get("last_verified"), "relationship_source": nb.get("source"), "relationship_confidence": nb.get("confidence")}}
                if not rules:
                    out.append(dict(base, rule_id=None, mechanism=None, direction=0.0, direction_conf=0.0, magnitude_adj=0.0, horizon=None, influence_class="none", applicability="",
                                    abstain="no rule links a %s event at %s to its %s %s (the effect on %s could be either way)" % (ev["event_type"].replace("_", " "), t, nb["role"].replace("_", " "), dst_t, dst_t)))
                    continue
                signs = {r["sign"] for r in rules if r["conf"] >= 0.2}
                top = max(rules, key=lambda r: r["conf"])
                if len(signs) > 1 and min(r["conf"] for r in rules if r["sign"] != top["sign"]) >= 0.8 * top["conf"]:
                    out.append(dict(base, rule_id="+".join(sorted(r["id"] for r in rules)), mechanism=None, direction=0.0, direction_conf=0.0, magnitude_adj=0.0, horizon=None, influence_class="none", applicability="",
                                    abstain="opposing channels of similar strength (%s): no clear net effect on %s" % (", ".join(sorted(r["id"] for r in rules)), dst_t)))
                    continue
                s = top["sign"] * sgn
                d_conf = top["conf"] * float(nb.get("confidence") or 0.4) * HOP_DECAY[hop] * (1.0 if "confirmed" in conditions(ev) else 0.55) * conf0
                strength = float(nb.get("strength") or 0.3)
                share = nb.get("cost_revenue_share")
                mag = max(0.0, min(1.0, strength * (float(share) if share else 1.0) * HOP_DECAY[hop] * (0.5 if hop == 1 else 0.4)))
                eff_dir = (ev["direction"] if hop == 1 else ev["direction"] * sgn) * top["sign"]
                direction = (1.0 if eff_dir > 0 else -1.0 if eff_dir < 0 else 0.0)
                cls = INFLUENCE[hop]
                if hop == 1 and mode == "active" and d_conf >= 0.20 and "confirmed" in conditions(ev) and (ev.get("state") in ("confirmed", None, "")):
                    cls = "trade"
                rec = dict(base, rule_id=top["id"], mechanism=("second-order, via %s: " % "->".join(path) if hop >= 2 else "") + _fmt(top["mech"], t, dst_t), direction=direction, direction_conf=round(d_conf, 3), magnitude_adj=round(mag, 3), horizon=top["horizon"],
                           influence_class=cls, applicability=_fmt(top["why"], t, dst_t), abstain=None)
                out.append(rec)
                seen.add(dst_e)
                nxt.append((dst_e, dst_t, sgn * top["sign"], conf0 * top["conf"], path + [dst_t], None))
        frontier = nxt
        if not frontier:
            break
    if persist and out:
        for r in out:
            ev_rows = conn.execute("SELECT kind,url,note,source FROM kb_relationship_evidence WHERE rel_id=? LIMIT 3", (r["rel_id"],)).fetchall()
            r["evidence"] = [dict(x) for x in ev_rows]
            conn.execute("INSERT INTO kb_derived_impacts(event_id,created,mode,src_ticker,dst_ticker,hops,path_json,rel_id,rel_type,rule_id,mechanism,direction,direction_conf,magnitude_adj,horizon,evidence_json,applicability,abstain_reason,influence_class,knowledge_json) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (r["event_id"], r["created"], r["mode"], r["src_ticker"], r["dst_ticker"], r["hops"], json.dumps(r["path"]), r["rel_id"], r["rel_type"], r["rule_id"], r["mechanism"],
                                                                         r["direction"], r["direction_conf"], r["magnitude_adj"], r["horizon"], json.dumps(r["evidence"]), r["applicability"], r["abstain"], r["influence_class"], json.dumps(r["knowledge"])))
    return out


def never_trades_on_correlation(impacts: List[dict]) -> bool:
    """The safety invariant, checkable in tests and in production: no impact that rests on a correlation-typed link may be trade-capable or even observe-class."""
    return all(not ((i.get("rel_type") or "").lower() in CORR_TYPES and i.get("influence_class") in ("trade", "observe")) for i in impacts)


# ------------------------------------------------------------------------------------------------ exposure-based effects (macro drivers, commodities, controls) via kb_exposures
def exposure_impacts(conn, cfg: dict, driver_subject: str, driver_move: float, event_id: Optional[int], now=None, as_of: Optional[str] = None, persist: bool = True, cap: int = 60) -> List[dict]:
    """A move in a named driver (e.g. 'oil price' up) reaches each company through ITS recorded exposure: dir_rise when the driver rises, dir_fall when it falls. A company with several exposures to the same driver
    whose channels disagree is an abstention, not a guess."""
    now = now or utcnow()
    as_of = as_of or iso(now)
    rows = conn.execute("SELECT * FROM kb_exposures WHERE subject=? AND (status IS NULL OR status='ok') AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR valid_to>?) AND (first_known IS NULL OR first_known<=?) ORDER BY confidence DESC LIMIT ?",
                        (driver_subject, as_of, as_of, as_of, cap * 4)).fetchall()
    by: Dict[str, List[dict]] = {}
    for r in rows:
        by.setdefault(r["entity_id"], []).append(dict(r))
    out = []
    for eid, rs in list(by.items())[:cap]:
        dirs = {(r["dir_rise"] if driver_move > 0 else r["dir_fall"]) > 0 for r in rs if (r["dir_rise"] or 0) != 0}
        t = _ticker(conn, eid)
        if len(dirs) > 1:
            out.append({"event_id": event_id, "dst_ticker": t, "direction": 0.0, "direction_conf": 0.0, "abstain": "opposing channels for %s on %s (%s): no clear net effect" % (driver_subject, t, "; ".join(sorted({r["channel"] or "?" for r in rs}))), "influence_class": "none", "rule_id": "EXPOSURE_OPPOSING"})
            continue
        r = rs[0]
        d = r["dir_rise"] if driver_move > 0 else r["dir_fall"]
        out.append({"event_id": event_id, "dst_ticker": t, "direction": 1.0 if d > 0 else -1.0, "direction_conf": round(float(r["confidence"] or 0.3) * (1.0 if abs(driver_move) >= 0.5 else 0.7), 3), "magnitude_adj": round(float(r["strength"] or 0.3) * min(1.0, abs(driver_move)), 3),
                    "mechanism": "%s %s: %s" % (driver_subject, "rises" if driver_move > 0 else "falls", r["channel"]), "rule_id": "EXPOSURE", "horizon": r["horizon"], "influence_class": "observe", "abstain": None, "exposure_id": r["id"], "source": r["source"]})
    if persist and out:
        for o in out:
            conn.execute("INSERT INTO kb_derived_impacts(event_id,created,mode,src_ticker,dst_ticker,hops,path_json,rel_id,rel_type,rule_id,mechanism,direction,direction_conf,magnitude_adj,horizon,evidence_json,applicability,abstain_reason,influence_class,knowledge_json) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (event_id, iso(now), (cfg.get("kb") or {}).get("relationship_mode", "observe"), driver_subject, o["dst_ticker"], 0, json.dumps([driver_subject, o["dst_ticker"]]), None, "exposure", o["rule_id"],
                                                                         o.get("mechanism"), o["direction"], o["direction_conf"], o.get("magnitude_adj", 0.0), o.get("horizon"), json.dumps([{"exposure_id": o.get("exposure_id"), "source": o.get("source")}]), "a recorded exposure to this driver", o.get("abstain"), o["influence_class"], "{}"))
    return out
