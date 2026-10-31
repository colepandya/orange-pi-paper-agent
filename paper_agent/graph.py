"""Relationship graph: who supplies whom, who competes, who moves together. An event at one company becomes a smaller, discounted idea at the companies it affects.
Rules and data only, no LLM (the LLM analyst used to be the only thing that knew about second-order effects, and it is off).

Where edges come from
  seed   config/relations.json: hand-written supplier / customer / competitor / peer links for the major themes (edit freely; re-read every cycle).
  data   how stocks have ACTUALLY moved together: the 60-day correlation of daily returns for every pair we hold price history for (refreshed daily; strongest neighbours only).
How an event spreads (see FACTORS): suppliers feel a customer's demand signals, competitors lose what a rival wins, peers share a sector read-through, and a supply-side shock hurts the supplier's customers.
Every derived idea is discounted, carries the reason ("supplier of NVDA: earnings beat"), is tagged with the relationship (source graph:supplier ...) so the learning loop can score each relationship kind
separately, and then goes through exactly the same gates, sizing and exits as any other idea."""
from __future__ import annotations

import json
import math
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
    rows: Dict[Tuple[str, str, str], Tuple[float, str]] = {}
    for g in d.get("peer_groups") or []:
        ts = [str(t).upper() for t in g.get("tickers") or []]
        w = float(g.get("weight", 0.35))
        for a in ts:
            for b in ts:
                if a != b:
                    rows[(a, b, "peer")] = (max(w, rows.get((a, b, "peer"), (0, ""))[0]), g.get("name", ""))
    for e in d.get("edges") or []:
        a, b, k, w = str(e["src"]).upper(), str(e["dst"]).upper(), e.get("kind", "peer"), float(e.get("weight", 0.3))
        if a == b or k not in ("supplies", "competes", "peer"):
            continue
        rows[(a, b, k)] = (w, e.get("note", ""))
        if k in ("competes", "peer"):
            rows[(b, a, k)] = (w, e.get("note", ""))
    with db.tx(conn):
        conn.execute("DELETE FROM relations WHERE source='seed'")
        conn.executemany("INSERT OR REPLACE INTO relations(src,dst,kind,weight,source,note,updated) VALUES(?,?,?,?,'seed',?,?)", [(a, b, k, w, note, iso(now)) for (a, b, k), (w, note) in rows.items()])
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


def refresh_data_edges(conn, cfg: dict, now=None, force: bool = False) -> dict:
    """Once a day: from every stock we hold enough price history for, link each to its closest co-movers (correlation >= graph.peer_min_corr, at most 4 each)."""
    now = now or utcnow()
    if not force and not db.due(conn, "graph_corr", 23.0, now):
        return {"skipped": "not due"}
    lo = float((cfg.get("graph") or {}).get("peer_min_corr", 0.75))
    tickers = [r[0] for r in conn.execute("SELECT symbol FROM market_series GROUP BY symbol HAVING COUNT(*) >= 40") if r[0] and r[0].isalpha() and len(r[0]) <= 5 and r[0] not in ("SPY", "QQQ", "IWM", "DIA", "VIX")]
    cache: dict = {}
    best: Dict[str, List[Tuple[float, str]]] = {t: [] for t in tickers}
    for i, a in enumerate(tickers):
        for b in tickers[i + 1:]:
            c = correlation(conn, a, b, 60, cache)
            if c is not None and c >= lo:
                best[a].append((c, b))
                best[b].append((c, a))
    rows = []
    for a, lst in best.items():
        for c, b in sorted(lst, reverse=True)[:4]:
            rows.append((a, b, "corr", round(c, 3), "data", "moved together over the last 60 sessions (correlation %.2f)" % c, iso(now)))
    with db.tx(conn):
        conn.execute("DELETE FROM relations WHERE source='data'")
        conn.executemany("INSERT OR REPLACE INTO relations(src,dst,kind,weight,source,note,updated) VALUES(?,?,?,?,?,?,?)", rows)
        db.mark_run(conn, "graph_corr", "", now)
    return {"stocks": len(tickers), "edges": len(rows)}


def related(conn, ticker: str) -> List[dict]:
    """Every neighbour of `ticker` with the target's ROLE relative to it: supplier / customer / competitor / peer / corr."""
    out: Dict[Tuple[str, str], dict] = {}
    for r in conn.execute("SELECT * FROM relations WHERE src=? OR dst=?", (ticker, ticker)):
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
            out[k] = {"ticker": other, "role": role, "weight": float(r["weight"]), "source": r["source"], "note": r["note"] or ""}
    # the MOST SPECIFIC relationship wins: a rival that is also in the same peer group is a competitor (not both: they pull opposite ways), and a measured co-mover that we already know as a supplier,
    # customer, competitor or peer is that, not "correlated"
    rank = {"supplier": 4, "customer": 4, "competitor": 3, "peer": 2, "corr": 1}
    best: Dict[str, dict] = {}
    for e in out.values():
        if e["ticker"] not in best or rank[e["role"]] > rank[best[e["ticker"]]["role"]] or (rank[e["role"]] == rank[best[e["ticker"]]["role"]] and e["weight"] > best[e["ticker"]]["weight"]):
            best[e["ticker"]] = e
    return list(best.values())


# ------------------------------------------------------------------------------------------------ propagation
def derive(conn, cfg: dict, cands: List[dict], universe=None, now=None, per_cycle: int = 12) -> List[dict]:
    """New candidates for the companies connected to those the day's events are about. Only strong, credible company news spreads; each source event creates at most graph.max_derived_per_event ideas."""
    g = cfg.get("graph") or {}
    if not g.get("enabled", True):
        return []
    min_mag, min_conf = float(g.get("min_event_magnitude", 0.45)), float(g.get("min_event_conf", 0.60))
    per_event, discount, min_w = int(g.get("max_derived_per_event", 4)), float(g.get("derived_discount", 0.75)), float(g.get("min_edge_weight", 0.20))
    have = {c["ticker"] for c in cands}
    out: List[dict] = []
    for c in sorted(cands, key=lambda x: -(abs(x.get("direction", 0)) * x.get("magnitude", 0))):
        if len(out) >= per_cycle:
            break
        if c.get("origin") not in (None, "event") or c.get("magnitude", 0) < min_mag or c.get("conf", 0) < min_conf or abs(c.get("direction", 0)) < 0.3:
            continue
        et = c.get("event_type", "")
        mine: List[Tuple[float, dict, float]] = []
        for e in related(conn, c["ticker"]):
            f = factor(et, e["role"])
            if f == 0.0 or e["ticker"] in have or e["ticker"] == c["ticker"] or e["weight"] < min_w:
                continue
            if universe is not None and e["ticker"] not in universe:
                continue
            strength = abs(f) * e["weight"] * discount
            mine.append((strength, e, f))
        for strength, e, f in sorted(mine, key=lambda x: -x[0])[:per_event]:
            sgn = 1.0 if (c["direction"] * f) > 0 else -1.0
            scale = min(1.0, 0.35 + 1.4 * strength)                                      # a strong, close relationship keeps most of the source's strength; a weak one a third of it
            out.append({"ticker": e["ticker"], "direction": sgn * min(1.0, abs(c["direction"]) * scale), "magnitude": max(0.1, c["magnitude"] * scale), "event_type": et,
                        "conf": c.get("conf", 0.7) * 0.85, "credibility": c.get("credibility", 0.5) * 0.9, "age_hours": c.get("age_hours", 0.0), "horizon": c.get("horizon", 8),
                        "event_id": c.get("event_id"), "news_uid": c.get("news_uid"), "amount_usd": None, "origin": "graph", "source": "graph:" + e["role"], "chain_order": 2,
                        "mechanism": "%s of %s (%s)" % (e["role"], c["ticker"], et.replace("_", " ")), "graph_from": c["ticker"], "graph_role": e["role"], "graph_weight": round(e["weight"], 2),
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
