"""Self-learning engine. Records every probabilistic claim the system makes, scores it against reality, and recalibrates.

Learned (all stored in ``model_params`` / ``source_stats`` and read by the engines at run time):
  * source credibility    - Beta posterior of (source, event_type) hits vs misses            -> evidence.source_credibility
  * event predictive power- shrunk hit rate per event type                                    -> evidence.predictive_power
  * move-size scale       - realised directional abnormal move / prior expected move          -> ev.move_scale
  * probability calibration- linear recalibration of forecast probabilities (Brier-driven)    -> ev.calibrate_p
  * thesis priors         - seed prior nudged toward the thesis' realised hit rate            -> thesis.effective_prior
Nothing is adjusted until ``learning_min_samples`` outcomes exist, and every adjustment is shrunk toward its default."""
from __future__ import annotations

import datetime as dt
import json
import statistics
from typing import Callable, Dict, List, Optional

from . import db, ev, marketdata as md, nlp, quotes
from .timeutil import add_trading_days, iso, parse_ts, utcnow


def observing(cfg: Optional[dict]) -> bool:
    """True (the default) = keep learning but only RECORD what would change; decisions stay exactly as they are. 'apply' = rewrite live probabilities, move sizes, source credibility."""
    return str(((cfg or {}).get("intelligence") or {}).get("learning_mode", "observe")) != "apply"


def _base_prices(conn, ticker: str, now):
    q = quotes.valuation_price(conn, ticker, now)
    spy = quotes.valuation_price(conn, "SPY", now)
    return (q["price"] if q else None), (spy["price"] if spy else None)


def record_forecast_prediction(conn, forecast_id: int, f: dict, c: dict, now) -> int:
    p, spy = _base_prices(conn, f["ticker"], now)
    up = f["direction"] == "LONG"
    prob = f["p_up"] if up else 1 - f["p_up"]
    cur = conn.execute("INSERT INTO predictions(created,kind,subject,claim,probability,baseline_price,benchmark_price,expected_return,horizon_date,source,event_type,thesis_key,forecast_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (iso(now), "forecast", f["ticker"], "abnormal_up" if up else "abnormal_down", prob, p, spy, f["ev_net"], iso(add_trading_days(now, f["horizon_days"])), c.get("source"), c.get("event_type"), f["thesis_key"], forecast_id))
    return cur.lastrowid


def record_chain_prediction(conn, ticker: str, claim: str, prob: float, horizon_trading_days: int, event_type: str, source: str, now) -> int:
    p, spy = _base_prices(conn, ticker, now)
    cur = conn.execute("INSERT INTO predictions(created,kind,subject,claim,probability,baseline_price,benchmark_price,horizon_date,source,event_type) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (iso(now), "llm_chain", ticker, claim, prob, p, spy, iso(add_trading_days(now, min(40, horizon_trading_days))), source, event_type))
    return cur.lastrowid


def record_position_prediction(conn, position_id: int, f: dict, fill: float, now) -> int:
    cur = conn.execute("INSERT INTO predictions(created,kind,subject,claim,probability,baseline_price,expected_return,horizon_date,event_type,thesis_key,forecast_id,position_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (iso(now), "position", f["ticker"], "position_profitable", f["p_positive"], fill, f["ev_net"], iso(add_trading_days(now, f["horizon_days"] + 5)), f["inputs"].get("event_type"), f["thesis_key"], None, position_id))
    return cur.lastrowid


def record_thesis_predictions(conn, now, every_days: int = 7, horizon_days: int = 20) -> int:
    n = 0
    for t in conn.execute("SELECT key,probability FROM theses WHERE active=1").fetchall():
        if conn.execute("SELECT 1 FROM predictions WHERE kind='thesis' AND subject=? AND created>?", (t["key"], iso(now - dt.timedelta(days=every_days)))).fetchone():
            continue
        conn.execute("INSERT INTO predictions(created,kind,subject,claim,probability,horizon_date,thesis_key) VALUES(?,?,?,?,?,?,?)",
                     (iso(now), "thesis", t["key"], "abnormal_up", t["probability"], iso(add_trading_days(now, horizon_days)), t["key"]))
        n += 1
    return n


def _abn(conn, ticker: str, start_day: str, end_day: str, start_px: Optional[float] = None, bench_px: Optional[float] = None):
    a0 = md.close_on_or_before(conn, ticker, start_day)
    a1 = md.close_on_or_before(conn, ticker, end_day)
    b0 = md.close_on_or_before(conn, "SPY", start_day)
    b1 = md.close_on_or_before(conn, "SPY", end_day)
    last = conn.execute("SELECT MAX(day) FROM market_series WHERE symbol=?", (ticker,)).fetchone()[0]
    if not (a1 and b1 and last and last >= end_day):
        return None
    s = start_px or (a0["close"] if a0 else None)
    bs = bench_px or (b0["close"] if b0 else None)
    if not s or not bs:
        return None
    r, rb = a1["close"] / s - 1, b1["close"] / bs - 1
    return r, rb, r - rb


def resolve_due(conn, cfg: dict, now=None, fetch: Optional[Callable] = None, max_n: int = 40) -> dict:
    now = now or utcnow()
    rows = conn.execute("SELECT * FROM predictions WHERE resolved=0 AND kind IN ('forecast','thesis','llm_chain') AND horizon_date<=? ORDER BY horizon_date LIMIT ?", (iso(now), max_n)).fetchall()
    done = skipped = 0
    refreshed = set()
    for p in rows:
        subs = [p["subject"]]
        if p["kind"] == "thesis":
            subs = [r[0] for r in conn.execute("SELECT ticker FROM thesis_companies WHERE thesis_key=? AND role='beneficiary' ORDER BY weight DESC LIMIT 4", (p["subject"],))]
        for s in subs + ["SPY"]:
            if s not in refreshed:
                md.refresh_symbol(conn, s, 6, "1y", fetch, now)
                refreshed.add(s)
        d0, d1 = parse_ts(p["created"]).date().isoformat(), parse_ts(p["horizon_date"]).date().isoformat()
        if p["kind"] == "thesis":
            res = [x for x in (_abn(conn, s, d0, d1) for s in subs) if x]
            r = (statistics.mean(x[0] for x in res), statistics.mean(x[1] for x in res), statistics.mean(x[2] for x in res)) if len(res) >= 2 else None
        else:
            r = _abn(conn, p["subject"], d0, d1, p["baseline_price"], p["benchmark_price"])
        if r is None:
            skipped += 1
            if (now - parse_ts(p["horizon_date"])).days > 12:   # data never arrived: close out unscored so it cannot block the queue
                conn.execute("UPDATE predictions SET resolved=2 WHERE id=?", (p["id"],))
            continue
        ret, bret, abn = r
        outcome = 1.0 if ((abn > 0) if p["claim"] == "abnormal_up" else (abn < 0)) else 0.0
        brier = (p["probability"] - outcome) ** 2
        with db.tx(conn):
            conn.execute("INSERT OR REPLACE INTO outcomes(prediction_id,observed_at,outcome,realized_return,benchmark_return,abnormal_return,brier,detail_json) VALUES(?,?,?,?,?,?,?,?)",
                         (p["id"], iso(now), outcome, ret, bret, abn, brier, json.dumps({"claim": p["claim"]})))
            conn.execute("UPDATE predictions SET resolved=1 WHERE id=?", (p["id"],))
            if p["kind"] in ("forecast", "llm_chain") and p["source"] and p["event_type"]:
                src_key = ("observed:" + p["source"]) if observing(cfg) else p["source"]                 # observe mode: kept apart, where credibility never looks
                conn.execute("INSERT INTO source_stats(source,event_type,hits,misses,updated) VALUES(?,?,?,?,?) ON CONFLICT(source,event_type) DO UPDATE SET hits=hits+excluded.hits,misses=misses+excluded.misses,updated=excluded.updated",
                             (src_key, p["event_type"], outcome, 1 - outcome, iso(now)))
        done += 1
    return {"resolved": done, "waiting": skipped}


def position_closed(conn, position_id: int, pnl: float, cost: float, trigger: str, now=None) -> None:
    now = now or utcnow()
    p = conn.execute("SELECT * FROM predictions WHERE kind='position' AND position_id=? AND resolved=0", (position_id,)).fetchone()
    ret = pnl / cost if cost else 0.0
    if p:
        out = 1.0 if pnl > 0 else 0.0
        conn.execute("INSERT OR REPLACE INTO outcomes(prediction_id,observed_at,outcome,realized_return,brier,detail_json) VALUES(?,?,?,?,?,?)", (p["id"], iso(now), out, ret, (p["probability"] - out) ** 2, json.dumps({"trigger": trigger})))
        conn.execute("UPDATE predictions SET resolved=1 WHERE id=?", (p["id"],))
    pos = conn.execute("SELECT ticker FROM positions WHERE id=?", (position_id,)).fetchone()
    code = trigger if pnl >= 0 else trigger + "_LOSS"
    conn.execute("INSERT INTO lessons(created,category,ticker,detail) VALUES(?,?,?,?)", (iso(now), code, pos["ticker"] if pos else "", "return=%.4f" % ret))
    win = 1 if pnl > 0 else 0
    conn.execute("INSERT INTO rule_stats(rule_key,wins,losses,total_pnl,updated) VALUES('event_trade',?,?,?,?) ON CONFLICT(rule_key) DO UPDATE SET wins=wins+?,losses=losses+?,total_pnl=total_pnl+?,updated=?", (win, 1 - win, pnl, iso(now), win, 1 - win, pnl, iso(now)))


def recalibrate(conn, cfg: dict, now=None) -> dict:
    now = now or utcnow()
    minn = int(cfg["intelligence"]["learning_min_samples"])
    observe = observing(cfg)
    out: Dict = {"calibration": None, "event_types": {}, "theses": {}, "mode": "observe" if observe else "apply"}

    def put(key: str, value, why: str) -> None:
        db.set_param(conn, ("proposed:" + key) if observe else key, value, why + (" [PROPOSAL ONLY: learning_mode=observe, not used by decisions]" if observe else ""))
    rows = conn.execute("SELECT p.event_type,p.probability,p.claim,o.outcome,o.abnormal_return FROM outcomes o JOIN predictions p ON p.id=o.prediction_id WHERE p.kind='forecast'").fetchall()
    n = len(rows)
    if n >= max(minn, 40):
        ps, ys = [r["probability"] for r in rows], [r["outcome"] for r in rows]
        mp, my = statistics.mean(ps), statistics.mean(ys)
        var = statistics.pvariance(ps)
        slope = (sum((x - mp) * (y - my) for x, y in zip(ps, ys)) / len(ps) / var) if var > 1e-6 else 1.0
        w = n / (n + 60.0)
        b = max(0.3, min(1.5, w * slope + (1 - w) * 1.0))
        a = my - b * mp
        put("calibration", {"a": a, "b": b, "n": n}, "linear recalibration of forecast probabilities")
        out["calibration"] = {"a": a, "b": b, "n": n}
    by: Dict[str, List] = {}
    for r in rows:
        by.setdefault(r["event_type"] or "general_news", []).append(r)
    for et, g in by.items():
        if len(g) < minn:
            continue
        hit = statistics.mean(r["outcome"] for r in g)
        default = nlp.BY_NAME[et].power if et in nlp.BY_NAME else 0.4
        power = max(0.15, min(0.9, (default * 10 + hit * len(g)) / (10 + len(g))))
        put("power:" + et, power, "n=%d hit=%.2f" % (len(g), hit))
        signed = [(r["abnormal_return"] or 0.0) * (1 if r["claim"] == "abnormal_up" else -1) for r in g]
        expected = ev.MOVE_PRIOR.get(et)
        if expected:
            scale = max(0.5, min(1.5, (len(g) * (statistics.mean(signed) / expected) + 10 * 1.0) / (len(g) + 10)))
            put("move_scale:" + et, scale, "n=%d realised %.4f vs prior %.4f" % (len(g), statistics.mean(signed), expected))
        else:
            scale = 1.0
        out["event_types"][et] = {"n": len(g), "hit": hit, "power": power, "scale": scale}
    trows = conn.execute("SELECT p.thesis_key,p.probability,o.outcome FROM outcomes o JOIN predictions p ON p.id=o.prediction_id WHERE p.kind='thesis'").fetchall()
    tby: Dict[str, List] = {}
    for r in trows:
        tby.setdefault(r["thesis_key"], []).append(r)
    from .theses_seed import SEED_THESES
    seed = {t["key"]: t["prior"] for t in SEED_THESES}
    for k, g in tby.items():
        if len(g) < max(6, minn // 3) or k not in seed:
            continue
        gap = statistics.mean(r["outcome"] for r in g) - statistics.mean(r["probability"] for r in g)
        newp = max(seed[k] - 0.15, min(seed[k] + 0.15, thesis_prior_now(conn, k, seed[k]) + 0.3 * gap))
        put("prior:" + k, newp, "n=%d realised-minus-predicted %.3f" % (len(g), gap))
        out["theses"][k] = {"n": len(g), "gap": gap, "prior": newp}
    return out


def proposals(conn) -> tuple:
    """What observe mode has recorded: parameter proposals (with the live value beside each) and per-source hit/miss counts kept apart."""
    props = []
    for r in conn.execute("SELECT key,value_json,reason FROM model_params WHERE key LIKE 'proposed:%' ORDER BY key"):
        live = r["key"][len("proposed:"):]
        props.append({"key": live, "proposed": json.loads(r["value_json"]), "live": db.param(conn, live), "why": r["reason"] or ""})
    obs = [dict(r) for r in conn.execute("SELECT source,event_type,hits,misses FROM source_stats WHERE source LIKE 'observed:%' ORDER BY source,event_type")]
    return props, obs


def promote(conn, cfg: dict, now=None, apply: bool = False) -> dict:
    """Make reviewed proposals live. Dry run unless apply=True. Recorded in the strategy history so later results are attributed to the new behaviour."""
    from . import stamp
    now = now or utcnow()
    props, obs = proposals(conn)
    if apply and (props or obs):
        with db.tx(conn):
            for p in props:
                db.set_param(conn, p["key"], p["proposed"], "promoted by the owner: " + p["why"].replace(" [PROPOSAL ONLY: learning_mode=observe, not used by decisions]", ""))
                conn.execute("DELETE FROM model_params WHERE key=?", ("proposed:" + p["key"],))
            for o in obs:
                live = o["source"][len("observed:"):]
                conn.execute("INSERT INTO source_stats(source,event_type,hits,misses,updated) VALUES(?,?,?,?,?) ON CONFLICT(source,event_type) DO UPDATE SET hits=hits+excluded.hits,misses=misses+excluded.misses,updated=excluded.updated",
                             (live, o["event_type"], o["hits"], o["misses"], iso(now)))
                conn.execute("DELETE FROM source_stats WHERE source=? AND event_type=?", (o["source"], o["event_type"]))
        db.audit(conn, "owner", "learning_promote", "", {"parameters": len(props), "source_records": len(obs)})
        stamp.record(conn, cfg, now, note="learning promoted: %d parameters, %d source records made live" % (len(props), len(obs)))
    return {"parameters": props, "sources": obs, "applied": bool(apply and (props or obs))}


def thesis_prior_now(conn, key: str, default: float) -> float:
    v = db.param(conn, "prior:" + key)
    return float(v) if isinstance(v, (int, float)) else default


def metrics(conn) -> dict:
    rows = conn.execute("SELECT p.kind,p.event_type,p.source,p.thesis_key,p.probability,o.outcome,o.brier,o.abnormal_return FROM outcomes o JOIN predictions p ON p.id=o.prediction_id").fetchall()
    out: Dict = {"n": len(rows), "brier": None, "baseline_brier": None, "kinds": {}, "bins": [], "event_types": {}, "sources": {}, "theses": {}}
    if not rows:
        return out
    out["brier"] = statistics.mean(r["brier"] for r in rows)
    ys = [r["outcome"] for r in rows]
    base = statistics.mean(ys)
    out["baseline_brier"] = statistics.mean((base - y) ** 2 for y in ys)
    for kind in {r["kind"] for r in rows}:
        g = [r for r in rows if r["kind"] == kind]
        out["kinds"][kind] = {"n": len(g), "brier": statistics.mean(r["brier"] for r in g), "hit": statistics.mean(r["outcome"] for r in g)}
    for lo in (0.0, 0.2, 0.4, 0.6, 0.8):
        g = [r for r in rows if lo <= r["probability"] < lo + 0.2 + (0.001 if lo == 0.8 else 0)]
        if g:
            out["bins"].append({"range": "%.0f-%.0f%%" % (lo * 100, (lo + 0.2) * 100), "n": len(g), "predicted": statistics.mean(r["probability"] for r in g), "actual": statistics.mean(r["outcome"] for r in g)})
    for fld, key in (("event_type", "event_types"), ("source", "sources"), ("thesis_key", "theses")):
        for v in {r[fld] for r in rows if r[fld]}:
            g = [r for r in rows if r[fld] == v]
            out[key][v] = {"n": len(g), "hit": statistics.mean(r["outcome"] for r in g), "brier": statistics.mean(r["brier"] for r in g)}
    return out
