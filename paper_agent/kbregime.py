"""Regime context: nine deterministic market states derived from the market features the bot already stores (index trend, volatility, yields, credit spread, dollar, oil, gold). No model, no library.
A regime may modify expected magnitude, position size, required confidence, a liquidity penalty and the holding timeframe; it can NEVER reverse a strong company-specific signal. In observe mode (the default)
the adjustments are recorded and measured, and change nothing."""
from __future__ import annotations

import datetime as dt
import json
from typing import Dict, List, Optional

from . import db, regime as base_regime
from .timeutil import iso, parse_ts, utcnow

REGIME_IDS = ["ordinary", "risk_on", "risk_off", "rising_rate", "falling_rate", "inflation_shock", "commodity_shock", "high_volatility", "liquidity_stress"]
EXPIRY_DAYS = 3                                    # a detection stays valid for three days unless it is detected again
# (condition text, function of the features -> (fires, strength 0..1))
_g = lambda f, k: f.get(k)  # noqa: E731


def _rules(f: Dict[str, Optional[float]]) -> Dict[str, dict]:
    """Every rule with its inputs and the exact thresholds, so the trace can show why a regime fired or did not."""
    def has(*ks):
        return all(f.get(k) is not None for k in ks)
    R: Dict[str, dict] = {}
    R["risk_on"] = {"needs": ["spy_r20", "vix", "spy_vs200"], "rule": "SPY 20-day return > +1% AND VIX < 20 AND SPY above its 200-day average",
                    "fires": has("spy_r20", "vix", "spy_vs200") and f["spy_r20"] > 0.01 and f["vix"] < 20 and f["spy_vs200"] > 0, "strength": (min(1.0, (f["spy_r20"] - 0.01) / 0.03 + (20 - f["vix"]) / 10) / 2) if has("spy_r20", "vix") else 0}
    R["risk_off"] = {"needs": ["spy_r20", "vix", "spy_vs200"], "rule": "SPY 20-day return < -2% OR (VIX > 25 AND SPY below its 200-day average)",
                     "fires": has("spy_r20") and (f["spy_r20"] < -0.02 or (has("vix", "spy_vs200") and f["vix"] > 25 and f["spy_vs200"] < 0)), "strength": min(1.0, max((-(f.get("spy_r20") or 0) - 0.02) / 0.04, ((f.get("vix") or 0) - 25) / 10, 0.0) + 0.3)}
    R["rising_rate"] = {"needs": ["y10_chg20"], "rule": "10-year yield up at least 20 basis points over 20 sessions", "fires": has("y10_chg20") and f["y10_chg20"] >= 0.20, "strength": min(1.0, ((f.get("y10_chg20") or 0) - 0.20) / 0.3 + 0.4)}
    R["falling_rate"] = {"needs": ["y10_chg20"], "rule": "10-year yield down at least 20 basis points over 20 sessions", "fires": has("y10_chg20") and f["y10_chg20"] <= -0.20, "strength": min(1.0, (-(f.get("y10_chg20") or 0) - 0.20) / 0.3 + 0.4)}
    R["inflation_shock"] = {"needs": ["oil_r20", "y10_chg20", "gold_r20"], "rule": "oil +10% AND 10-year yield +10bp AND gold +2% over 20 sessions (a proxy: no inflation print is stored)",
                            "fires": has("oil_r20", "y10_chg20", "gold_r20") and f["oil_r20"] >= 0.10 and f["y10_chg20"] >= 0.10 and f["gold_r20"] >= 0.02, "strength": min(1.0, (f.get("oil_r20") or 0) / 0.2)}
    R["commodity_shock"] = {"needs": ["oil_r20", "gold_r20"], "rule": "oil moved at least 15% OR gold at least 8% over 20 sessions", "fires": (has("oil_r20") and abs(f["oil_r20"]) >= 0.15) or (has("gold_r20") and abs(f["gold_r20"]) >= 0.08),
                            "strength": min(1.0, max(abs(f.get("oil_r20") or 0) / 0.3, abs(f.get("gold_r20") or 0) / 0.16))}
    R["high_volatility"] = {"needs": ["vix", "vix_chg20"], "rule": "VIX >= 28 OR VIX up at least 40% over 20 sessions", "fires": (has("vix") and f["vix"] >= 28) or (has("vix_chg20") and f["vix_chg20"] >= 0.40), "strength": min(1.0, max(((f.get("vix") or 0) - 28) / 15, (f.get("vix_chg20") or 0) / 0.8, 0.0) + 0.3)}
    R["liquidity_stress"] = {"needs": ["credit", "vix", "usd_r20"], "rule": "high-yield vs investment-grade ratio down at least 2% over 20 sessions AND (VIX > 25 OR dollar up 3%)",
                             "fires": has("credit") and f["credit"] <= -0.02 and ((has("vix") and f["vix"] > 25) or (has("usd_r20") and f["usd_r20"] >= 0.03)), "strength": min(1.0, (-(f.get("credit") or 0) - 0.02) / 0.04 + 0.4)}
    return R


# adjustments per regime: (size multiplier long, size multiplier short, magnitude multiplier, confidence required (added), liquidity penalty, holding-time multiplier)
ADJUST = {"ordinary": (1.0, 1.0, 1.0, 0.0, 0.0, 1.0), "risk_on": (1.0, 0.9, 1.0, 0.0, 0.0, 1.0), "risk_off": (0.85, 1.0, 1.0, 0.05, 0.05, 0.9), "rising_rate": (0.95, 1.0, 1.0, 0.0, 0.0, 1.0),
          "falling_rate": (1.0, 0.97, 1.0, 0.0, 0.0, 1.0), "inflation_shock": (0.9, 0.95, 1.0, 0.03, 0.0, 0.9), "commodity_shock": (0.9, 0.9, 1.0, 0.03, 0.0, 0.9),
          "high_volatility": (0.8, 0.8, 1.15, 0.07, 0.10, 0.8), "liquidity_stress": (0.7, 0.8, 1.0, 0.10, 0.20, 0.7)}
STRONG_SIGNAL = 0.60                                # |direction| x confidence at or above this is a strong company-specific signal


def _input_times(conn) -> Dict[str, Optional[str]]:
    out = {}
    for name, sym in base_regime.SYMBOLS.items():
        r = conn.execute("SELECT MAX(day) FROM market_series WHERE symbol=?", (sym,)).fetchone()
        out[name] = r[0] if r else None
    return out


def detect(conn, now=None, persist: bool = True) -> dict:
    """The regimes active now: each with inputs, input times, confidence, start, last update, expiry and rule trace. A detection older than its inputs allow is not made: stale inputs yield `unknown`."""
    now = now or utcnow()
    f = base_regime.features(conn)
    times = _input_times(conn)
    newest = max((t for t in times.values() if t), default=None)
    stale_days = (now.date() - dt.date.fromisoformat(newest[:10])).days if newest else None
    rules = _rules(f)
    if f.get("spy_r20") is None or f.get("vix") is None:
        return {"detected": [], "status": "unknown", "why": "index and volatility history is not stored", "features": f}
    if stale_days is not None and stale_days > 5:
        return {"detected": [], "status": "stale_inputs", "why": "the newest market input is %d days old (limit 5): no regime is declared" % stale_days, "features": f, "input_times": times}
    fired = [k for k, r in rules.items() if r["fires"]]
    if not fired:
        fired = ["ordinary"]
    out = []
    for k in fired:
        r = rules.get(k, {"rule": "none of the other eight regimes fired", "needs": [], "strength": 0.5})
        conf = round(max(0.2, min(0.95, 0.45 + 0.5 * float(r.get("strength") or 0.0))), 2) if k != "ordinary" else 0.6
        trace = "%s: %s" % (k, r["rule"]) + "; inputs " + ", ".join("%s=%s" % (n, ("%.4g" % f[n]) if f.get(n) is not None else "missing") for n in r.get("needs", []))
        rec = {"regime_id": k, "confidence": conf, "inputs": {n: f.get(n) for n in r.get("needs", [])}, "input_times": {x: times.get(x) for x in times}, "rule_trace": trace}
        out.append(rec)
    if persist:
        conn.execute("UPDATE market_regimes SET active=0 WHERE active=1 AND (expiry<? OR regime_id NOT IN (%s))" % ",".join("?" * len(fired)), (iso(now),) + tuple(fired))
        for rec in out:
            prev = conn.execute("SELECT id,started FROM market_regimes WHERE regime_id=? AND active=1 AND expiry>=? ORDER BY id DESC LIMIT 1", (rec["regime_id"], iso(now))).fetchone()
            exp = iso(now + dt.timedelta(days=EXPIRY_DAYS))
            if prev:
                conn.execute("UPDATE market_regimes SET last_update=?,expiry=?,confidence=?,inputs_json=?,input_ts_json=?,rule_trace=? WHERE id=?", (iso(now), exp, rec["confidence"], json.dumps(rec["inputs"]), json.dumps(rec["input_times"]), rec["rule_trace"], prev["id"]))
            else:
                conn.execute("INSERT INTO market_regimes(regime_id,detected,inputs_json,input_ts_json,confidence,started,last_update,expiry,rule_trace,active) VALUES(?,?,?,?,?,?,?,?,?,1)",
                             (rec["regime_id"], iso(now), json.dumps(rec["inputs"]), json.dumps(rec["input_times"]), rec["confidence"], iso(now), iso(now), exp, rec["rule_trace"]))
    return {"detected": out, "status": "ok", "features": f, "input_times": times}


def active(conn, as_of=None) -> List[dict]:
    """The regimes that were active at `as_of` (detected by then and not yet expired): point-in-time for replays."""
    when = iso(as_of) if as_of and hasattr(as_of, "isoformat") else (as_of or iso(utcnow()))
    return [dict(r) for r in conn.execute("SELECT * FROM market_regimes WHERE detected<=? AND expiry>=? ORDER BY id", (when, when))]


def adjust(regimes: List[str], side: str, direction: float, confidence: float) -> dict:
    """What the active regimes would change for an idea, and why. Direction is NEVER changed. A strong company-specific signal (|direction| x confidence >= 0.60) is protected: size cannot fall below 0.80 and the
    confidence bar cannot rise more than 0.05, and the record says so."""
    ids = [r for r in regimes if r in ADJUST] or ["ordinary"]
    long_ = side != "SHORT"
    size = mag = hold = 1.0
    conf_add = liq = 0.0
    for r in ids:
        a = ADJUST[r]
        size *= a[0] if long_ else a[1]
        mag *= a[2]
        conf_add += a[3]
        liq += a[4]
        hold *= a[5]
    strong = abs(direction or 0.0) * float(confidence or 0.0) >= STRONG_SIGNAL
    notes = []
    if strong:
        if size < 0.80:
            notes.append("strong company-specific signal: size reduction limited to 0.80 (was %.2f)" % size)
            size = 0.80
        if conf_add > 0.05:
            notes.append("strong company-specific signal: confidence bar rise limited to 0.05 (was %.2f)" % conf_add)
            conf_add = 0.05
    return {"regimes": ids, "size_mult": round(max(0.5, size), 3), "magnitude_mult": round(mag, 3), "confidence_add": round(conf_add, 3), "liquidity_penalty": round(min(0.3, liq), 3), "holding_mult": round(hold, 3),
            "direction_changed": False, "protected_strong_signal": strong, "notes": notes, "mode_effect": "recorded only (observe)"}
