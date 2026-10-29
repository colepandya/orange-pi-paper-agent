"""Entry decisions. Every candidate gets a ledger row and an auditable reason trail; nothing is executed on a stale or unverified quote."""
from __future__ import annotations

import datetime as dt
import time
import json
import math
import os
from typing import Callable, Dict, List, Optional

from . import pretrade, candqueue, company, db, ev, learning, marketdata as md, opportunity, portfolio as pf, quotes, redteam, revisions, sizing, thesis as thesis_engine
from .timeutil import add_trading_days, iso, market_state, parse_ts, utcnow

PENDING = ("AWAITING_MARKET", "AWAITING_QUOTE", "AWAITING_CAPACITY", "HELD")


def quote_diagnosis(q: Optional[dict], cfg: dict) -> str:
    """Why a price could not be verified, in words: which providers failed and how (so a waiting BUY is never a mystery)."""
    if not q:
        return "no quote returned"
    max_age = float(cfg["quotes"]["entry_max_age_seconds"])
    need = 1 if cfg["quotes"].get("allow_single_source") else int(cfg["quotes"]["min_providers"])
    bits = []
    for o in q.get("observations") or []:
        if o.get("error"):
            bits.append("%s %s" % (o["provider"], str(o["error"])[:28]))
        elif not o.get("accepted"):
            bits.append("%s rejected (%s)" % (o["provider"], o.get("reject_reason") or "validation"))
        elif o.get("age_seconds") is None:
            bits.append("%s has no timestamp" % o["provider"])
        elif o["age_seconds"] > max_age:
            bits.append("%s stale (%ds old, limit %ds)" % (o["provider"], o["age_seconds"], max_age))
        else:
            bits.append("%s ok (%ds)" % (o["provider"], max(0, o["age_seconds"])))
    msg = "%s%s; need %d fresh agreeing provider(s), have %d" % ("; ".join(bits), "" if bits else (q.get("status") or "none"), need, int(q.get("provider_count") or 0) if q.get("status") == "PASS" else sum(1 for o in q.get("observations") or [] if o.get("accepted") and o.get("age_seconds") is not None and o["age_seconds"] <= max_age))
    return ("%s; " % q["note"] if q.get("note") else "") + msg


def eligibility_score(c: dict) -> float:
    fresh = max(0.0, 1.0 - c.get("age_hours", 0) / 24.0)
    return 100.0 * (0.30 * c.get("conf", 0.7) + 0.25 * c.get("credibility", 0.5) + 0.25 * min(1.0, c.get("magnitude", 0.3) / 0.7) + 0.20 * fresh)


def paused(cfg: dict) -> bool:
    return os.path.exists(cfg["runtime"]["pause_file"])


def _decision(conn, c: dict, status: str, action: str, reason: dict, score: float, now, forecast_id=None, ev_net=None, size_pct=None, theme="", thesis_key=None) -> int:
    cur = conn.execute("INSERT INTO decisions(created,news_uid,ticker,action,score,reason,rules_version,status,theme,event_id,forecast_id,ev_net,thesis_key,size_pct,origin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (iso(now), c.get("news_uid"), c["ticker"], action, score, json.dumps(reason, sort_keys=True, default=str), "5.0.0", status, theme, c.get("event_id"), forecast_id, ev_net, thesis_key, size_pct, c.get("origin", "event")))
    return cur.lastrowid


def _set_decision(conn, did: int, status: str, reason: dict, **kw) -> None:
    sets = ["status=?", "reason=?"]
    vals = [status, json.dumps(reason, sort_keys=True, default=str)]
    for k, v in kw.items():
        sets.append("%s=?" % k)
        vals.append(v)
    conn.execute("UPDATE decisions SET %s WHERE id=?" % ",".join(sets), vals + [did])


def insert_plan(conn, position_id: int, decision_id: int, f: dict, entry: float, opened_iso: str, version: str = "5.0.0") -> int:
    t, s = f["target_return"], abs(f["stop_return"])
    pp = f["inputs"].get("partial_exit_pct")
    partial = (pp if pp is not None and pp < 0.999 else 1.0) if f["inputs"].get("swing") else (0.7 if t >= 0.025 else 1.0)       # swing: bank part at the target, let the rest run behind the protected stop
    runner = max(f["runner_target"], t * 1.5) if partial < 1 else t
    rstop = 0.005 if partial < 1 else -s
    sc = f["scenarios"]
    cur = conn.execute(
        "INSERT INTO position_forecast_plans(position_id,decision_id,created,plan_version,active,event_type,forecast_decision,target_return,stop_return,target_price,stop_price,max_hold_days,estimated_exit,target_probability,confidence,completeness,"
        "partial_exit_pct,runner_target_return,runner_target_price,runner_stop_return,runner_stop_price,bear_return,base_return,bull_return,expected_return,residual_return,priced_in,rationale,invalidation_json,forecast_hash) "
        "VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (position_id, decision_id, opened_iso, version, f["inputs"].get("event_type", "thesis_signal"), f["verdict"], t, -s, entry * (1 + t), entry * (1 - s), f["horizon_days"], iso(add_trading_days(opened_iso, f["horizon_days"])),
         f["target_probability"], f["confidence"] * 100, f["completeness"] * 100, partial, runner, entry * (1 + runner), rstop, entry * (1 + rstop), sc["bear"]["ret"], sc["base"]["ret"], sc["bull"]["ret"], f["ev_net"], f["edge"], f["inputs"]["priced_in"],
         ("Swing plan: expected %+.2f%% inside %d session(s) (news works for ~%d), stop %.2f%% (%.1fx the stock's daily move), break-even lock and trailing stop active. Thesis %s." % (f["edge"] * 100, f["horizon_days"], f["inputs"].get("event_horizon_days", f["horizon_days"]), s * 100, s / max(f["inputs"].get("vol_daily") or s, 1e-6), f["thesis_key"] or "n/a")
          if f["inputs"].get("swing") else "EV-derived plan: base case %+.2f%%, stop %.2f%% (1.25 daily-vol x sqrt(min(h,5))), horizon %d sessions. Thesis %s." % (f["edge"] * 100, s * 100, f["horizon_days"], f["thesis_key"] or "n/a")),
         json.dumps(["event facts contradicted or corrected", "entity mapping invalidated", "thesis broken", "fresh quote unavailable", "adverse high-credibility evidence"]), f["hash"]))
    return cur.lastrowid


def execute_buy(conn, cfg: dict, c: dict, f: dict, sz: dict, q: dict, did: int, forecast_id: int, now, theme: str, sector: str, reason: dict) -> dict:
    fill = q["price"] * (1 + cfg["portfolio"]["slippage_bps"] / 1e4)
    shares = math.floor(sz["amount"] / fill)
    if shares < 1:
        with db.tx(conn):
            _set_decision(conn, did, "CAPACITY_DEFERRED", reason)
        return {"status": "CAPACITY_DEFERRED"}
    cost = round(shares * fill, 2)
    with db.tx(conn):
        cash = db.cash(conn)
        eq = pf.equity_of(conn, now=now)["equity"]
        if cash - cost < cfg["risk"]["min_cash_pct"] * eq * 0.99 or cash - cost < 0:
            _set_decision(conn, did, "CAPACITY_DEFERRED", dict(reason, note="cash reserve would be breached"))
            return {"status": "CAPACITY_DEFERRED"}
        conn.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)", (iso(now), "VIRTUAL_BUY", -cost, "%s:%d" % (c["ticker"], did), round(cash - cost, 2)))
        cur = conn.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,decision_id,status,thesis_key,forecast_id,sector) VALUES(?,?,?,?,?,?,?,'OPEN',?,?,?)",
                           (c["ticker"], theme, iso(now), fill, shares, cost, did, f["thesis_key"], forecast_id, sector))
        pid = cur.lastrowid
        conn.execute("INSERT INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(?,?,?,?,?)", (pid, "OPEN", shares, cost, iso(now)))
        insert_plan(conn, pid, did, f, fill, iso(now))
        why0 = "initial plan: %s" % (c.get("headline") or c.get("event_type"))
        revisions.record(conn, now, "position", str(pid), c["ticker"], "target_price", None, fill * (1 + f["target_return"]), why0, None, f["confidence"], {"ev_net": f["ev_net"]})
        revisions.record(conn, now, "position", str(pid), c["ticker"], "stop_price", None, fill * (1 + f["stop_return"]), why0, None, f["confidence"])
        revisions.record(conn, now, "position", str(pid), c["ticker"], "max_hold_days", None, f["horizon_days"], why0, None, f["confidence"])
        reason = dict(reason, quote={"price": q["price"], "providers": q.get("providers_used"), "quoted_at": q.get("quoted_at"), "age_s": q.get("age_seconds")}, fill=fill, shares=shares)
        _set_decision(conn, did, "EXECUTED", reason, entry_price=fill, shares=shares, amount=cost)
        conn.execute("INSERT INTO quote_cache(ticker,price,source,quoted_at,updated_epoch) VALUES(?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET price=excluded.price,source=excluded.source,quoted_at=excluded.quoted_at,updated_epoch=excluded.updated_epoch",
                     (c["ticker"], q["price"], "ENTRY_CONSENSUS", iso(now), now.timestamp()))   # mark-to-market starts from the verified entry quote
        learning.record_position_prediction(conn, pid, f, fill, now)
        db.audit(conn, "trader", "virtual_buy", c["ticker"], {"decision": did, "shares": shares, "cost": cost, "ev_net": f["ev_net"]})
    return {"status": "EXECUTED", "position_id": pid, "cost": cost}


def process_candidate(conn, cfg: dict, universe, c: dict, regime: dict, now, quote_fn: Callable, md_fetch=None, existing_decision: Optional[int] = None, intel: Optional[dict] = None) -> dict:
    t = c["ticker"]
    meta = universe.meta(t) if universe is not None else {}
    score = eligibility_score(c)
    reason: Dict = {"candidate": {k: c.get(k) for k in ("event_id", "news_uid", "event_type", "direction", "magnitude", "conf", "credibility", "age_hours", "headline", "source", "amount_usd", "horizon", "origin", "thesis_key", "p", "mechanism", "chain_order")}, "gates": []}
    if not company.get(conn, t):
        company.refresh_company(conn, t, meta, cfg, use_network=(md_fetch is None))
    md.refresh_symbol(conn, t, 12, "1y", md_fetch if callable(md_fetch) else None, now)
    md.refresh_symbol(conn, "SPY", 12, "1y", md_fetch if callable(md_fetch) else None, now)
    reason["decided_at"] = c.get("decided_at") or iso(now)
    ref = c.get("ref_price")
    if ref is None and not existing_decision and not market_state(now)["execution"]:
        cl = md.closes(conn, t, 1)
        ref = cl[-1] if cl else None                                              # decided while the market is closed: the previous close is what the open will be compared with
    reason["ref_price"] = ref
    c["ref_price"] = ref
    f = ev.build(conn, c, cfg, regime, now, universe)
    with db.tx(conn):
        fid = ev.persist(conn, f, None, c.get("event_id"), now)
        did = existing_decision or _decision(conn, c, "PROPOSED", "BUY" if f["verdict"] == "BUY" else "NO_TRADE", reason, score, now, fid, f["ev_net"], None, "", f["thesis_key"])
        conn.execute("UPDATE forecasts SET decision_id=? WHERE id=?", (did, fid))
        if not existing_decision:
            learning.record_forecast_prediction(conn, fid, f, c, now)
    reason["forecast"] = {"id": fid, "verdict": f["verdict"], "ev_net": f["ev_net"], "confidence": f["confidence"], "why": f["why"]}
    comp = company.get(conn, t) or {}
    theme = (conn.execute("SELECT name FROM theses WHERE key=?", (f["thesis_key"],)).fetchone() or [comp.get("sector") or "Unclassified"])[0] if f["thesis_key"] else (comp.get("sector") or "Unclassified")

    def finish(status, why, **kw):
        reason["gates"].append(why)
        with db.tx(conn):
            _set_decision(conn, did, status, reason, **dict({"ev_net": f["ev_net"], "forecast_id": fid, "theme": theme, "thesis_key": f["thesis_key"], "action": "BUY" if f["verdict"] == "BUY" else "NO_TRADE"}, **kw))
        return {"status": status, "decision_id": did, "why": why}

    if score < cfg["portfolio"]["entry_score"]:
        return finish("REJECTED", "eligibility %.2f < %.2f" % (score, float(cfg["portfolio"]["entry_score"])), action="NO_TRADE")
    if f["verdict"] != "BUY":
        return finish("REJECTED", "; ".join(f["why"]) or f["verdict"], action="NO_TRADE")
    if paused(cfg):
        return finish("PAUSED", "trading paused by operator")
    th = thesis_engine.ticker_thesis_score(conn, t, regime.get("regime", "unknown"))
    if th["state"] == "Broken":
        return finish("RISK_REJECTED", "primary thesis %s is Broken" % th["primary"])
    hs = pf.holdings(conn, now)
    eq = pf.equity_of(conn, hs, now)
    dd = pf.drawdown(conn, eq["equity"])
    if dd >= cfg["risk"]["drawdown_pause_pct"]:
        return finish("RISK_REJECTED", "portfolio drawdown %.1f%% >= pause %.0f%%" % (dd * 100, cfg["risk"]["drawdown_pause_pct"] * 100))
    if pf.daily_return(conn, eq["equity"], now) <= -cfg["risk"]["daily_loss_pause_pct"]:
        return finish("RISK_REJECTED", "daily loss limit hit")
    state = {"equity": eq["equity"], "cash": eq["cash"], "holdings": hs, "scenario_probs": pf.scenario_probabilities(conn, regime)}
    sz = sizing.size_position(f, comp, state, cfg, regime, {"state": th["state"], "score": th["score"], "primary": f["thesis_key"]})
    reason["sizing"] = sz["reasons"]
    gross_free = max(0.0, cfg["risk"]["max_gross_pct"] - eq["gross"])
    opp = opportunity.evaluate(conn, f, sz["ok"], state, cfg, now, headroom_pct=min(sz["pct"], gross_free))
    reason["opportunity"] = opp["reason"]
    if not opp["ok"]:
        if opp.get("rotate_out"):
            conn.execute("INSERT OR IGNORE INTO exit_signals(position_id,created,trigger,severity,reason,status) VALUES(?,?,?,?,?,'OPEN')",
                         (opp["rotate_out"], iso(now), "SUPERIOR_OPPORTUNITY", 0.6, "%s offers EV %.2f%% (%s)" % (t, f["ev_net"] * 100, opp["reason"])))
            return finish("AWAITING_CAPACITY", opp["reason"])
        return finish("RISK_REJECTED", opp["reason"])
    if not sz["ok"]:
        return finish("CAPACITY_DEFERRED", "size %.2f%% below minimum %.2f%% (%s)" % (sz["pct"] * 100, cfg["risk"]["min_position_pct"] * 100, sz["binding"]))
    # ---- red team: a separate LLM pass tries to prove the idea wrong; the deterministic gate decides what to do with it
    rt, size_mult = None, 1.0
    client = (intel or {}).get("client")
    if cfg["intelligence"]["redteam_enabled"] and client is not None:
        rt = redteam.review(conn, cfg, client, c, f, comp, {"primary": f["thesis_key"], "state": th["state"], "p": th["p"]}, regime, now, did)
        if rt:
            g = redteam.gate(rt, cfg)
            reason["redteam"] = {"net_conviction": rt["net_conviction"], "valuation": rt["valuation_view"], "fatal": rt["fatal_flaw"], "gate": g, "bear": [b["point"] for b in rt["bear_points"][:3]], "bull": [b["point"] for b in rt["bull_points"][:3]]}
            conn.execute("UPDATE redteam_reviews SET gate=? WHERE decision_id=?", (json.dumps(g), did))
            if not g["ok"]:
                return finish("RISK_REJECTED", "red team: " + "; ".join(g["reasons"]))
            size_mult = g["size_mult"]
        elif cfg["intelligence"]["redteam_required"] and client.enabled("analyst"):
            return finish("RISK_REJECTED", "red-team review required but unavailable")
    if size_mult < 1.0:
        sz["amount"] *= size_mult
        sz["pct"] *= size_mult
        if sz["pct"] < cfg["risk"]["min_position_pct"]:
            return finish("CAPACITY_DEFERRED", "size after red-team adjustment %.2f%% below minimum" % (sz["pct"] * 100))
    if not market_state(now)["execution"]:
        return finish("AWAITING_MARKET", "market session %s; will retry while the event is fresh" % market_state(now)["session"])
    if not cfg["risk"].get("allow_add_to_position", False):
        held = conn.execute("SELECT id,opened FROM positions WHERE ticker=? AND status='OPEN' ORDER BY id LIMIT 1", (t,)).fetchone()
        if held:
            return finish("DUPLICATE", "already holding %s (position #%d, opened %s): a second position in the same stock would stack the same bet" % (t, held["id"], held["opened"][:16].replace("T", " ")))
    pt = pretrade.review(conn, cfg, c, reason["decided_at"], now, universe=universe)      # last look #1: later news / market shock (no price needed)
    if pt["action"] == "veto":
        return finish("VETOED", pt["reason"])
    if pt["action"] == "hold":
        return finish("HELD", pt["reason"])
    q = quote_fn(conn, t, "entry")
    if not q or q.get("status") != "PASS" or not q.get("execution_eligible") or q.get("price") is None:
        return finish("AWAITING_QUOTE", "market is open but no verified price: " + quote_diagnosis(q, cfg))      # distinct from "market closed"; retried every cycle
    pt = pretrade.review(conn, cfg, c, reason["decided_at"], now, q["price"], universe=universe)      # last look #2: the overnight gap, now that the price is verified
    if pt["action"] == "veto":
        return finish("VETOED", pt["reason"])
    reason["gates"].append("all gates passed")
    out = execute_buy(conn, cfg, c, f, sz, q, did, fid, now, theme, comp.get("sector") or "Unclassified", reason)
    out["decision_id"] = did
    if out.get("status") == "EXECUTED" and rt:
        with db.tx(conn):
            redteam.store_tripwires(conn, out["position_id"], conn.execute("SELECT entry_price FROM positions WHERE id=?", (out["position_id"],)).fetchone()[0], rt, now)
    return out


def candidate_from_decision(row) -> Optional[dict]:
    try:
        r = json.loads(row["reason"])
        c = dict(r["candidate"])
    except (ValueError, KeyError, TypeError):
        return None
    c["ticker"] = row["ticker"]
    c["ref_price"] = r.get("ref_price")                                           # previous close when the BUY was decided (overnight gap test)
    c["decided_at"] = r.get("decided_at") or row["created"]                      # "since when" the last-look news / shock checks apply
    c.setdefault("origin", row["origin"] or "event")
    c["horizon"] = c.get("horizon") or 8
    return c


def _defer(conn, c: dict, now, why: str) -> int:
    """Record a candidate we have no capacity to trade THIS cycle, so it is retried next cycle instead of silently vanishing (the old loop just `break`-ed)."""
    reason = {"candidate": {k: c.get(k) for k in ("event_id", "news_uid", "event_type", "direction", "magnitude", "conf", "credibility", "age_hours", "headline", "source", "amount_usd", "horizon", "origin", "thesis_key", "p", "mechanism", "chain_order")},
              "gates": [why]}
    with db.tx(conn):
        return _decision(conn, c, "AWAITING_CAPACITY", "BUY", reason, eligibility_score(c), now, None, None, None, "", c.get("thesis_key"))


def backfill_reference_prices(conn, now) -> int:
    """BUYs queued by an OLDER build have no 'previous close' to measure the overnight gap against, so the gap check would silently skip them (one executed straight into a -9% gap in testing).
    While the market is closed the last close IS that reference, so fill it in then; never during the session, when the 'last' bar is today's live price."""
    if market_state(now)["execution"]:
        return 0
    n = 0
    for row in conn.execute("SELECT id,ticker,created,reason FROM decisions WHERE status IN ('AWAITING_MARKET','AWAITING_QUOTE','AWAITING_CAPACITY','HELD') AND ticker!=''").fetchall():
        try:
            r = json.loads(row["reason"] or "{}")
        except ValueError:
            continue
        if not isinstance(r, dict) or r.get("ref_price"):
            continue
        bar = conn.execute("SELECT close,day FROM market_series WHERE symbol=? ORDER BY day DESC LIMIT 1", (row["ticker"],)).fetchone()
        when = parse_ts(bar["day"]) if bar else None
        if not bar or not bar["close"] or not when or (now - when).total_seconds() > 6 * 86400:
            continue                                                    # no recent daily bar: better no gap test than a wrong one
        r["ref_price"] = float(bar["close"])
        r.setdefault("decided_at", row["created"])
        conn.execute("UPDATE decisions SET reason=? WHERE id=?", (json.dumps(r, sort_keys=True, default=str), row["id"]))
        n += 1
    return n


def run_entries(conn, cfg: dict, universe, candidates: List[dict], regime: dict, now, quote_fn: Callable, md_fetch=None, intel: Optional[dict] = None, deadline: Optional[float] = None) -> dict:
    out = {"processed": 0, "executed": 0, "retried": 0, "deferred": 0, "errors": 0, "by_status": {}}
    icfg = cfg["intelligence"]
    cap = int(cfg["risk"]["max_new_per_cycle"])          # 0 = unlimited: funds / risk limits / the time budget are the real limits
    new_exec = 0                                    # executions this cycle (retries AND fresh) count against the per-cycle cap
    try:
        out["backfilled_ref"] = backfill_reference_prices(conn, now)
    except Exception:  # noqa: BLE001 - bookkeeping must never stop trading
        out["backfilled_ref"] = 0

    def late() -> bool:
        return deadline is not None and time.time() > deadline            # out of time: the rest stay in the durable queue for the next cycle

    def attempt(c: dict, **kw) -> dict:
        """One candidate, isolated: an exception becomes a visible ERROR decision instead of aborting every candidate after it."""
        try:
            return process_candidate(conn, cfg, universe, c, regime, now, quote_fn, md_fetch, intel=intel, **kw)
        except Exception as e:  # noqa: BLE001
            out["errors"] += 1
            try:
                with db.tx(conn):
                    did = _decision(conn, c, "ERROR", "BUY", {"error": "%s: %s" % (type(e).__name__, str(e)[:200]), "candidate": {k: c.get(k) for k in ("event_id", "event_type", "headline")}, "gates": ["internal error"]},
                                    eligibility_score(c), now, None, None, None, "", c.get("thesis_key"))
            except Exception:  # noqa: BLE001
                did = 0
            return {"status": "ERROR", "decision_id": did}

    def tally(r):
        nonlocal new_exec
        out["by_status"][r["status"]] = out["by_status"].get(r["status"], 0) + 1
        if r["status"] == "EXECUTED":
            out["executed"] += 1
            new_exec += 1

    # 1. retry pending decisions while the underlying event is still fresh (skipped cheaply once the cap is used up)
    market_open = market_state(now)["execution"]
    out["waiting_market"] = 0
    for row in conn.execute("SELECT * FROM decisions WHERE status IN ('AWAITING_MARKET','AWAITING_QUOTE','AWAITING_CAPACITY','HELD') ORDER BY COALESCE(ev_net,-9) DESC, id LIMIT 80").fetchall():
        c = candidate_from_decision(row)
        age = (now - parse_ts(row["created"])).total_seconds() / 3600 if parse_ts(row["created"]) else 999
        total_age = ((c.get("age_hours") or 0) + age) if c else 999          # how old the NEWS is now = its age when decided + the time spent waiting
        if not c or total_age > icfg["trade_event_max_age_hours"]:
            with db.tx(conn):
                conn.execute("UPDATE decisions SET status='EXPIRED' WHERE id=?", (row["id"],))
            continue
        if row["status"] in ("AWAITING_MARKET", "AWAITING_QUOTE", "HELD") and not market_open:
            out["waiting_market"] += 1              # nothing can change until the open: do NOT redo the evaluation and market-data calls every 10 minutes all night
            continue
        if (cap > 0 and new_exec >= cap):
            out["deferred"] += 1                    # still pending; will be retried next cycle
            continue
        c["age_hours"] = total_age
        if conn.execute("SELECT 1 FROM positions WHERE decision_id=?", (row["id"],)).fetchone():
            continue
        if late():
            break
        r = attempt(c, existing_decision=row["id"])
        out["retried"] += 1
        tally(r)
    # 2. fresh candidates, best first. Beyond the per-cycle cap they are DEFERRED (recorded, retried next cycle), never dropped.
    for c in sorted(candidates, key=lambda x: -(x["magnitude"] * abs(x["direction"]) * x.get("credibility", 0.5))):
        if c.get("event_id") and conn.execute("SELECT 1 FROM decisions WHERE event_id=? AND ticker=? AND origin!='thesis'", (c["event_id"], c["ticker"])).fetchone():
            candqueue.done(conn, c, now)                     # already decided (e.g. a retry after a crash): just close the queue entry
            continue
        if late():
            break
        if c.get("origin") == "thesis" and conn.execute("SELECT 1 FROM decisions WHERE ticker=? AND origin='thesis' AND created>?", (c["ticker"], iso(now - dt.timedelta(hours=24)))).fetchone():
            continue
        if (cap > 0 and new_exec >= cap):
            _defer(conn, c, now, "per-cycle new-position cap (%d) reached; retried next cycle" % cap)
            candqueue.done(conn, c, now)                     # the AWAITING_CAPACITY decision row now carries it
            out["deferred"] += 1
            out["by_status"]["AWAITING_CAPACITY"] = out["by_status"].get("AWAITING_CAPACITY", 0) + 1
            continue
        r = attempt(c)
        candqueue.done(conn, c, now, "ERROR" if r["status"] == "ERROR" else "DONE")
        out["processed"] += 1
        tally(r)
    return out


def thesis_candidates(conn, cfg: dict, regime: dict, universe) -> List[dict]:
    icfg = cfg["intelligence"]
    if not icfg["allow_thesis_entries"]:
        return []
    held = {r[0] for r in conn.execute("SELECT ticker FROM positions WHERE status='OPEN'")}
    out = []
    for o in thesis_engine.thesis_candidates(conn, regime.get("regime", "unknown"), icfg["thesis_entry_min_probability"], held, limit=4):
        if universe is not None and o["ticker"] not in universe:
            continue
        out.append({"ticker": o["ticker"], "direction": min(1.0, 0.4 + o["net"]), "magnitude": min(1.0, 0.25 + 0.5 * (o["p"] - 0.5)), "event_type": "thesis_signal", "conf": 0.7, "credibility": 0.7,
                    "age_hours": 0.0, "horizon": 10, "origin": "thesis", "thesis_key": o["thesis_key"], "p": o["p"], "headline": "Thesis-driven: %s (P=%.0f%%)" % (o["thesis_key"], o["p"] * 100), "source": "thesis_engine"})
    return out
