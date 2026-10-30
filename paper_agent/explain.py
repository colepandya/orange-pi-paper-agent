"""Decision explainer: turns the raw decision / forecast / plan records into a page a person can read.
Sections: what happened -> the trigger -> how we sized it up -> the checks (each in words) -> the plan -> what would change this -> timeline.
Everything is derived from what was actually stored; nothing is invented, and old decisions with missing fields still render."""
from __future__ import annotations

import html
import json
import re
from typing import Dict, List, Optional, Tuple

from . import earnings, prices
from .timeutil import to_nz, utcnow, next_market_open

EVENT_LABEL = {
    "guidance_raise": "the company raised its outlook", "guidance_cut": "the company cut its outlook", "earnings_beat": "earnings beat expectations", "earnings_miss": "earnings missed expectations",
    "contract_win": "the company won a contract", "product_launch": "a product launch", "partnership": "a partnership", "merger_acquisition": "merger / acquisition news",
    "capital_raise": "the company is raising money (shares or debt)", "litigation": "a legal action", "regulatory_action": "a regulatory action", "bankruptcy_distress": "financial distress",
    "analyst_upgrade": "an analyst upgrade", "analyst_downgrade": "an analyst downgrade", "structural_theme": "a long-running industry theme", "buyback_dividend": "a buyback or dividend",
    "executive_change": "a leadership change", "general_news": "general news with no specific catalyst", "thesis_signal": "a signal from one of our market theses",
    "market_crash": "a market crash", "war_escalation": "a military escalation", "emergency_policy": "emergency central-bank action", "financial_stress_event": "a bank / financial failure",
    "market_infrastructure_outage": "an exchange or payments outage", "geopolitical_conflict": "geopolitical conflict",
}
TONE = {"EXECUTED": "good", "AWAITING_MARKET": "wait", "AWAITING_QUOTE": "wait", "AWAITING_CAPACITY": "wait", "HELD": "wait", "VETOED": "bad", "REJECTED": "bad", "RISK_REJECTED": "bad",
        "NO_TRADE": "info", "DUPLICATE": "info", "EXPIRED": "info", "PAUSED": "bad", "ERROR": "bad", "CAPACITY_DEFERRED": "wait"}


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def pct(x, d: int = 1, sign: bool = True) -> str:
    return ("%+." + str(d) + "f%%") % (x * 100) if sign else ("%." + str(d) + "f%%") % (x * 100)


def money(x) -> str:
    return "${:,.0f}".format(x) if x is not None else "n/a"


def direction_word(d: Optional[float]) -> str:
    d = d or 0.0
    return "strongly positive" if d >= 0.6 else "positive" if d >= 0.25 else "slightly positive" if d > 0.08 else "neutral" if d >= -0.08 else "slightly negative" if d > -0.25 else "negative" if d > -0.6 else "strongly negative"


def importance_word(m: Optional[float]) -> str:
    m = m or 0.0
    return "major" if m >= 0.7 else "significant" if m >= 0.4 else "moderate" if m >= 0.2 else "minor"


def source_word(c: Optional[float]) -> str:
    c = c or 0.0
    return "a very reliable source" if c >= 0.8 else "a reliable source" if c >= 0.6 else "a source of mixed reliability" if c >= 0.45 else "a low-reliability source"


def age_text(h: Optional[float]) -> str:
    h = h or 0.0
    return "just now" if h < 0.2 else ("%d minutes old" % round(h * 60) if h < 1.5 else "%.1f hours old" % h)


_RULES: List[Tuple[re.Pattern, str, Optional[bool]]] = [
    (re.compile(r"^all gates passed$"), "Every check passed.", True),
    (re.compile(r"^eligibility ([\d.]+) < ([\d.]+)"), r"The story's quality score is \1 out of 100; \2 is needed. It is too weak, too old, or from too unreliable a source to act on.", False),
    (re.compile(r"^EV net (-?[\d.]+)% <= 0"), r"Expected profit after costs is \1%, i.e. negative on average. Buying would lose money in expectation.", False),
    (re.compile(r"^EV net (-?[\d.]+)% below minimum ([\d.]+)%"), r"Expected profit after costs is only \1%; at least \2% is needed to be worth the risk. Not enough edge.", False),
    (re.compile(r"^bearish event; long-only book"), "This is bad news for the company. The portfolio only buys (it never shorts), so it stays out.", False),
    (re.compile(r"^confidence (\d+)% below minimum"), r"Our confidence in this read is only \1%, below the minimum.", False),
    (re.compile(r"^P\(positive\) (\d+)% too low"), r"The chance of ending in profit is only \1%, too low.", False),
    (re.compile(r"^ADV \$([\d.]+)M below minimum"), r"The stock trades only $\1M a day: too thinly traded to buy and sell safely.", False),
    (re.compile(r"^swing reward:risk ([\d.]+) below minimum ([\d.]+) \(target ([\d.]+)% vs stop ([\d.]+)%\)"), r"For a swing trade the possible gain (\3%) is too small next to the possible loss (\4%): reward:risk \1, \2 needed.", False),
    (re.compile(r"^too quiet for a swing: it moves ([\d.]+)% a day, minimum ([\d.]+)%"), r"The stock only moves about \1% a day; a 1-2% swing needs at least \2%.", False),
    (re.compile(r"^market session (\w+); will retry"), r"The US market is closed (\1). The idea stays queued and is re-checked at the open while the news is still fresh.", None),
    (re.compile(r"^market is open but no verified price: (.*)"), r"The market is open but no data provider has given a fresh, trustworthy price yet. Details: \1", None),
    (re.compile(r"^earnings report due (.*?), inside the (\d+)-session hold.*"), r"A quarterly earnings report is due \1, inside the planned \2-session hold. Earnings can gap a stock 5-10% overnight, straight through a tight stop, so the trade is skipped.", False),
    (re.compile(r"^already holding (\w+).*"), r"Already holding \1: a second position in the same stock would stack the same bet.", False),
    (re.compile(r"^no verified company mapped", re.I), "No listed company could be identified in this story, so there is nothing to buy.", False),
]


def translate_gate(g: str) -> Tuple[Optional[bool], str]:
    """-> (True = passed, False = failed, None = waiting / informational, plain-English text)"""
    g = str(g or "").strip()
    for rx, text, ok in _RULES:
        m = rx.match(g)
        if m:
            return ok, rx.sub(text, g, count=1) if "\\" in text else text
    if g.startswith(("VETO:", "HELD:")):
        return (False if g.startswith("VETO") else None), g.split(": ", 1)[1] if ": " in g else g
    return None, g


def _json(s) -> dict:
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def _money(v) -> str:
    v = float(v or 0)
    return "$%.2fB" % (v / 1e9) if abs(v) >= 1e9 else "$%.0fM" % (v / 1e6) if abs(v) >= 1e6 else "$%.2f" % v


def describe_fact(f: dict) -> str:
    """One plain-English line for a fact the article reader extracted."""
    k = f.get("kind")
    if k == "eps_surprise":
        return "Earnings per share %s versus %s expected (%+.0f%%)" % (_money(f["actual"]), _money(f["estimate"]), f["surprise"] * 100)
    if k == "revenue_surprise":
        return "Revenue %s versus %s expected (%+.1f%%)" % (_money(f["actual"]), _money(f["estimate"]), f["surprise"] * 100)
    if k == "growth":
        return "%s %s %.0f%%" % (str(f.get("metric", "figure")).capitalize(), "rose" if f["pol"] > 0 else "fell", f.get("pct", 0))
    if k == "contract":
        return "Contract %s%s" % ("worth " + _money(f["amount"]) if f.get("amount") else "", " (%.1f%% of the company's value)" % f["pct_of_cap"] if f.get("pct_of_cap") else "") if f["pol"] > 0 else "A contract was lost or cancelled"
    if k == "buyback":
        return "Share buyback%s%s" % (" of " + _money(f["amount"]) if f.get("amount") else "", " (%.1f%% of the company's value)" % f["pct_of_cap"] if f.get("pct_of_cap") else "")
    if k == "deal":
        return "%s deal%s%s" % ("Agreed" if f.get("definitive") else "Rumoured / in talks", " worth " + _money(f["amount"]) if f.get("amount") else "", ", %.0f%% premium" % f["premium"] if f.get("premium") else "")
    names = {"guidance_raise": "Raised its outlook", "guidance_cut": "Cut its outlook", "guidance_keep": "Kept its outlook", "beat_generic": "Beat expectations", "miss_generic": "Missed expectations", "dividend_raise": "Raised the dividend",
             "dividend_cut": "Cut the dividend", "upgrade": "Analyst upgrade", "downgrade": "Analyst downgrade", "target_raise": "Price target raised", "target_cut": "Price target cut", "bankruptcy": "Bankruptcy / going-concern warning",
             "delisting": "Delisting notice", "restatement": "Accounting restatement", "investigation": "Regulatory investigation", "lawsuit": "Lawsuit", "recall": "Product recall", "fda_reject": "Regulator rejected / trial failed",
             "fda_approve": "Regulator approval / trial success", "short_report": "Short-seller report", "cyber": "Cyber incident", "exec_exit": "Executive departure", "layoffs": "Layoffs", "strike_outage": "Strike / outage / halted production",
             "record": "Record results", "partnership": "New partnership", "insider_buy": "Insider buying", "insider_sell": "Insider selling", "product_launch": "Product launch"}
    return names.get(k, str(k).replace("_", " ").capitalize())


def article_section(conn, event_id) -> Optional[dict]:
    """What the in-house article reader found for the story behind a decision (None if it never looked)."""
    if not event_id:
        return None
    try:
        row = conn.execute("SELECT r.* FROM article_reads r JOIN events e ON e.news_uid=r.uid WHERE e.id=?", (event_id,)).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    a = _json(row["analysis_json"])
    return {"via": row["via"], "status": row["status"], "domain": row["domain"], "type": a.get("type"), "direction": a.get("direction", 0.0), "confidence": a.get("confidence", 0.0), "mixed": a.get("mixed"),
            "stale": a.get("stale_why") if a.get("stale") else "", "changed": row["merge_note"] or "",
            "facts": [(f.get("pol", 0.0), describe_fact(f), f.get("text", "")) for f in (a.get("facts") or []) if f.get("kind") == "deal" or f.get("w", 0) >= 0.12][:6]}


def explain(conn, cfg: dict, did: int, now=None) -> Optional[dict]:
    now = now or utcnow()
    d = conn.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
    if not d:
        return None
    d = dict(d)
    r = _json(d["reason"]) if d["reason"] and str(d["reason"]).lstrip().startswith("{") else {"gates": [str(d["reason"] or "")]}
    r = {k: v for k, v in r.items()}
    c = r.get("candidate") if isinstance(r.get("candidate"), dict) else {}
    f = conn.execute("SELECT * FROM forecasts WHERE id=?", (d["forecast_id"],)).fetchone() if d.get("forecast_id") else None
    f = dict(f) if f else {}
    inp, scen = _json(f.get("inputs_json")), _json(f.get("scenarios_json")) if f else {}
    pos = conn.execute("SELECT * FROM positions WHERE decision_id=? ORDER BY id LIMIT 1", (did,)).fetchone()
    pos = dict(pos) if pos else None
    plan = conn.execute("SELECT * FROM position_forecast_plans WHERE position_id=? AND active=1 ORDER BY id DESC LIMIT 1", (pos["id"],)).fetchone() if pos else None
    plan = dict(plan) if plan else None
    t, st = d["ticker"] or "(no company)", d["status"]
    gates = [translate_gate(g) for g in (r.get("gates") if isinstance(r.get("gates"), list) else []) if g not in (None, "")]
    sw = cfg.get("strategy") or {}
    risk = cfg.get("risk") or {}

    # ------------------------------------------------------------------ what happened
    why_main = next((txt for ok, txt in gates if ok is False), None)
    if st == "EXECUTED" and pos:
        head = "Bought %.0f shares of %s at $%.2f (%s)" % (pos["shares"], t, pos["entry_price"], money(pos["cost"]))
        if pos["status"] == "CLOSED":
            head += ", then sold at $%.2f for a %s of %s (%s)" % (pos["exit_price"] or 0, "profit" if (pos["pnl"] or 0) >= 0 else "loss", money(abs(pos["pnl"] or 0)), (pos["review_code"] or "").replace("_", " ").lower())
        summary = "This idea passed every check and was bought."
    elif st == "AWAITING_MARKET":
        head, summary = "Queued to buy %s: waiting for the US market to open" % t, "The idea is good enough to buy, but the market is closed. It is re-checked, and the news / price / last-look tests re-run, when the market opens."
    elif st == "AWAITING_QUOTE":
        head, summary = "Ready to buy %s, but no fresh price is available yet" % t, "The market is open; trading needs a fresh price from a data provider. It is retried every cycle."
    elif st == "AWAITING_CAPACITY":
        head, summary = "Good idea on %s, but the portfolio had no room this cycle" % t, "It was not rejected: it is retried while the news is fresh."
    elif st == "HELD":
        head, summary = "On hold: a market-wide shock happened after %s was queued" % t, "Buying into a shock is how paper accounts get hurt. It is re-checked every cycle and released when the shock has aged out."
    elif st == "EXPIRED":
        head, summary = "Expired: the news behind %s was too old by the time it could be acted on" % t, "News loses its edge quickly; a BUY that could not be executed while it was fresh is dropped rather than traded late."
    elif st == "DUPLICATE":
        head, summary = "Skipped: %s" % (why_main or "already holding this stock"), "Another position in %s is already open, so a second BUY would stack the same bet. The strongest idea is processed first." % t
    elif st == "VETOED":
        head, summary = "Cancelled at the last look: %s" % (why_main or "something changed after this was decided"), "The BUY was decided earlier, but the situation changed before money moved. Cancelled decisions are final."
    elif st in ("REJECTED", "NO_TRADE", "RISK_REJECTED"):
        head = "Passed on %s: %s" % (t, why_main or "it did not meet the entry rules")
        summary = {"NO_TRADE": "No trade was made.", "RISK_REJECTED": "A portfolio risk limit blocked it."}.get(st, "It was looked at and rejected; nothing was bought.")
    else:
        head, summary = "%s: %s" % (t, st.replace("_", " ").lower()), why_main or ""

    # ------------------------------------------------------------------ the trigger
    trig = None
    if d.get("origin") == "thesis":
        th = conn.execute("SELECT name,probability FROM theses WHERE key=?", (d.get("thesis_key") or "",)).fetchone() if d.get("thesis_key") else None
        label = (th["name"] if th else (d.get("thesis_key") or "market")).replace("_", " ")
        trig = {"headline": "An idea from our \u201c%s\u201d thesis, not from a news story" % label, "source": "thesis engine (no news or article involved)",
                "line": "Nothing was published about %s to trigger this. Our \u201c%s\u201d thesis is rated %s likely and lists %s as a company that would benefit, so the thesis engine proposed it. With no story there is no article to read: "
                        "the forecast rests on the thesis and the stock's recent price history alone." % (t, label, ("%.0f%%" % (th["probability"] * 100)) if th else "highly", t), "match": ""}
    elif c:
        et = c.get("event_type") or "general_news"
        trig = {"headline": c.get("headline") or "(no headline stored)", "source": c.get("source") or "unknown source",
                "line": "%s. We read it as: %s. Effect on the stock: %s. Importance: %s. Reported by %s." % (age_text(c.get("age_hours")).capitalize(), EVENT_LABEL.get(et, et.replace("_", " ")),
                                                                                                      direction_word(c.get("direction")), importance_word(c.get("magnitude")), source_word(c.get("credibility"))),
                "match": "Matched to %s with %.0f%% confidence." % (t, (c.get("conf") or 0) * 100) if d["ticker"] else ""}

    # ------------------------------------------------------------------ the numbers
    nums: List[Tuple[str, str, str]] = []
    if f:
        nums.append(("Expected profit after costs", pct(f["ev_net"], 2), "average outcome across the scenarios below, after trading costs; the minimum to buy is %s" % pct(risk.get("min_ev_net", 0.006), 2, False)))
        nums.append(("Chance the price rises", "%.0f%%" % ((f.get("p_up") or 0) * 100), "our estimate over %d session(s)" % (f.get("horizon_days") or 0)))
        nums.append(("Confidence in the read", "%.0f%%" % ((f.get("confidence") or 0) * 100), "source reliability, company match, data completeness, track record"))
        nums.append(("Risk score", "%.0f / 100" % (f.get("risk_score") or 0), "volatility, tail risk, uncertainty, liquidity"))
        if inp.get("priced_in") is not None:
            nums.append(("Already priced in", pct(inp["priced_in"], 1, False), "how far the stock has already moved in this direction (subtracted from the edge)"))
        if inp.get("swing"):
            nums.append(("Holding window", "%d session(s)" % inp.get("horizon_days", 0), "the news is expected to work for ~%s; swing mode counts only the part that fits (%.0f%% of the move)" % (inp.get("event_horizon_days"), (inp.get("capture") or 1) * 100)))
        if inp.get("regime"):
            nums.append(("Market regime", str(inp["regime"]).replace("_", " "), "shrinks or grows position size"))
    scen_rows = [(k, v.get("p", 0), v.get("ret", 0)) for k, v in sorted(scen.items(), key=lambda kv: -kv[1].get("ret", 0))] if isinstance(scen, dict) else []

    # ------------------------------------------------------------------ the checks, in words
    checks: List[Tuple[Optional[bool], str]] = []
    if d["ticker"] and c:
        checks.append((True, "Company identified: %s (%.0f%% sure)." % (t, (c.get("conf") or 0) * 100)))
    elig = d.get("score")
    if elig is not None and d["ticker"]:
        elig_failed = any("quality score" in txt for _ok, txt in gates)
        checks.append((not elig_failed, "Story quality score %.0f / 100." % elig))
    if f:
        v = f.get("verdict")
        edge_txt = "expected profit %s clears the minimum" % pct(f["ev_net"], 2) if v == "BUY" else (translate_gate((_json(json.dumps(r.get("forecast") or {})).get("why") or [""])[0])[1] or v)
        ran = float(inp.get("priced_in") or 0.0) / 0.8                                    # the forecast treats 80% of the last 5 sessions' rise as already in the price
        if v != "BUY" and ran >= 0.02:
            edge_txt = edge_txt.rstrip(".") + ". Main reason: the stock has already risen about %.0f%% over the last 5 trading days, and the forecast treats most of a recent rise as already in the price" % (ran * 100)
        checks.append((v == "BUY", "Edge check: %s." % edge_txt))
    for ok, txt in gates:
        if not (txt.startswith("The story's quality") or txt.startswith("Every check")):
            checks.append((ok, txt))
    if st == "EXECUTED":
        checks.append((True, "Market open, fresh price verified, last-look checks (news, market shock, overnight gap) clear."))
        q = r.get("quote") if isinstance(r.get("quote"), dict) else {}
        fill = r.get("fill")
        px = fill if isinstance(fill, (int, float)) else (pos["entry_price"] if pos else None)
        if px:
            checks.append((True, "Bought at $%.2f using %s." % (px, ", ".join(q.get("providers_used") or []) or "verified quotes")))
    if r.get("opportunity"):
        checks.append((True if st == "EXECUTED" else None, "Portfolio room: %s." % r["opportunity"]))

    # ------------------------------------------------------------------ the plan
    planrows: List[Tuple[str, str]] = []
    if pos and plan:
        e = pos["entry_price"]
        planrows += [("Entry", "$%.2f" % e), ("Profit target", "%s ($%.2f): sell %.0f%% there" % (pct(plan["target_return"]), plan["target_price"], (plan.get("partial_exit_pct") or 1) * 100)),
                     ("Stop loss", "%s ($%.2f)%s" % (pct(plan["stop_return"]), plan["stop_price"], "  (already raised to protect profit)" if plan["stop_return"] > 0 else "")),
                     ("Time limit", "%d session(s) from entry" % plan["max_hold_days"])]
        if sw.get("swing_enabled"):
            planrows.append(("Profit protection", "after +%.1f%% the stop moves to break-even, then trails %.1f%% below the highest price" % (sw["breakeven_trigger_pct"] * 100, sw["trail_pct"] * 100)))
        if r.get("sizing"):
            planrows.append(("Why this size", "; ".join(r["sizing"])))
    elif st in ("AWAITING_MARKET", "AWAITING_QUOTE", "HELD", "AWAITING_CAPACITY") and f:
        planrows += [("If bought", "target %s, stop %s, hold up to %d session(s)" % (pct(f.get("target_return") or 0), pct(f.get("stop_return") or 0), f.get("horizon_days") or 0))]

    # ------------------------------------------------------------------ what happens next / what would change it
    nxt: List[str] = []
    if st in ("AWAITING_MARKET", "AWAITING_QUOTE", "HELD", "AWAITING_CAPACITY"):
        ind = prices.indication(conn, d["ticker"], r.get("ref_price"), now) if d["ticker"] else None
        if ind:
            nxt.append("Right now %s is trading %s versus the price it was decided against ($%.2f), %.0f min ago (%s)." % (t, pct(ind["gap"]), r.get("ref_price") or 0, ind["age_minutes"], ind["session"].replace("_", " ").lower()))
        if st == "AWAITING_MARKET":
            nxt.append("Next market open: %s." % to_nz(next_market_open(now)))
        er = earnings.upcoming(conn, d["ticker"], now) if d["ticker"] else None
        if er:
            nxt.append("Next earnings report for %s: %s." % (t, er["label"]))
        if risk.get("veto_adverse_news", True):
            nxt.append("CANCELLED if later news about %s is clearly negative (direction at or below %.2f)." % (t, risk.get("veto_news_direction", -0.35)))
            nxt.append("HELD for %s hours if a market-wide shock (war, crash, emergency policy) happens." % risk.get("shock_hold_hours", 6))
        if r.get("ref_price"):
            nxt.append("CANCELLED if it opens more than %.1f%% below, or %.1f%% above, the previous close of $%.2f." % (risk.get("max_gap_down_pct", .03) * 100, risk.get("max_gap_up_pct", .05) * 100, r["ref_price"]))
        nxt.append("EXPIRES if the news is older than %s hours by the time it can be acted on." % (cfg.get("intelligence") or {}).get("trade_event_max_age_hours", 24))
    elif st in ("REJECTED", "NO_TRADE") and f and f.get("ev_net") is not None and f["ev_net"] < risk.get("min_ev_net", 0.006):
        nxt.append("It would qualify if the expected profit after costs rose from %s to at least %s, for example on stronger or fresher news." % (pct(f["ev_net"], 2), pct(risk.get("min_ev_net", 0.006), 2, False)))
    elif st == "EXECUTED" and pos and pos["status"] == "OPEN":
        er = earnings.upcoming(conn, d["ticker"], now)
        if er and cfg.get("strategy", {}).get("avoid_earnings", True):
            nxt.append("%s reports earnings %s: it will be sold before the close if the report would land while we hold overnight." % (t, er["label"]))
        nxt.append("It will be sold at the target, the stop, the time limit, or earlier if the thesis breaks, risk escalates, or a better opportunity needs the cash.")

    # ------------------------------------------------------------------ timeline
    tl: List[Tuple[str, str]] = [(to_nz(d["created"], "%d %b %H:%M"), "Decision created: %s" % st.replace("_", " ").lower())]
    if pos:
        tl.append((to_nz(pos["opened"], "%d %b %H:%M"), "Bought %.0f shares at $%.2f" % (pos["shares"], pos["entry_price"])))
        for rv in conn.execute("SELECT ts,old_value,new_value,reason FROM forecast_revisions WHERE subject=? AND subject_type='position' AND field='stop_price' ORDER BY id LIMIT 8", (str(pos["id"]),)):
            tl.append((to_nz(rv["ts"], "%d %b %H:%M"), ("Stop moved $%.2f -> $%.2f. %s" % (rv["old_value"], rv["new_value"], rv["reason"] or "")) if rv["old_value"] else ("Initial stop set at $%.2f." % rv["new_value"])))
        if pos["status"] == "CLOSED":
            tl.append((to_nz(pos["closed"], "%d %b %H:%M"), "Sold at $%.2f: %s, P&L %s" % (pos["exit_price"] or 0, (pos["review_code"] or "").replace("_", " ").lower(), money(pos["pnl"]))))
    return {"id": did, "ticker": t, "status": st, "tone": TONE.get(st, "info"), "headline": head, "summary": summary, "trigger": trig, "numbers": nums, "scenarios": scen_rows,
            "checks": checks, "plan": planrows, "next": nxt, "timeline": tl, "article": article_section(conn, d.get("event_id")),
            "technical": {"gates_raw": r.get("gates"), "forecast": r.get("forecast"), "sizing": r.get("sizing"), "quote": r.get("quote")}}


def one_line(conn, cfg: dict, did: int) -> str:
    ex = explain(conn, cfg, did)
    return ex["headline"] if ex else ""


def render_html(ex: dict) -> str:
    mark = {True: "<span class=ok>&#10003;</span>", False: "<span class=bad>&#10007;</span>", None: "<span class=mut>&#8987;</span>"}
    o = ["<div class='why why-%s'>" % esc(ex["tone"]), "<h2>%s</h2><p>%s</p>" % (esc(ex["headline"]), esc(ex["summary"]))]
    if ex["trigger"]:
        t = ex["trigger"]
        o.append("<h3>What triggered it</h3><blockquote>&ldquo;%s&rdquo;<br><span class=mut>%s</span></blockquote><p>%s %s</p>" % (esc(t["headline"]), esc(t["source"]), esc(t["line"]), esc(t["match"])))
    art = ex.get("article")
    if art:
        how = ("Read the full article page" if art["via"] == "page" else "Only the feed's short snippet was available (%s)" % art["status"].lower().replace("_", " "))
        verdict = ("%s (%+.2f, confidence %.0f%%)" % ("good news" if art["direction"] > 0.15 else "bad news" if art["direction"] < -0.15 else "no clear direction", art["direction"], art["confidence"] * 100))
        facts = "".join("<li>%s <b>%s</b><br><span class=mut>&ldquo;%s&rdquo;</span></li>" % (mark[True if pol > 0.05 else False if pol < -0.05 else None], esc(txt), esc(src)) for pol, txt, src in art["facts"]) or "<li class=mut>No hard facts (numbers, deals, ratings) found in the text.</li>"
        o.append("<h3>What the full article says</h3><p>%s. Reads as <b>%s</b>%s.</p><ul class=checks>%s</ul>%s%s" % (
            esc(how), esc(verdict), " &middot; %s" % esc(art["type"]) if art["type"] and art["type"] != "report" else "", facts,
            ("<p class=bad>Old news: %s</p>" % esc(art["stale"])) if art["stale"] else "", ("<p><b>What this changed:</b> %s</p>" % esc(art["changed"])) if art["changed"] else ""))
    if ex["checks"]:
        o.append("<h3>The checks, in plain words</h3><ul class=checks>%s</ul>" % "".join("<li>%s %s</li>" % (mark[ok if ok in (True, False) else None], esc(txt)) for ok, txt in ex["checks"]))
    if ex["numbers"]:
        o.append("<h3>How we sized it up</h3><table>%s</table>" % "".join("<tr><td>%s</td><td><b>%s</b></td><td class=mut>%s</td></tr>" % (esc(a), esc(b), esc(c)) for a, b, c in ex["numbers"]))
    if ex["scenarios"]:
        o.append("<h3>What we think could happen</h3><table><tr><th>Scenario</th><th>Chance</th><th>Return</th></tr>%s</table>" % "".join(
            "<tr><td>%s</td><td>%.0f%%</td><td class=%s>%s</td></tr>" % (esc(k.replace("_", " ")), p * 100, "ok" if r >= 0 else "bad", pct(r, 2)) for k, p, r in ex["scenarios"]))
    if ex["plan"]:
        o.append("<h3>The plan</h3><table>%s</table>" % "".join("<tr><td>%s</td><td>%s</td></tr>" % (esc(a), esc(b)) for a, b in ex["plan"]))
    if ex["next"]:
        o.append("<h3>What happens next / what would change it</h3><ul>%s</ul>" % "".join("<li>%s</li>" % esc(x) for x in ex["next"]))
    if ex["timeline"]:
        o.append("<h3>Timeline</h3><table>%s</table>" % "".join("<tr><td class=mut>%s</td><td>%s</td></tr>" % (esc(a), esc(b)) for a, b in ex["timeline"]))
    o.append("<details><summary class=mut>Technical record</summary><pre>%s</pre></details></div>" % esc(json.dumps(ex["technical"], indent=1, default=str)))
    return "".join(o)
