"""Paper options: BOUGHT calls (bullish) and puts (bearish), and selling them back. All paper money, and all MODEL-PRICED.

There is no options data feed behind this bot, so every price here comes from a Black-Scholes model fed with a volatility estimate (the stock's own recent volatility x instruments.options_iv_mult, a little
higher for earnings), minus a spread and a commission on each side. That is a reasonable stand-in for a liquid large-cap, but it is NOT a verified track record: real prices have skew, an implied volatility
that collapses after the news, wider spreads in a fast market, and early-exercise quirks that this model ignores. Results are reported separately from stocks for exactly that reason.

Risk is defined by construction: a bought option can lose at most its premium, and the whole sleeve is capped (instruments.options_max_premium_pct of equity, options_per_trade_pct per trade).
An option is used instead of the stock only for the strongest, shortest-horizon ideas in the most liquid names; everything else is traded in the stock.
Writing (selling) options and spreads are deliberately NOT supported: their risk is open-ended or needs margin rules this simulation does not model."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from typing import Callable, Dict, Optional

from . import db, marketdata as md, optionsdata, portfolio as pf
from .timeutil import iso, market_state, parse_ts, trading_days_between, utcnow

EARNINGS_TYPES = {"earnings_beat", "earnings_miss", "guidance_raise", "guidance_cut"}
CALL, PUT = "CALL", "PUT"


# ------------------------------------------------------------------------------------------------ the maths
_ncdf, bs_price, bs_delta, years_left = optionsdata._ncdf, optionsdata.bs_price, optionsdata.bs_delta, optionsdata.years_left


def strike_step(S: float) -> float:
    return 0.5 if S < 25 else 1.0 if S < 100 else 2.5 if S < 200 else 5.0 if S < 500 else 10.0


def pick_strike(kind: str, S: float) -> float:
    """At the money or just out of it: a call at the first strike on or above the price, a put at the first on or below."""
    st = strike_step(S)
    return round((math.ceil(S / st - 1e-9) if kind == CALL else math.floor(S / st + 1e-9)) * st, 2)


def next_expiry(today: dt.date, min_dte: int) -> dt.date:
    """The first Friday that is at least `min_dte` calendar days away (weekly options exist for the liquid names this is used on)."""
    d = today + dt.timedelta(days=int(min_dte))
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def iv_estimate(conn, ticker: str, cfg: dict, event_type: str = "") -> float:
    """The volatility the model prices with: realised volatility x a premium (options trade above realised), a little more around earnings, clamped to a sane range."""
    inst = cfg["instruments"]
    vd = md.daily_vol(conn, ticker, 60, default=0.025)
    iv = vd * math.sqrt(252.0) * float(inst.get("options_iv_mult", 1.15)) * (1.10 if event_type in EARNINGS_TYPES else 1.0)
    return max(0.20, min(1.50, iv))


def price_option(kind: str, S: float, K: float, expiry: dt.date, sigma: float, now: dt.datetime, cfg: dict) -> dict:  # noqa: D401
    """mid / bid / ask: the model value, with a spread of 4% of the price (at least 3 cents) each side of it."""
    T = years_left(expiry, now)
    mid = bs_price(kind, S, K, T, float(cfg["instruments"].get("options_risk_free", 0.04)), sigma)
    half = max(0.03, 0.04 * mid)
    return {"mid": mid, "bid": max(0.01, mid - half), "ask": mid + half, "half": half, "T": T}


# ------------------------------------------------------------------------------------------------ the sleeve
def sleeve_used(conn) -> float:
    return float(conn.execute("SELECT COALESCE(SUM(cost),0) FROM option_positions WHERE status='OPEN'").fetchone()[0] or 0.0)


RESCUE_WHY = re.compile(r"^(swing reward:risk|EV net [-+0-9.]+% below minimum)")


def rescuable(f: dict, cfg: dict) -> bool:
    """A bearish idea the STOCK rules turned down only for reasons that are about a stock position (the swing reward:risk shape, or an edge a little under the stock minimum). An option's loss is capped at its premium and its
    payoff is not a stop-and-target plan, so those reasons do not apply to it. Anything else (negative EV, low confidence, an earnings report inside the hold, illiquidity ...) is NOT rescuable."""
    if not isinstance(f, dict) or not isinstance(cfg, dict):
        return False
    inst = cfg.get("instruments") if isinstance(cfg.get("instruments"), dict) else {}
    if not (inst.get("options_enabled") and inst.get("options_rescue_enabled", True)):
        return False
    try:
        ev_ok = float(f.get("ev_net") or 0.0) > 0.0
    except (TypeError, ValueError, OverflowError):
        return False
    if f.get("verdict") != "WATCH" or f.get("direction") != "SHORT" or not ev_ok:
        return False
    why = f.get("why")
    return isinstance(why, list) and bool(why) and all(isinstance(w, str) and RESCUE_WHY.match(w) for w in why)


def option_ev(cfg: dict, f: dict, plan: dict, price: float, now) -> Optional[float]:
    """The expected return on the PREMIUM of this actual contract, under the forecast's own scenarios: for each scenario the underlying moves, the option is repriced with time passed (decay) and sold at the BID (the exit
    spread), commissions both ways. Each scenario's return is capped at +200% so a tail scenario cannot carry the number. None when the forecast has no scenarios."""
    scen = f.get("scenarios") if isinstance(f, dict) else None
    if not isinstance(scen, dict) or not scen or not isinstance(plan, dict) or not plan:
        return None
    try:
        kind, K, expiry, sigma = plan["kind"], float(plan["strike"]), dt.date.fromisoformat(plan["expiry"]), float(plan["sigma"])
        h = max(1, int(f.get("horizon_days") or 1))
        then = now + dt.timedelta(days=int(math.ceil(h * 7 / 5)))
        comm, ask = float(plan["commission"]), float(plan["ask"])
        cap = float((cfg.get("instruments") or {}).get("options_scenario_cap", 2.0))
        ev_, ptot = 0.0, 0.0
        for v in scen.values():
            p, ret = float(v["p"]), float(v["ret"])
            s_t = max(0.01, price * (1.0 - ret) if kind == PUT else price * (1.0 + ret))
            bid = price_option(kind, s_t, K, expiry, sigma, then, cfg)["bid"]
            r = (bid * 100.0 - comm) / (ask * 100.0 + comm) - 1.0
            ev_ += p * max(-1.0, min(cap, r))
            ptot += p
        return ev_ / ptot if ptot > 0 else None
    except Exception:  # noqa: BLE001 - an expected return that cannot be computed is "unknown", never an error in a trade decision
        return None


def _no(refusals: Optional[list], msg: str) -> None:
    """Refuse, and say why: every reason an approved idea did not become an option is recorded so the next review can see which limit actually binds."""
    if refusals is not None:
        refusals.append(msg)
    return None


def trial_breaker(conn, cfg: dict, equity: float) -> Optional[str]:
    """The put trial pauses itself: if its last N closed trades have lost more than a set share of equity, no new trial trade opens until the record recovers."""
    inst = cfg["instruments"]
    n, pct = int(inst.get("options_trial_breaker_trades", 8)), float(inst.get("options_trial_breaker_pct", 0.005))
    rows = conn.execute("SELECT pnl FROM option_positions WHERE status='CLOSED' AND (note LIKE '[TRIAL%' OR note LIKE '[RESCUE%') ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    if len(rows) < n:
        return None
    total = sum((r[0] or 0.0) for r in rows)
    return ("the trial is paused: its last %d trades lost $%.0f, more than %.1f%% of equity" % (n, -total, pct * 100)) if total < -pct * equity else None


def scorecard(conn) -> list:
    """What each kind of option trade has actually done, by tier. The sample size is part of the answer: a handful of trades is an anecdote."""
    out: dict = {}
    for r in conn.execute("SELECT kind,note,status,cost,pnl FROM option_positions").fetchall():
        n_ = r["note"] or ""
        k = (r["kind"], "rescue" if n_.startswith("[RESCUE") else "trial" if n_.startswith("[TRIAL") else "core")
        g = out.setdefault(k, {"kind": k[0], "tier": k[1], "open": 0, "closed": 0, "wins": 0, "pnl": 0.0, "cost_closed": 0.0, "best": None, "worst": None})
        if r["status"] == "OPEN":
            g["open"] += 1
            continue
        p = float(r["pnl"] or 0.0)
        g["closed"] += 1
        g["wins"] += 1 if p > 0 else 0
        g["pnl"] += p
        g["cost_closed"] += float(r["cost"] or 0.0)
        g["best"] = p if g["best"] is None else max(g["best"], p)
        g["worst"] = p if g["worst"] is None else min(g["worst"], p)
    for g in out.values():
        g["return_on_premium"] = (g["pnl"] / g["cost_closed"]) if g["cost_closed"] else None
        g["evidence"] = "anecdote: too few trades to say anything" if g["closed"] < 10 else "informational" if g["closed"] < 30 else "preliminary" if g["closed"] < 60 else "review candidate"
    return sorted(out.values(), key=lambda g: (g["kind"], g["tier"]))


def plan_for(conn, cfg: dict, f: dict, c: dict, comp: dict, price: float, equity: float, now, refusals: Optional[list] = None) -> Optional[dict]:
    """Should this approved idea be expressed as an option? -> a plan, or None. Real contracts from the free feeds are used when they answer (the contract must really exist and be liquid); the pure model is the
    labelled fallback. Every reason it says no is a cost control, not a hunch."""
    inst = cfg["instruments"]
    if not inst.get("options_enabled"):
        return None
    kind = PUT if f["verdict"] == "SHORT" else CALL
    if f["horizon_days"] > int(inst["options_max_horizon_days"]):
        return _no(refusals, "the idea runs %d sessions, longer than the %d an option can carry before time decay bites" % (f["horizon_days"], int(inst["options_max_horizon_days"])))
    tier = "core"
    if f.get("rescue"):
        if float(f["ev_net"]) <= 0.0:
            return _no(refusals, "edge %.1f%% is not positive" % (f["ev_net"] * 100))
        paused = trial_breaker(conn, cfg, equity)
        if paused:
            return _no(refusals, paused)
        tier = "rescue"
    elif f["ev_net"] < float(inst["options_min_ev"]):                                              # time decay punishes weak ideas: only the half-size PUT trial may go lower
        if not inst.get("options_trial_enabled", True):
            return _no(refusals, "edge %.1f%% is below the %.0f%% an option needs" % (f["ev_net"] * 100, float(inst["options_min_ev"]) * 100))
        if inst.get("options_trial_puts_only", True) and kind != PUT:
            return _no(refusals, "edge %.1f%% is below the %.0f%% an option needs (the smaller trial tier is for puts only)" % (f["ev_net"] * 100, float(inst["options_min_ev"]) * 100))
        floor = max(float(inst.get("options_trial_min_ev", 0.02)), float(inst["options_min_ev"]) - 0.02)          # the trial is a bounded step BELOW the normal minimum (never more than two points): an owner who raises the minimum has not asked for puts on much weaker ideas
        if f["ev_net"] < floor:
            return _no(refusals, "edge %.1f%% is below even the %.1f%% the smaller put trial needs" % (f["ev_net"] * 100, floor * 100))
        paused = trial_breaker(conn, cfg, equity)
        if paused:
            return _no(refusals, paused)
        tier = "trial"
    adv = (comp or {}).get("adv_usd")
    if price < float(inst["options_min_price"]) or not adv or adv < float(inst["options_min_adv_usd"]):
        return _no(refusals, "options need a stock over $%.0f trading at least $%.0fM a day; this one does not" % (float(inst["options_min_price"]), float(inst["options_min_adv_usd"]) / 1e6))      # only liquid, higher-priced names have the option markets this resembles
    if conn.execute("SELECT 1 FROM option_positions WHERE underlying=? AND status='OPEN'", (c["ticker"],)).fetchone():
        return _no(refusals, "an option on this stock is already open")
    model_sigma = iv_estimate(conn, c["ticker"], cfg, c.get("event_type", ""))
    source, contract, K, expiry, sigma, px, mkt_note = "MODEL", "", None, None, model_sigma, None, ""
    ch = optionsdata.chain_for(conn, cfg, c["ticker"], now, price) if inst.get("options_use_real_quotes", True) else None      # fetching also records TODAY's implied volatility
    rank = optionsdata.iv_rank(conn, c["ticker"])
    if rank is not None and rank > float(inst.get("options_max_iv_rank", 80)):
        return _no(refusals, "implied volatility is at the %.0fth percentile of its range: the option would be bought expensive" % rank)                                                                             # volatility is expensive: the option would be bought at the top of its range
    if inst.get("options_use_real_quotes", True):
        if ch:
            q, why = optionsdata.pick(ch["contracts"], kind, price, now, cfg)
            if q is None:
                return _no(refusals, "the real market has no liquid contract for this (%s)" % "spread, open interest or quote age failed")      # trade the stock instead
            a = optionsdata.anchor(q, price, ch.get("underlying"), now, cfg)
            if a:
                source, K, expiry, sigma, px = "CHAIN", q["strike"], dt.date.fromisoformat(q["expiry"]), a["iv"], a
                contract = optionsdata.build_occ(c["ticker"], q["expiry"], kind, q["strike"])
                mkt_note = "real %s contract %s, market spread %.0f%%, open interest %s, IV %.0f%%, from %s" % (kind.lower(), contract, (q["ask"] - q["bid"]) / q["mid"] * 100, q.get("oi") if q.get("oi") is not None else "n/a", a["iv"] * 100, "+".join(q["sources"]))
                try:
                    optionsdata.log_model_check(conn, c["ticker"], q, price, model_sigma, now, cfg)
                except Exception:  # noqa: BLE001 - a log line must never stop a trade
                    pass
        if source != "CHAIN" and not inst.get("options_allow_model_fallback", True):
            return _no(refusals, "no real option quote answered and the model fallback is switched off")
    if px is None:
        expiry = next_expiry(now.date(), int(inst["options_min_dte"]))
        K = pick_strike(kind, price)
        px = price_option(kind, price, K, expiry, sigma, now, cfg)
        mkt_note = "MODEL price: no free feed answered"
    if px["ask"] < 0.30:
        return _no(refusals, "the option costs under $0.30: penny options do not trade like this")                                                                             # penny options do not trade like this
    comm = float(inst.get("options_commission", 0.65))
    room = float(inst["options_max_premium_pct"]) * equity - sleeve_used(conn)
    per_trade = float(inst.get("options_rescue_per_trade_pct", 0.006)) if tier == "rescue" else float(inst.get("options_trial_per_trade_pct", 0.006)) if tier == "trial" else float(inst["options_per_trade_pct"])
    budget = min(per_trade * equity, room)
    contracts = int(budget // (px["ask"] * 100.0 + comm))
    if contracts < 1:
        return _no(refusals, "the budget ($%.0f) does not buy one contract at $%.2f" % (budget, px["ask"] * 100.0))
    sg = -1.0 if kind == PUT else 1.0
    oev = None
    if tier != "core":                                                                           # the smaller tiers must pay on THIS contract: its premium, its decay, its exit spread
        draft = {"kind": kind, "strike": K, "expiry": expiry.isoformat(), "sigma": sigma, "ask": px["ask"], "commission": comm}
        oev = option_ev(cfg, f, draft, price, now)
        need = float(inst.get("options_min_option_ev", 0.10))
        if oev is None or oev < need:
            return _no(refusals, "this contract's own expected return on the premium is %s, under the %.0f%% the smaller tiers need" % ("unknown" if oev is None else "%+.0f%%" % (oev * 100), need * 100))
    return {"kind": kind, "strike": K, "expiry": expiry.isoformat(), "contracts": contracts, "sigma": sigma, "ask": px["ask"], "mid": px["mid"], "bid": px["bid"], "commission": comm, "source": source, "contract": contract, "tier": tier, "option_ev": oev,
            "stop_underlying": price * (1.0 - sg * abs(f["stop_return"])), "target_underlying": price * (1.0 + sg * f["target_return"]),
            "why": ("[TRIAL, half size] " if tier == "trial" else "[RESCUE, half size] " if tier == "rescue" else "") + "%s: edge %.1f%% over %d sessions is strong and short enough that time decay will not eat it; %s is liquid ($%.0fM a day); %s" % ("a put" if kind == PUT else "a call", f["ev_net"] * 100, f["horizon_days"], c["ticker"], adv / 1e6, mkt_note)}


def open_option(conn, cfg: dict, c: dict, f: dict, plan: dict, price: float, did: int, fid: int, now, theme: str, sector: str, reason: dict, set_decision: Callable) -> dict:
    cost = round(plan["contracts"] * 100.0 * plan["ask"] + plan["contracts"] * plan["commission"], 2)
    with db.tx(conn):
        hs = pf.holdings(conn, now)
        eq = pf.equity_of(conn, hs, now)["equity"]
        cash = db.cash(conn)
        if cash - cost - pf.short_reserve(hs, cfg) < cfg["risk"]["min_cash_pct"] * eq * 0.99 or cash - cost < 0:
            set_decision(conn, did, "CAPACITY_DEFERRED", dict(reason, note="cash reserve would be breached by the option premium"))
            return {"status": "CAPACITY_DEFERRED"}
        conn.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)", (iso(now), "OPTION_BUY", -cost, "%s:%d" % (c["ticker"], did), round(cash - cost, 2)))
        cur = conn.execute("INSERT INTO option_positions(underlying,kind,strike,expiry,contracts,multiplier,opened,entry_premium,entry_underlying,entry_iv,cost,decision_id,forecast_id,status,last_mark,last_underlying,last_mark_at,"
                           "stop_underlying,target_underlying,max_hold_days,theme,sector,event_type,note) VALUES(?,?,?,?,?,100,?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,?,?,?,?,?)",
                           (c["ticker"], plan["kind"], plan["strike"], plan["expiry"], plan["contracts"], iso(now), plan["ask"], price, plan["sigma"], cost, did, fid, plan["bid"], price, iso(now), plan["stop_underlying"],
                            plan["target_underlying"], int(cfg["instruments"]["options_max_hold_days"]), theme, sector, c.get("event_type"), plan["why"]))
        oid = cur.lastrowid
        conn.execute("UPDATE option_positions SET price_source=?,contract=?,last_iv=? WHERE id=?", (plan.get("source", "MODEL"), plan.get("contract", ""), plan["sigma"], oid))
        reason = dict(reason, instrument="OPTION", option={k: plan[k] for k in ("kind", "strike", "expiry", "contracts", "ask", "sigma")}, option_why=plan["why"], model_priced=(plan.get("source", "MODEL") != "CHAIN"), price_source=plan.get("source", "MODEL"))
        set_decision(conn, did, "EXECUTED", reason, entry_price=plan["ask"], shares=plan["contracts"], amount=cost, action="OPTION_" + plan["kind"])
        db.audit(conn, "options", "option_buy", c["ticker"], {"decision": did, "kind": plan["kind"], "strike": plan["strike"], "expiry": plan["expiry"], "contracts": plan["contracts"], "cost": cost})
    return {"status": "EXECUTED", "option_id": oid, "cost": cost, "instrument": "OPTION"}


def maybe_open(conn, cfg: dict, c: dict, f: dict, q: dict, did: int, fid: int, now, theme: str, comp: dict, sector: str, reason: dict, equity: float, set_decision: Callable) -> Optional[dict]:
    why: list = []
    plan = plan_for(conn, cfg, f, c, comp, float(q["price"]), equity, now, why)
    if not plan and why:
        reason["option_considered"] = why[0]                                                     # recorded for EVERY approved idea: why it stayed a stock trade
    return open_option(conn, cfg, c, f, plan, float(q["price"]), did, fid, now, theme, sector, reason, set_decision) if plan else None


# ------------------------------------------------------------------------------------------------ marking and exits
def _row_inputs(cfg: dict, o) -> tuple:
    return o["kind"], float(o["strike"]), parse_ts(o["expiry"] + "T20:00:00+00:00").date(), float(o["entry_iv"])


def mark(cfg: dict, o, S: float, now) -> dict:
    kind, K, expiry, sigma = _row_inputs(cfg, o)
    return price_option(kind, S, K, expiry, sigma, now, cfg)


def close_option(conn, cfg: dict, o, S: float, px: dict, trigger: str, reason: str, now) -> dict:
    expired = px["T"] <= 0
    fill = max(0.0, px["mid"]) if expired else max(0.01, px["bid"])                           # at expiry it settles at intrinsic value (no spread, no commission); before that you sell at the bid
    comm = 0.0 if expired else float(cfg["instruments"].get("options_commission", 0.65)) * o["contracts"]
    proceeds = round(o["contracts"] * o["multiplier"] * fill - comm, 2)
    pnl = round(proceeds - float(o["cost"]), 2)
    with db.tx(conn):
        cash = db.cash(conn)
        conn.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)", (iso(now), "OPTION_SELL", proceeds, "%s:%d" % (o["underlying"], o["id"]), round(cash + proceeds, 2)))
        conn.execute("UPDATE option_positions SET status='CLOSED',closed=?,exit_premium=?,exit_underlying=?,pnl=?,review_code=?,last_mark=?,last_underlying=?,last_mark_at=?,note=COALESCE(note,'')||? WHERE id=?",
                     (iso(now), fill, S, pnl, trigger, fill, S, iso(now), " | closed: " + reason, o["id"]))
        db.audit(conn, "options", "option_sell", o["underlying"], {"id": o["id"], "trigger": trigger, "pnl": pnl})
    return {"ticker": o["underlying"], "result": "CLOSED", "trigger": trigger, "pnl": pnl}


def run(conn, cfg: dict, now=None, quote_fn: Optional[Callable] = None) -> dict:
    """Mark every open option to the model at the current stock price, and close the ones whose exit has come. Marks are written whenever there is a price (so equity is current); closing needs an open
    market and a fresh verified quote, like a stock exit."""
    now = now or utcnow()
    inst = cfg["instruments"]
    out = {"checked": 0, "closed": 0, "blocked": 0, "details": []}
    rows = conn.execute("SELECT * FROM option_positions WHERE status='OPEN' ORDER BY id").fetchall()
    open_now = market_state(now)["execution"]
    for o in rows:
        out["checked"] += 1
        try:
            q = quote_fn(conn, o["underlying"], "exit") if (quote_fn and open_now) else None
            fresh = bool(q and q.get("status") == "PASS" and q.get("execution_eligible") and q.get("price") is not None)
            if fresh:
                S = float(q["price"])
            else:
                v = md_valuation(conn, o["underlying"], now)
                S = v if v else float(o["last_underlying"] or o["entry_underlying"])
            px = None
            if (o["price_source"] or "MODEL") == "CHAIN" and inst.get("options_use_real_quotes", True):
                ch = optionsdata.chain_for(conn, cfg, o["underlying"], now, S)                      # the market's latest implied volatility and spread (about 15 minutes old), applied to the LIVE stock price
                q = optionsdata.find(ch, o)
                px = optionsdata.anchor(q, S, ch.get("underlying"), now, cfg) if q else None
            if px is None:
                px = mark(cfg, o, S, now)
            conn.execute("UPDATE option_positions SET last_mark=?,last_underlying=?,last_mark_at=?,last_iv=? WHERE id=?", (px["bid"] if px["T"] > 0 else px["mid"], S, iso(now), px.get("iv", o["entry_iv"]), o["id"]))
            kind = o["kind"]
            expired = px["T"] <= 0
            if not (fresh or expired):
                out["blocked"] += 1
                out["details"].append({"ticker": o["underlying"], "result": "MARKED_ONLY"})
                continue
            held = trading_days_between(o["opened"], now)
            entry = float(o["entry_premium"])
            bid = px["bid"]
            trig, why = None, ""
            if expired:
                trig, why = "EXPIRED", "reached expiry: settled at intrinsic value"
            elif (S <= float(o["stop_underlying"]) if kind == CALL else S >= float(o["stop_underlying"])):
                trig, why = "UNDERLYING_STOP", "the stock reached the idea's stop (%.2f)" % float(o["stop_underlying"])
            elif bid <= entry * (1.0 - float(inst["options_stop_loss_pct"])):
                trig, why = "PREMIUM_STOP", "the option lost %.0f%% of its price" % ((1 - bid / entry) * 100)
            elif bid >= entry * (1.0 + float(inst["options_target_pct"])):
                trig, why = "TARGET_REACHED", "the option gained %.0f%%" % ((bid / entry - 1) * 100)
            elif (S >= float(o["target_underlying"]) if kind == CALL else S <= float(o["target_underlying"])):
                trig, why = "UNDERLYING_TARGET", "the stock reached the idea's target (%.2f)" % float(o["target_underlying"])
            elif (parse_ts(o["expiry"] + "T20:00:00+00:00") - now).days <= int(inst["options_exit_dte"]):
                trig, why = "EXPIRY_NEAR", "%d days to expiry: time decay is at its fastest" % max(0, (parse_ts(o["expiry"] + "T20:00:00+00:00") - now).days)
            elif held >= int(o["max_hold_days"]):
                trig, why = "TIME_EXPIRED", "held %d sessions" % held
            if trig:
                r = close_option(conn, cfg, o, S, px, trig, why, now)
                out["closed"] += 1
                out["details"].append(r)
            else:
                out["details"].append({"ticker": o["underlying"], "result": "HOLD"})
        except Exception as e:  # noqa: BLE001 - one option's failure must not stop the others
            out["details"].append({"ticker": o["underlying"], "result": "ERROR", "error": "%s: %s" % (type(e).__name__, e)})
    return out


def md_valuation(conn, ticker: str, now) -> Optional[float]:
    from . import quotes
    v = quotes.valuation_price(conn, ticker, now)
    return float(v["price"]) if v else None
