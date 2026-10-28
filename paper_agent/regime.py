"""Regime engine: soft probabilities over the 8 OMEGA regimes from keyless market data (SPY, VIX, rates, credit, USD, oil, gold)."""
from __future__ import annotations

import json
import math
from typing import Callable, Dict, Optional

from . import marketdata as md
from .theses_seed import REGIME_RISK_MULT
from .timeutil import iso, utcnow

SYMBOLS = {"SPY": "SPY", "VIX": "^VIX", "TNX": "^TNX", "IRX": "^IRX", "HYG": "HYG", "LQD": "LQD", "USD": "DX-Y.NYB", "OIL": "CL=F", "GOLD": "GC=F", "IWM": "IWM"}
REGIMES = ["risk_on", "risk_off", "liquidity_expansion", "liquidity_contraction", "recovery", "recession", "boom", "stagflation"]


def _s(x: Optional[float], c: float, w: float) -> float:
    if x is None:
        return 0.5
    return 1.0 / (1.0 + math.exp(-(x - c) / w))


def _yield(xs):
    if not xs:
        return None
    v = xs[-1]
    return v / 10.0 if v > 20 else v


def features(conn) -> Dict[str, Optional[float]]:
    c = {k: md.closes(conn, s, 260) for k, s in SYMBOLS.items()}
    spy = c["SPY"]
    f: Dict[str, Optional[float]] = {}
    f["spy_r20"], f["spy_r60"] = md.ret_n(spy, 20), md.ret_n(spy, 60)
    f["spy_vs200"] = (spy[-1] / (sum(spy[-200:]) / len(spy[-200:])) - 1) if len(spy) >= 150 else None
    f["spy_dd"] = (spy[-1] / max(spy[-250:]) - 1) if spy else None
    f["vix"] = c["VIX"][-1] if c["VIX"] else None
    f["vix_chg20"] = (c["VIX"][-1] / c["VIX"][-21] - 1) if len(c["VIX"]) > 21 else None
    t10, t3m = _yield(c["TNX"]), _yield(c["IRX"])
    f["curve"] = (t10 - t3m) if t10 is not None and t3m is not None else None
    f["y10_chg20"] = ((c["TNX"][-1] - c["TNX"][-21]) / (10.0 if c["TNX"][-1] > 20 else 1.0)) if len(c["TNX"]) > 21 else None
    if len(c["HYG"]) > 21 and len(c["LQD"]) > 21 and c["LQD"][-1] and c["LQD"][-21]:
        f["credit"] = (c["HYG"][-1] / c["LQD"][-1]) / (c["HYG"][-21] / c["LQD"][-21]) - 1
    else:
        f["credit"] = None
    f["usd_r20"], f["oil_r20"], f["gold_r20"] = md.ret_n(c["USD"], 20), md.ret_n(c["OIL"], 20), md.ret_n(c["GOLD"], 20)
    iwm = md.ret_n(c["IWM"], 20)
    f["small_rel"] = (iwm - f["spy_r20"]) if iwm is not None and f["spy_r20"] is not None else None
    return f


def classify(f: Dict[str, Optional[float]]) -> dict:
    if f.get("spy_r20") is None or f.get("vix") is None:
        return {"regime": "unknown", "probs": {}, "features": f, "risk_mult": REGIME_RISK_MULT["unknown"]}
    s = _s
    sc = {
        "risk_on": .35 * s(f["spy_r20"], .01, .02) + .25 * s(f["spy_vs200"], 0, .03) + .25 * (1 - s(f["vix"], 20, 4)) + .15 * s(f["credit"], 0, .01),
        "risk_off": .30 * (1 - s(f["spy_r20"], -.01, .02)) + .30 * s(f["vix"], 24, 4) + .20 * (1 - s(f["spy_vs200"], 0, .03)) + .20 * (1 - s(f["credit"], 0, .01)),
        "liquidity_expansion": .35 * (1 - s(f["y10_chg20"], 0, .15)) + .25 * (1 - s(f["usd_r20"], 0, .015)) + .20 * s(f["credit"], 0, .01) + .20 * s(f["spy_r20"], 0, .02),
        "liquidity_contraction": .35 * s(f["y10_chg20"], .15, .15) + .25 * s(f["usd_r20"], .01, .015) + .20 * (1 - s(f["credit"], 0, .01)) + .20 * (1 - s(f["spy_r20"], 0, .02)),
        "recovery": .40 * s(-(f["spy_dd"] or 0), .08, .03) + .35 * s(f["spy_r20"], .02, .02) + .25 * (1 - s(f["vix_chg20"], 0, .10)),
        "recession": (.25 * (1 - s(f["curve"], 0, .5)) + .30 * (1 - s(f["spy_vs200"], -.02, .04)) + .25 * (1 - s(f["credit"], -.01, .01)) + .20 * (1 - s(f["oil_r20"], -.05, .07))) * (0.5 + 0.5 * s(f["vix"], 20, 5)),
        "boom": .30 * s(f["spy_vs200"], .06, .03) + .25 * (1 - s(f["vix"], 17, 3)) + .25 * s(f["curve"], .5, .5) + .20 * s(f["small_rel"], 0, .02),
        "stagflation": .35 * s(f["oil_r20"], .08, .05) + .25 * s(f["y10_chg20"], .10, .15) + .25 * (1 - s(f["spy_r20"], 0, .025)) + .15 * s(f["gold_r20"], .03, .03),
    }
    m = max(sc.values())
    ex = {k: math.exp(5.0 * (v - m)) for k, v in sc.items()}
    z = sum(ex.values())
    probs = {k: v / z for k, v in ex.items()}
    primary = max(probs, key=probs.get)
    mult = sum(probs[k] * REGIME_RISK_MULT[k] for k in probs)
    return {"regime": primary, "probs": probs, "features": f, "risk_mult": mult}


def refresh(conn, fetch: Optional[Callable] = None, now=None, max_age_hours: float = 6) -> dict:
    status = {s: md.refresh_symbol(conn, s, max_age_hours, "1y", fetch, now) for s in SYMBOLS.values()}
    r = classify(features(conn))
    conn.execute("INSERT INTO regimes(ts,regime,probs_json,features_json) VALUES(?,?,?,?)", (iso(now), r["regime"], json.dumps(r["probs"], sort_keys=True), json.dumps(r["features"], sort_keys=True)))
    r["fetch_status"] = status
    return r


def current(conn) -> dict:
    row = conn.execute("SELECT * FROM regimes ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {"regime": "unknown", "probs": {}, "risk_mult": REGIME_RISK_MULT["unknown"], "ts": None}
    probs = json.loads(row["probs_json"] or "{}")
    mult = sum(probs.get(k, 0) * v for k, v in REGIME_RISK_MULT.items()) if probs else REGIME_RISK_MULT["unknown"]
    return {"regime": row["regime"], "probs": probs, "risk_mult": mult, "ts": row["ts"], "features": json.loads(row["features_json"] or "{}")}
