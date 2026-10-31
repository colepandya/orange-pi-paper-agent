"""What actually happened to each prediction, measured properly (the old tracker kept ONE number per prediction).

For every forecast the bot made (whether or not it traded) this records, at 1, 3, 5, 10 and 20 sessions after it: the position's return (a short earns the opposite of the stock), how that compares with the MARKET
and with the stock's SECTOR, the best and worst the position went through the period (maximum favourable and adverse excursion, from daily highs and lows), how many sessions each took, whether the idea's own
TARGET or STOP was reached first, and the overnight gap. Daily data only (that is what the Pi keeps for every stock), so "which came first inside one day" is reported as ambiguous and counted as the stop.
Then hierarchical statistics: a small group (one source, one event type) is pulled toward its parent group instead of being believed, with an honest interval, so 8 trades never look like proof."""
from __future__ import annotations

import datetime as dt
import json
import math
from typing import Dict, List, Optional, Tuple

from . import db
from .skeptic import wilson
from .timeutil import iso, parse_ts, utcnow

HORIZONS = (1, 3, 5, 10, 20)
SECTOR_ETF = {"semiconductors": "SMH", "technology": "XLK", "information technology": "XLK", "health care": "XLV", "healthcare": "XLV", "financials": "XLF", "financial services": "XLF", "energy": "XLE", "industrials": "XLI",
              "consumer discretionary": "XLY", "consumer cyclical": "XLY", "consumer staples": "XLP", "consumer defensive": "XLP", "utilities": "XLU", "materials": "XLB", "basic materials": "XLB", "real estate": "XLRE",
              "communication services": "XLC"}


def bars(conn, symbol: str, after_day: str, n: int = 25) -> List[dict]:
    return [dict(r) for r in conn.execute("SELECT day,open,high,low,close FROM market_series WHERE symbol=? AND day>? ORDER BY day LIMIT ?", (symbol, after_day, n))]


def _close_on_or_before(conn, symbol: str, day: str) -> Optional[float]:
    r = conn.execute("SELECT close FROM market_series WHERE symbol=? AND day<=? ORDER BY day DESC LIMIT 1", (symbol, day)).fetchone()
    return float(r[0]) if r and r[0] else None


def measure(entry: float, sg: float, bs: List[dict], target: Optional[float], stop: Optional[float], horizon: int) -> Optional[dict]:
    """The pure calculation for one prediction and one horizon (no database): returns None until `horizon` sessions of data exist."""
    if len(bs) < horizon or not entry:
        return None
    w = bs[:horizon]
    last = w[-1]["close"]
    ret = sg * (last / entry - 1.0)
    if sg > 0:
        mfe_v, mae_v = max(b["high"] for b in w) / entry - 1.0, min(b["low"] for b in w) / entry - 1.0
        t_mfe = max(range(len(w)), key=lambda i: w[i]["high"]) + 1
        t_mae = min(range(len(w)), key=lambda i: w[i]["low"]) + 1
    else:
        mfe_v, mae_v = 1.0 - min(b["low"] for b in w) / entry, 1.0 - max(b["high"] for b in w) / entry
        t_mfe = min(range(len(w)), key=lambda i: w[i]["low"]) + 1
        t_mae = max(range(len(w)), key=lambda i: w[i]["high"]) + 1
    first, first_day = "none", None
    for i, b in enumerate(w):
        fav = (b["high"] / entry - 1.0) if sg > 0 else (1.0 - b["low"] / entry)
        adv = (b["low"] / entry - 1.0) if sg > 0 else (1.0 - b["high"] / entry)
        hit_t = target is not None and target > 0 and fav >= target
        hit_s = stop is not None and stop < 0 and adv <= stop
        if hit_t and hit_s:
            first, first_day = "ambiguous_stop_assumed", i + 1
            break
        if hit_s:
            first, first_day = "stop", i + 1
            break
        if hit_t:
            first, first_day = "target", i + 1
            break
    gap = sg * (bs[0]["open"] / entry - 1.0) if bs[0].get("open") else None
    return {"ret": ret, "mfe": mfe_v, "mae": mae_v, "t_mfe": t_mfe, "t_mae": t_mae, "first_hit": first, "first_hit_day": first_day, "gap": gap, "last_day": w[-1]["day"]}


def compute(conn, cfg: dict, now=None, max_n: int = 80) -> dict:
    """Fill prediction_paths for forecasts old enough to have data. Idempotent and incremental: a prediction gets each horizon the first time enough sessions exist."""
    now = now or utcnow()
    out = {"predictions": 0, "rows": 0}
    rows = conn.execute("SELECT p.id,p.subject,p.created,p.baseline_price,p.event_type,p.source,f.direction,f.target_return,f.stop_return FROM predictions p JOIN forecasts f ON f.id=p.forecast_id WHERE p.kind='forecast' AND p.baseline_price>0 "
                        "AND p.created>=? AND (SELECT COUNT(*) FROM prediction_paths x WHERE x.prediction_id=p.id) < ? ORDER BY p.id DESC LIMIT ?", (iso(now - dt.timedelta(days=120)), len(HORIZONS), max_n)).fetchall()
    sec_cache: Dict[str, Optional[str]] = {}
    for r in rows:
        day0 = r["created"][:10]
        sg = -1.0 if r["direction"] == "SHORT" else 1.0
        bs = bars(conn, r["subject"], day0)
        if not bs:
            continue
        done = {x[0] for x in conn.execute("SELECT horizon FROM prediction_paths WHERE prediction_id=?", (r["id"],))}
        sector = None
        if r["subject"] not in sec_cache:
            c = conn.execute("SELECT sector,industry FROM companies WHERE ticker=?", (r["subject"],)).fetchone()
            key = ((c["industry"] if c and (c["industry"] or "").lower() in SECTOR_ETF else c["sector"]) if c else "") or ""
            sec_cache[r["subject"]] = SECTOR_ETF.get(key.lower())
        sector = sec_cache[r["subject"]]
        spy0 = _close_on_or_before(conn, "SPY", day0)
        sec0 = _close_on_or_before(conn, sector, day0) if sector else None
        n_new = 0
        for h in HORIZONS:
            if h in done:
                continue
            m = measure(float(r["baseline_price"]), sg, bs, r["target_return"], r["stop_return"], h)
            if not m:
                continue
            spy1 = _close_on_or_before(conn, "SPY", m["last_day"])
            sec1 = _close_on_or_before(conn, sector, m["last_day"]) if sector else None
            stock_ret = (bs[h - 1]["close"] / float(r["baseline_price"]) - 1.0)
            abn_spy = sg * (stock_ret - (spy1 / spy0 - 1.0)) if spy0 and spy1 else None
            abn_sec = sg * (stock_ret - (sec1 / sec0 - 1.0)) if sec0 and sec1 else None
            conn.execute("INSERT OR REPLACE INTO prediction_paths(prediction_id,horizon,ret,abn_spy,abn_sector,mfe,mae,t_mfe,t_mae,first_hit,first_hit_day,gap,computed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (r["id"], h, m["ret"], abn_spy, abn_sec, m["mfe"], m["mae"], m["t_mfe"], m["t_mae"], m["first_hit"], m["first_hit_day"], m["gap"], iso(now)))
            n_new += 1
        if n_new:
            out["predictions"] += 1
            out["rows"] += n_new
    return out


def hierarchical(conn, horizon: int = 5, k: int = 20, min_n: int = 1) -> List[dict]:
    """Win rate (beat the market in the position's direction) at one horizon, by group, each group shrunk toward its parent: global -> event type -> source within event type. Rows carry n, the raw rate, the
    shrunk rate, an interval, and the mean abnormal return. A group's own result only takes over from its parent as its sample grows (k pseudo-observations of the parent)."""
    rows = conn.execute("SELECT p.event_type et,p.source src,x.abn_spy a FROM prediction_paths x JOIN predictions p ON p.id=x.prediction_id WHERE x.horizon=? AND x.abn_spy IS NOT NULL", (horizon,)).fetchall()
    if not rows:
        return []
    g = [r["a"] for r in rows]
    gp = sum(1 for a in g if a > 0) / len(g)
    out = [{"level": "all", "key": "all predictions", "n": len(g), "win": gp, "shrunk": gp, "low": wilson(sum(1 for a in g if a > 0), len(g))[0], "high": wilson(sum(1 for a in g if a > 0), len(g))[1], "mean_abn": sum(g) / len(g)}]
    by_et: Dict[str, List[float]] = {}
    by_src: Dict[Tuple[str, str], List[float]] = {}
    for r in rows:
        by_et.setdefault(r["et"] or "?", []).append(r["a"])
        by_src.setdefault((r["et"] or "?", r["src"] or "?"), []).append(r["a"])
    et_p = {}
    for et, v in sorted(by_et.items()):
        w = sum(1 for a in v if a > 0)
        et_p[et] = (w + k * gp) / (len(v) + k)
        lo, hi = wilson(w, len(v))
        out.append({"level": "event type", "key": et, "n": len(v), "win": w / len(v), "shrunk": et_p[et], "low": lo, "high": hi, "mean_abn": sum(v) / len(v)})
    for (et, src), v in sorted(by_src.items()):
        if len(v) < min_n:
            continue
        w = sum(1 for a in v if a > 0)
        lo, hi = wilson(w, len(v))
        out.append({"level": "source", "key": "%s / %s" % (et, src), "n": len(v), "win": w / len(v), "shrunk": (w + k * et_p[et]) / (len(v) + k), "low": lo, "high": hi, "mean_abn": sum(v) / len(v)})
    return out
