"""Thesis engine: Bayesian log-odds updating over a decaying evidence ledger, dependency graph, contradiction scoring, states.

Why recompute from the ledger instead of updating in place?  Evidence expires and decays, sources get corrected, and
duplicate coverage must not stack.  A stateless recompute is deterministic, auditable (``thesis_history``) and cannot drift.
Naive-Bayes independence is protected by (a) clustering duplicate coverage, (b) tanh saturation per cluster and in total.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from typing import Dict, List, Optional

from . import db, nlp
from .theses_seed import REGIME_STYLE_TILT, SEED_THESES
from .timeutil import iso, parse_ts, utcnow

LMAX = 0.85      # max |log-LR| a single independent story can contribute
S0 = 0.25        # evidence mass at which a story reaches ~76% of LMAX
TOTAL_CAP = 4.0  # |total log-odds shift| cap
N0 = 4.0         # prior pseudo-observations


def logit(p: float) -> float:
    p = min(0.999, max(0.001, p))
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def seed(conn) -> int:
    n = 0
    with db.tx(conn):
        for t in SEED_THESES:
            cur = conn.execute("INSERT OR IGNORE INTO theses(key,name,parent_key,description,style,prior,probability,uncertainty,last_updated) VALUES(?,?,?,?,?,?,?,?,?)",
                               (t["key"], t["name"], t["parent"], t["description"], t["style"], t["prior"], t["prior"], 0.22, iso()))
            n += cur.rowcount
            if t["parent"]:
                conn.execute("INSERT OR IGNORE INTO thesis_links(parent_key,child_key,weight) VALUES(?,?,?)", (t["parent"], t["key"], t.get("edge", 0.6)))
            for tk in t["tickers"]:
                conn.execute("INSERT OR IGNORE INTO thesis_companies(thesis_key,ticker,role,weight,origin) VALUES(?,?,?,?,?)", (t["key"], tk, "beneficiary", 1.0, "seed"))
            for tk in t.get("victims", []):
                conn.execute("INSERT OR IGNORE INTO thesis_companies(thesis_key,ticker,role,weight,origin) VALUES(?,?,?,?,?)", (t["key"], tk, "victim", 1.0, "seed"))
    return n


def effective_prior(conn, key: str, default: float) -> float:
    v = db.param(conn, "prior:" + key)
    return float(v) if isinstance(v, (int, float)) else default


def _state(p: float, unc: float, trend: float, mass: float, net: float, crowding: float, prior: float = 0.5) -> str:
    """Precedence: Broken > Overcrowded > Weakening > Emerging > Strong > Active/Forming.
    Risk theses with a low prior (e.g. Taiwan escalation) are judged against their prior, not an absolute 25% bar."""
    if (net < -0.5 and mass >= 0.5) or (prior >= 0.4 and p < min(0.25, prior - 0.25)):
        return "Broken"
    if crowding >= 0.8 and p >= 0.6:
        return "Overcrowded"
    if (trend <= -0.05 and p < 0.65 and prior >= 0.4) or (net < -0.15 and mass >= 0.4 and p < 0.55):
        return "Weakening"
    if mass < 0.25:
        return "Emerging"
    if p >= 0.75 and unc <= 0.15:
        return "Strong"
    if p >= 0.55:
        return "Active" if mass >= 0.8 else "Forming"
    return "Forming" if p >= 0.35 else "Emerging"


def recompute(conn, now: Optional[dt.datetime] = None, record_history: bool = True) -> List[dict]:
    now = now or utcnow()
    theses = [dict(r) for r in conn.execute("SELECT * FROM theses WHERE active=1")]
    base: Dict[str, dict] = {}
    for t in theses:
        prior = effective_prior(conn, t["key"], t["prior"])
        rows = conn.execute("SELECT e.event_id,e.created,e.source,e.credibility,e.strength,e.novelty,e.predictive_power,e.direction,e.decay_days,e.cluster_id "
                            "FROM evidence e WHERE e.thesis_key=? AND e.expiry_date>?", (t["key"], iso(now))).fetchall()
        clusters: Dict[int, float] = {}
        mass, sources, last = 0.0, set(), None
        for r in rows:
            c = parse_ts(r["created"])
            age = max(0.0, (now - c).total_seconds() / 86400)
            w = r["credibility"] * r["strength"] * r["novelty"] * r["predictive_power"] * nlp.decay_weight(age, r["decay_days"])
            if w <= 0:
                continue
            key = r["cluster_id"] or r["event_id"]
            clusters[key] = clusters.get(key, 0.0) + w * r["direction"]
            mass += w
            sources.add(r["source"])
            last = max(last, c) if last else c
        shift = sum(LMAX * math.tanh(s / S0) for s in clusters.values())
        shift = TOTAL_CAP * math.tanh(shift / TOTAL_CAP)
        support = sum(s for s in clusters.values() if s > 0)
        contra = -sum(s for s in clusters.values() if s < 0)
        base[t["key"]] = {"t": t, "prior": prior, "log": logit(prior) + shift, "shift": shift, "mass": mass, "support": support, "contra": contra,
                          "sources": len(sources), "clusters": len(clusters), "last": last}
    # dependency propagation: children lift/lower parents; a broken parent drags children down (never lifts them)
    links = conn.execute("SELECT parent_key,child_key,weight FROM thesis_links").fetchall()
    adj = {k: v["log"] for k, v in base.items()}
    for l in links:
        c, p = base.get(l["child_key"]), base.get(l["parent_key"])
        if c and p:
            delta = logit(sigmoid(c["log"])) - logit(c["prior"])
            adj[l["parent_key"]] += max(-1.5, min(1.5, 0.5 * l["weight"] * delta)) / max(1, sum(1 for x in links if x["parent_key"] == l["parent_key"]) ** 0.5)
    for l in links:
        p = base.get(l["parent_key"])
        if p:
            pd = adj[l["parent_key"]] - logit(p["prior"])
            if pd < 0:
                adj[l["child_key"]] = adj.get(l["child_key"], 0) + 0.3 * l["weight"] * pd
    out = []
    for key, b in base.items():
        t = b["t"]
        p = sigmoid(adj[key])
        n_eff = b["mass"] * 8.0 + N0
        unc = math.sqrt(p * (1 - p) / (n_eff + 1.0))
        div = b["sources"] / (b["sources"] + 2.0)
        cq = max(0.0, min(1.0, (1 - 2 * unc) * (0.55 + 0.45 * div) * min(1.0, 0.3 + b["mass"])))
        net = (b["support"] - b["contra"]) / (b["support"] + b["contra"] + 0.5)
        old = parse_ts(t["last_updated"])
        hist = conn.execute("SELECT probability FROM thesis_history WHERE thesis_key=? AND ts<=? ORDER BY ts DESC LIMIT 1", (key, iso(now - dt.timedelta(days=7)))).fetchone()
        if not hist:
            hist = conn.execute("SELECT probability FROM thesis_history WHERE thesis_key=? AND ts<=? ORDER BY ts LIMIT 1", (key, iso(now - dt.timedelta(days=1)))).fetchone()
        trend = p - float(hist[0]) if hist else 0.0
        nar = conn.execute("SELECT crowding FROM narrative_states WHERE thesis_key=? ORDER BY ts DESC LIMIT 1", (key,)).fetchone()
        crowd = float(nar[0]) if nar and nar[0] is not None else 0.0
        state = _state(p, unc, trend, b["mass"], net, crowd, b["prior"])
        row = {"key": key, "name": t["name"], "probability": p, "prior": b["prior"], "uncertainty": unc, "confidence_quality": cq, "trend": trend, "state": state,
               "support_score": b["support"], "contradiction_score": b["contra"], "net_strength": net, "evidence_mass": b["mass"], "clusters": b["clusters"], "prev_state": t["state"],
               "style": t["style"]}
        with db.tx(conn):
            conn.execute("UPDATE theses SET probability=?,uncertainty=?,confidence_quality=?,trend=?,state=?,support_score=?,contradiction_score=?,net_strength=?,evidence_mass=?,last_evidence=?,last_updated=? WHERE key=?",
                         (p, unc, cq, trend, state, b["support"], b["contra"], net, b["mass"], iso(b["last"]) if b["last"] else None, iso(now), key))
            if record_history:
                last_h = conn.execute("SELECT ts,probability FROM thesis_history WHERE thesis_key=? ORDER BY ts DESC LIMIT 1", (key,)).fetchone()
                lt = parse_ts(last_h["ts"]) if last_h else None
                if not last_h or (now - lt).total_seconds() > 3300 or abs(p - last_h["probability"]) > 0.01 or state != t["state"]:
                    conn.execute("INSERT INTO thesis_history(thesis_key,ts,prior,probability,uncertainty,state,net_strength,new_evidence,detail_json) VALUES(?,?,?,?,?,?,?,?,?)",
                                 (key, iso(now), b["prior"], p, unc, state, net, b["clusters"], json.dumps({"shift": b["shift"], "mass": b["mass"], "sources": b["sources"]})))
            if state != t["state"]:
                db.audit(conn, "thesis_engine", "state_change", key, {"from": t["state"], "to": state, "p": round(p, 3)})
        out.append(row)
    return out


def style_tilt(regime: str, style: str) -> float:
    return REGIME_STYLE_TILT.get(regime, {}).get(style, 1.0)


def views_for_ticker(conn, ticker: str, regime: str = "unknown") -> List[dict]:
    """Theses touching a ticker, signed by role (victims of a strong thesis are bearish) and tilted by regime."""
    rows = conn.execute("SELECT t.key,t.name,t.style,t.probability,t.uncertainty,t.state,t.net_strength,t.confidence_quality,t.prior,c.role,c.weight FROM thesis_companies c "
                        "JOIN theses t ON t.key=c.thesis_key WHERE c.ticker=? AND t.active=1", (ticker,)).fetchall()
    out = []
    for r in rows:
        sign = 1.0 if r["role"] == "beneficiary" else -1.0
        out.append(
                   {**dict(r), "sign": sign, "tilt": style_tilt(regime, r["style"]), "tilted_edge": sign * (r["probability"] - r["prior"]) * style_tilt(regime, r["style"])})
    return out


def ticker_thesis_score(conn, ticker: str, regime: str = "unknown") -> dict:
    """Aggregate [-1,1] thesis tailwind for a ticker + the primary thesis key (largest |edge|*quality)."""
    views = views_for_ticker(conn, ticker, regime)
    if not views:
        return {"score": 0.0, "primary": None, "p": None, "state": None, "views": []}
    best = max(views, key=lambda v: abs(v["tilted_edge"]) * (0.3 + v["confidence_quality"]))
    score = math.tanh(sum(v["tilted_edge"] * (0.3 + v["confidence_quality"]) for v in views) / 0.25)
    return {"score": score, "primary": best["key"], "p": best["probability"], "state": best["state"], "views": views}


def thesis_candidates(conn, regime: str, min_p: float, held: set, limit: int = 6) -> List[dict]:
    """Thesis-driven ideas: beneficiaries of Active/Strong theses (never Overcrowded/Weakening/Broken) not already held."""
    out = []
    for t in conn.execute("SELECT * FROM theses WHERE active=1 AND state IN ('Active','Strong') AND probability>=? AND net_strength>0 ORDER BY probability DESC", (min_p,)):
        tilt = style_tilt(regime, t["style"])
        if tilt < 0.85:
            continue
        for c in conn.execute("SELECT ticker FROM thesis_companies WHERE thesis_key=? AND role='beneficiary' ORDER BY weight DESC", (t["key"],)):
            if c["ticker"] not in held:
                out.append({"ticker": c["ticker"], "thesis_key": t["key"], "p": t["probability"], "net": t["net_strength"], "tilt": tilt})
    seen, res = set(), []
    for o in sorted(out, key=lambda x: -(x["p"] * x["tilt"])):
        if o["ticker"] not in seen:
            seen.add(o["ticker"])
            res.append(o)
    return res[:limit]
