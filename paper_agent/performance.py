"""Insights on every trade: what worked, what did not, and why.

Numbers come from the stored records only. A closed position's own `cost` is zeroed when it closes, so the ORIGINAL size comes from its decision; `pnl` already includes any
partial sells. The verdict is deliberately cautious about small samples: ~20 trades is the minimum before win rates mean anything."""
from __future__ import annotations

import json
import math
from typing import Callable, Dict, List, Optional

from .timeutil import NY, parse_ts, to_nz, trading_days_between

EXIT_LABEL = {"STOP_REACHED": "Stop loss hit", "RUNNER_STOP": "Runner stopped out", "TARGET_REACHED": "Target reached (partial or full)", "RUNNER_TARGET": "Runner target reached",
              "TIME_EXPIRED": "Time limit (went nowhere)", "RUNNER_TIME": "Runner time limit", "THESIS_INVALIDATED": "Thesis broke", "RISK_ESCALATION": "Risk escalated",
              "SUPERIOR_OPPORTUNITY": "Sold to fund a better idea", "EOD_FLATTEN": "Sold before the close (day-trade mode)"}
HOLD_BUCKETS = ["same day", "1 night", "2 nights", "3+ nights"]


def closed_trades(conn) -> List[dict]:
    out = []
    rows = conn.execute("SELECT p.id pid,p.ticker,p.opened,p.closed,p.entry_price,p.exit_price,p.pnl,p.review_code,p.decision_id,d.amount,d.shares dshares,d.reason,d.thesis_key,d.ev_net "
                        "FROM positions p LEFT JOIN decisions d ON d.id=p.decision_id WHERE p.status='CLOSED' AND p.closed IS NOT NULL ORDER BY p.closed, p.id").fetchall()
    for r in rows:
        try:
            rj = json.loads(r["reason"] or "{}")
            et = (rj.get("candidate") or {}).get("event_type") if isinstance(rj, dict) else None
        except (ValueError, TypeError):
            et = None
        basis = r["amount"] or ((r["dshares"] or 0) * (r["entry_price"] or 0)) or None
        pnl = float(r["pnl"] or 0.0)
        o, c = parse_ts(r["opened"]), parse_ts(r["closed"])
        nights = max(0, (c.astimezone(NY).date() - o.astimezone(NY).date()).days) if o and c else 0
        parts = conn.execute("SELECT COUNT(*) FROM virtual_exit_actions WHERE position_id=? AND status='COMMITTED'", (r["pid"],)).fetchone()[0]
        out.append({"position_id": r["pid"], "decision_id": r["decision_id"], "ticker": r["ticker"], "opened": r["opened"], "closed": r["closed"], "entry": r["entry_price"], "exit": r["exit_price"],
                    "pnl": pnl, "basis": basis, "ret": (pnl / basis) if basis else None, "exit_reason": r["review_code"] or "UNKNOWN", "event_type": et or "unknown", "thesis": r["thesis_key"],
                    "hold_hours": ((c - o).total_seconds() / 3600.0) if o and c else None, "hold_bucket": HOLD_BUCKETS[min(3, nights)], "open_hour_ny": o.astimezone(NY).hour if o else None,
                    "partial": parts > 1, "ev_net": r["ev_net"]})
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def summarize(trades: List[dict]) -> dict:
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_win, gross_loss = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    aw, al = _mean([t["ret"] for t in wins]), _mean([t["ret"] for t in losses])
    streak = best = 0
    for t in trades:
        streak = streak + 1 if t["pnl"] < 0 else 0
        best = max(best, streak)
    return {"n": n, "wins": len(wins), "losses": len(losses), "flat": n - len(wins) - len(losses), "win_rate": (len(wins) / n) if n else None, "total_pnl": sum(t["pnl"] for t in trades),
            "gross_win": gross_win, "gross_loss": gross_loss, "profit_factor": (gross_win / gross_loss) if gross_loss else (math.inf if gross_win else None),
            "avg_win_pct": aw, "avg_loss_pct": al, "payoff": (aw / abs(al)) if aw is not None and al else None, "expectancy_pct": _mean([t["ret"] for t in trades]),
            "expectancy_usd": (sum(t["pnl"] for t in trades) / n) if n else None, "avg_hold_hours": _mean([t["hold_hours"] for t in trades]),
            "best": max(trades, key=lambda t: t["pnl"]) if n else None, "worst": min(trades, key=lambda t: t["pnl"]) if n else None, "max_losing_streak": best}


def group(trades: List[dict], key: Callable[[dict], object], order: Optional[List] = None) -> List[dict]:
    buckets: Dict[object, List[dict]] = {}
    for t in trades:
        buckets.setdefault(key(t), []).append(t)
    rows = []
    for k, ts in buckets.items():
        s = summarize(ts)
        rows.append({"key": k, "n": s["n"], "win_rate": s["win_rate"], "total_pnl": s["total_pnl"], "avg_ret": s["expectancy_pct"], "avg_hold_hours": s["avg_hold_hours"]})
    if order:
        rows.sort(key=lambda r: order.index(r["key"]) if r["key"] in order else 99)
    else:
        rows.sort(key=lambda r: -r["total_pnl"])
    return rows


def by_day(trades: List[dict]) -> List[dict]:
    d: Dict[str, dict] = {}
    for t in trades:
        day = to_nz(t["closed"], "%Y-%m-%d")
        x = d.setdefault(day, {"day": day, "pnl": 0.0, "n": 0, "wins": 0})
        x["pnl"] += t["pnl"]
        x["n"] += 1
        x["wins"] += 1 if t["pnl"] > 0 else 0
    return [d[k] for k in sorted(d)]


def cumulative(trades: List[dict]) -> List[float]:
    acc, out = 0.0, []
    for t in trades:
        acc += t["pnl"]
        out.append(acc)
    return out


def verdict(s: dict, by_exit: List[dict]) -> dict:
    """{'level': 'early'|'good'|'marginal'|'bad', 'headline': str, 'points': [str]} in plain words, honest about sample size."""
    n = s["n"]
    if n == 0:
        return {"level": "early", "headline": "No closed trades yet.", "points": ["Results appear here as positions are sold. Nothing can be concluded before then."]}
    pts: List[str] = []
    share = {r["key"]: r["n"] / n for r in by_exit}
    if share.get("STOP_REACHED", 0) + share.get("RUNNER_STOP", 0) > 0.6:
        pts.append("%.0f%% of trades ended at a stop. If this stays high with a low win rate, the stops may be too tight for these stocks (Settings -> Swing strategy: stop multiple / minimum stop)." % ((share.get("STOP_REACHED", 0) + share.get("RUNNER_STOP", 0)) * 100))
    if share.get("TIME_EXPIRED", 0) > 0.3:
        pts.append("%.0f%% of trades hit the time limit without reaching target or stop: ideas that go nowhere. Raise the minimum edge or shorten the hold." % (share["TIME_EXPIRED"] * 100))
    if s["win_rate"] is not None and s["payoff"] is not None and s["win_rate"] < 0.4 and s["payoff"] < 1.5:
        pts.append("Win rate %.0f%% with average win only %.1fx the average loss: the maths needs a higher win rate or bigger winners." % (s["win_rate"] * 100, s["payoff"]))
    if s["max_losing_streak"] >= 5:
        pts.append("Longest losing streak: %d. Check the Decisions -> Why pages for those trades: is one kind of news misleading the system?" % s["max_losing_streak"])
    if n < 20:
        return {"level": "early", "headline": "Too early to judge: %d closed trade%s." % (n, "" if n == 1 else "s"),
                "points": ["Fewer than ~20 trades is mostly luck, good or bad. Keep these numbers in view, but do not change settings because of them yet."] + pts}
    pf, ex = s["profit_factor"], s["expectancy_pct"] or 0.0
    if pf is not None and pf >= 1.3 and ex > 0:
        lvl, head = "good", "Working so far: profit factor %s, average trade %+.2f%%." % ("%.2f" % pf if pf != math.inf else "n/a (no losses)", ex * 100)
    elif pf is not None and pf >= 1.0:
        lvl, head = "marginal", "Marginal: profit factor %.2f. Costs and slippage can erase an edge this thin." % pf
    else:
        lvl, head = "bad", "Losing: profit factor %s, average trade %+.2f%%." % ("%.2f" % pf if pf is not None else "n/a", ex * 100)
    return {"level": lvl, "headline": head, "points": pts or ["Nothing stands out. Keep watching the exit-reason table."]}


def svg_line(values: List[float], w: int = 720, h: int = 150, zero: bool = True) -> str:
    if len(values) < 2:
        return "<p class=mut>Not enough data for a chart yet.</p>"
    lo, hi = min(values + ([0.0] if zero else [])), max(values + ([0.0] if zero else []))
    span = (hi - lo) or 1.0
    pts = " ".join("%.1f,%.1f" % (10 + i * (w - 20) / (len(values) - 1), h - 10 - (v - lo) / span * (h - 20)) for i, v in enumerate(values))
    zy = h - 10 - (0 - lo) / span * (h - 20)
    colour = "var(--ok)" if values[-1] >= 0 else "var(--bad)"
    return ("<svg viewBox='0 0 %d %d' width='100%%' style='max-width:%dpx;background:var(--card);border-radius:6px'><line x1=10 x2=%d y1=%.1f y2=%.1f stroke='var(--line)' stroke-dasharray='4 3'/>"
            "<polyline fill=none stroke='%s' stroke-width=2 points='%s'/><text x=12 y=14 font-size=11 fill='var(--mut)'>high %s</text><text x=12 y=%d font-size=11 fill='var(--mut)'>low %s</text></svg>"
            % (w, h, w, w - 10, zy, zy, colour, pts, "{:,.0f}".format(hi), h - 3, "{:,.0f}".format(lo)))


def svg_bars(values: List[float], w: int = 720, h: int = 110) -> str:
    if not values:
        return "<p class=mut>No closed trades yet.</p>"
    m = max(abs(v) for v in values) or 1.0
    bw = max(3.0, min(40.0, (w - 20) / len(values) - 3))
    mid = h / 2
    bars = "".join("<rect x=%.1f y=%.1f width=%.1f height=%.1f fill='%s'/>" % (10 + i * (bw + 3), mid - max(v, 0) / m * (mid - 8), bw, max(1.0, abs(v) / m * (mid - 8)), "var(--ok)" if v >= 0 else "var(--bad)")
                   for i, v in enumerate(values[-int((w - 20) / (bw + 3)):]))
    return "<svg viewBox='0 0 %d %d' width='100%%' style='max-width:%dpx;background:var(--card);border-radius:6px'><line x1=10 x2=%d y1=%.0f y2=%.0f stroke='var(--line)'/>%s</svg>" % (w, h, w, w - 10, mid, mid, bars)
