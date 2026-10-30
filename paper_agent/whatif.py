"""What would the trades we did NOT take have done?

For every decision the bot made (taken or not) this replays the idea through the REAL exit rules (`executor.run`: stop ratchet, partial exit, runner, target extension, time stop) on a throw-away
in-memory book, using 5-minute price bars. Nothing here touches the trading database except to write its own results table, and it never changes a decision.

How it stays honest (the assumptions, version ASSUMPTIONS):
  * Entry: the open of the first 5-minute bar after the decision, plus the configured slippage. A decision made out of hours enters at the next session's open, and the same last-look gap rule that
    cancels a real BUY (stock opens too far below / above the previous close) cancels it here. An idea older than `trade_event_max_age_hours` at that moment is "EXPIRED".
  * Inside one bar the price is assumed to walk open -> low -> high -> close, i.e. the WORST case for a long position: if a stop and a target both fall inside one bar, the stop wins.
  * Every idea is sized at a fixed notional, so only percentage results are meaningful; portfolio limits (cash, exposure, capacity) are deliberately ignored: this measures the IDEA, not the book.
  * Exits driven by news (thesis broken, adverse evidence) and the earnings-overnight rule are not simulated: only the price rules are. Ideas the bot would never take as a long (bearish) are not simulated.
  * Trades we DID take are replayed too ("controls", from their actual entry) so the engine's accuracy against reality is measured, not assumed.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
import re
import statistics
import time
import urllib.parse
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple

from . import db, executor, net, trader
from .timeutil import UTC, iso, market_state, parse_ts, trading_days_between, utcnow

ASSUMPTIONS = "w1"
CHART_5M = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=5m&includePrePost=false"
STEPS = ("open", "low", "high", "close")                       # the worst-case walk inside a bar for a long position
NOTIONAL = 100_000.0
PENDING = ("AWAITING_MARKET", "AWAITING_QUOTE", "AWAITING_CAPACITY", "HELD")
PRICE_RULE_EXITS = ("STOP_REACHED", "TARGET_REACHED", "RUNNER_STOP", "RUNNER_TARGET", "RUNNER_TIME", "TIME_EXPIRED")
MIN_N = 30                                                     # below this a group's average says nothing


# ------------------------------------------------------------------------------------------------------ 5-minute bars
def parse_bars(payload: dict) -> List[dict]:
    if not isinstance(payload, dict):
        raise net.FetchError("chart payload is not a JSON object")
    chart = payload.get("chart") or {}
    res = (chart.get("result") or [None])[0]
    if not res or chart.get("error"):
        raise net.FetchError("chart error: %s" % chart.get("error"))
    stamps = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]

    def g(k, i):
        try:
            v = q[k][i]
            return float(v) if v is not None and math.isfinite(float(v)) and float(v) > 0 else None
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    out = []
    for i, t in enumerate(stamps):
        o, h, l, c = g("open", i), g("high", i), g("low", i), g("close", i)
        if c is None or h is None or l is None:
            continue
        o = c if o is None else o
        h, l = max(h, l, o, c), min(h, l, o, c)                  # a malformed bar is repaired, never trusted to be inside its own range
        out.append({"ts": iso(dt.datetime.fromtimestamp(t, UTC)), "open": o, "high": h, "low": l, "close": c, "volume": g("volume", i)})
    out.sort(key=lambda b: b["ts"])
    return out


def store_bars(conn, symbol: str, bars: List[dict]) -> int:
    conn.executemany("INSERT OR REPLACE INTO intraday_bars(symbol,ts,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?)", [(symbol, b["ts"], b["open"], b["high"], b["low"], b["close"], b["volume"]) for b in bars])
    return len(bars)


def fetch_symbol(conn, symbol: str, range_: str, fetch: Optional[Callable], now) -> str:
    """Fetch and store 5-minute bars. Never raises: the outcome is remembered so a failing symbol is not hammered."""
    try:
        payload, _ = (fetch or net.get_json)(CHART_5M % (urllib.parse.quote(symbol), range_), headers={"Accept": "application/json"})
        bars = parse_bars(payload)
        if not bars:
            raise net.FetchError("no bars")
        with db.tx(conn):
            store_bars(conn, symbol, bars)
            conn.execute("INSERT OR REPLACE INTO intraday_state(symbol,fetched_at,last_ts,status) VALUES(?,?,?,?)", (symbol, iso(now), bars[-1]["ts"], "ok"))
        return "ok"
    except (net.FetchError, KeyError, TypeError, ValueError, AttributeError) as e:
        with db.tx(conn):
            conn.execute("INSERT OR REPLACE INTO intraday_state(symbol,fetched_at,last_ts,status) VALUES(?,?,?,?)", (symbol, iso(now), None, ("error: %s" % e)[:120]))
        return "error"


def _bars_for(conn, symbol: str) -> List[dict]:
    return [dict(r) for r in conn.execute("SELECT ts,open,high,low,close FROM intraday_bars WHERE symbol=? ORDER BY ts", (symbol,))]


# ------------------------------------------------------------------------------------------------------ which decisions, and what plan each would have had
def gate_of(reason) -> str:
    try:
        g = json.loads(reason).get("gates") or [""]
        return str(g[-1])
    except (ValueError, AttributeError, TypeError):
        return str(reason or "")


def gate_family(status: str, gate: str) -> str:
    """A readable group for what stopped an idea, so results can be added up per rule."""
    g = (gate or "").lower()
    if status == "EXECUTED":
        return "traded"
    if "reward:risk" in g:
        return "reward:risk too low"
    if "eligib" in g or "story quality" in g:
        return "story quality (eligibility score)"
    if g.startswith("ev net") or "expected profit" in g:
        return "edge too small or negative"
    if "liquid" in g or "daily dollar volume" in g or "adv" in g.split():
        return "liquidity"
    if status == "VETOED" or "veto" in g or "arrived after this buy" in g:
        return "vetoed by later news"
    if "gap" in g or "opened" in g and "previous close" in g:
        return "gapped at the open"
    if status in ("EXPIRED", "CANCELLED") or "expired" in g or "stale" in g:
        return "expired or cancelled"
    if status == "RISK_REJECTED" or "capacity" in g or "exposure" in g or "cash" in g or "stress" in g:
        return "portfolio limits (capacity, exposure, cash)"
    if "article" in g:
        return "article reader"
    return "other: " + (status or "?").lower()


def rebuild_forecast(row: dict, cfg: dict) -> dict:
    """The forecast dict `trader.insert_plan` needs, rebuilt from the stored row. Everything that drives an exit is stored; the runner target is re-derived exactly as `ev.build` derives it
    (max(target x 1.6, bull case), capped); `edge`, `completeness` and `target_probability` only feed the plan's description text."""
    inputs = json.loads(row["inputs_json"] or "{}")
    scen = json.loads(row["scenarios_json"] or "{}")
    inputs.setdefault("priced_in", 0.0)
    swing = bool(inputs.get("swing"))
    target = float(row["target_return"])
    runner = max(target * 1.6, float((scen.get("bull") or {}).get("ret", 0.0))) if target else 0.0
    cap = float((cfg.get("strategy") or {}).get("runner_cap_pct", 0.35)) if swing else 0.35
    return {"target_return": target, "stop_return": float(row["stop_return"]), "inputs": inputs, "runner_target": min(cap, runner), "scenarios": scen, "target_probability": float(row["p_up"] or 0.5),
            "confidence": float(row["confidence"] or 0.0), "completeness": 0.8, "ev_net": float(row["ev_net"] or 0.0), "edge": float(row["ev_gross"] or 0.0), "verdict": row["verdict"],
            "horizon_days": int(row["horizon_days"]), "thesis_key": row["fthesis"], "hash": row["hash"] or ""}


def select_ideas(conn, cfg: dict, now, days: float) -> Tuple[List[dict], int]:
    """Decisions of the last `days` that carry a long forecast and are no longer pending. Returns (ideas, number skipped because they were bearish / not long)."""
    since = iso(now - dt.timedelta(days=days))
    rows = conn.execute(
        "SELECT d.id did,d.created,d.ticker,d.status,d.origin,d.reason,f.direction,f.horizon_days,f.p_up,f.ev_gross,f.ev_net,f.confidence,f.target_return,f.stop_return,f.verdict,f.scenarios_json,f.inputs_json,f.hash,"
        "f.thesis_key fthesis,p.opened p_opened,p.entry_price p_entry,p.status p_status,p.pnl p_pnl,p.review_code p_code,p.closed p_closed,"
        "(SELECT -SUM(amount) FROM ledger WHERE kind='VIRTUAL_BUY' AND ref=d.ticker||':'||d.id) buy_cost "
        "FROM decisions d JOIN forecasts f ON f.id=d.forecast_id LEFT JOIN positions p ON p.decision_id=d.id "
        "WHERE d.created>=? AND d.status NOT IN (%s,'DUPLICATE') ORDER BY d.id" % ",".join("'%s'" % s for s in PENDING), (since,)).fetchall()
    out, bearish = [], 0
    for r in rows:
        r = dict(r)
        if not (r["direction"] == "LONG" and r["target_return"] and r["target_return"] > 0 and r["stop_return"] and r["stop_return"] < 0):
            bearish += 1
            continue
        r["kind"] = "control" if r["status"] == "EXECUTED" and r["p_opened"] else "idea"
        if r["kind"] == "control":
            # the plan of a trade we took was built from the forecast re-made at execution (fresh quote), not the one stored on the decision: use the same one
            fr = conn.execute("SELECT horizon_days,p_up,ev_gross,ev_net,confidence,target_return,stop_return,verdict,scenarios_json,inputs_json,hash,thesis_key fthesis FROM forecasts WHERE ticker=? AND created<=? ORDER BY id DESC LIMIT 1",
                              (r["ticker"], r["p_opened"])).fetchone()
            if fr:
                r.update(dict(fr))
        r["gate"] = gate_of(r["reason"])
        try:
            inp = json.loads(r["inputs_json"] or "{}")
            r["event_type"] = inp.get("event_type") or "unknown"
            r["profile"] = "swing" if inp.get("swing") else "classic"            # forecasts made under the older multi-week profile are kept apart: they are not the strategy in force
        except ValueError:
            r["event_type"], r["profile"] = "unknown", "classic"
        out.append(r)
    return out, bearish


# ------------------------------------------------------------------------------------------------------ entry assumptions
def entry_for(idea: dict, bars: List[dict], cfg: dict, conn, now) -> dict:
    """Terminal outcome (NO_DATA / EXPIRED / GAP_CANCELLED) or the entry (bar timestamp, price, opened time) the simulation will use."""
    slip = float(cfg["portfolio"]["slippage_bps"]) / 1e4
    if idea["kind"] == "control":
        start = idea["p_opened"]
        b = next((x for x in bars if x["ts"] >= start), None)
        if not b:
            return {"outcome": "NO_DATA", "note": "no bars after the actual entry"}
        return {"ts": b["ts"], "px": float(idea["p_entry"]), "opened": start}
    b = next((x for x in bars if x["ts"] >= idea["created"]), None)
    if not b:
        return {"outcome": "NO_DATA", "note": "no bars after the decision yet"}
    age_h = (parse_ts(b["ts"]) - parse_ts(idea["created"])).total_seconds() / 3600.0
    if age_h > float(cfg["intelligence"].get("trade_event_max_age_hours", 24)):
        return {"outcome": "EXPIRED", "note": "the idea was %.0f hours old by the first tradable bar" % age_h}
    day = b["ts"][:10]
    first_of_session = not any(x["ts"][:10] == day and x["ts"] < b["ts"] for x in bars)
    if first_of_session:
        prev = [x for x in bars if x["ts"][:10] < day]
        pc = prev[-1]["close"] if prev else None
        if pc is None:
            row = conn.execute("SELECT close FROM market_series WHERE symbol=? AND day<? ORDER BY day DESC LIMIT 1", (idea["ticker"], day)).fetchone()
            pc = float(row[0]) if row else None
        if pc:
            gap = b["open"] / pc - 1.0
            if gap < -float(cfg["risk"]["max_gap_down_pct"]) or gap > float(cfg["risk"]["max_gap_up_pct"]):
                return {"outcome": "GAP_CANCELLED", "note": "opened %+.1f%% from the previous close: the last-look rule would have cancelled the buy" % (gap * 100)}
    return {"ts": b["ts"], "px": b["open"] * (1.0 + slip), "opened": b["ts"]}


# ------------------------------------------------------------------------------------------------------ the shadow book
def _open_shadow(sh, e: dict, f: dict) -> Tuple[int, float]:
    fill = e["px"]
    shares = max(1, math.floor(NOTIONAL / fill))
    cost = round(shares * fill, 2)
    with db.tx(sh):
        sh.execute("INSERT OR IGNORE INTO decisions(id,created,ticker,action,status,reason) VALUES(?,?,?,?,?,?)", (e["did"], e["opened"], e["ticker"], "BUY", "EXECUTED", "{}"))
        cash = db.cash(sh)
        sh.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)", (e["opened"], "VIRTUAL_BUY", -cost, "%s:%d" % (e["ticker"], e["did"]), round(cash - cost, 2)))
        pid = sh.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,decision_id,status,thesis_key,forecast_id,sector) VALUES(?,?,?,?,?,?,?,'OPEN',?,?,?)",
                         (e["ticker"], "", e["opened"], fill, shares, cost, e["did"], f.get("thesis_key"), None, "")).lastrowid
        sh.execute("INSERT INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(?,?,?,?,?)", (pid, "OPEN", shares, cost, e["opened"]))
        trader.insert_plan(sh, pid, e["did"], f, fill, e["opened"])
    return pid, cost


def simulate(cfg: dict, entries: List[dict], bars: Dict[str, List[dict]], budget_s: float = 40.0, clock: Callable[[], float] = time.monotonic) -> Tuple[Dict[int, dict], bool]:
    """Run the entries through the real exit code on an in-memory book. Returns ({decision id: result}, finished). `entries` carry: did, ticker, ts, px, opened, f."""
    t0 = clock()
    sh = db.open_db(":memory:", 1_000_000_000.0)
    cfgx = copy.deepcopy(cfg)
    cur: Dict[str, float] = {}

    def quote_fn(conn, ticker, purpose="exit"):
        p = cur.get(ticker)
        if p is None:
            return {"status": "FAILED", "execution_eligible": False, "price": None, "observations": [], "provider_count": 0}
        return {"status": "PASS", "execution_eligible": True, "price": p, "providers_used": ["WHATIF"], "quoted_at": None, "observations": [], "provider_count": 1, "age_seconds": 3}
    by_bar = {s: {b["ts"]: b for b in bs} for s, bs in bars.items()}
    first_ts = min((e["ts"] for e in entries), default=None)
    grid = sorted({b["ts"] for e in entries for b in bars.get(e["ticker"], []) if b["ts"] >= e["ts"]}) if entries else []
    waiting = defaultdict(list)
    for e in entries:
        waiting[e["ts"]].append(e)
    pids: Dict[int, int] = {}
    cost0: Dict[int, float] = {}                                         # the executor zeroes cost on close, so the original is kept here
    finished = True
    for ts in grid:
        if clock() - t0 > budget_s:
            finished = False
            break
        now_t = parse_ts(ts) + dt.timedelta(minutes=4)                  # inside the bar, so the session check passes on the last bar of the day too
        for e in waiting.get(ts, []):
            pids[e["did"]], cost0[e["did"]] = _open_shadow(sh, e, e["f"])
        if not sh.execute("SELECT 1 FROM positions WHERE status='OPEN' LIMIT 1").fetchone():
            if not any(t > ts for t in waiting):
                break
            continue
        for step in STEPS:
            cur.clear()
            for s, m in by_bar.items():
                b = m.get(ts)
                if b:
                    cur[s] = b[step]
            executor.run(sh, cfgx, now_t, quote_fn)
    results: Dict[int, dict] = {}
    for e in entries:
        pid = pids.get(e["did"])
        if pid is None:
            continue
        p = sh.execute("SELECT p.*,l.realized_pnl,l.remaining_shares,l.remaining_cost,l.runner_plan_id FROM positions p JOIN position_lifecycle l ON l.position_id=p.id WHERE p.id=?", (pid,)).fetchone()
        seq = [b for b in bars.get(e["ticker"], []) if b["ts"] >= e["ts"]]
        last = seq[-1] if seq else None
        entry = float(p["entry_price"])
        if p["status"] == "CLOSED":
            ex_ts = p["closed"] or e["ts"]
            span = [b for b in seq if b["ts"] <= (ex_ts if isinstance(ex_ts, str) else iso(ex_ts))] or seq[:1]
            ret = float(p["pnl"]) / cost0[e["did"]]
            res = {"outcome": "CLOSED", "exit_ts": ex_ts, "exit_px": float(p["exit_price"] or 0.0), "exit_reason": p["review_code"], "ret_net": ret,
                   "held_sessions": trading_days_between(p["opened"], ex_ts), "partial": bool(p["runner_plan_id"])}
        else:
            mark = (last["close"] if last else entry)
            unreal = float(p["realized_pnl"] or 0.0) + float(p["remaining_shares"]) * mark - float(p["remaining_cost"])
            span = seq
            res = {"outcome": "OPEN_AT_END", "exit_ts": last["ts"] if last else None, "exit_px": mark, "exit_reason": "still open when the data ends", "ret_net": unreal / cost0[e["did"]],
                   "held_sessions": trading_days_between(p["opened"], last["ts"]) if last else 0, "partial": bool(p["runner_plan_id"])}
        res["mfe"] = (max(b["high"] for b in span) / entry - 1.0) if span else None
        res["mae"] = (min(b["low"] for b in span) / entry - 1.0) if span else None
        res["bars_n"] = len(span)
        res["data_through"] = last["ts"] if last else None
        results[e["did"]] = res
    return results, finished


# ------------------------------------------------------------------------------------------------------ the scheduled stage
def _store(conn, now, idea: dict, res: dict, fingerprint: str, strat_version: str) -> None:
    actual = None
    if idea["kind"] == "control" and idea.get("p_status") == "CLOSED" and idea.get("buy_cost"):
        actual = float(idea["p_pnl"]) / float(idea["buy_cost"])                 # a closed position keeps no cost of its own: the original buy is in the ledger
    final = 1 if res["outcome"] in ("CLOSED", "EXPIRED", "GAP_CANCELLED") else 0
    conn.execute(
        "INSERT OR REPLACE INTO whatif_results(decision_id,assumptions,simulated_at,ticker,kind,origin,decision_status,gate,family,verdict,event_type,strategy_version,strategy_fp,outcome,final,note,entry_ts,entry_px,exit_ts,exit_px,exit_reason,"
        "held_sessions,partial,ret_net,mfe,mae,bars_n,data_through,actual_ret,actual_exit_reason,profile) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (idea["did"], ASSUMPTIONS, iso(now), idea["ticker"], idea["kind"], idea["origin"] or "event", idea["status"], idea["gate"][:200], gate_family(idea["status"], idea["gate"]), idea["verdict"], idea["event_type"],
         strat_version, fingerprint, res["outcome"], final, res.get("note", ""), res.get("entry_ts"), res.get("entry_px"), res.get("exit_ts"), res.get("exit_px"), res.get("exit_reason"),
         res.get("held_sessions"), 1 if res.get("partial") else 0, res.get("ret_net"), res.get("mfe"), res.get("mae"), res.get("bars_n"), res.get("data_through"), actual,
         idea.get("p_code") if actual is not None else None, idea.get("profile", "swing")))


def run_stage(conn, cfg: dict, now=None, fetch: Optional[Callable] = None, clock: Callable[[], float] = time.monotonic) -> dict:
    """Fetch the bars that are missing and simulate every idea that can be simulated. Safe to call repeatedly: finished ideas are never redone, unfinished ones only when new bars arrive."""
    from . import stamp
    now = now or utcnow()
    w = cfg.get("whatif") or {}
    out = {"enabled": bool(w.get("enabled", True)), "ideas": 0, "bearish_skipped": 0, "fetched": 0, "fetch_errors": 0, "simulated": 0, "pending": 0}
    if not out["enabled"]:
        return out
    t0 = clock()
    ideas, out["bearish_skipped"] = select_ideas(conn, cfg, now, float(w.get("lookback_days", 5)))
    out["ideas"] = len(ideas)
    done = {r["decision_id"]: dict(r) for r in conn.execute("SELECT decision_id,final,data_through FROM whatif_results WHERE assumptions=?", (ASSUMPTIONS,))}
    todo = [i for i in ideas if not (done.get(i["did"]) or {}).get("final")][: int(w.get("max_ideas_per_run", 150))]
    # 1. bars: fetch the symbols that are stale, a limited number per run (the data source allows about 30 requests a minute)
    oldest = min((parse_ts(i["created"]) for i in todo), default=now)
    range_ = "5d" if (now - oldest).days <= 4 else "1mo"
    need = defaultdict(int)
    for i in todo:
        need[i["ticker"]] += 1
    for sym in sorted(need, key=lambda s: -need[s]):
        if out["fetched"] >= int(w.get("max_symbols_per_cycle", 20)) or clock() - t0 > float(w.get("time_budget_seconds", 40)) * 0.5:
            break
        st = conn.execute("SELECT fetched_at,status FROM intraday_state WHERE symbol=?", (sym,)).fetchone()
        age_h = ((now - parse_ts(st["fetched_at"])).total_seconds() / 3600.0) if st and parse_ts(st["fetched_at"]) else 1e9
        if st and age_h < (3.0 if st["status"] == "ok" else 1.0):
            continue
        if fetch_symbol(conn, sym, range_, fetch, now) == "ok":
            out["fetched"] += 1
        else:
            out["fetch_errors"] += 1
    # 2. entries
    bars = {s: _bars_for(conn, s) for s in need}
    sv = stamp.STRATEGY_VERSION
    cur = stamp.version_at(conn, iso(now))
    fp = (cur or {}).get("fingerprint", "")
    entries, idea_by = [], {}
    for i in todo:
        bs = bars.get(i["ticker"]) or []
        if (done.get(i["did"]) or {}).get("data_through") and bs and bs[-1]["ts"] == done[i["did"]]["data_through"]:
            continue                                                            # nothing new since the last simulation of this unfinished idea
        e = entry_for(i, bs, cfg, conn, now)
        if "outcome" in e:
            _store(conn, now, i, e, fp, sv)
            out["simulated"] += 1
            continue
        try:
            f = rebuild_forecast(i, cfg)
        except (KeyError, TypeError, ValueError) as ex:
            _store(conn, now, i, {"outcome": "NO_DATA", "note": "forecast could not be rebuilt: %s" % ex}, fp, sv)
            continue
        entries.append({"did": i["did"], "ticker": i["ticker"], "ts": e["ts"], "px": e["px"], "opened": e["opened"], "f": f})
        idea_by[i["did"]] = (i, e)
    # 3. simulate
    left = max(5.0, float(w.get("time_budget_seconds", 40)) - (clock() - t0))
    if entries:
        results, finished = simulate(cfg, entries, bars, left, clock)
        for did, res in results.items():
            i, e = idea_by[did]
            if res["outcome"] == "OPEN_AT_END" and not finished:
                continue                                                        # unfinished: try again next run rather than record a half-simulated idea
            res = dict(res, entry_ts=e["ts"], entry_px=e["px"])
            _store(conn, now, i, res, fp, sv)
            out["simulated"] += 1
    out["pending"] = max(0, len([i for i in ideas if not (done.get(i["did"]) or {}).get("final")]) - out["simulated"])
    return out


# ------------------------------------------------------------------------------------------------------ the report
def _stats(rets: List[float]) -> dict:
    n = len(rets)
    if n == 0:
        return {"n": 0}
    m = statistics.mean(rets)
    sd = statistics.stdev(rets) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else 0.0
    return {"n": n, "mean": m, "median": statistics.median(rets), "win": sum(1 for r in rets if r > 0) / n, "se": se, "lo": m - 1.96 * se, "hi": m + 1.96 * se}


def reading(s: dict) -> str:
    if s["n"] < MIN_N:
        return "too few (n<%d): no conclusion" % MIN_N
    if s["lo"] > 0.002:
        return "would have MADE money: this rule may be too strict"
    if s["hi"] < -0.002:
        return "would have LOST money: this rule earns its keep"
    return "inconclusive"


def report(conn, now=None, days: float = 30, profile: str = "swing") -> dict:
    """Group the finished simulations. Only CLOSED simulations of the strategy profile in force enter the averages; provisional (still open when the data ends), terminal non-trades and ideas made under
    the older profile are counted separately."""
    now = now or utcnow()
    since = iso(now - dt.timedelta(days=days))
    allrows = [dict(r) for r in conn.execute("SELECT * FROM whatif_results WHERE assumptions=? AND simulated_at>=?", (ASSUMPTIONS, since))]
    rows = [r for r in allrows if (r.get("profile") or "swing") == profile]
    other_profile = len(allrows) - len(rows)
    closed = [r for r in rows if r["outcome"] == "CLOSED" and r["ret_net"] is not None]
    ideas = [r for r in closed if r["kind"] == "idea"]

    def group(key):
        by = defaultdict(list)
        for r in ideas:
            by[r[key] or "?"].append(r)
        res = []
        for k, rs in by.items():
            rets = [r["ret_net"] for r in rs]
            s = _stats(rets)
            s.update(label=k, reading=reading(s), stopped=sum(1 for r in rs if (r["exit_reason"] or "").endswith("STOP_REACHED") or r["exit_reason"] == "RUNNER_STOP") / len(rs),
                     targeted=sum(1 for r in rs if "TARGET" in (r["exit_reason"] or "") or r["partial"]) / len(rs), timed=sum(1 for r in rs if "TIME" in (r["exit_reason"] or "")) / len(rs),
                     mfe=statistics.mean(r["mfe"] for r in rs if r["mfe"] is not None) if any(r["mfe"] is not None for r in rs) else None,
                     mae=statistics.mean(r["mae"] for r in rs if r["mae"] is not None) if any(r["mae"] is not None for r in rs) else None)
            res.append(s)
        return sorted(res, key=lambda s: -s["n"])
    controls = [r for r in closed if r["kind"] == "control" and r["actual_ret"] is not None and r["actual_exit_reason"] in PRICE_RULE_EXITS]
    fid = {"n": len(controls)}
    if controls:
        d = [r["ret_net"] - r["actual_ret"] for r in controls]
        fid.update(mean_diff=statistics.mean(d), mean_abs=statistics.mean(abs(x) for x in d), same_exit=sum(1 for r in controls if r["exit_reason"] == r["actual_exit_reason"]) / len(controls))
    by_outcome = defaultdict(int)
    for r in rows:
        by_outcome[r["outcome"]] += 1
    return {"days": days, "by_family": group("family"), "by_origin": group("origin"), "by_event": group("event_type"), "overall": _stats([r["ret_net"] for r in ideas]) if ideas else {"n": 0},
            "traded": _stats([r["ret_net"] for r in closed if r["kind"] == "control"]) if any(r["kind"] == "control" for r in closed) else {"n": 0}, "fidelity": fid, "outcomes": dict(by_outcome), "assumptions": ASSUMPTIONS, "profile": profile, "other_profile": other_profile}


def format_report(rep: dict) -> str:
    pc = lambda x: "%+.2f%%" % (x * 100)  # noqa: E731
    L = ["MISSED-TRADE ANALYSIS (assumptions %s, last %d days): what the ideas we did NOT take would have earned under the real exit rules" % (rep["assumptions"], rep["days"]), ""]
    o = rep["outcomes"]
    L.append("simulated (%s profile): %s%s" % (rep["profile"], ", ".join("%s %d" % (k.lower().replace("_", " "), v) for k, v in sorted(o.items())) or "nothing yet",
             ("; %d idea(s) from the older profile are stored but not averaged" % rep["other_profile"]) if rep["other_profile"] else ""))
    ov = rep["overall"]
    if ov["n"]:
        L.append("all missed ideas together: n=%d, average %s per idea, %.0f%% winners (95%% range %s to %s)" % (ov["n"], pc(ov["mean"]), ov["win"] * 100, pc(ov["lo"]), pc(ov["hi"])))
    tr = rep["traded"]
    if tr["n"]:
        L.append("trades we DID take, replayed from their entry: n=%d, average %s" % (tr["n"], pc(tr["mean"])))
    fid = rep["fidelity"]
    if fid["n"]:
        L.append("engine accuracy on trades we took (price-rule exits): n=%d, average difference %s, typical error %s, same exit reason %.0f%%" % (fid["n"], pc(fid["mean_diff"]), pc(fid["mean_abs"]), fid["same_exit"] * 100))
    for title, key in (("BY WHAT STOPPED THE IDEA", "by_family"), ("BY ORIGIN", "by_origin"), ("BY KIND OF NEWS", "by_event")):
        L += ["", title]
        for s in rep[key]:
            L.append("  %-44s n=%-3d avg %-8s win %3.0f%%  stop-out %3.0f%%  hit target %3.0f%%  | %s" % (s["label"][:44], s["n"], pc(s["mean"]), s["win"] * 100, s["stopped"] * 100, s["targeted"] * 100, s["reading"]))
        if not rep[key]:
            L.append("  (nothing finished yet)")
    return "\n".join(L)


def html_section(conn, cfg: dict, now=None) -> str:
    """The block shown on the Intelligence tab."""
    from html import escape as E
    rep = report(conn, now)
    pc = lambda x: "%+.2f%%" % (x * 100)  # noqa: E731
    o = rep["outcomes"]
    head = "<h2>Missed trades: what would have happened</h2><p class=mut>Every idea the bot looked at, replayed through the real exit rules on 5-minute prices (assumptions %s). Averages use finished ideas only and say nothing below %d of a kind. " \
           "Worst-case price order inside each bar; portfolio limits ignored; news-driven exits not simulated.</p>" % (E(rep["assumptions"]), MIN_N)
    if not o:
        return head + "<p>Nothing has been simulated yet. It runs after the US close once prices are available.</p>"
    parts = [head, "<p>%s%s</p>" % (E(", ".join("%s %d" % (k.lower().replace("_", " "), v) for k, v in sorted(o.items()))), (" &middot; %d idea(s) from the older multi-week profile are stored but not averaged" % rep["other_profile"]) if rep["other_profile"] else "")]
    fid = rep["fidelity"]
    if fid["n"]:
        parts.append("<p><b>Engine accuracy</b> on %d trades we actually took: average difference %s, typical error %s, same exit reason %.0f%%.</p>" % (fid["n"], pc(fid["mean_diff"]), pc(fid["mean_abs"]), fid["same_exit"] * 100))
    for title, key in (("By what stopped the idea", "by_family"), ("By origin", "by_origin"), ("By kind of news", "by_event")):
        rows = "".join("<tr><td>%s</td><td>%d</td><td>%s</td><td>%.0f%%</td><td>%.0f%%</td><td>%.0f%%</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            E(s["label"]), s["n"], pc(s["mean"]), s["win"] * 100, s["stopped"] * 100, s["targeted"] * 100, pc(s["mfe"]) if s["mfe"] is not None else "-", pc(s["mae"]) if s["mae"] is not None else "-", E(s["reading"])) for s in rep[key])
        parts.append("<h3>%s</h3>%s" % (E(title), ("<table><tr><th>Group</th><th>Ideas</th><th>Average result</th><th>Winners</th><th>Stopped out</th><th>Hit target</th><th>Best point</th><th>Worst point</th><th>Reading</th></tr>%s</table>" % rows) if rows else "<p class=mut>Nothing finished yet.</p>"))
    return "".join(parts)
