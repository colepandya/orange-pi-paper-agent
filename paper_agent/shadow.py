"""The shadow book. Every decision the bot takes, traded or not, is followed to a COSTED outcome, so a gate or a threshold can be judged on hundreds of observations instead of the dozen trades that actually happened.

It is observation only: it READS the decisions table and the stored daily bars and writes its own tables; nothing it computes is ever fed back into a trade. What it can and cannot say is stated in CAVEATS and printed with
every report: daily bars only (no intraday path), a flat cost model, no market impact, one week of history at first, and many slices tested at once.

Conventions (fixed, written down before any result was looked at):
  * The idea's SIDE is the sign of its direction (long if positive); ideas with |direction| below ``shadow.min_abs_direction`` carry no view and are not followed.
  * ENTRY: the decision-time reference price if the regular session was open; the same day's open if it was decided before the open on a trading day; otherwise the next trading day's open.
  * HORIZON h = the close of the h-th session counting the entry day as session 1 (h=1 is the entry day's close).
  * COST: ``shadow.cost_bps`` round trip, taken off every outcome (net = gross - cost).
  * Only COMPLETED days count: a day counts once it is past 16:30 New York time.
  * One OBSERVATION per stock, entry day and side: several ideas about the same stock on the same day are one bet, not several independent ones.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
import re
from typing import Dict, List, Optional, Tuple

from . import timeutil
from .timeutil import NY, iso, parse_ts, utcnow

HORIZONS = (1, 3, 5, 10)
SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_candidates(
 decision_id INTEGER PRIMARY KEY, event_id INTEGER, ticker TEXT NOT NULL, side TEXT NOT NULL, decided_at TEXT NOT NULL, status TEXT, gate TEXT, gate_text TEXT,
 event_type TEXT, origin TEXT, source TEXT, thesis_key TEXT, regime TEXT, sector TEXT, conf REAL, direction REAL, magnitude REAL, ev_net REAL, forecast_conf REAL, score REAL, size_pct REAL,
 ref_price REAL, entry_day TEXT, entry_price REAL, entry_basis TEXT, cluster TEXT, created TEXT);
CREATE INDEX IF NOT EXISTS shadow_c_gate ON shadow_candidates(gate);
CREATE INDEX IF NOT EXISTS shadow_c_ticker ON shadow_candidates(ticker, entry_day);
CREATE TABLE IF NOT EXISTS shadow_outcomes(
 decision_id INTEGER NOT NULL, horizon INTEGER NOT NULL, exit_day TEXT, exit_price REAL, gross_ret REAL, net_ret REAL, bench_ret REAL, excess_net REAL, mfe REAL, mae REAL, measured TEXT,
 PRIMARY KEY(decision_id, horizon));
"""

CAVEATS = ("Daily bars only: an intraday stop or target is not simulated. A flat cost per round trip, no market impact. Outcomes are hypothetical: nobody traded them. One observation per stock-day-side. "
           "Many gates and event types are compared at once, so some will look special by chance: the intervals below are not corrected for that. Treat 'review candidate' as a question to investigate, not a verdict.")

GATE_RULES: List[Tuple[str, str]] = [
    ("monitor_conflict", r"position monitor would close"), ("market_shock", r"market-wide shock"), ("after_hours_retry", r"market session"), ("long_only_book", r"long-only book"),
    ("short_minimums", r"short minimum"), ("reward_risk", r"reward:risk"), ("eligibility", r"eligibility"), ("ev_below_minimum", r"EV net"), ("liquidity_adv", r"ADV|trades \$"),
    ("duplicate", r"already holding|same bet|duplicate"), ("concentration", r"concentrat|exposure|limit"), ("quote", r"quote|no price")]


def ensure(conn) -> None:
    for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
        conn.execute(stmt)


def classify_gate(status: str, gates: List[str]) -> Tuple[str, str]:
    """(category, full text). EXECUTED is TRADED; a veto or an expiry is its own category; otherwise the FIRST gate text decides."""
    text = " | ".join(g for g in (gates or []) if isinstance(g, str))
    if status == "EXECUTED":
        return "traded", text
    if status == "VETOED":
        return "veto", text
    if status == "EXPIRED":
        return "expired", text
    if status == "DUPLICATE":
        return "duplicate", text
    first = next((g for g in (gates or []) if isinstance(g, str)), "")
    for name, pat in GATE_RULES:
        if re.search(pat, first, re.I):
            return name, text
    return ("risk_rejected" if status == "RISK_REJECTED" else "other"), text


def _side(direction, min_abs: float) -> Optional[str]:
    try:
        d = float(direction)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(d) or abs(d) < min_abs:
        return None
    return "LONG" if d > 0 else "SHORT"


def _next_trading_day(d: dt.date) -> dt.date:
    d = d + dt.timedelta(days=1)
    while not timeutil.is_trading_day(d):
        d += dt.timedelta(days=1)
    return d


def entry_day_and_basis(decided: dt.datetime) -> Tuple[dt.date, str]:
    """The day the position would have been entered and how: 'decision_price' (market open when decided), 'open' (before the open on a trading day) or 'next_open'."""
    ny = decided.astimezone(NY)
    d = ny.date()
    if timeutil.is_trading_day(d):
        if timeutil.market_state(decided).get("session") == "REGULAR":
            return d, "decision_price"
        if ny.time() < dt.time(9, 30):
            return d, "open"
    return _next_trading_day(d), "next_open"


def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _capture_one(conn, r, min_abs: float, now) -> bool:
    try:
        rs = _dict(json.loads(r["reason"] or "{}"))
    except (ValueError, TypeError):
        rs = {}
    cand, fc = _dict(rs.get("candidate")), _dict(rs.get("forecast"))
    gates = rs.get("gates") if isinstance(rs.get("gates"), list) else []
    side = _side(cand.get("direction"), min_abs)
    if side is None or not r["ticker"] or not isinstance(r["ticker"], str):
        return False
    decided = parse_ts(rs.get("decided_at") or r["created"])
    if decided is None:
        return False
    gate, text = classify_gate(r["status"], gates)
    sector = conn.execute("SELECT sector FROM kb_entities e JOIN kb_identifiers i ON i.entity_id=e.entity_id WHERE i.kind='ticker' AND i.value=? AND i.valid_to IS NULL LIMIT 1", (r["ticker"],)).fetchone() if _has(conn, "kb_entities") else None
    regime = conn.execute("SELECT regime_id FROM market_regimes WHERE detected<=? ORDER BY detected DESC LIMIT 1", (iso(decided),)).fetchone() if _has(conn, "market_regimes") else None
    conn.execute("INSERT OR IGNORE INTO shadow_candidates(decision_id,event_id,ticker,side,decided_at,status,gate,gate_text,event_type,origin,source,thesis_key,regime,sector,conf,direction,magnitude,ev_net,forecast_conf,score,size_pct,ref_price,created) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (r["id"], r["event_id"], r["ticker"], side, iso(decided), r["status"], gate, text[:400], _s(cand.get("event_type")), _s(cand.get("origin") or r["origin"]), _s(cand.get("source")), _s(cand.get("thesis_key") or r["thesis_key"]),
                  regime[0] if regime else "unknown", sector[0] if sector else None, _n(cand.get("conf")), _n(cand.get("direction")), _n(cand.get("magnitude")), _n(r["ev_net"] if r["ev_net"] is not None else fc.get("ev_net")),
                  _n(fc.get("confidence")), _n(r["score"]), _n(r["size_pct"]), _n(rs.get("ref_price")), iso(now)))
    return True


def _n(x):
    """A finite number or None: junk and non-finite values are stored as 'unknown', never as a number."""
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def _s(x):
    return x if isinstance(x, str) else None


def sync_candidates(conn, cfg: dict, now=None, limit: int = 3000, clock=None, budget_s: float = 3.0) -> dict:
    """Copy new decisions into the shadow book (idempotent) and refresh the status of recent ones (an order waiting for the market becomes executed or expired)."""
    import time as _t
    ensure(conn)
    sc = cfg.get("shadow") or {}
    min_abs = float(sc.get("min_abs_direction", 0.15))
    now = now or utcnow()
    clock = clock or _t.monotonic
    t0 = clock()
    cur = conn.execute("SELECT value FROM meta WHERE key='shadow_last_decision_id'").fetchone()
    last = int(cur[0]) if cur else 0
    rows = conn.execute("SELECT id,event_id,ticker,action,status,created,score,ev_net,size_pct,thesis_key,origin,reason FROM decisions WHERE id>? ORDER BY id LIMIT ?", (last, int(limit))).fetchall()
    added = skipped = 0
    top = last
    for r in rows:
        if clock() - t0 > budget_s:                                                      # a long history is taken a bounded bite at a time; the cursor stays behind the rows not yet done
            break
        top = max(top, r["id"])
        try:
            if _capture_one(conn, r, min_abs, now):
                added += 1
            else:
                skipped += 1
        except Exception:  # noqa: BLE001 - one malformed decision must not stop the capture of the others, nor leave the cursor stuck behind it
            skipped += 1
    if top > last:
        conn.execute("INSERT OR REPLACE INTO meta VALUES('shadow_last_decision_id',?)", (str(top),))
    refreshed = 0
    for r in conn.execute("SELECT d.id,d.status,d.reason FROM decisions d JOIN shadow_candidates s ON s.decision_id=d.id WHERE d.created>=? AND d.status<>s.status", (iso(now - dt.timedelta(days=5)),)).fetchall():
        try:
            gates = _dict(json.loads(r["reason"] or "{}")).get("gates")
        except (ValueError, TypeError):
            gates = []
        gates = gates if isinstance(gates, list) else []
        gate, text = classify_gate(r["status"], gates)
        conn.execute("UPDATE shadow_candidates SET status=?,gate=?,gate_text=? WHERE decision_id=?", (r["status"], gate, text[:400], r["id"]))
        refreshed += 1
    return {"added": added, "skipped_no_view": skipped, "status_refreshed": refreshed, "cursor": top}


def _has(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _bars(conn, ticker: str, from_day: str, n: int = 14) -> List[dict]:
    return [dict(r) for r in conn.execute("SELECT day,open,high,low,close FROM market_series WHERE symbol=? AND day>=? ORDER BY day LIMIT ?", (ticker, from_day, n)).fetchall()]


def _ok(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def _measure_one(conn, c, now, cost: float, last_complete: str) -> str:
    """'measured' | 'pending' | 'no_prices' for one candidate. Raises nothing the caller cannot isolate."""
    decided = parse_ts(c["decided_at"])
    if decided is None:
        return "no_prices"
    eday, basis = entry_day_and_basis(decided)
    bars = _bars(conn, c["ticker"], eday.isoformat(), 12)
    if not bars or bars[0]["day"] != eday.isoformat():
        return "no_prices" if decided < now - dt.timedelta(days=6) else "pending"
    b0 = bars[0]
    spy = {b["day"]: b for b in _bars(conn, "SPY", eday.isoformat(), 12)}                      # the market over the same window: outcomes are judged against it as well as in absolute terms
    spy_entry = ((spy[eday.isoformat()]["close"] if basis == "decision_price" else spy[eday.isoformat()]["open"]) if eday.isoformat() in spy else None)
    ref = c["ref_price"]
    entry = ref if basis == "decision_price" and _ok(ref) else (b0["open"] if basis != "decision_price" else b0["close"])
    if not _ok(entry):
        return "no_prices"
    sg = 1.0 if c["side"] == "LONG" else -1.0
    conn.execute("UPDATE shadow_candidates SET entry_day=?,entry_price=?,entry_basis=?,cluster=? WHERE decision_id=?", (eday.isoformat(), entry, basis, "%s|%s|%s" % (c["ticker"], eday.isoformat(), c["side"]), c["decision_id"]))
    any_new = False
    for h in HORIZONS:
        if len(bars) < h:
            continue
        bx = bars[h - 1]
        if bx["day"] > last_complete:                                                # an unfinished day is never used
            continue
        window = bars[(1 if basis == "decision_price" else 0):h]                      # a mid-day entry cannot claim the part of that day that came before it
        if not _ok(bx["close"]) or not all(_ok(b["high"]) and _ok(b["low"]) for b in window):
            continue                                                                  # a bar with a missing or impossible price is never turned into a number
        hi = max((b["high"] for b in window), default=None)
        lo = min((b["low"] for b in window), default=None)
        mfe = (sg * (hi / entry - 1.0) if sg > 0 else sg * (lo / entry - 1.0)) if hi and lo else None
        mae = (sg * (lo / entry - 1.0) if sg > 0 else sg * (hi / entry - 1.0)) if hi and lo else None
        gross = sg * (bx["close"] / entry - 1.0)
        bench = (spy[bx["day"]]["close"] / spy_entry - 1.0) if (_ok(spy_entry) and bx["day"] in spy and _ok(spy[bx["day"]]["close"])) else None
        excess = (sg * ((bx["close"] / entry - 1.0) - bench) - cost) if bench is not None else None          # a long beats the market by rising more than it; a short by falling more
        conn.execute("INSERT OR IGNORE INTO shadow_outcomes(decision_id,horizon,exit_day,exit_price,gross_ret,net_ret,bench_ret,excess_net,mfe,mae,measured) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (c["decision_id"], h, bx["day"], bx["close"], gross, gross - cost, bench, excess, mfe, mae, iso(now)))
        any_new = True
    return "measured" if any_new else "pending"


def measure_due(conn, cfg: dict, now=None, limit: int = 300, clock=None, budget_s: float = 3.0) -> dict:
    """Fill in the outcomes that can now be measured (a completed day exists for the horizon). Re-runnable; each (decision, horizon) is written once. One bad candidate is counted and skipped."""
    import time as _t
    ensure(conn)
    now = now or utcnow()
    clock = clock or _t.monotonic
    t0 = clock()
    cost = float((cfg.get("shadow") or {}).get("cost_bps", 30.0)) / 1e4
    ny_now = now.astimezone(NY)
    last_complete = (ny_now.date() if ny_now.time() >= dt.time(16, 30) else ny_now.date() - dt.timedelta(days=1)).isoformat()          # a day counts once the session is over
    todo = conn.execute("SELECT * FROM shadow_candidates WHERE decision_id NOT IN (SELECT decision_id FROM shadow_outcomes WHERE horizon=?) ORDER BY decision_id LIMIT ?", (HORIZONS[-1], int(limit) * 4)).fetchall()
    res = {"measured": 0, "pending_prices": 0, "no_prices": 0, "errors": 0}
    for c in todo:
        if clock() - t0 > budget_s or res["measured"] >= limit:
            break
        try:
            r = _measure_one(conn, c, now, cost, last_complete)
        except Exception:  # noqa: BLE001
            res["errors"] += 1
            continue
        res[{"measured": "measured", "pending": "pending_prices", "no_prices": "no_prices"}[r]] += 1
    res["seconds"] = round(clock() - t0, 2)
    return res


def run(conn, cfg: dict, now=None) -> dict:
    """The per-cycle step: bounded, isolated, and never raises into a trade."""
    sc = cfg.get("shadow") or {}
    if not sc.get("enabled", True):
        return {"status": "disabled"}
    out: dict = {"status": "ok"}
    try:
        out["sync"] = sync_candidates(conn, cfg, now, budget_s=float(sc.get("max_seconds", 3.0)))
        out["measure"] = measure_due(conn, cfg, now, int(sc.get("measure_per_cycle", 300)), budget_s=float(sc.get("max_seconds", 3.0)))
        conn.commit()
    except Exception as ex:  # noqa: BLE001
        out["status"] = "error"
        out["error"] = "%s: %s" % (type(ex).__name__, str(ex)[:120])
    return out


# ---------------------------------------------------------------------------------------------------------------- statistics
def _rank(xs: List[float]) -> List[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return r


def spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    a, b = _rank(xs), _rank(ys)
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    return None if sa == 0 or sb == 0 else sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)


def bootstrap_ci(xs: List[float], seed: int = 49, n: int = 1000) -> Tuple[Optional[float], Optional[float]]:
    if len(xs) < 5:
        return None, None
    rnd = random.Random(seed)
    k = len(xs)
    means = sorted(sum(xs[rnd.randrange(k)] for _ in range(k)) / k for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def perm_p(xs: List[float], ys: List[float], seed: int = 49, n: int = 1000) -> Optional[float]:
    rho = spearman(xs, ys)
    if rho is None:
        return None
    rnd = random.Random(seed)
    yy = list(ys)
    hit = 0
    for _ in range(n):
        rnd.shuffle(yy)
        r2 = spearman(xs, yy)
        if r2 is not None and abs(r2) >= abs(rho):
            hit += 1
    return (hit + 1) / (n + 1)


def maturity(n: int) -> str:
    return "insufficient" if n < 20 else "informational" if n < 50 else "preliminary" if n < 100 else "review candidate"


def _clusters(rows: List[dict], key: str = "net_ret") -> List[float]:
    """One observation per stock-day-side: the mean of the ideas that share it."""
    g: Dict[str, List[float]] = {}
    for r in rows:
        if r.get(key) is not None:
            g.setdefault(r["cluster"] or "%s|%s" % (r["ticker"], r["decision_id"]), []).append(r[key])
    return [sum(v) / len(v) for v in g.values()]


def _stats(rows: List[dict]) -> dict:
    net, gross, exc = _clusters(rows, "net_ret"), _clusters(rows, "gross_ret"), _clusters(rows, "excess_net")
    n = len(net)
    lo, hi = bootstrap_ci(net)
    elo, ehi = bootstrap_ci(exc)
    ne = len(exc)
    verdict = "insufficient evidence"
    if ne >= 30 and elo is not None:                                                  # judged against the MARKET: a week's drift must not look like skill
        verdict = "beat the market after costs: review the gate" if elo > 0 else "trailed the market after costs: the gate looks protective" if ehi < 0 else "no evidence either way"
    return {"ideas": len(rows), "observations": n, "mean_net_pct": round(100 * sum(net) / n, 3) if n else None, "mean_gross_pct": round(100 * sum(gross) / len(gross), 3) if gross else None,
            "hit_rate_net": round(sum(1 for x in net if x > 0) / n, 3) if n else None, "ci95_net_pct": [round(100 * lo, 3), round(100 * hi, 3)] if lo is not None else None,
            "mean_excess_pct": round(100 * sum(exc) / ne, 3) if ne else None, "ci95_excess_pct": [round(100 * elo, 3), round(100 * ehi, 3)] if elo is not None else None,
            "maturity": maturity(ne if ne else n), "verdict": verdict}


def report(conn, cfg: Optional[dict] = None, horizon: int = 3) -> dict:
    """Everything the dashboard and `shadow report` show. Pure read."""
    if not _has(conn, "shadow_candidates"):
        return {"coverage": {"candidates": 0}, "caveats": CAVEATS}
    rows = [dict(r) for r in conn.execute("SELECT c.*,o.net_ret,o.gross_ret,o.excess_net,o.bench_ret,o.mfe,o.mae FROM shadow_candidates c JOIN shadow_outcomes o ON o.decision_id=c.decision_id AND o.horizon=? WHERE c.cluster IS NOT NULL", (horizon,)).fetchall()]
    total = conn.execute("SELECT COUNT(*) FROM shadow_candidates").fetchone()[0]
    cov = {"candidates": total, "with_entry": conn.execute("SELECT COUNT(*) FROM shadow_candidates WHERE entry_day IS NOT NULL").fetchone()[0], "measured_by_horizon": {h: conn.execute("SELECT COUNT(*) FROM shadow_outcomes WHERE horizon=?", (h,)).fetchone()[0] for h in HORIZONS},
           "first_decision": (conn.execute("SELECT MIN(decided_at) FROM shadow_candidates").fetchone() or [None])[0], "last_decision": (conn.execute("SELECT MAX(decided_at) FROM shadow_candidates").fetchone() or [None])[0], "horizon_sessions": horizon,
           "cost_bps_round_trip": float(((cfg or {}).get("shadow") or {}).get("cost_bps", 30.0))}
    by_gate: Dict[str, dict] = {}
    for g in sorted({r["gate"] for r in rows}):
        by_gate[g] = _stats([r for r in rows if r["gate"] == g])
    by_type: Dict[str, dict] = {}
    allnet = _clusters(rows)
    grand = sum(allnet) / len(allnet) if allnet else 0.0
    for t in sorted({r["event_type"] or "unknown" for r in rows}):
        s = _stats([r for r in rows if (r["event_type"] or "unknown") == t])
        n = s["observations"]
        s["shrunk_mean_net_pct"] = round(100 * ((n * ((s["mean_net_pct"] or 0) / 100) + 30 * grand) / (n + 30)), 3) if n else None      # pulled toward the overall mean: a small sample is not a finding
        by_type[t] = s
    ev_rows = [r for r in rows if r["ev_net"] is not None]
    cal: dict = {"n": len(ev_rows)}
    if len(ev_rows) >= 20:
        xs, ys = [r["ev_net"] for r in ev_rows], [r["net_ret"] for r in ev_rows]
        order = sorted(range(len(ev_rows)), key=lambda i: xs[i])
        q = max(1, len(order) // 5)
        buckets = []
        for b in range(5):
            idx = order[b * q:(b + 1) * q] if b < 4 else order[4 * q:]
            if idx:
                buckets.append({"bucket": b + 1, "n": len(idx), "mean_ev": round(sum(xs[i] for i in idx) / len(idx), 4), "mean_net_pct": round(100 * sum(ys[i] for i in idx) / len(idx), 3)})
        ex_rows = [r for r in ev_rows if r.get("excess_net") is not None]
        cal.update({"spearman_rho_excess": None if len(ex_rows) < 20 or spearman([r["ev_net"] for r in ex_rows], [r["excess_net"] for r in ex_rows]) is None else round(spearman([r["ev_net"] for r in ex_rows], [r["excess_net"] for r in ex_rows]), 3),
                    "spearman_rho": None if spearman(xs, ys) is None else round(spearman(xs, ys), 3), "permutation_p": perm_p(xs, ys), "quintiles": buckets,
                    "reading": "higher estimated EV should mean higher realised return: quintile 5 above quintile 1 and a positive rho. A rho near zero means the EV estimate does not rank ideas."})
    by_side = {sd: _stats([r for r in rows if r["side"] == sd]) for sd in ("LONG", "SHORT")}
    bench = [r["bench_ret"] for r in rows if r.get("bench_ret") is not None]
    traded = _stats([r for r in rows if r["gate"] == "traded"])
    blocked = _stats([r for r in rows if r["gate"] != "traded"])
    return {"coverage": cov, "overall": {"traded": traded, "not_traded": blocked}, "by_gate": by_gate, "by_event_type": by_type, "ev_calibration": cal, "by_side": by_side, "market_move_pct": round(100 * sum(bench) / len(bench), 3) if bench else None, "caveats": CAVEATS, "gates_compared": len(by_gate), "event_types_compared": len(by_type)}


def format_report(rep: dict) -> str:
    cov = rep.get("coverage") or {}
    L = ["SHADOW BOOK (observation only: nothing here changes a trade)", ""]
    L.append("candidates followed: %s | with an entry price: %s | outcomes measured by horizon %s" % (cov.get("candidates"), cov.get("with_entry"), cov.get("measured_by_horizon")))
    L.append("decisions from %s to %s | horizon shown: %s sessions | cost taken off every outcome: %s bps round trip" % (cov.get("first_decision"), cov.get("last_decision"), cov.get("horizon_sessions"), cov.get("cost_bps_round_trip")))
    ov = rep.get("overall") or {}
    for k in ("traded", "not_traded"):
        s = ov.get(k)
        if s:
            L.append("\n%-11s observations %4s | mean net %s%% | hit rate %s | 95%% interval %s | %s" % (k.replace("_", " ").upper(), s["observations"], s["mean_net_pct"], s["hit_rate_net"], s["ci95_net_pct"], s["maturity"]))
    sd = rep.get("by_side") or {}
    L.append("\nLONG vs SHORT ideas (market move over the same windows: %s%%)" % rep.get("market_move_pct"))
    for k in ("LONG", "SHORT"):
        if sd.get(k):
            s = sd[k]
            L.append("  %-6s obs %4d | net %7s%% | vs market %7s%% (95%% %s) | hit %s" % (k, s["observations"], s["mean_net_pct"], s["mean_excess_pct"], s["ci95_excess_pct"], s["hit_rate_net"]))
    L.append("\nBY GATE (why the idea did or did not trade) - the question is whether what a gate BLOCKED would have earned money after costs")
    for g, s in sorted((rep.get("by_gate") or {}).items(), key=lambda kv: -kv[1]["observations"]):
        L.append("  %-18s obs %4d | net %7s%% | vs market %7s%% (95%% %-17s) | %-16s | %s" % (g, s["observations"], s["mean_net_pct"], s["mean_excess_pct"], s["ci95_excess_pct"], s["maturity"], s["verdict"]))
    cal = rep.get("ev_calibration") or {}
    L.append("\nDOES HIGHER ESTIMATED EV EARN MORE? (n=%s)" % cal.get("n"))
    if cal.get("quintiles"):
        for b in cal["quintiles"]:
            L.append("  quintile %d  n=%3d  mean EV %7.4f  realised net %7s%%" % (b["bucket"], b["n"], b["mean_ev"], b["mean_net_pct"]))
        L.append("  rank correlation %s (permutation p %s); against the market-adjusted outcome %s. %s" % (cal.get("spearman_rho"), cal.get("permutation_p"), cal.get("spearman_rho_excess"), cal.get("reading")))
    else:
        L.append("  not enough measured ideas yet")
    L.append("\nBY EVENT TYPE (shrunk mean pulls small samples toward the overall mean)")
    for t, s in sorted((rep.get("by_event_type") or {}).items(), key=lambda kv: -kv[1]["observations"])[:14]:
        L.append("  %-22s obs %4d | net %7s%% | shrunk %7s%% | hit %-5s | %s" % (t, s["observations"], s["mean_net_pct"], s.get("shrunk_mean_net_pct"), s["hit_rate_net"], s["maturity"]))
    L.append("\nREAD THIS FIRST: " + rep.get("caveats", CAVEATS))
    L.append("%d gates and %d event types were compared at once." % (rep.get("gates_compared", 0), rep.get("event_types_compared", 0)))
    return "\n".join(L)


def run_cli(args, conn, cfg: dict) -> int:
    what = getattr(args, "what", None) or "report"
    now = utcnow()
    if what == "sync":
        print(json.dumps(sync_candidates(conn, cfg, now), indent=1))
        return 0
    if what == "measure":
        print(json.dumps(measure_due(conn, cfg, now, limit=100000, budget_s=120.0), indent=1))
        return 0
    if what == "status":
        print(json.dumps(report(conn, cfg, int(getattr(args, "horizon", 3) or 3))["coverage"], indent=1, default=str))
        return 0
    rep = report(conn, cfg, int(getattr(args, "horizon", 3) or 3))
    print(json.dumps(rep, indent=1, default=str) if getattr(args, "json", False) else format_report(rep))
    return 0
