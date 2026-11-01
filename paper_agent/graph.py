"""Relationship graph: who supplies whom, who competes, who moves together. An event at one company becomes a smaller, discounted idea at the companies it affects.
Rules and data only, no LLM (the LLM analyst used to be the only thing that knew about second-order effects, and it is off).

Where edges come from
  seed   config/relations.json: hand-written supplier / customer / competitor / peer links for the major themes (edit freely; re-read every cycle).
  data   how stocks have ACTUALLY moved together: the 60-day correlation of daily returns for every pair we hold price history for (refreshed daily; strongest neighbours only).
How an event spreads (see FACTORS): suppliers feel a customer's demand signals, competitors lose what a rival wins, peers share a sector read-through, and a supply-side shock hurts the supplier's customers.
Every derived idea is discounted, carries the reason ("supplier of NVDA: earnings beat"), is tagged with the relationship (source graph:supplier ...) so the learning loop can score each relationship kind
separately, and then goes through exactly the same gates, sizing and exits as any other idea."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import os
from typing import Dict, List, Optional, Tuple

from . import db, marketdata as md
from .timeutil import iso, utcnow

ROLES = ("supplier", "customer", "competitor", "peer", "corr")

# event types that say something about the COMPANY ONLY: nothing spreads
IDIOSYNCRATIC = {"insider_filing", "analyst_action", "leadership_change", "buyback_dividend", "capital_raise", "thesis_signal", "structural_theme", "technology_shift", "price_recap", "opinion_piece",
                 "sec_filing"}

# derived direction = source direction x factor. Role is the TARGET's relation to the company the news is about.
DEFAULT = {"supplier": 0.60, "customer": 0.0, "competitor": -0.35, "peer": 0.35, "corr": 0.30}
OVERRIDES = {
    "production_disruption": {"supplier": 0.50, "customer": 0.50, "competitor": -0.30, "peer": 0.15, "corr": 0.2},        # a plant outage hurts its suppliers' volumes AND its customers' supply
    "regulatory_action": {"supplier": 0.0, "competitor": -0.10, "peer": 0.60, "corr": 0.35},                             # a rule aimed at one usually applies to the sector
    "regulatory_approval": {"supplier": 0.0, "competitor": -0.15, "peer": 0.40, "corr": 0.25},
    "guidance_cut": {"supplier": 0.60, "customer": 0.30, "competitor": -0.35, "peer": 0.35, "corr": 0.30},          # a supplier's weak outlook is a warning about the demand (or supply) its customers face
    "guidance_raise": {"supplier": 0.60, "customer": 0.30, "competitor": -0.35, "peer": 0.35, "corr": 0.30},
    "contract_win": {"supplier": 0.40, "competitor": -0.40, "peer": 0.15, "corr": 0.2},
    "contract_loss": {"supplier": 0.40, "competitor": -0.40, "peer": 0.15, "corr": 0.2},
    "merger_acquisition": {"supplier": 0.0, "competitor": 0.0, "peer": 0.40, "corr": 0.25},                              # a takeover re-rates the whole group
    "litigation": {"supplier": 0.0, "competitor": -0.20, "peer": 0.20, "corr": 0.1},
    "cyber_incident": {"supplier": 0.0, "competitor": -0.25, "peer": 0.25, "corr": 0.15},
    "product_launch": {"supplier": 0.40, "competitor": -0.40, "peer": 0.10, "corr": 0.15},
    "partnership": {"supplier": 0.30, "competitor": -0.25, "peer": 0.10, "corr": 0.15},
    "bankruptcy_distress": {"supplier": 0.70, "customer": 0.40, "competitor": -0.30, "peer": 0.40, "corr": 0.3},
}


def factor(event_type: str, role: str) -> float:
    if event_type in IDIOSYNCRATIC:
        return 0.0
    return float(OVERRIDES.get(event_type, DEFAULT).get(role, DEFAULT.get(role, 0.0)))


# ------------------------------------------------------------------------------------------------ the edges
def _seed_paths() -> List[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        from . import config as _cfg
        base = _cfg.CONFIG_DIR
    except Exception:  # noqa: BLE001
        base = "/etc/paper-agent"
    return [os.path.join(base, "relations.json"), os.path.join(base, "relations.default.json"), os.path.join(os.path.dirname(here), "config", "relations.json")]


def load_seed(path: Optional[str] = None) -> dict:
    for p in ([path] if path else _seed_paths()):
        try:
            with open(p) as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                return d
        except (OSError, ValueError):
            continue
    return {"peer_groups": [], "edges": []}


def seed(conn, now=None, path: Optional[str] = None) -> dict:
    """Make the relations table match the seed file (symmetric kinds are stored in both directions). Idempotent; seed rows that are no longer in the file are removed, data-driven rows are untouched."""
    now = now or utcnow()
    d = load_seed(path)
    import hashlib
    sig = hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()
    if db.param(conn, "relations_hash") == sig and conn.execute("SELECT 1 FROM relations WHERE source='seed' LIMIT 1").fetchone():
        return {"edges": conn.execute("SELECT COUNT(*) FROM relations WHERE source='seed'").fetchone()[0], "unchanged": True}            # nothing to write: the file has not changed
    rows: Dict[Tuple[str, str, str], tuple] = {}
    for g in d.get("peer_groups") or []:
        ts = [str(t).upper() for t in g.get("tickers") or []]
        w = float(g.get("weight", 0.35))
        for a in ts:
            for b in ts:
                if a != b:
                    rows[(a, b, "peer")] = (max(w, rows.get((a, b, "peer"), (0, ""))[0]), g.get("name", ""), g.get("evidence", ""), g.get("verified", ""), g.get("valid_from"), g.get("valid_to"))
    for e in d.get("edges") or []:
        a, b, k, w = str(e["src"]).upper(), str(e["dst"]).upper(), e.get("kind", "peer"), float(e.get("weight", 0.3))
        if a == b or k not in ("supplies", "competes", "peer"):
            continue
        rows[(a, b, k)] = (w, e.get("note", ""), e.get("evidence", ""), e.get("verified", ""), e.get("valid_from"), e.get("valid_to"))
        if k in ("competes", "peer"):
            rows[(b, a, k)] = (w, e.get("note", ""), e.get("evidence", ""), e.get("verified", ""), e.get("valid_from"), e.get("valid_to"))
    with db.tx(conn):
        conn.execute("DELETE FROM relations WHERE source='seed'")
        conn.executemany("INSERT OR REPLACE INTO relations(src,dst,kind,weight,source,note,updated,confidence,evidence,verified,valid_from,valid_to,status) VALUES(?,?,?,?,'seed',?,?,?,?,?,?,?,'active')",
                         [(a, b, k, v[0], v[1], iso(now), v[0], v[2], v[3], v[4], v[5]) for (a, b, k), v in rows.items()])
        db.set_param(conn, "relations_hash", sig, "hash of the relationship map last loaded")
    return {"edges": len(rows)}


def ensure_edges(conn, cfg: dict, now=None) -> dict:
    """Keep the graph current: reload the seed map only if the file changed, and refresh the measured co-movement edges once a day."""
    return {"seed": seed(conn, now), "data": refresh_data_edges(conn, cfg, now)}


def returns(conn, ticker: str, n: int = 60) -> List[float]:
    cl = md.closes(conn, ticker, n + 1)
    return [cl[i] / cl[i - 1] - 1.0 for i in range(1, len(cl)) if cl[i - 1]] if cl and len(cl) > 5 else []


def correlation(conn, a: str, b: str, n: int = 60, cache: Optional[dict] = None) -> Optional[float]:
    """Correlation of the two stocks' daily returns over the last n sessions (None if either has too little history)."""
    cache = cache if cache is not None else {}
    for t in (a, b):
        if t not in cache:
            cache[t] = returns(conn, t, n)
    x, y = cache[a], cache[b]
    m = min(len(x), len(y))
    if m < 20:
        return None
    x, y = x[-m:], y[-m:]
    mx, my = sum(x) / m, sum(y) / m
    sxx, syy = sum((v - mx) ** 2 for v in x), sum((v - my) ** 2 for v in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((p - mx) * (q - my) for p, q in zip(x, y)) / math.sqrt(sxx * syy)


def _winsor(x: List[float]) -> List[float]:
    if len(x) < 8:
        return x
    s = sorted(x)
    med = s[len(s) // 2]
    mad = (sorted(abs(v - med) for v in x)[len(x) // 2] * 1.4826) or 1e-9                               # robust spread: one huge day cannot inflate the yardstick it is measured against
    return [max(med - 5 * mad, min(med + 5 * mad, v)) for v in x]                                      # one wild shared day must not manufacture a relationship


def _corr(x: List[float], y: List[float], n: int) -> Optional[float]:
    m = min(len(x), len(y), n)
    if m < 40:
        return None                                                                                    # too few overlapping sessions to say anything
    x, y = _winsor(x[-m:]), _winsor(y[-m:])
    mx, my = sum(x) / m, sum(y) / m
    sxx, syy = sum((v - mx) ** 2 for v in x), sum((v - my) ** 2 for v in y)
    return sum((p - mx) * (q - my) for p, q in zip(x, y)) / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else None


def _residual(x: List[float], mkt: List[float]) -> List[float]:
    """What is left of a stock's returns after removing the market's influence (a one-factor regression): shared market beta is not a relationship."""
    m = min(len(x), len(mkt))
    x, mk = x[-m:], mkt[-m:]
    mm, mx = sum(mk) / m, sum(x) / m
    var = sum((v - mm) ** 2 for v in mk) or 1e-12
    beta = sum((a - mm) * (b - mx) for a, b in zip(mk, x)) / var
    return [b - beta * a for a, b in zip(mk, x)]


def refresh_data_edges(conn, cfg: dict, now=None, force: bool = False) -> dict:
    """Once a day: STATISTICAL association between stocks. Kept only when it holds over BOTH the last 60 and 120 sessions, and still holds after removing the market's influence. These links are labelled
    `corr`: they explain and warn (the correlation gate, the explanation), they are NEVER treated as a cause, and no trade is built on them."""
    now = now or utcnow()
    if not force and not db.due(conn, "graph_corr", 23.0, now):
        return {"skipped": "not due"}
    lo = float((cfg.get("graph") or {}).get("peer_min_corr", 0.75))
    tickers = [r[0] for r in conn.execute("SELECT symbol FROM market_series GROUP BY symbol HAVING COUNT(*) >= 60") if r[0] and r[0].isalpha() and len(r[0]) <= 5 and r[0] not in ("SPY", "QQQ", "IWM", "DIA", "VIX")]
    rets = {t: returns(conn, t, 260) for t in tickers}
    mkt = returns(conn, "SPY", 260)
    resid = {t: _residual(v, mkt) for t, v in rets.items() if mkt and len(v) >= 40}
    best: Dict[str, List[Tuple[float, str, str]]] = {t: [] for t in tickers}
    for i, a in enumerate(tickers):
        for b in tickers[i + 1:]:
            c60, c120 = _corr(rets[a], rets[b], 60), _corr(rets[a], rets[b], 120)
            if c60 is None or c60 < lo or (c120 is not None and c120 < lo - 0.10):
                continue
            adj = _corr(resid[a], resid[b], 60) if a in resid and b in resid else None
            if adj is not None and adj < 0.45:
                continue                                                                               # they only move together because the whole market does
            note = "statistical association (not a cause): correlation %.2f over 60 sessions%s%s" % (c60, (", %.2f over 120" % c120) if c120 is not None else "", (", %.2f after removing the market" % adj) if adj is not None else "")
            w = min(c60, c120 if c120 is not None else c60)
            best[a].append((w, b, note))
            best[b].append((w, a, note))
    rows = []
    for a, lst in best.items():
        for w, b, note in sorted(lst, reverse=True)[:4]:
            rows.append((a, b, "corr", round(w, 3), "data", note, iso(now), round(w, 3), "computed from your own price history", now.date().isoformat(), None, None))
    with db.tx(conn):
        conn.execute("DELETE FROM relations WHERE source='data'")
        conn.executemany("INSERT OR REPLACE INTO relations(src,dst,kind,weight,source,note,updated,confidence,evidence,verified,valid_from,valid_to,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'active')", rows)
        db.mark_run(conn, "graph_corr", "", now)
    return {"stocks": len(tickers), "edges": len(rows), "market_adjusted": bool(mkt)}


def related(conn, ticker: str) -> List[dict]:
    """Every neighbour of `ticker` with the target's ROLE relative to it: supplier / customer / competitor / peer / corr."""
    out: Dict[Tuple[str, str], dict] = {}
    today = utcnow().date().isoformat()
    for r in conn.execute("SELECT * FROM relations WHERE (src=? OR dst=?) AND COALESCE(status,'active')='active' AND (valid_to IS NULL OR valid_to>=?) AND (valid_from IS NULL OR valid_from<=?)", (ticker, ticker, today, today)):
        if r["kind"] == "supplies":
            other, role = (r["dst"], "customer") if r["src"] == ticker else (r["src"], "supplier")       # src supplies dst: dst is src's CUSTOMER, src is dst's SUPPLIER
        elif r["kind"] == "competes":
            other, role = (r["dst"] if r["src"] == ticker else r["src"]), "competitor"
        elif r["kind"] == "corr":
            if r["src"] != ticker:
                continue
            other, role = r["dst"], "corr"
        else:
            other, role = (r["dst"] if r["src"] == ticker else r["src"]), "peer"
        if other == ticker:
            continue
        k = (other, role)
        if k not in out or r["weight"] > out[k]["weight"]:
            out[k] = {"ticker": other, "role": role, "weight": float(r["weight"]), "source": r["source"], "note": r["note"] or "", "evidence": r["evidence"] or "", "verified": r["verified"] or ""}
    # the MOST SPECIFIC relationship wins: a rival that is also in the same peer group is a competitor (not both: they pull opposite ways), and a measured co-mover that we already know as a supplier,
    # customer, competitor or peer is that, not "correlated"
    rank = {"supplier": 4, "customer": 4, "competitor": 3, "peer": 2, "corr": 1}
    best: Dict[str, dict] = {}
    for e in out.values():
        if e["ticker"] not in best or rank[e["role"]] > rank[best[e["ticker"]]["role"]] or (rank[e["role"]] == rank[best[e["ticker"]]["role"]] and e["weight"] > best[e["ticker"]]["weight"]):
            best[e["ticker"]] = e
    return list(best.values())


# ------------------------------------------------------------------------------------------------ propagation
INDUSTRY_CUES = re.compile(r"\b(sector|industry|industry-wide|across the (?:sector|industry|board)|demand|tariffs?|regulat\w+|rules?|bans?|export controls?|shortages?|supply constraints?|downturn|slowdown)\b", re.I)
SIGN_CONFIDENCE = {"supplier": 0.70, "customer": 0.45, "competitor": 0.50, "peer": 0.60, "corr": 0.30}


def industry_wide(conn, ticker: str, etype: str, direction: float, headline: str, now) -> bool:
    """Is this event about the whole industry rather than one company? Then a competitor moves WITH it, not against it. True if the headline says so, or another company in the same peer group had the
    same kind of event in the same direction in the last 48 hours."""
    if INDUSTRY_CUES.search(headline or ""):
        return True
    peers = [e["ticker"] for e in related(conn, ticker) if e["role"] in ("peer", "competitor")][:40]
    if not peers:
        return False
    q = ",".join("?" * len(peers))
    rows = conn.execute("SELECT tickers_json,direction FROM events WHERE event_type=? AND created>=? AND created<=? AND stale=0 AND magnitude>=0.4", (etype, iso(now - dt.timedelta(hours=48)), iso(now))).fetchall()
    return any(r["direction"] * direction > 0.3 and any('"%s"' % p in (r["tickers_json"] or "") for p in peers) for r in rows)


def derive(conn, cfg: dict, cands: List[dict], universe=None, now=None, per_cycle: int = 12) -> List[dict]:
    """New candidates for the FIRST-HOP neighbours of the companies the day's events are about: suppliers, customers, competitors, peers. Only strong, credible company news spreads; each source event creates at
    most graph.max_derived_per_event ideas. Measured co-movement never trades on its own (graph.trade_on_correlation, default off). Every idea records its mechanism and how sure we are of its sign."""
    g = cfg.get("graph") or {}
    if not g.get("enabled", True):
        return []
    now = now or utcnow()
    min_mag, min_conf = float(g.get("min_event_magnitude", 0.45)), float(g.get("min_event_conf", 0.60))
    per_event, discount, min_w = int(g.get("max_derived_per_event", 4)), float(g.get("derived_discount", 0.75)), float(g.get("min_edge_weight", 0.20))
    allow_corr = bool(g.get("trade_on_correlation", False))
    out: List[dict] = []

    def num(x):
        try:
            v = float(x)
            return v if v == v else 0.0
        except (TypeError, ValueError):
            return 0.0
    clean = [dict(c, direction=num(c.get("direction")), magnitude=num(c.get("magnitude")), conf=num(c.get("conf"))) for c in (cands or []) if isinstance(c, dict) and c.get("ticker")]
    have = {c["ticker"] for c in clean}
    for c in sorted(clean, key=lambda x: -(abs(x["direction"]) * x["magnitude"])):
        if len(out) >= per_cycle:
            break
        if c.get("origin") not in (None, "event") or c.get("magnitude", 0) < min_mag or c.get("conf", 0) < min_conf or abs(c.get("direction", 0)) < 0.3:
            continue
        et = c.get("event_type", "")
        wide = None
        mine: List[Tuple[float, dict, float, str]] = []
        for e in related(conn, c["ticker"]):
            if e["role"] == "corr" and not allow_corr:
                continue                                                                               # an association is not a cause: it may explain, it may not trade
            f = factor(et, e["role"])
            how = ""
            if e["role"] == "competitor" and f != 0.0:
                if wide is None:
                    wide = industry_wide(conn, c["ticker"], et, c["direction"], c.get("headline") or "", now)
                if wide:
                    f, how = abs(f) * 0.7, " (industry-wide news: a rival moves WITH it)"
            if f == 0.0 or e["ticker"] in have or e["ticker"] == c["ticker"] or e["weight"] < min_w:
                continue
            if universe is not None and e["ticker"] not in universe:
                continue
            mine.append((abs(f) * e["weight"] * discount, e, f, how))
        for strength, e, f, how in sorted(mine, key=lambda x: -x[0])[:per_event]:
            sgn = 1.0 if (c["direction"] * f) > 0 else -1.0
            scale = min(1.0, 0.35 + 1.4 * strength)                                                    # a strong, close relationship keeps most of the source's strength; a weak one a third of it
            mech = "%s of %s (%s)%s" % (e["role"], c["ticker"], et.replace("_", " "), how)
            out.append({"ticker": e["ticker"], "direction": sgn * min(1.0, abs(c["direction"]) * scale), "magnitude": max(0.1, c["magnitude"] * scale), "event_type": et,
                        "conf": c.get("conf", 0.7) * 0.85, "credibility": c.get("credibility", 0.5) * 0.9, "age_hours": c.get("age_hours", 0.0), "horizon": c.get("horizon", 8),
                        "event_id": c.get("event_id"), "news_uid": c.get("news_uid"), "amount_usd": None, "origin": "graph", "source": "graph:" + e["role"], "chain_order": 2,
                        "mechanism": mech, "graph_from": c["ticker"], "graph_role": e["role"], "graph_weight": round(e["weight"], 2),
                        "graph_detail": {"relationship": e["role"], "edge_weight": round(e["weight"], 2), "factor": round(f, 2), "sign_confidence": SIGN_CONFIDENCE.get(e["role"], 0.4) * (0.8 if how else 1.0),
                                         "magnitude_transform": round(scale, 2), "edge_source": e["source"], "edge_evidence": e.get("evidence", ""), "edge_verified": e.get("verified", ""), "industry_wide": bool(how)},
                        "headline": "Related to %s: %s" % (c["ticker"], (c.get("headline") or et)[:120]), "thesis_key": c.get("thesis_key")})
            have.add(e["ticker"])
    return out


# ------------------------------------------------------------------------------------------------ the portfolio side
def max_same_side_corr(conn, ticker: str, hs: List[dict], side: str, cache: Optional[dict] = None) -> Tuple[float, Optional[str]]:
    """The strongest correlation between `ticker` and any holding on the SAME side (two stocks that move together are one bet). (0, None) if nothing comparable."""
    cache = cache if cache is not None else {}
    best, who = 0.0, None
    for h in hs:
        if h["ticker"] == ticker or h.get("side", "LONG") != side:
            continue
        c = correlation(conn, ticker, h["ticker"], 60, cache)
        if c is not None and c > best:
            best, who = c, h["ticker"]
    return best, who
