"""Momentum core: a long-only, monthly-rebalanced sleeve with capped leverage and a drawdown brake.

Why it exists: on 5 years of stored prices, news-reaction trading had no edge after costs, while 12-1 momentum (buy the strongest stocks by return over months 12 to 2) was the only rule that
paid net of a 0.3% cost in 4 of 5 years. This sleeve runs that rule as its own virtual account so the live result is measured, separate from the news engine's ledger.

Everything is deterministic and uses only the daily bars already in market_series, only up to the latest stored day (no future data). Standard library only.
"""
from __future__ import annotations

import json
import math
from typing import Dict, List, Optional

from .timeutil import iso

DEFAULTS = {"enabled": True, "names": 50, "leverage": 2.0, "start_capital": 1_000_000.0, "lookback_days": 252, "skip_days": 21, "rebalance_days": 21,
            "min_price": 5.0, "min_adv_usd": 2_000_000.0, "max_stale_days": 6, "finance_rate": 0.05, "cost_pct": 0.0015,
            "brake_soft_dd": 0.25, "brake_soft_clear_dd": 0.15, "brake_hard_dd": 0.40, "hard_pause_days": 21}

DDL = ["CREATE TABLE IF NOT EXISTS momentum_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
       "CREATE TABLE IF NOT EXISTS momentum_holdings(ticker TEXT PRIMARY KEY,shares REAL NOT NULL,entry_price REAL NOT NULL,opened TEXT NOT NULL,score REAL)",
       "CREATE TABLE IF NOT EXISTS momentum_nav(day TEXT PRIMARY KEY,nav REAL NOT NULL,cash REAL NOT NULL,gross REAL NOT NULL,leverage REAL NOT NULL,drawdown REAL NOT NULL,cap REAL NOT NULL,note TEXT)",
       "CREATE TABLE IF NOT EXISTS momentum_trades(id INTEGER PRIMARY KEY AUTOINCREMENT,day TEXT NOT NULL,ticker TEXT NOT NULL,side TEXT NOT NULL,shares REAL NOT NULL,price REAL NOT NULL,cost REAL NOT NULL,reason TEXT)"]


def settings(cfg: Optional[dict]) -> dict:
    s = dict(DEFAULTS)
    for k, v in ((cfg or {}).get("momentum") or {}).items():
        if k in s and v is not None:
            s[k] = type(DEFAULTS[k])(v) if not isinstance(DEFAULTS[k], bool) else bool(v)
    s["leverage"] = max(0.0, min(3.0, float(s["leverage"])))                                  # hard ceiling: above ~3x the backtest got worse, and 6x was ruined
    s["names"] = max(5, int(s["names"]))
    return s


def ensure(conn) -> None:
    for q in DDL:
        conn.execute(q)


def _get(conn, key, default=None):
    r = conn.execute("SELECT value FROM momentum_state WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def _put(conn, key, value) -> None:
    conn.execute("INSERT INTO momentum_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


def trading_days(conn) -> List[str]:
    """The stored trading calendar (SPY's days; any symbol's if SPY is missing)."""
    rows = conn.execute("SELECT day FROM market_series WHERE symbol='SPY' ORDER BY day").fetchall()
    if len(rows) < 30:
        rows = conn.execute("SELECT DISTINCT day FROM market_series ORDER BY day").fetchall()
    return [r[0] for r in rows]


def _excluded(conn) -> set:
    """Funds, ETFs, SPACs and preferred shares are not stocks with momentum to follow."""
    try:
        return {r[0] for r in conn.execute("SELECT i.value FROM kb_identifiers i JOIN kb_entities e ON e.entity_id=i.entity_id WHERE i.kind='ticker' AND e.entity_kind IN ('etf','fund','spac','preferred')")}
    except Exception:  # noqa: BLE001 - no knowledge base yet: nothing is excluded by name
        return set()


def rank(conn, cfg: dict, as_of_day: str) -> List[dict]:
    """12-1 momentum as of ``as_of_day`` (inclusive): return from lookback_days ago to skip_days ago, among liquid, fresh, non-fund stocks. Highest first."""
    s = settings(cfg)
    days = [d for d in trading_days(conn) if d <= as_of_day]
    lb, sk = int(s["lookback_days"]), int(s["skip_days"])
    if len(days) < lb + 5:
        return []
    d_now, d_skip, d_back = days[-1], days[-1 - sk], days[-1 - lb]
    stale_from = days[max(0, len(days) - 1 - int(s["max_stale_days"]))]
    skip = _excluded(conn) | {"SPY"}
    rows = conn.execute("SELECT symbol,day,close,volume FROM market_series WHERE day IN (?,?,?) AND close>0", (d_now, d_skip, d_back)).fetchall()
    px: Dict[str, dict] = {}
    for sym, day, close, vol in rows:
        px.setdefault(sym, {})[day] = (float(close), float(vol or 0))
    out = []
    for sym, p in px.items():
        if sym in skip or d_now not in p or d_skip not in p or d_back not in p:
            continue
        last, _v = p[d_now]
        if last < float(s["min_price"]):
            continue
        adv = conn.execute("SELECT AVG(close*volume) FROM (SELECT close,volume FROM market_series WHERE symbol=? AND day<=? ORDER BY day DESC LIMIT 20)", (sym, d_now)).fetchone()[0] or 0.0
        if adv < float(s["min_adv_usd"]):
            continue
        out.append({"ticker": sym, "score": p[d_skip][0] / p[d_back][0] - 1.0, "price": last})
    out.sort(key=lambda r: (-r["score"], r["ticker"]))
    return out


def _price(conn, ticker: str, day: str) -> Optional[float]:
    r = conn.execute("SELECT close FROM market_series WHERE symbol=? AND day<=? AND close>0 ORDER BY day DESC LIMIT 1", (ticker, day)).fetchone()
    return float(r[0]) if r else None


def _book(conn, day: str):
    cash = float(_get(conn, "cash", 0.0))
    hs = conn.execute("SELECT ticker,shares FROM momentum_holdings").fetchall()
    gross = 0.0
    for t, sh in hs:
        p = _price(conn, t, day) or 0.0
        gross += sh * p
    return cash, gross, hs


def cap_for(drawdown: float, state_cap: float, s: dict) -> float:
    """The brake. Soft: leverage drops to 1x once the sleeve is 25% below its peak and returns when within 15%. Hard: cash at 40% below peak (handled by the caller)."""
    if drawdown >= float(s["brake_hard_dd"]):
        return 0.0
    if drawdown >= float(s["brake_soft_dd"]):
        return min(1.0, float(s["leverage"]))
    if state_cap < float(s["leverage"]) and state_cap > 0 and drawdown > float(s["brake_soft_clear_dd"]):
        return state_cap
    return float(s["leverage"])


def _rebalance(conn, s: dict, day: str, nav: float, cap: float, reason: str) -> dict:
    cash, gross, hs = _book(conn, day)
    ranked = rank(conn, {"momentum": s}, day)
    target = ranked[: int(s["names"])]
    lev = cap
    want: Dict[str, float] = {}
    if target and lev > 0 and nav > 0:
        per = nav * lev * (1.0 - lev * float(s["cost_pct"])) / len(target)          # leave room for the trading cost so gross never exceeds the cap
        for r in target:
            want[r["ticker"]] = per / r["price"]
    have = {t: sh for t, sh in hs}
    cost_total, turnover = 0.0, 0.0
    for t in sorted(set(have) | set(want)):
        p = _price(conn, t, day)
        if not p:
            continue
        delta = want.get(t, 0.0) - have.get(t, 0.0)
        if abs(delta) * p < 1.0:
            continue
        value = abs(delta) * p
        cost = value * float(s["cost_pct"])
        cost_total += cost
        turnover += value
        cash += -delta * p - cost
        conn.execute("INSERT INTO momentum_trades(day,ticker,side,shares,price,cost,reason) VALUES(?,?,?,?,?,?,?)", (day, t, "BUY" if delta > 0 else "SELL", abs(delta), p, cost, reason))
        if want.get(t, 0.0) <= 0:
            conn.execute("DELETE FROM momentum_holdings WHERE ticker=?", (t,))
        else:
            sc = next((r["score"] for r in target if r["ticker"] == t), None)
            if t in have:
                conn.execute("UPDATE momentum_holdings SET shares=?,score=? WHERE ticker=?", (want[t], sc, t))
            else:
                conn.execute("INSERT INTO momentum_holdings(ticker,shares,entry_price,opened,score) VALUES(?,?,?,?,?)", (t, want[t], p, day, sc))
    _put(conn, "cash", cash)
    _put(conn, "last_rebalance", day)
    return {"names": len(want), "turnover": round(turnover, 2), "cost": round(cost_total, 2), "ranked": len(ranked)}


def run(conn, cfg: dict, now=None, as_of: Optional[str] = None) -> dict:
    """Bring the sleeve up to the latest stored day: charge financing, mark to market, apply the brake, rebalance when due. Idempotent: running twice on the same day changes nothing."""
    ensure(conn)
    s = settings(cfg)
    if not s["enabled"]:
        return {"status": "disabled"}
    days = [d for d in trading_days(conn) if as_of is None or d <= as_of]                # as_of: replay a past day (tests and the history check); never used in live running
    if not days:
        return {"status": "no_prices"}
    day = days[-1]
    if _get(conn, "last_day") == day:
        return {"status": "up_to_date", "day": day}
    if _get(conn, "cash") is None:
        _put(conn, "cash", float(s["start_capital"]))
        _put(conn, "peak", float(s["start_capital"]))
        _put(conn, "cap", float(s["leverage"]))
        _put(conn, "last_day", None)
    cash, gross, hs = _book(conn, day)
    # financing: interest on the borrowed part, for the trading days since the last run
    prev = _get(conn, "last_day")
    n_days = 1 if not prev else max(1, sum(1 for d in days if prev < d <= day))
    borrowed = max(0.0, -cash)
    cash -= borrowed * float(s["finance_rate"]) * n_days / 252.0
    _put(conn, "cash", cash)
    nav = cash + gross
    peak = max(float(_get(conn, "peak", nav)), nav)
    dd = 0.0 if peak <= 0 else max(0.0, 1.0 - nav / peak)
    cap_before = float(_get(conn, "cap", s["leverage"]))
    cap = cap_for(dd, cap_before, s)
    note, did = [], None
    if cap == 0.0 and cap_before > 0.0:
        did = _rebalance(conn, s, day, nav, 0.0, "hard brake: drawdown %.0f%%" % (dd * 100))
        _put(conn, "hard_since", day)
        note.append("hard brake: all cash")
    elif cap == 0.0:
        since = _get(conn, "hard_since", day)
        if sum(1 for d in days if since < d <= day) >= int(s["hard_pause_days"]):          # sat out long enough: restart with a fresh peak
            cash, gross, hs = _book(conn, day)
            nav = cash + gross
            peak, dd, cap = nav, 0.0, float(s["leverage"])
            did = _rebalance(conn, s, day, nav, cap, "restart after hard brake")
            note.append("restarted after hard brake")
    elif cap < cap_before:
        did = _rebalance(conn, s, day, nav, cap, "soft brake: drawdown %.0f%%, leverage %.1fx" % (dd * 100, cap))
        note.append("soft brake")
    else:
        last = _get(conn, "last_rebalance")
        since = None if not last else sum(1 for d in days if last < d <= day)
        if last is None or since >= int(s["rebalance_days"]) or cap > cap_before:
            did = _rebalance(conn, s, day, nav, cap, "scheduled rebalance" if last else "first rebalance")
            note.append("rebalanced")
    cash, gross, hs = _book(conn, day)
    nav = cash + gross
    peak = max(peak, nav) if cap > 0 else peak
    dd = 0.0 if peak <= 0 else max(0.0, 1.0 - nav / peak)
    _put(conn, "peak", peak)
    _put(conn, "cap", cap)
    _put(conn, "last_day", day)
    conn.execute("INSERT OR REPLACE INTO momentum_nav(day,nav,cash,gross,leverage,drawdown,cap,note) VALUES(?,?,?,?,?,?,?,?)", (day, nav, cash, gross, (gross / nav) if nav > 0 else 0.0, dd, cap, "; ".join(note)))
    conn.commit()
    return {"status": "ok", "day": day, "nav": round(nav, 2), "gross": round(gross, 2), "cap": cap, "drawdown": round(dd, 4), "action": did, "note": note}


def report(conn, cfg: dict) -> dict:
    ensure(conn)
    s = settings(cfg)
    rows = conn.execute("SELECT * FROM momentum_nav ORDER BY day").fetchall()
    holds = conn.execute("SELECT ticker,shares,entry_price,opened,score FROM momentum_holdings ORDER BY shares*entry_price DESC").fetchall()
    last = rows[-1] if rows else None
    start = float(s["start_capital"])
    return {"enabled": s["enabled"], "leverage_setting": s["leverage"], "names": s["names"], "start": start,
            "nav": None if not last else last["nav"], "return_pct": None if not last else (last["nav"] / start - 1.0) * 100.0,
            "leverage_now": None if not last else last["leverage"], "drawdown_pct": None if not last else last["drawdown"] * 100.0, "brake_cap": None if not last else last["cap"],
            "days": len(rows), "since": rows[0]["day"] if rows else None, "last_day": last["day"] if last else None,
            "holdings": [dict(h) for h in holds], "trades": conn.execute("SELECT COUNT(*) FROM momentum_trades").fetchone()[0]}


def run_stage(conn, cfg: dict, now=None) -> dict:
    """Pipeline hook: at most once every 6 hours, and never able to break the trading cycle."""
    from . import db
    if not settings(cfg)["enabled"] or not db.due(conn, "momentum", 6.0, now):
        return {"status": "skipped"}
    out = run(conn, cfg, now)
    db.mark_run(conn, "momentum", json.dumps({k: out.get(k) for k in ("status", "day", "nav")}), now)
    return out


def run_cli(args, conn, cfg: dict) -> int:
    what = getattr(args, "what", None) or "report"
    if what == "run":
        print(json.dumps(run(conn, cfg), indent=1, default=str))
        return 0
    if what == "rank":
        days = trading_days(conn)
        for i, r in enumerate(rank(conn, cfg, days[-1])[: int(getattr(args, "limit", 20) or 20)], 1):
            print("%2d  %-8s  12-1 momentum %+6.1f%%   $%.2f" % (i, r["ticker"], r["score"] * 100, r["price"]))
        return 0
    r = report(conn, cfg)
    print(json.dumps(r, indent=1, default=str))
    return 0
