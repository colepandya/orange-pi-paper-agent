"""Independent evaluation on REAL stored news. The cases are SAMPLED by data characteristics (seeded hashing: reproducible, not hand-picked around the engine's rules) and then LABELLED by a person-equivalent reader
(here: the build's author, which is a stated limitation); the engine is scored against those labels on separate, named aspects. There is deliberately NO single combined accuracy figure: a combined figure would hide
the aspects that fail. The labelled set lives apart from everything production learns from."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Callable, Dict, List, Optional

from . import db, nlp, reasoning as R, skeptic, timeframe
from .timeutil import iso, utcnow

DATASET_VERSION = "real-news-v1"
STATE_MAP = {"reported": "confirmed", "agreed": "confirmed", "completed": "confirmed", "rumored": "rumored", "denied": "denied", "rejected": "denied", "proposed": "proposed"}
MAT_MAP = {"transformative": "high", "material": "high", "moderate": "medium", "immaterial": "low"}
HORIZON_MAP = timeframe.LEGACY
FAMILY_MAP = dict(skeptic.FAMILY, analyst_action="analyst", general_news="none", opinion_piece="none", price_recap="none", insider_filing="capital")
CONTRA = {"contradiction_factual", "contradiction_superseded", "contradiction_newer", "contradiction_tension"}


def _h(seed: str, key) -> str:
    return hashlib.sha256(("%s|%s" % (seed, key)).encode()).hexdigest()


STRATA: Dict[str, str] = {
    "material_event": "SELECT e.id FROM events e WHERE e.magnitude>=0.6 AND e.tickers_json!='[]'",
    "likely_abstain": "SELECT e.id FROM events e WHERE e.event_type IN ('general_news','opinion_piece','price_recap') AND e.tickers_json!='[]'",
    "candidate_producing": "SELECT DISTINCT e.id FROM events e JOIN decisions d ON d.event_id=e.id WHERE d.action='BUY'",
    "short_candidate": "SELECT DISTINCT e.id FROM events e JOIN decisions d ON d.event_id=e.id WHERE d.action='SHORT'",
    "blocked_idea": "SELECT DISTINCT e.id FROM events e JOIN decisions d ON d.event_id=e.id WHERE d.status IN ('RISK_REJECTED','VETOED','REJECTED','HELD')",
    "relationship_derived": "SELECT DISTINCT e.id FROM events e JOIN decisions d ON d.event_id=e.id WHERE d.origin IN ('graph','derived','relationship')",
    "rumour_or_denial": "SELECT e.id FROM events e WHERE e.tickers_json!='[]' AND (e.headline LIKE '%reportedly%' OR e.headline LIKE '%sources%' OR e.headline LIKE '%rumor%' OR e.headline LIKE '%rumour%' OR e.headline LIKE '%denies%' OR e.headline LIKE '%denied%' OR e.headline LIKE '%no plans%')",
    "filing_derived": "SELECT e.id FROM events e WHERE e.tickers_json!='[]' AND (e.event_type='sec_filing' OR e.source LIKE '%sec%' OR e.source LIKE '%edgar%')",
    "duplicate_cluster": "SELECT e.id FROM events e WHERE e.cluster_id IN (SELECT cluster_id FROM events GROUP BY cluster_id HAVING COUNT(*)>=3) AND e.tickers_json!='[]'",
    "contradiction_candidate": "SELECT a.id FROM events a JOIN events b ON b.id>a.id AND b.tickers_json=a.tickers_json AND a.tickers_json!='[]' AND a.tickers_json NOT LIKE '%,%' AND abs(a.direction)>=0.4 AND a.direction*b.direction<0 AND b.created BETWEEN a.created AND datetime(a.created,'+2 days')",
    "control_random": "SELECT e.id FROM events e WHERE e.tickers_json!='[]'",
}


def sample(conn, per: int = 10, seed: str = "paper-agent-eval-1", strata: Optional[List[str]] = None, universe: Optional[dict] = None) -> List[dict]:
    """A reproducible stratified sample. Available strata only: an empty one is reported, never padded."""
    out, taken = [], set()
    report = {}
    for name in strata or list(STRATA):
        ids = [r[0] for r in conn.execute(STRATA[name])]
        ids = sorted(set(ids), key=lambda i: _h(seed, "%s|%s" % (name, i)))
        pick = []
        for i in ids:
            if i in taken:
                continue
            pick.append(i)
            taken.add(i)
            if len(pick) >= per:
                break
        report[name] = {"available": len(ids), "sampled": len(pick)}
        for i in pick:
            e = conn.execute("SELECT id,event_type,headline,published,created,source,tickers_json,direction,magnitude,cluster_id FROM events WHERE id=?", (i,)).fetchone()
            out.append({"case_id": "c%05d" % i, "stratum": name, "event_id": i, "headline": e["headline"], "published": e["published"], "source": e["source"], "engine_tickers_at_sampling": json.loads(e["tickers_json"] or "[]"), "cluster_id": e["cluster_id"]})
    if universe is not None:                                                                                        # extra stratum: companies matched to a story that never names them (suspect entity matches)
        sus = []
        for r in conn.execute("SELECT id,headline,tickers_json FROM events WHERE tickers_json!='[]' ORDER BY id DESC LIMIT 4000"):
            if r["id"] in taken:
                continue
            for t in json.loads(r["tickers_json"] or "[]")[:3]:
                nm = ((universe.get(t) or {}).get("name") or "").split()
                if nm and nm[0].lower() not in (r["headline"] or "").lower() and t.lower() not in (r["headline"] or "").lower().split():
                    sus.append(r["id"])
                    break
        sus = sorted(set(sus), key=lambda i: _h(seed, "suspect|%s" % i))[:per]
        report["suspect_entity_match"] = {"available": len(sus), "sampled": len(sus)}
        for i in sus:
            e = conn.execute("SELECT id,headline,published,source,tickers_json,cluster_id FROM events WHERE id=?", (i,)).fetchone()
            out.append({"case_id": "c%05d" % i, "stratum": "suspect_entity_match", "event_id": i, "headline": e["headline"], "published": e["published"], "source": e["source"], "engine_tickers_at_sampling": json.loads(e["tickers_json"] or "[]"), "cluster_id": e["cluster_id"]})
    return out, report


# ------------------------------------------------------------------------------------------------ the engine's reading of a case
def pair_partner(conn, event_id: int) -> Optional[int]:
    r = conn.execute("SELECT b.id FROM events a JOIN events b ON b.id>a.id AND b.tickers_json=a.tickers_json AND b.created BETWEEN a.created AND datetime(a.created,'+2 days') AND a.direction*b.direction<0 WHERE a.id=? ORDER BY b.id LIMIT 1", (event_id,)).fetchone()
    return r[0] if r else None


def engine_read(conn, cfg: dict, universe, case: dict, now) -> dict:
    """Run production's own path on a stored headline: entity match (confidence >= 0.5 as in evidence.py), then the analyst."""
    title, summ = case["headline"], case.get("summary")
    if summ is None:
        try:
            r_ = conn.execute("SELECT n.summary FROM events e JOIN news n ON n.uid=e.news_uid WHERE e.id=?", (case.get("event_id"),)).fetchone()
            summ = (r_[0] if r_ else "") or ""
        except Exception:  # noqa: BLE001
            summ = ""
    matches = universe.match(title, summ)
    tickers = [t for t, c, h in matches if c >= 0.5]
    item = {"uid": case["case_id"], "title": title, "summary": summ, "analysis": dict(nlp.analyze(title, summ)), "tickers": tickers}
    r = R.analyze_item(conn, cfg, universe, item, now) if tickers else None
    out = {"tickers": tickers, "abstained": r is None, "state": None, "family": None, "direction": {}, "materiality": None, "timeframe": None, "roles": {}, "event_type": None}
    out["state_raw"] = item.get("_state")
    if r is None:
        out["state"] = STATE_MAP.get(item.get("_state") or "", None)
        return out
    out.update({"event_type": r["event_type"], "family": FAMILY_MAP.get(r["event_type"], "other"), "state": STATE_MAP.get(r["state"], r["state"]), "materiality": MAT_MAP.get(r["materiality"], r["materiality"]),
                "timeframe": HORIZON_MAP.get(str(r.get("horizon")), str(r.get("horizon")))})
    for e in r["beneficiaries"] + r["losers"]:
        if e["order"] == 1:
            out["direction"][e["ticker"]] = 1 if e in r["beneficiaries"] else -1
    for t, v in (r.get("roles") or {}).items():
        out["roles"][t] = v[2] if isinstance(v, (list, tuple)) and len(v) > 2 else str(v)
    return out


_FILING_HEADER_RX = re.compile(r"^(?:[0-9A-Z/\-]{1,10})\s+-\s+(.+?)\s+\((\d{10})\)\s+\((?:Filer|Issuer|Reporting|Subject)\)")
_FILING_INDEX = re.compile(r"^(?:8-K|8-K/A|10-K|10-Q|20-F|6-K|[345]|SC 13[DG]|S-3|424B\d|FWP|DEF 14A)\s+-\s", re.I)
EVENT_FAMILIES = {"results", "contracts", "regulatory", "legal", "operations", "corporate", "capital", "people", "distress"}


def abstain_basis(case: dict) -> Optional[str]:
    """WHY a case is labelled should-abstain, from the label fields and the headline's form only: 'filing_no_content' (an index entry), 'immaterial_event' (a concrete event the labeller judged too small: the engine should ANALYSE it, with low
    materiality, not abstain), or 'non_event' (everything else: commentary, recap, question, list, background). Only the first and last are held against the engine's abstention."""
    L = case["label"]
    if not L.get("abstain"):
        return None
    if _FILING_INDEX.match(case["headline"] or ""):
        return "filing_no_content"
    if L.get("family") in EVENT_FAMILIES and L.get("direction") in (1, -1, 0) and not L.get("duplicate_of") and L.get("materiality") in ("low", "medium"):
        return "immaterial_event"
    return "non_event"


def coarse_role(why: str) -> str:
    w = (why or "").lower()
    if "named only" in w or "not named" in w or "platform" in w:
        return "mentioned_only"
    if any(k in w for k in ("target", "acquirer", "acquire")):
        return "party"
    return "subject"


# ------------------------------------------------------------------------------------------------ scoring
def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round((c - m) / d, 3), round((c + m) / d, 3))


def _rate(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"value": round(k / n, 3) if n else None, "k": k, "n": n, "ci95": [lo, hi]}


def score(cases: List[dict], reads: Dict[str, dict], labels: Dict[str, dict], pair_reads: Optional[Dict[str, bool]] = None, prop_reads: Optional[Dict[str, dict]] = None) -> dict:
    """Separate metrics, each with its own sample size and 95% interval. `labels[case_id]` follows the label schema in docs/EVALUATION.md."""
    m: Dict[str, dict] = {}
    tp = fp = fn = 0
    role_k = role_n = fam_k = fam_n = st_k = st_n = dir_k = dir_n = dir_abst = mat_k = mat_n = tf_k = tf_n = 0
    ab_tp = ab_fp = ab_fn = 0
    ab_by: Dict[str, list] = {}
    fm_n = fm_bad = 0
    tf_conf: Dict[tuple, int] = {}
    for c in cases:
        L, E = labels.get(c["case_id"]), reads.get(c["case_id"])
        if not L or not E:
            continue
        gold = {t: v for t, v in (L.get("involved") or {}).items()}
        pred = set(E["tickers"])
        tp += len(pred & set(gold))
        fp += len(pred - set(gold))
        fn += len(set(gold) - pred)
        fm_n += len(pred)
        fm_bad += len(pred - set(gold))
        for t in pred & set(gold):
            role_n += 1
            role_k += int(coarse_role(E["roles"].get(t, "")) == gold[t])
        if L.get("family") not in (None, "none") and not E["abstained"]:
            fam_n += 1
            fam_k += int(E["family"] == L["family"])
        if L.get("state") and E["state"] is not None:
            st_n += 1
            st_k += int(E["state"] == L["state"])
        prim = L.get("primary")
        if prim and L.get("direction") in (1, -1):
            if E["abstained"] or prim not in E["direction"]:
                dir_abst += 1
            else:
                dir_n += 1
                dir_k += int(E["direction"][prim] == L["direction"])
        if L.get("materiality") in ("high", "medium", "low") and not E["abstained"] and E["materiality"]:
            mat_n += 1
            mat_k += int(E["materiality"] == L["materiality"])
        gold_tf = timeframe.gold_for(L.get("family"), L.get("state"))
        if gold_tf != timeframe.UNKNOWN and not E["abstained"] and E["timeframe"]:
            tf_n += 1
            tf_k += int(E["timeframe"] == gold_tf)
            tf_conf[(gold_tf, E["timeframe"])] = tf_conf.get((gold_tf, E["timeframe"]), 0) + 1
        if L.get("abstain") is not None:
            basis = abstain_basis(c)
            if basis:
                b = ab_by.setdefault(basis, [0, 0])
                b[1] += 1
                b[0] += int(E["abstained"])
            if basis == "immaterial_event":
                pass                                                                                                  # a concrete but small event: analysing it (with low materiality) is correct, so it is neither a hit nor a miss
            elif E["abstained"] and L["abstain"]:
                ab_tp += 1
            elif E["abstained"] and not L["abstain"]:
                ab_fp += 1
            elif (not E["abstained"]) and L["abstain"]:
                ab_fn += 1
    m["entity_precision"] = _rate(tp, tp + fp)
    m["entity_recall"] = _rate(tp, tp + fn)
    m["false_company_match_rate"] = _rate(fm_bad, fm_n)
    m["role_accuracy"] = _rate(role_k, role_n)
    m["event_family_accuracy"] = _rate(fam_k, fam_n)
    m["event_state_accuracy"] = _rate(st_k, st_n)
    m["direction_accuracy_when_stated"] = _rate(dir_k, dir_n)
    m["direction_abstained_or_missing"] = {"k": dir_abst, "n": dir_n + dir_abst, "value": round(dir_abst / (dir_n + dir_abst), 3) if dir_n + dir_abst else None, "note": "cases with a labelled direction where the engine gave none (counted separately, not as wrong)"}
    m["materiality_accuracy"] = _rate(mat_k, mat_n)
    m["timeframe_accuracy"] = dict(_rate(tf_k, tf_n), confusion={"%s->%s" % k: v for k, v in sorted(tf_conf.items())}, vocabulary=timeframe.BOUNDS)
    m["abstention_precision"] = _rate(ab_tp, ab_tp + ab_fp)
    m["abstention_recall"] = dict(_rate(ab_tp, ab_tp + ab_fn), note="against non-events only (filing-index entries with no content, commentary, recaps, questions, lists, background); small concrete events are excluded and reported below")
    m["abstained_by_basis"] = {"value": None, "n": sum(v[1] for v in ab_by.values()), "detail": {k: {"abstained": v[0], "of": v[1]} for k, v in sorted(ab_by.items())}}
    if pair_reads is not None:
        ptp = sum(1 for p in pair_reads["pairs"] if p["pred"] and p["gold"])
        pfp = sum(1 for p in pair_reads["pairs"] if p["pred"] and not p["gold"])
        pfn = sum(1 for p in pair_reads["pairs"] if not p["pred"] and p["gold"])
        m["contradiction_precision"] = _rate(ptp, ptp + pfp)
        m["contradiction_recall"] = _rate(ptp, ptp + pfn)
    if prop_reads is not None:
        k = sum(1 for v in prop_reads.values() if v["pred"] and v["gold"])
        npred = sum(1 for v in prop_reads.values() if v["pred"])
        ngold = sum(1 for v in prop_reads.values() if v["gold"])
        m["propagation_precision"] = _rate(k, npred)
        m["propagation_recall"] = _rate(k, ngold)
    return m


def save_results(conn, run_id: str, metrics: dict, engine_version: str, now=None) -> None:
    now = now or utcnow()
    for k, v in metrics.items():
        conn.execute("INSERT INTO evaluation_results(run_id,dataset_version,engine_version,metric,value,n,detail_json,created) VALUES(?,?,?,?,?,?,?,?)", (run_id, DATASET_VERSION, engine_version, k, v.get("value"), v.get("n"), json.dumps(v), iso(now)))


def load_labels(conn, path: Optional[str] = None, data: Optional[dict] = None) -> dict:
    """The labelled dataset (a JSON file shipped in the package). Stored in evaluation_labels, separate from every production table."""
    if data is None:
        with open(path) as f:
            data = json.load(f)
    now = iso(utcnow())
    for c in data["cases"]:
        conn.execute("INSERT OR REPLACE INTO evaluation_labels(case_id,stratum,event_uid,label_json,labeler,labeled_at,dataset_version) VALUES(?,?,?,?,?,?,?)",
                     (c["case_id"], c["stratum"], str(c.get("event_id")), json.dumps(c, sort_keys=True), data.get("labeler", "unknown"), data.get("labeled_at", now), data.get("dataset_version", DATASET_VERSION)))
    return data


def collapse_to_issuers(reads: Dict[str, dict], labels: Dict[str, dict], universe) -> None:
    """Score at the level of the COMPANY, not the ticker: several tickers of one issuer (GOOG / GOOGL, a bank and the ETNs it issues, preferred series) are one company. A predicted ticker whose issuer is a labelled company counts as that
    company; the other tickers of an unlabelled issuer count once. Mutates ``reads`` (tickers, direction and role maps follow)."""
    issuer = getattr(universe, "issuer", None)
    if not issuer:
        return
    for cid, E in reads.items():
        gold = set((labels.get(cid) or {}).get("involved") or {})
        gold_by_issuer = {issuer.get(t, t): t for t in gold}
        rep: Dict[object, str] = {}
        mapping: Dict[str, str] = {}
        for t in sorted(E["tickers"]):
            iss = issuer.get(t, t)
            if iss in gold_by_issuer:
                mapping[t] = gold_by_issuer[iss]
            else:
                mapping[t] = rep.setdefault(iss, t)
        E["tickers"] = list(dict.fromkeys(mapping[t] for t in E["tickers"]))
        for key in ("direction", "roles"):
            if isinstance(E.get(key), dict):
                new: Dict[str, object] = {}
                for t, v in E[key].items():
                    new.setdefault(mapping.get(t, t), v)
                E[key] = new


def run(conn, cfg: dict, universe, data: dict, now=None, issuer_level: bool = True) -> dict:
    """Score the engine on a labelled dataset. Pair cases (contradictions) and propagation cases are scored with their own rules."""
    now = now or utcnow()
    cases = data["cases"]
    inu = getattr(universe, "watch", {}) or {}
    labels = {}
    out_of_universe = 0
    for c in cases:
        if "label" not in c:
            continue
        lb = dict(c["label"])
        fh = _FILING_HEADER_RX.match(c.get("headline") or "")
        inv = dict(lb.get("involved") or {})
        if fh and getattr(universe, "by_cik", None) and universe.by_cik.get(int(fh.group(2))):
            inv.setdefault(universe.by_cik[int(fh.group(2))], "subject")                                           # the filer named by the index entry's own CIK: an objective fact, not a judgement
        lb["involved"] = inv
        keep = {t: r for t, r in (lb.get("involved") or {}).items() if t in inu}
        out_of_universe += len(lb.get("involved") or {}) - len(keep)
        lb["involved"] = keep                                                                                   # a company that is not in the universe cannot be matched by any engine: it is not held against it
        labels[c["case_id"]] = lb
    reads = {c["case_id"]: engine_read(conn, cfg, universe, c, now) for c in cases}
    if issuer_level:
        collapse_to_issuers(reads, labels, universe)
    pairs = []
    byid = {c["case_id"]: c for c in cases}
    for p in data.get("pairs", []):
        a, b = byid.get(p["a"]), byid.get(p["b"])
        if not a or not b:
            continue
        ea = conn.execute("SELECT headline,event_type,direction,created FROM events WHERE id=?", (a["event_id"],)).fetchone()
        eb = conn.execute("SELECT headline,event_type,direction,magnitude,credibility,created FROM events WHERE id=?", (b["event_id"],)).fetchone()
        if not ea or not eb:
            continue
        from .timeutil import parse_ts
        res = skeptic.classify_contradiction({"headline": ea["headline"], "event_type": ea["event_type"]}, eb, 1.0 if (ea["direction"] or 0) >= 0 else -1.0, parse_ts(ea["created"]))
        pairs.append({"a": p["a"], "b": p["b"], "gold": bool(p["contradicts"]), "pred": bool(res and res["rule"] in CONTRA), "rule": res["rule"] if res else None})
    prop: Dict[str, dict] = {}
    from . import kbrel
    for c in cases:
        Lb = c.get("label") or {}
        if Lb.get("propagate") is None:
            continue
        E = reads[c["case_id"]]
        prim, pred = Lb.get("primary"), False
        if prim and not E["abstained"] and prim in E["direction"] and E["event_type"]:
            imp = kbrel.propagate(conn, cfg, {"event_id": None, "ticker": prim, "event_type": E["event_type"], "direction": 0.8 * E["direction"][prim], "magnitude": 0.8, "state": "confirmed", "industry_wide": False, "source_role": "subject", "headline": c["headline"]}, now, persist=False)
            pred = any(i["rule_id"] and i["direction"] != 0 and i["hops"] == 1 for i in imp)
        prop[c["case_id"]] = {"gold": bool(Lb["propagate"]), "pred": pred}
    metrics = score(cases, reads, labels, {"pairs": pairs} if pairs else None, prop if prop else None)
    metrics["n_cases"] = {"value": len(labels), "n": len(labels), "gold_companies_outside_universe": out_of_universe}
    metrics["_pairs"] = {"value": None, "n": len(pairs), "detail": pairs}
    metrics["by_stratum"] = {"value": None, "n": None, "counts": {s: sum(1 for c in cases if c["stratum"] == s) for s in sorted({c["stratum"] for c in cases})}}
    return {"metrics": metrics, "reads": reads}
