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
from typing import Callable, Dict, Optional

from . import db, marketdata as md, portfolio as pf
from .timeutil import iso, market_state, parse_ts, trading_days_between, utcnow

EARNINGS_TYPES = {"earnings_beat", "earnings_miss", "guidance_raise", "guidance_cut"}
CALL, PUT = "CALL", "PUT"


# ------------------------------------------------------------------------------------------------ the maths
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(kind: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes value of a European option (no dividends). At or past expiry it is the intrinsic value."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if kind == CALL else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == CALL:
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def bs_delta(kind: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return (1.0 if S > K else 0.0) if kind == CALL else (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return _ncdf(d1) if kind == CALL else _ncdf(d1) - 1.0


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


def years_left(expiry: dt.date, now: dt.datetime) -> float:
    close = dt.datetime(expiry.year, expiry.month, expiry.day, 20, 0, tzinfo=dt.timezone.utc)          # ~4pm New York on the expiry date
    return max(0.0, (close - now).total_seconds() / (365.0 * 86400.0))


def iv_estimate(conn, ticker: str, cfg: dict, event_type: str = "") -> float:
    """The volatility the model prices with: realised volatility x a premium (options trade above realised), a little more around earnings, clamped to a sane range."""
    inst = cfg["instruments"]
    vd = md.daily_vol(conn, ticker, 60, default=0.025)
    iv = vd * math.sqrt(252.0) * float(inst.get("options_iv_mult", 1.15)) * (1.10 if event_type in EARNINGS_TYPES else 1.0)
    return max(0.20, min(1.50, iv))


def price_option(kind: str, S: float, K: float, expiry: dt.date, sigma: float, now: dt.datetime, cfg: dict) -> dict:
    """mid / bid / ask: the model value, with a spread of 4% of the price (at least 3 cents) each side of it."""
    T = years_left(expiry, now)
    mid = bs_price(kind, S, K, T, float(cfg["instruments"].get("options_risk_free", 0.04)), sigma)
    half = max(0.03, 0.04 * mid)
    return {"mid": mid, "bid": max(0.01, mid - half), "ask": mid + half, "half": half, "T": T}


# ------------------------------------------------------------------------------------------------ the sleeve
def sleeve_used(conn) -> float:
    return float(conn.execute("SELECT COALESCE(SUM(cost),0) FROM option_positions WHERE status='OPEN'").fetchone()[0] or 0.0)


def plan_for(conn, cfg: dict, f: dict, c: dict, comp: dict, price: float, equity: float, now) -> Optional[dict]:
    """Should this approved idea be expressed as an option? -> a plan, or None. Every reason it says no is a cost control, not a hunch."""
    inst = cfg["instruments"]
    if not inst.get("options_enabled"):
        return None
    if f["ev_net"] < float(inst["options_min_ev"]) or f["horizon_days"] > int(inst["options_max_horizon_days"]):
        return None                                                                              # time decay punishes weak and slow ideas
    adv = (comp or {}).get("adv_usd")
    if price < float(inst["options_min_price"]) or not adv or adv < float(inst["options_min_adv_usd"]):
        return None                                                                              # only liquid, higher-priced names have the option markets this model resembles
    if conn.execute("SELECT 1 FROM option_positions WHERE underlying=? AND status='OPEN'", (c["ticker"],)).fetchone():
        return None
    kind = PUT if f["verdict"] == "SHORT" else CALL
    sigma = iv_estimate(conn, c["ticker"], cfg, c.get("event_type", ""))
    expiry = next_expiry(now.date(), int(inst["options_min_dte"]))
    K = pick_strike(kind, price)
    px = price_option(kind, price, K, expiry, sigma, now, cfg)
    if px["ask"] < 0.30:
        return None                                                                              # penny options do not trade like this
    comm = float(inst.get("options_commission", 0.65))
    room = float(inst["options_max_premium_pct"]) * equity - sleeve_used(conn)
    budget = min(float(inst["options_per_trade_pct"]) * equity, room)
    contracts = int(budget // (px["ask"] * 100.0 + comm))
    if contracts < 1:
        return None
    sg = -1.0 if kind == PUT else 1.0
    return {"kind": kind, "strike": K, "expiry": expiry.isoformat(), "contracts": contracts, "sigma": sigma, "ask": px["ask"], "mid": px["mid"], "bid": px["bid"], "commission": comm,
            "stop_underlying": price * (1.0 - sg * abs(f["stop_return"])), "target_underlying": price * (1.0 + sg * f["target_return"]),
            "why": "%s: edge %.1f%% over %d sessions is strong and short enough that time decay will not eat it; %s is liquid ($%.0fM a day)" % ("a put" if kind == PUT else "a call", f["ev_net"] * 100, f["horizon_days"], c["ticker"], adv / 1e6)}


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
        reason = dict(reason, instrument="OPTION", option={k: plan[k] for k in ("kind", "strike", "expiry", "contracts", "ask", "sigma")}, option_why=plan["why"], model_priced=True)
        set_decision(conn, did, "EXECUTED", reason, entry_price=plan["ask"], shares=plan["contracts"], amount=cost, action="OPTION_" + plan["kind"])
        db.audit(conn, "options", "option_buy", c["ticker"], {"decision": did, "kind": plan["kind"], "strike": plan["strike"], "expiry": plan["expiry"], "contracts": plan["contracts"], "cost": cost})
    return {"status": "EXECUTED", "option_id": oid, "cost": cost, "instrument": "OPTION"}


def maybe_open(conn, cfg: dict, c: dict, f: dict, q: dict, did: int, fid: int, now, theme: str, comp: dict, sector: str, reason: dict, equity: float, set_decision: Callable) -> Optional[dict]:
    plan = plan_for(conn, cfg, f, c, comp, float(q["price"]), equity, now)
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
            px = mark(cfg, o, S, now)
            conn.execute("UPDATE option_positions SET last_mark=?,last_underlying=?,last_mark_at=? WHERE id=?", (px["bid"] if px["T"] > 0 else px["mid"], S, iso(now), o["id"]))
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
