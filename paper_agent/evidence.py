"""Event + Evidence engines. Every news item becomes an event; every event becomes signed, decaying evidence."""
from __future__ import annotations

import datetime as dt
import json
from typing import Callable, Dict, List, Optional

from . import db, nlp
from .timeutil import UTC, iso, parse_ts, utcnow

RULE_MIN_DIR = 0.12


def source_credibility(conn, source_id: str, event_type: str, trust: float) -> float:
    """Prior = feed trust (worth 6 observations) updated by learned hit/miss counts for (source, event_type)."""
    r = conn.execute("SELECT hits,misses FROM source_stats WHERE source=? AND event_type=?", (source_id, event_type)).fetchone()
    hits, misses = (float(r[0]), float(r[1])) if r else (0.0, 0.0)
    return max(0.05, min(0.99, (trust * 6 + hits) / (6 + hits + misses)))


def predictive_power(conn, event_type: str, default: float) -> float:
    v = db.param(conn, "power:" + event_type)
    return float(v) if isinstance(v, (int, float)) else default


def rule_matches(rule: dict, text_n: str, a: dict) -> bool:
    if rule["types"] and a["event_type"] not in rule["types"]:
        return False
    if rule["regions"] and not all(r in a["regions"] for r in rule["regions"]):
        return False
    return any(nlp.has_phrase(text_n, p) for p in rule["any"])


def thesis_matches(rules_by_key: Dict[str, List[dict]], text_n: str, a: dict) -> List[tuple]:
    """-> [(thesis_key, direction[-1..1], rule_weight, mode)] choosing the strongest matching rule per thesis."""
    out = []
    for key, rules in rules_by_key.items():
        best = None
        for rule in rules:
            if not rule_matches(rule, text_n, a):
                continue
            m = rule["mode"]
            if m == "support":
                d = 1.0
            elif m == "contradict":
                d = -1.0
            elif m == "inverse":
                d = -a["direction"]
            else:
                d = a["direction"]
            if m in ("topic", "inverse") and abs(d) < RULE_MIN_DIR:
                continue
            score = abs(d) * rule["w"]
            if best is None or score > best[0]:
                best = (score, d, rule["w"], m)
        if best:
            out.append((key, best[1], best[2], best[3]))
    return out


def load_rules(conn) -> Dict[str, List[dict]]:
    from .theses_seed import SEED_THESES
    seeds = {t["key"]: t["rules"] for t in SEED_THESES}
    keys = {r[0] for r in conn.execute("SELECT key FROM theses WHERE active=1")}
    return {k: v for k, v in seeds.items() if k in keys}


def find_cluster(conn, toks: List[str], tickers: List[str], event_type: str, since: dt.datetime) -> Optional[int]:
    rows = conn.execute("SELECT id,cluster_id,tokens,event_type,tickers_json FROM events WHERE created>=? ORDER BY id DESC LIMIT 400", (iso(since),)).fetchall()
    tset = set(tickers)
    for r in rows:
        j = nlp.jaccard(toks, (r["tokens"] or "").split())
        same = bool(tset) and tset == set(json.loads(r["tickers_json"] or "[]")) and r["event_type"] == event_type
        if j >= 0.55 or (same and j >= 0.35):
            return r["cluster_id"] or r["id"]
    return None


def process_new_news(conn, universe, cfg: dict, now: Optional[dt.datetime] = None, analyst: Optional[Callable] = None, limit: int = 600) -> dict:
    from . import event_intel
    now = now or utcnow()
    icfg = cfg["intelligence"]
    rules = load_rules(conn)
    rows = conn.execute("SELECT n.* FROM news n LEFT JOIN events e ON e.news_uid=n.uid WHERE e.id IS NULL ORDER BY n.id LIMIT ?", (limit,)).fetchall()
    stats = {"events": 0, "evidence": 0, "stale": 0, "duplicates": 0, "llm": 0}
    candidates: List[dict] = []
    # ---- pass 1: cheap deterministic read of everything
    pre = []
    for n in rows:
        title, summary = n["title"] or "", n["summary"] or ""
        a = nlp.analyze(title, summary)
        pub = parse_ts(n["published"])
        if pub is None or pub > now + dt.timedelta(minutes=10):
            pub = parse_ts(n["ingested"]) or now
        age_h = max(0.0, (now - pub).total_seconds() / 3600)
        matches = universe.match(title, summary) if universe is not None else []
        pre.append((n, a, pub, age_h, age_h > float(icfg["stale_event_hours"]), matches))
    # ---- which items deserve an LLM read this cycle (budgeted, ranked)
    chosen = event_intel.pick_for_llm(conn, pre, icfg, analyst)
    # ---- pass 2: persist events + evidence
    for (n, a, pub, age_h, stale, matches) in pre:
        title, summary = n["title"] or "", n["summary"] or ""
        text_n = nlp.norm(title + " . " + (summary or "")[:1500])
        tickers = [t for t, c, h in matches if c >= 0.5]
        conf = {t: c for t, c, h in matches}
        llm_json, intel = None, None
        if n["uid"] in chosen:
            try:
                ex = analyst({"title": title, "summary": summary[:1500], "analysis": a, "tickers": tickers, "tier": chosen[n["uid"]]})
            except Exception:  # noqa: BLE001
                ex = None
            if ex:
                stats["llm"] += 1
                llm_json = json.dumps({k: v for k, v in ex.items() if k != "intel"}, sort_keys=True, default=str)
                intel = ex.get("intel")
                a["direction"] = max(-1, min(1, 0.5 * a["direction"] + 0.5 * ex["direction"]))
                a["magnitude"] = max(a["magnitude"], min(1.0, ex["magnitude"]))
                if intel:
                    if intel["event_type"] != "other" and a["event_type"] == "general_news":
                        a["event_type"] = intel["event_type"]
                for t in ex.get("tickers", []):
                    if t not in tickers and (universe is None or t in universe):
                        tickers.append(t)
                        conf[t] = 0.6
        toks = nlp.tokens(title + " " + summary[:300])
        cluster = None if stale else find_cluster(conn, toks, tickers, a["event_type"], now - dt.timedelta(hours=72))
        novelty = 0.0 if stale else (0.25 if cluster else 1.0)
        source_id = n["source"] or ""
        cred = source_credibility(conn, source_id, a["event_type"], float(n["trust"] or 0.5))
        power = predictive_power(conn, a["event_type"], a["power"])
        intel_effects: List[dict] = []
        with db.tx(conn):
            cur = conn.execute(
                "INSERT INTO events(news_uid,created,published,source,source_id,headline,event_type,scope,direction,magnitude,amount_usd,tickers_json,regions_json,tokens,cluster_id,novelty,credibility,age_hours,stale,llm_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (n["uid"], iso(now), iso(pub), n["source"], source_id, title, a["event_type"], a["scope"], a["direction"], a["magnitude"], a["amount_usd"],
                 json.dumps(tickers), json.dumps(a["regions"]), " ".join(toks), cluster, novelty, cred, age_h, int(stale), llm_json))
            eid = cur.lastrowid
            if not cluster:
                conn.execute("UPDATE events SET cluster_id=? WHERE id=?", (eid, eid))
                cluster = eid
            elif not stale:
                conn.execute("UPDATE events SET credibility=MIN(0.98,credibility+0.04) WHERE id=?", (cluster,))  # corroboration
                stats["duplicates"] += 1
            stats["events"] += 1
            if stale:
                stats["stale"] += 1
                continue
            expiry = iso(pub + dt.timedelta(days=a["decay_days"]))
            if parse_ts(expiry) <= now:
                continue
            if intel:
                intel_effects = event_intel.apply(conn, cfg, eid, a, intel, pub, cluster, novelty, now, stats,
                                                  lambda src, et, prior: source_credibility(conn, src, et, prior), power)
            llm_owned = {e["ticker"] for e in intel_effects} if intel and intel["certainty"] >= 0.5 else set()
            ev_rows = []
            for key, d, w, mode in thesis_matches(rules, text_n, a):
                mag = max(a["magnitude"], 0.35) if mode in ("support", "contradict") else a["magnitude"]  # stance evidence floor
                ev_rows.append((eid, key, None, d, mag * w))
            for t in tickers:
                if conf.get(t, 0) < 0.5 or t in llm_owned:     # the LLM's reasoned effect supersedes the keyword read for the same ticker
                    continue
                d = a["direction"]
                if a["event_type"] == "merger_acquisition":
                    role = universe.mna_role(t, title) if universe is not None else "unknown"
                    d = 0.8 if role == "target" else -0.15 if role == "acquirer" else 0.25
                ev_rows.append((eid, None, t, d, a["magnitude"] * conf[t]))
            for (e_id, key, t, d, strength) in ev_rows:
                if abs(d) < 0.05:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO evidence(event_id,thesis_key,ticker,created,source,credibility,strength,novelty,predictive_power,direction,cluster_id,event_type,decay_days,expiry_date,note) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (e_id, key, t, iso(pub), source_id, cred, max(0.02, min(1.0, strength)), novelty, power, max(-1.0, min(1.0, d)), cluster, a["event_type"], a["decay_days"], expiry, a.get("matched") or ""))
                stats["evidence"] += 1
        # trading candidates: single-company, fresh, first-of-cluster, strong enough
        have = set()
        if (a["scope"] in ("company", "sector") and not cluster_is_dup(novelty) and age_h <= float(icfg["trade_event_max_age_hours"])
                and abs(a["direction"]) >= 0.25 and a["magnitude"] >= 0.2 and 0 < len(tickers) <= 3):
            for t in tickers:
                if conf.get(t, 0) >= 0.6 and not (intel and t in {e["ticker"] for e in intel_effects if e["order"] > int(icfg["max_trade_order"])}):
                    d = a["direction"]
                    if a["event_type"] == "merger_acquisition":
                        role = universe.mna_role(t, title) if universe is not None else "unknown"
                        d = 0.8 if role == "target" else -0.15 if role == "acquirer" else 0.0
                    if abs(d) >= 0.25:
                        have.add(t)
                        candidates.append({"event_id": eid, "ticker": t, "conf": conf[t], "direction": d, "magnitude": a["magnitude"], "event_type": a["event_type"],
                                           "age_hours": age_h, "headline": title, "source": n["source"], "news_uid": n["uid"], "amount_usd": a["amount_usd"],
                                           "credibility": cred, "horizon": a["horizon"], "decay_days": a["decay_days"]})
        if intel and not cluster_is_dup(novelty):
            candidates.extend(event_intel.candidates_from(intel_effects, intel, a, n, eid, age_h, cred, cfg, have))
    stats["candidates"] = len(candidates)
    return {"stats": stats, "candidates": candidates}


def cluster_is_dup(novelty: float) -> bool:
    return novelty < 0.9


def ticker_evidence(conn, ticker: str, now: Optional[dt.datetime] = None) -> dict:
    """Net decayed evidence on a ticker: {'net':[-1..1],'mass':float,'n':int}."""
    now = now or utcnow()
    rows = conn.execute("SELECT created,credibility,strength,novelty,predictive_power,direction,decay_days,cluster_id FROM evidence WHERE ticker=? AND expiry_date>?", (ticker, iso(now))).fetchall()
    clusters: Dict[int, float] = {}
    mass = 0.0
    for r in rows:
        age = (now - parse_ts(r["created"])).total_seconds() / 86400
        w = r["credibility"] * r["strength"] * r["novelty"] * r["predictive_power"] * nlp.decay_weight(age, r["decay_days"])
        clusters[r["cluster_id"]] = clusters.get(r["cluster_id"], 0.0) + w * r["direction"]
        mass += w
    import math
    net = math.tanh(sum(clusters.values()) / 0.4) if clusters else 0.0
    return {"net": net, "mass": mass, "n": len(rows)}
