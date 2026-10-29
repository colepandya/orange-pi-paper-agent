"""Read-only dashboard (plus pause/resume). No network I/O while rendering a page. Times shown in NZ time."""
from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import datetime as dt
import json
import math
import os
import secrets as _secrets
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

from . import auth as auth_mod, config, control, explain, performance, report as report_engine, db, dossier as dossier_engine, funnel as funnel_engine, learning, llm_client, portfolio as pf, regime as regime_engine, revisions, settings as S, settings_ui
from .config import DB_PATH, load_config
from .timeutil import market_state, parse_ts, to_nz, utcnow, iso, SESSION_LABEL

E = lambda x: html.escape("" if x is None else str(x))  # noqa: E731
CSS = """:root{--bg:#fff;--fg:#14181f;--mut:#5b6573;--card:#f5f7fa;--line:#dde2ea;--ok:#0a7d3b;--bad:#b3261e;--warn:#9a6700;--acc:#1f5fbf}
@media(prefers-color-scheme:dark){:root{--bg:#0e1116;--fg:#e6e9ee;--mut:#98a2b3;--card:#171c24;--line:#2a313c;--ok:#4cc38a;--bad:#ff7b72;--warn:#e3b341;--acc:#6ea8ff}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;flex-wrap:wrap;align-items:center}
nav a{margin-right:12px;color:var(--acc);text-decoration:none;font-weight:600}nav a.on{border-bottom:2px solid var(--acc)}
main{padding:14px 16px;max-width:none;margin:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}.card b{display:block;font-size:20px}.card span{color:var(--mut);font-size:12px}
table{border-collapse:collapse;width:100%;margin:6px 0 16px}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--mut);font-size:12px}
.wrap{overflow-x:auto}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.mut{color:var(--mut)}h2{margin:18px 0 4px;font-size:16px}
details summary{cursor:pointer;color:var(--acc)}pre{white-space:pre-wrap;background:var(--card);padding:8px;border-radius:6px;font-size:12px}button{padding:5px 10px}"""
CSS += """.why{max-width:860px}.why h2{margin:.2em 0}.why h3{margin:1.2em 0 .3em;font-size:15px;border-bottom:1px solid var(--line);padding-bottom:3px}.why blockquote{margin:.4em 0;padding:.5em .8em;background:var(--card);border-left:3px solid var(--acc)}
.why ul.checks{list-style:none;padding:0}.why ul.checks li{margin:.35em 0}.why table{border-collapse:collapse}.why td,.why th{padding:3px 12px 3px 0;vertical-align:top;text-align:left}.why-good h2{color:var(--ok)}.why-bad h2{color:var(--bad)}.why-wait h2{color:var(--warn)}"""
CSS += settings_ui.FORM_CSS
TABS = ["overview", "reports", "performance", "theses", "positions", "dossiers", "decisions", "funnel", "risk", "intelligence", "system", "settings"]


def pct(x, d=1):
    return "n/a" if x is None else "%.*f%%" % (d, x * 100)


def money(x):
    return "n/a" if x is None else "${:,.0f}".format(x)


def cls(x):
    return "ok" if x and x > 0 else "bad" if x and x < 0 else ""


def table(head: List[str], rows: List[List]) -> str:
    h = "".join("<th>%s</th>" % E(c) for c in head)
    b = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % (c if isinstance(c, Raw) else E(c)) for c in r) for r in rows) or "<tr><td colspan=%d class=mut>none</td></tr>" % len(head)
    return "<div class=wrap><table><tr>%s</tr>%s</table></div>" % (h, b)


class Raw(str):
    pass


def card(label, value, c=""):
    return "<div class=card><span>%s</span><b class=%s>%s</b></div>" % (E(label), c, E(value))


def spark(vals: List[float], w=260, h=44) -> Raw:
    if len(vals) < 2:
        return Raw("")
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    pts = " ".join("%.1f,%.1f" % (i * w / (len(vals) - 1), h - 3 - (v - lo) / rng * (h - 6)) for i, v in enumerate(vals))
    return Raw('<svg width=%d height=%d><polyline fill=none stroke="currentColor" stroke-width=1.6 points="%s"/></svg>' % (w, h, pts))


def build_state(c, cfg) -> Dict:
    now = utcnow()
    hs = pf.holdings(c, now)
    eq = pf.equity_of(c, hs, now)
    reg = regime_engine.current(c)
    snap = c.execute("SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return {"now": now, "hs": hs, "eq": eq, "reg": reg, "snap": snap, "start": db.start_capital(c)}


def _today(c, now):
    day = to_nz(now, "%Y-%m-%d")
    return [t for t in performance.closed_trades(c) if to_nz(t["closed"], "%Y-%m-%d") == day]


def page_overview(c, cfg, s):
    eq, reg, snap = s["eq"], s["reg"], s["snap"]
    pnl = eq["equity"] - s["start"]
    last = c.execute("SELECT * FROM cycle_runs ORDER BY id DESC LIMIT 1").fetchone()
    paused = os.path.exists(cfg["runtime"]["pause_file"])
    top = sorted(reg.get("probs", {}).items(), key=lambda x: -x[1])[:3]
    out = ['<div class=grid>', card("Equity (mark-to-market)", money(eq["equity"])), card("P&L vs start", "%s (%s)" % (money(pnl), pct(pnl / s["start"] if s["start"] else 0, 2)), cls(pnl)),
           card("Cash", money(eq["cash"])), card("Realized today", money(sum(t["pnl"] for t in _today(c, s["now"]))), cls(sum(t["pnl"] for t in _today(c, s["now"])))), card("Invested", pct(eq["gross"])), card("Drawdown", pct(snap["drawdown"] if snap else 0), "bad" if snap and snap["drawdown"] > 0.05 else ""),
           card("Regime", reg["regime"].replace("_", " ")), card("Survival probability", pct(snap["survival_probability"]) if snap else "n/a"), card("Worst stress loss", pct(snap["max_stress_loss"]) if snap else "n/a"),
           card("Market", market_state(s["now"])["session"]), card("Trading", "PAUSED" if paused else "active", "warn" if paused else "ok"), '</div>']
    out.append("<p class=mut>Regime mix: %s &middot; Last cycle: %s (%s)</p>" % (E(", ".join("%s %.0f%%" % (k.replace("_", " "), v * 100) for k, v in top)), E(to_nz(last["started"]) if last else "never"), E(last["status"] if last else "-")))
    vals = [r[0] for r in c.execute("SELECT equity FROM daily_values ORDER BY day DESC LIMIT 90")][::-1]
    out.append("<h2>Equity (last %d days)</h2>%s" % (len(vals), spark(vals)))
    sig = c.execute("SELECT s.created,p.ticker,s.trigger,s.reason,s.status FROM exit_signals s JOIN positions p ON p.id=s.position_id WHERE s.status='OPEN' ORDER BY s.created DESC LIMIT 10").fetchall()
    out.append("<h2>Open exit signals</h2>" + table(["Time", "Ticker", "Trigger", "Reason"], [[to_nz(r["created"]), r["ticker"], r["trigger"], r["reason"]] for r in sig]))
    th = c.execute("SELECT name,state,probability,trend,net_strength FROM theses WHERE active=1 ORDER BY probability DESC LIMIT 6").fetchall()
    out.append("<h2>Leading theses</h2>" + table(["Thesis", "State", "P", "7d trend", "Net"], [[r["name"], r["state"], pct(r["probability"], 0), "%+.1f pts" % (r["trend"] * 100), "%+.2f" % r["net_strength"]] for r in th]))
    ev = c.execute("SELECT created,headline,event_type,direction,tickers_json FROM events WHERE stale=0 AND novelty>=0.9 ORDER BY id DESC LIMIT 12").fetchall()
    out.append("<h2>Latest events</h2>" + table(["Seen", "Type", "Dir", "Tickers", "Headline"], [[to_nz(r["created"], "%d %b %H:%M"), r["event_type"], Raw('<span class=%s>%+.2f</span>' % (cls(r["direction"]), r["direction"])), ",".join(json.loads(r["tickers_json"])), r["headline"][:120]] for r in ev]))
    return "".join(out)


def page_theses(c, cfg, s, q):
    key = (q.get("thesis") or [""])[0]
    rows = c.execute("SELECT t.*,n.crowding,n.velocity,n.acceleration,n.exhaustion,n.strength nstr FROM theses t LEFT JOIN narrative_states n ON n.id=(SELECT id FROM narrative_states WHERE thesis_key=t.key ORDER BY ts DESC LIMIT 1) WHERE t.active=1 ORDER BY t.probability DESC").fetchall()
    out = [table(["Thesis", "Parent", "State", "P", "Prior", "Unc.", "Quality", "Trend 7d", "Support", "Contra", "Net", "Narr. str", "Velocity", "Crowding", "Exhaust."],
                 [[Raw('<a href="?tab=theses&thesis=%s">%s</a>' % (E(r["key"]), E(r["name"]))), r["parent_key"] or "", r["state"], pct(r["probability"], 0), pct(r["prior"], 0), "±%.0f" % (r["uncertainty"] * 100), "%.2f" % r["confidence_quality"],
                   "%+.1f" % (r["trend"] * 100), "%.2f" % r["support_score"], "%.2f" % r["contradiction_score"], "%+.2f" % r["net_strength"], "%.2f" % (r["nstr"] or 0), "%+.2f" % (r["velocity"] or 0), "%.2f" % (r["crowding"] or 0), "%.2f" % (r["exhaustion"] or 0)] for r in rows])]
    if key:
        th = c.execute("SELECT * FROM theses WHERE key=?", (key,)).fetchone()
        if th:
            out.append("<h2>%s</h2><p>%s</p>" % (E(th["name"]), E(th["description"])))
            for label, sign in (("Supporting evidence", ">"), ("Contradicting evidence", "<")):
                ev = c.execute("SELECT e.headline,e.source,e.event_type,ev.direction,ev.credibility*ev.strength*ev.novelty*ev.predictive_power w,e.published FROM evidence ev JOIN events e ON e.id=ev.event_id WHERE ev.thesis_key=? AND ev.direction%s0 AND ev.expiry_date>? ORDER BY w DESC LIMIT 8" % (sign, ), (key, iso(s["now"]))).fetchall()
                out.append("<h2>%s</h2>" % label + table(["Published", "Source", "Type", "Weight", "Headline"], [[to_nz(r["published"], "%d %b %H:%M"), r["source"], r["event_type"], "%.3f" % r["w"], r["headline"][:130]] for r in ev]))
            comp = c.execute("SELECT ticker,role FROM thesis_companies WHERE thesis_key=?", (key,)).fetchall()
            out.append("<p class=mut>Beneficiaries: %s<br>Victims: %s</p>" % (E(", ".join(r["ticker"] for r in comp if r["role"] == "beneficiary")), E(", ".join(r["ticker"] for r in comp if r["role"] == "victim") or "none")))
            h = c.execute("SELECT ts,probability,state FROM thesis_history WHERE thesis_key=? ORDER BY ts DESC LIMIT 60", (key,)).fetchall()
            out.append("<h2>Probability history</h2>%s" % spark([r["probability"] for r in h][::-1]))
    return "".join(out)


def price_label(source: str, age_seconds) -> str:
    """'YAHOO_EXT:AFTER_HOURS' + 240s -> 'after-hours, 4 min ago' (the Positions table used to show 'CONSENSUS:TWEL')."""
    src = source or ""
    if src.startswith("YAHOO_EXT:"):
        what = SESSION_LABEL.get(src.split(":", 1)[1], "extended hours")
    elif src.startswith("CONSENSUS:"):
        what = "verified " + src.split(":", 1)[1].replace("+", "/")[:22]
    elif src == "DAILY_CLOSE":
        what = "last close"
    elif src == "COST":
        what = "cost (no price yet)"
    else:
        what = src[:16].lower()
    if age_seconds is None:
        return what
    m = age_seconds / 60.0
    return "%s, %s" % (what, "just now" if m < 2 else ("%d min ago" % m if m < 120 else "%.0fh ago" % (m / 60)))


def page_positions(c, cfg, s):
    rows = []
    for h in s["hs"]:
        p = c.execute("SELECT * FROM positions WHERE id=?", (h["position_id"],)).fetchone()
        plan = c.execute("SELECT * FROM position_forecast_plans WHERE position_id=? AND active=1 ORDER BY id DESC LIMIT 1", (h["position_id"],)).fetchone()
        ph = c.execute("SELECT * FROM position_health WHERE position_id=? ORDER BY id DESC LIMIT 1", (h["position_id"],)).fetchone()
        sig = [r[0] for r in c.execute("SELECT trigger FROM exit_signals WHERE position_id=? AND status='OPEN'", (h["position_id"],))]
        unreal = h["value"] - h["cost"]
        rows.append([h["ticker"], h["phase"], "%.0f" % h["shares"], "%.2f" % p["entry_price"], "%.2f (%s)" % (h["price"], price_label(h["price_source"], h.get("price_age"))), Raw('<span class=%s>%s (%s)</span>' % (cls(unreal), money(unreal), pct(unreal / h["cost"] if h["cost"] else 0))),
                     h["thesis_key"] or "", "%.0f" % ph["health"] if ph else "n/a", ("%+.1f%% / %+.1f%% / %dd" % (plan["target_return"] * 100, plan["stop_return"] * 100, plan["max_hold_days"])) if plan else "no plan", ", ".join(sig) or "-", to_nz(p["opened"], "%d %b")])
    out = [table(["Ticker", "Phase", "Shares", "Entry", "Price", "Unrealised", "Thesis", "Health", "Target/Stop/Hold", "Signals", "Opened"], rows)]
    cl = c.execute("SELECT ticker,opened,closed,entry_price,exit_price,pnl,review_code FROM positions WHERE status='CLOSED' ORDER BY id DESC LIMIT 25").fetchall()
    out.append("<h2>Closed positions</h2>" + table(["Ticker", "Opened", "Closed", "Entry", "Exit", "P&L", "Exit reason"], [[r["ticker"], to_nz(r["opened"], "%d %b"), to_nz(r["closed"], "%d %b"), "%.2f" % r["entry_price"], "%.2f" % (r["exit_price"] or 0), Raw('<span class=%s>%s</span>' % (cls(r["pnl"]), money(r["pnl"]))), r["review_code"]] for r in cl]))
    return "".join(out)


def _pts(rows, key):
    return "<br>".join("&bull; %s" % E(r[key]) for r in rows) or "<span class=mut>none</span>"


def page_dossiers(c, cfg, s, q):
    tk = (q.get("ticker") or [""])[0].upper()
    if not tk:
        rows = [[Raw('<a href="?tab=dossiers&ticker=%s">%s</a>' % (E(h["ticker"]), E(h["ticker"]))), h["thesis_key"] or "", pct((h["value"] - h["cost"]) / h["cost"] if h["cost"] else 0)] for h in s["hs"]]
        recent = [r[0] for r in c.execute("SELECT DISTINCT ticker FROM decisions ORDER BY id DESC LIMIT 12")]
        return "<h2>Open positions</h2>" + table(["Ticker", "Thesis", "Unrealised"], rows) + "<h2>Recent candidates</h2><p>" + " ".join('<a href="?tab=dossiers&ticker=%s">%s</a>' % (E(t), E(t)) for t in recent) + "</p>"
    d = dossier_engine.build(c, cfg, tk, s["now"])
    if not d:
        return "<p class=mut>No data for %s yet.</p>" % E(tk)
    out = ["<h2>%s &mdash; %s</h2>" % (E(tk), E(d["company"].get("name") or ""))]
    comp = d["company"]
    out.append("<p class=mut>%s &middot; %s &middot; regime %s &middot; completeness %s</p>" % (E(comp.get("sector")), E(comp.get("dna")), E(d["regime"]), pct(comp.get("completeness"), 0)))
    cards = ["<div class=grid>"]
    if d.get("position"):
        p = d["position"]
        cards += [card("Position", "%s @ %.2f" % (p["status"], p["entry"])), card("Unrealised", pct(p["pnl_pct"]) if p["pnl_pct"] is not None else "n/a", cls(p["pnl_pct"]))]
    if d.get("forecast"):
        f = d["forecast"]
        cards += [card("Target / Stop", "%.2f / %.2f" % (f["target_price"], f["stop_price"])), card("P(target)", pct(f["probability"], 0)), card("Review date", to_nz(f["review_date"], "%d %b"))]
    if d.get("thesis"):
        t = d["thesis"]
        cards += [card("Thesis", "%s (%s)" % (t["name"], t["state"])), card("Thesis probability", "%s (prior %s)" % (pct(t["probability"], 0), pct(t["prior"], 0)))]
    cl = (d.get("confidence") or {}).get("latest")
    if cl:
        cards += [card("Thesis conf.", "%.0f%%" % (cl["thesis"] * 100)), card("Forecast conf.", "%.0f%%" % (cl["forecast"] * 100)), card("Target conf.", "%.0f%%" % (cl["target"] * 100)), card("Position conf.", "%.0f%%" % (cl["position"] * 100)), card("Valuation", cl["valuation"])]
    cards.append("</div>")
    out.append("".join(cards))
    ev = d["evidence"]
    out.append("<h2>Supporting evidence</h2>" + table(["Source", "Weight", "Scope", "Headline"], [[r["source"], "%.3f" % r["weight"], r["scope"], r["headline"][:120]] for r in ev["supporting"]]))
    out.append("<h2>Contradicting evidence</h2>" + table(["Source", "Weight", "Scope", "Headline"], [[r["source"], "%.3f" % r["weight"], r["scope"], r["headline"][:120]] for r in ev["contradicting"]]))
    rt = d.get("redteam")
    if rt:
        out.append("<h2>Red-team review</h2><p>Net conviction <b>%.2f</b> &middot; valuation <b>%s</b> &middot; fatal flaw <b>%s</b></p>" % (rt["net_conviction"], E(rt["valuation_view"]), "YES: " + E(rt["fatal_reason"]) if rt["fatal_flaw"] else "no"))
        out.append("<table><tr><th>Bear case</th><th>Bull case</th></tr><tr><td>%s</td><td>%s</td></tr></table>" % (_pts(rt["bear_points"], "point"), _pts(rt["bull_points"], "point")))
    kb = d.get("knowledge")
    if kb:
        lat = kb.get("latest") or {}
        out.append("<h2>Company knowledge (read from SEC filings)</h2>")
        out.append(table(["Revenue drivers", "Cost drivers", "Customers", "Competitors", "Benefits"], [[_pts([{"x": i} for i in kb.get(k, [])], "x") for k in ("revenue_drivers", "cost_drivers", "customers", "competitors", "benefits")]]))
        out.append(table(["Risk", "Severity"], [[r["risk"], "%.0f%%" % (r["severity"] * 100)] for r in kb.get("risks", [])]))
        g, res = lat.get("guidance") or {}, lat.get("results") or {}
        out.append("<p class=mut>Latest (%s filed %s): guidance revenue %s, EPS %s &middot; revenue growth %s &middot; margins %s &middot; vs expectations %s &middot; backlog %s &middot; tone %+.2f &middot; valuation %s</p>" % (
            E(lat.get("form")), E(lat.get("filed")), E(g.get("revenue")), E(g.get("eps")), pct(res.get("revenue_growth_yoy")) if res.get("revenue_growth_yoy") is not None else "n/a", E(res.get("margin_trend")), E(res.get("vs_expectations")),
            money(lat.get("backlog_usd")), lat.get("mgmt_tone") or 0, E(json.dumps(kb.get("valuation") or {}))))
        if lat.get("changes_vs_prior"):
            out.append("<p><b>What changed:</b> %s</p>" % E("; ".join(lat["changes_vs_prior"])))
    if d.get("tripwires"):
        out.append("<h2>Tripwires (what would prove us wrong)</h2>" + table(["Kind", "Condition", "Status"], [[t["kind"], t["description"] + (" (%.2f)" % t["threshold"] if t["threshold"] else ""), t["status"]] for t in d["tripwires"]]))
    it = d.get("intel")
    if it:
        out.append("<h2>Causal analysis</h2><p><b>%s</b><br>%s</p>" % (E(it["summary"]), E(it["why"])))
        out.append(table(["Order", "Effect", "Confidence", "Tickers"], [[ch["order"], ch["effect"], "%.0f%%" % (ch["confidence"] * 100), ", ".join(ch["tickers"])] for ch in it["chain"]]))
    out.append("<h2>Forecast revision ledger</h2>" + table(["When", "Field", "Old", "New", "Confidence", "Reason"], [[to_nz(r["ts"], "%d %b %H:%M"), r["field"], "" if r["old_value"] is None else "%.2f" % r["old_value"], "%.2f" % (r["new_value"] or 0),
                                                                                                      ("%.0f%%" % (r["conf_new"] * 100)) if r["conf_new"] is not None else "", (r["reason"] or "")[:110]] for r in d["revisions"]]))
    return "".join(out)


def page_decision_detail(c, cfg, did):
    ex = explain.explain(c, cfg, did)
    back = "<p><a href='?tab=decisions'>&larr; all decisions</a></p>"
    return back + (explain.render_html(ex) if ex else "<p class=bad>No such decision.</p>")


def page_decisions(c, cfg, s, q):
    if (q.get("id") or [""])[0].isdigit():
        return page_decision_detail(c, cfg, int((q.get("id") or ["0"])[0]))
    st = (q.get("status") or [""])[0]
    tk = (q.get("ticker") or [""])[0].upper()
    sql, args = "SELECT * FROM decisions WHERE 1=1", []
    if st:
        sql += " AND status=?"; args.append(st)
    if tk:
        sql += " AND ticker=?"; args.append(tk)
    rows = c.execute(sql + " ORDER BY id DESC LIMIT 120", args).fetchall()
    stats = [r[0] for r in c.execute("SELECT DISTINCT status FROM decisions")]
    out = ["<form>Status <select name=status><option value=''>all</option>%s</select> Ticker <input name=ticker value='%s' size=6><input type=hidden name=tab value=decisions><button>Filter</button></form>" % ("".join("<option%s>%s</option>" % (" selected" if x == st else "", E(x)) for x in stats), E(tk))]
    body = []
    for r in rows:
        try:
            rs = json.loads(r["reason"])
        except ValueError:
            rs = {}
        f = c.execute("SELECT scenarios_json,inputs_json,verdict,horizon_days FROM forecasts WHERE id=?", (r["forecast_id"],)).fetchone() if r["forecast_id"] else None
        try:
            plain = explain.explain(c, cfg, r["id"])["headline"]
        except Exception as e:  # noqa: BLE001 - a bad old record must never break the whole page
            plain = "(could not summarise: %s)" % type(e).__name__
        waiting = "<br><span class=mut style='font-size:12px'>%s</span>" % E(plain[:200])
        det = "<a href='?tab=decisions&id=%d'>Why? &rarr;</a>" % r["id"]
        body.append([to_nz(r["created"], "%d %b %H:%M"), r["ticker"], Raw(E(r["status"]) + waiting), r["action"], "%.0f" % (r["score"] or 0), pct(r["ev_net"], 2), r["thesis_key"] or "", money(r["amount"]) if r["amount"] else "-", Raw(det)])
    out.append(table(["Time", "Ticker", "Status", "Action", "Elig.", "EV net", "Thesis", "Amount", "Why"], body))
    return "".join(out)


OUTCOME_MEANING = {"EXECUTED": "bought", "AWAITING_MARKET": "queued for the open", "AWAITING_QUOTE": "waiting for a fresh price", "AWAITING_CAPACITY": "waiting for room in the portfolio", "HELD": "held after a market shock",
                   "VETOED": "cancelled at the last look", "DUPLICATE": "skipped: already holding the stock", "REJECTED": "rejected (not enough edge / quality)", "NO_TRADE": "no trade (bad news / no company)",
                   "RISK_REJECTED": "blocked by a risk limit", "PAUSED": "paused", "ERROR": "error", "CAPACITY_DEFERRED": "deferred (time budget)"}


def page_performance(c, cfg, s, q):
    days = (q.get("days") or ["30"])[0]
    now = s["now"]
    allt = performance.closed_trades(c)
    cutoff = None if days == "all" else iso(now - dt.timedelta(days=int(days) if days.isdigit() else 30))
    tr = [t for t in allt if cutoff is None or t["closed"] >= cutoff]
    sm = performance.summarize(tr)
    by_exit = performance.group(tr, lambda t: t["exit_reason"])
    vd = performance.verdict(sm, by_exit)
    sel = " ".join("<a href='?tab=performance&days=%s'%s>%s</a>" % (v, " style='font-weight:700'" if v == days else "", lab) for v, lab in (("7", "7 days"), ("30", "30 days"), ("all", "all time")))
    pf = sm["profit_factor"]
    cards = ['<div class=grid>', card("Realized P&L", money(sm["total_pnl"]), cls(sm["total_pnl"])), card("Closed trades", "%d  (%d won, %d lost)" % (sm["n"], sm["wins"], sm["losses"])),
             card("Win rate", pct(sm["win_rate"], 0) if sm["win_rate"] is not None else "n/a"), card("Profit factor", ("%.2f" % pf if pf not in (None, math.inf) else ("no losses" if pf else "n/a"))),
             card("Average trade", "%s (%s)" % (pct(sm["expectancy_pct"], 2) if sm["expectancy_pct"] is not None else "n/a", money(sm["expectancy_usd"]) if sm["expectancy_usd"] is not None else "n/a"), cls(sm["expectancy_pct"] or 0)),
             card("Average win / loss", "%s / %s" % (pct(sm["avg_win_pct"], 2) if sm["avg_win_pct"] is not None else "n/a", pct(sm["avg_loss_pct"], 2) if sm["avg_loss_pct"] is not None else "n/a")),
             card("Average hold", ("%.1f h" % sm["avg_hold_hours"]) if sm["avg_hold_hours"] is not None else "n/a"), card("Unrealised (open now)", money(sum(h["value"] - h["cost"] for h in s["hs"])), cls(sum(h["value"] - h["cost"] for h in s["hs"]))), '</div>']
    vcls = {"good": "ok", "marginal": "warn", "bad": "bad", "early": "mut"}[vd["level"]]
    out = ["<p>Window: %s</p>" % sel] + cards + ["<h2 class=%s>%s</h2><ul>%s</ul>" % (vcls, E(vd["headline"]), "".join("<li>%s</li>" % E(p) for p in vd["points"]))]
    out.append("<h2>Cumulative realized P&L (trade by trade)</h2>" + performance.svg_line(performance.cumulative(tr)))
    dayrows = performance.by_day(tr)
    out.append("<h2>Daily realized P&L</h2>" + performance.svg_bars([r["pnl"] for r in dayrows]))
    if dayrows:
        out.append(table(["Day (NZ)", "Trades", "Won", "P&L"], [[r["day"], r["n"], r["wins"], Raw("<span class=%s>%s</span>" % (cls(r["pnl"]), money(r["pnl"])))] for r in dayrows[-14:][::-1]]))

    def grp(title, rows, label=lambda k: str(k)):
        return "<h2>%s</h2>%s" % (title, table(["", "Trades", "Win rate", "Avg trade", "Avg hold", "P&L"], [[label(r["key"]), r["n"], pct(r["win_rate"], 0), pct(r["avg_ret"], 2) if r["avg_ret"] is not None else "n/a",
                                                                                                           ("%.1f h" % r["avg_hold_hours"]) if r["avg_hold_hours"] is not None else "n/a", Raw("<span class=%s>%s</span>" % (cls(r["total_pnl"]), money(r["total_pnl"])))] for r in rows]))
    out.append(grp("How trades ended", by_exit, lambda k: performance.EXIT_LABEL.get(k, k.replace("_", " ").lower())))
    out.append(grp("By kind of news", performance.group(tr, lambda t: t["event_type"]), lambda k: explain.EVENT_LABEL.get(k, k.replace("_", " ")).capitalize()))
    out.append(grp("By how long it was held", performance.group(tr, lambda t: t["hold_bucket"], performance.HOLD_BUCKETS)))
    out.append(grp("By time of day opened (New York)", performance.group(tr, lambda t: t["open_hour_ny"], list(range(0, 24))), lambda k: "%02d:00-%02d:59" % (k, k) if k is not None else "unknown"))
    out.append(grp("By stock (best and worst)", (lambda g: g[:8] + [x for x in g[-8:] if x not in g[:8]])(performance.group(tr, lambda t: t["ticker"]))))

    def tline(t):
        return [t["ticker"], to_nz(t["closed"], "%d %b %H:%M"), Raw("<span class=%s>%s (%s)</span>" % (cls(t["pnl"]), money(t["pnl"]), pct(t["ret"], 2) if t["ret"] is not None else "n/a")),
                performance.EXIT_LABEL.get(t["exit_reason"], t["exit_reason"]), Raw("<a href='?tab=decisions&id=%d'>Why? &rarr;</a>" % t["decision_id"] if t["decision_id"] else "")]
    if tr:
        ranked = sorted(tr, key=lambda t: t["pnl"])
        out.append("<h2>Biggest wins</h2>" + table(["Stock", "Closed", "P&L", "Ended", "Why"], [tline(t) for t in ranked[::-1][:5] if t["pnl"] > 0]))
        out.append("<h2>Biggest losses</h2>" + table(["Stock", "Closed", "P&L", "Ended", "Why"], [tline(t) for t in ranked[:5] if t["pnl"] < 0]))
        out.append("<h2>Latest closed trades</h2>" + table(["Stock", "Closed", "P&L", "Ended", "Why"], [tline(t) for t in tr[::-1][:25]]))
    since = iso(now - dt.timedelta(days=int(days) if days.isdigit() else 3650))
    outc = c.execute("SELECT status,COUNT(*) n FROM decisions WHERE created>=? AND action='BUY' GROUP BY status ORDER BY n DESC", (since,)).fetchall()
    out.append("<h2>What happened to every BUY idea</h2>" + table(["Outcome", "Meaning", "Ideas"], [[r["status"], OUTCOME_MEANING.get(r["status"], ""), r["n"]] for r in outc]))
    out.append("<p class=mut>Numbers use each trade's original size and total P&amp;L including partial sells. Fewer than ~20 trades is mostly luck: do not tune settings on it.</p>")
    return "".join(out)


def page_reports(c, cfg, s, q):
    name = (q.get("file") or ["latest.txt"])[0]
    text = report_engine.read(name) or report_engine.read("latest.txt")
    rows = report_engine.list_reports(14)
    out = []
    if text:
        v = text.splitlines()[0] if text else ""
        cls = "bad" if "FAILED" in v else ("warn" if "ATTENTION" in v else "ok")
        out.append("<h2 class=%s>%s</h2><pre>%s</pre>" % (cls, E(v), E(text)))
    else:
        out.append("<p class=mut>No report yet. Reports are written automatically at 08:45, 09:45, 12:30 and 16:10 New York time on trading days (about 01:45, 02:45, 05:30 and 09:10 NZ time). "
                   "To write one now: <code>python3 -m paper_agent report</code></p>")
    out.append("<h2>Recent reports</h2>" + table(["Report", "Verdict"], [[Raw('<a href="?tab=reports&file=%s">%s</a>' % (E(r["file"]), E(r["file"]))), r["headline"]] for r in rows]))
    return "".join(out)


def page_funnel(c, cfg, s, q):
    hours = float((q.get("hours") or ["24"])[0] or 24) if (q.get("hours") or ["24"])[0].replace(".", "").isdigit() else 24.0
    r = funnel_engine.report(c, hours, s["now"])
    out = ["<p class=mut>Window: " + " ".join('<a href="?tab=funnel&hours=%d">%dh</a>' % (h, h) for h in (6, 24, 72)) + " &middot; US market now: <b>%s</b></p>" % E(r["market"])]
    out.append('<div class=grid>%s%s%s%s%s</div>' % (card("News ingested", r["news"]), card("Events", r["events"]), card("Trade candidates", r["candidates"]), card("Decisions", r["decisions_total"]), card("Trades opened", r["executed"])))
    out.append("<p class=mut>Cycles: %s &middot; last cycle %s &middot; last decision %s</p>" % (E(", ".join("%s=%d" % kv for kv in sorted(r["cycles"].items())) or "none"), E(to_nz(r["last_cycle"]) if r["last_cycle"] else "never"), E(to_nz(r["last_decision"]) if r["last_decision"] else "never")))
    for f in r["findings"]:
        out.append(settings_ui.flash_html(None, [f["text"] + (" Fix: " + f["fix"] if f["fix"] else "")] if f["severity"] == "HIGH" else None, [f["text"]] if f["severity"] == "MEDIUM" else None) if f["severity"] != "INFO" else "<p class=mut>%s</p>" % E(f["text"]))
    if r["pending"]:
        out.append("<h2>BUY decisions waiting</h2>" + table(["Ticker", "Status", "Since", "EV net", "Waiting for"], [[p["ticker"], p["status"], to_nz(p["since"], "%d %b %H:%M"), pct(p["ev_net"], 2) if p["ev_net"] is not None else "n/a", p["waiting_for"][:200]] for p in r["pending"]]))
    rows = []
    for g, n in r["gates"].items():
        ex = r["samples"].get(g, [])
        rows.append([n, g, funnel_engine.GATE_LABELS.get(g, ""), Raw("<br>".join(E("[%s %s] %s" % (x["type"], ",".join(x["tickers"]) or "-", x["headline"])) for x in ex[:2]))])
    out.append("<h2>Why events did or did not become candidates</h2>" + table(["Events", "Reason", "Meaning", "Examples"], rows))
    out.append("<h2>Decisions by outcome</h2>" + table(["Status", "Count"], [[k, v] for k, v in sorted(r["decisions"].items(), key=lambda kv: -kv[1])]))
    out.append("<h2>Recent merger / acquisition events</h2>" + table(["Seen", "Reason / gate", "Decision", "Companies", "Headline"], [[to_nz(m["seen"], "%d %b %H:%M"), m["gate"], m["decision"], ", ".join(m["tickers"]) or "none recognised", m["headline"]] for m in r["mna"]]))
    return "".join(out)


def page_risk(c, cfg, s):
    snap = c.execute("SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    if not snap:
        return "<p class=mut>No snapshot yet.</p>"
    ex, st = json.loads(snap["exposures_json"]), json.loads(snap["stress_json"])
    out = ["<div class=grid>", card("Survival probability", pct(st["survival_probability"])), card("Worst-case loss", pct(st["worst_loss"])), card("Recovery time (at 8%/yr)", "%.0f days" % st["recovery_days"]), card("Ruin threshold", pct(st["ruin_threshold"], 0)), "</div>"]
    out.append("<h2>Stress tests</h2>" + table(["Scenario", "Portfolio impact", "Scenario probability"], [[v["name"], Raw('<span class=%s>%s</span>' % (cls(v["pnl_pct"]), pct(v["pnl_pct"]))), pct(v["p"])] for v in st["scenarios"].values()]))
    for dim in ("sector", "theme", "country", "currency", "liquidity", "factor", "issuer"):
        out.append("<h2>%s exposure</h2>" % dim.title() + table(["Bucket", "% of equity"], [[k, pct(v)] for k, v in sorted(ex.get(dim, {}).items(), key=lambda x: -x[1])]))
    lim = cfg["risk"]
    out.append("<h2>Limits</h2><p class=mut>position %s · issuer %s · theme %s · gross %s · min cash %s · stress-loss %s · drawdown pause %s</p>" % (pct(lim["max_position_pct"], 0), pct(lim["max_issuer_pct"], 0), pct(lim["max_theme_pct"], 0), pct(lim["max_gross_pct"], 0), pct(lim["min_cash_pct"], 0), pct(lim["max_stress_loss_pct"], 0), pct(lim["drawdown_pause_pct"], 0)))
    return "".join(out)


def page_intel(c, cfg, s):
    m = learning.metrics(c)
    out = ["<div class=grid>", card("Scored predictions", m["n"]), card("Brier score", "%.3f" % m["brier"] if m["brier"] is not None else "n/a"), card("Naive baseline", "%.3f" % m["baseline_brier"] if m["baseline_brier"] is not None else "n/a"), "</div>"]
    out.append("<p class=mut>Brier lower is better; learning only recalibrates after %d samples.</p>" % cfg["intelligence"]["learning_min_samples"])
    out.append("<h2>Calibration</h2>" + table(["Predicted range", "N", "Predicted", "Actual"], [[b["range"], b["n"], pct(b["predicted"], 0), pct(b["actual"], 0)] for b in m["bins"]]))
    for key, label in (("event_types", "Event-type accuracy"), ("sources", "Source accuracy"), ("theses", "Thesis accuracy")):
        out.append("<h2>%s</h2>" % label + table(["Name", "N", "Hit rate", "Brier"], [[k, v["n"], pct(v["hit"], 0), "%.3f" % v["brier"]] for k, v in sorted(m[key].items(), key=lambda x: -x[1]["n"])[:15]]))
    out.append("<h2>Regime features</h2>" + table(["Feature", "Value"], [[k, "n/a" if v is None else "%.4f" % v] for k, v in sorted((s["reg"].get("features") or {}).items())]))
    out.append("<h2>Research tasks</h2>" + table(["Priority", "Kind", "Subject", "Description", "Status", "Result"], [[("%.1f" % r["priority"]), r["kind"], r["subject"], r["description"][:140], r["status"], r["result"] or ""] for r in c.execute("SELECT * FROM research_tasks ORDER BY status='OPEN' DESC,priority DESC,id DESC LIMIT 40")]))
    out.append("<h2>Learned parameters</h2>" + table(["Key", "Value", "Why", "Updated"], [[r["key"], r["value_json"][:80], r["reason"] or "", to_nz(r["updated"], "%d %b")] for r in c.execute("SELECT * FROM model_params ORDER BY updated DESC LIMIT 30")]))
    return "".join(out)


def page_system(c, cfg, s):
    u = llm_client.LLMClient(c, cfg).usage_summary()
    out = ["<div class=grid>" + card("LLM spend today", "$%.3f / $%.2f" % (u["today_usd"], u["daily_budget"])) + card("LLM spend this month", "$%.2f / $%.0f" % (u["month_usd"], u["monthly_budget"])) + card("LLM calls today", "%d (cache hits %d, failed %d)" % (u["calls_today"], u["cache_hits_today"], u["failures_today"])) + "</div>", "<h2>Sources</h2>" + table(["Source", "Last success", "Status", "Failures", "Items", "Error"], [[r["name"], to_nz(r["last_success"], "%d %b %H:%M") if r["last_success"] else "never", r["last_status"], r["failures"], r["items_total"], (r["last_error"] or "")[:60]] for r in c.execute("SELECT * FROM source_health ORDER BY failures DESC,name")])]
    out.append("<h2>Quote providers</h2>" + table(["Provider", "State", "OK", "Fail", "Rate-limited", "Auth fail", "Last success"], [[r["provider"], r["state"], r["successes"], r["failures"], r["rate_limits"], r["auth_failures"], to_nz(r["last_success"], "%d %b %H:%M") if r["last_success"] else "never"] for r in c.execute("SELECT * FROM provider_health")]))
    out.append("<h2>Recent cycles</h2>" + table(["Started", "Mode", "Status", "Errors"], [[to_nz(r["started"], "%d %b %H:%M"), r["mode"], r["status"], ", ".join(json.loads(r["report_json"] or "{}").get("errors", []))] for r in c.execute("SELECT * FROM cycle_runs ORDER BY id DESC LIMIT 15")]))
    out.append("<h2>Executor runs</h2>" + table(["Time", "Status", "Checked", "Committed", "Blocked", "Errors"], [[to_nz(r["created"], "%d %b %H:%M"), r["status"], r["positions_checked"], r["actions_committed"], r["blocked_stale"], r["errors"]] for r in c.execute("SELECT * FROM executor_runs ORDER BY id DESC LIMIT 10")]))
    out.append("<h2>Audit log</h2>" + table(["Time", "Actor", "Action", "Subject"], [[to_nz(r["ts"], "%d %b %H:%M"), r["actor"], r["action"], r["subject"]] for r in c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 15")]))
    return "".join(out)


def layout(inner: str, tab: str, cfg: dict, ctx: Optional[dict] = None) -> str:
    ctx = ctx or {}
    paused = os.path.exists(cfg["runtime"]["pause_file"])
    nav = "".join('<a class="%s" href="?tab=%s">%s</a>' % ("on" if t == tab else "", t, t.title()) for t in TABS)
    if ctx.get("authed"):
        csrf = settings_ui.csrf_input(ctx.get("csrf", ""))
        logout = ('<form method=post action=/logout>%s<button>Log out</button></form>' % csrf) if ctx.get("auth_required") else ""
        ctl = ('<span style="margin-left:auto;display:flex;gap:8px"><form method=post action="/control/%s">%s<button>%s</button></form>%s</span>'
               % ("resume" if paused else "pause", csrf, "Resume trading" if paused else "Pause trading", logout))
    else:
        ctl = '<span style="margin-left:auto"><b class="%s">%s</b> &nbsp; <a href="/login">Log in</a></span>' % ("warn" if paused else "mut", "TRADING PAUSED" if paused else "")
    return "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Paper Agent</title><style>%s</style><header><b>Paper Agent v5</b><nav>%s</nav>%s</header><main>%s</main>" % (CSS, nav, ctl, inner)


def render(c, cfg, tab: str, q: Dict, ctx: Optional[dict] = None) -> str:
    ctx = ctx or {}
    s = build_state(c, cfg)
    if tab == "settings":
        if not ctx.get("authed"):
            inner = "<p>Settings, API keys and service control need a login. <a href='/login'>Log in</a></p>"
        else:
            base = config.load_config(with_overrides=False)
            from . import pipeline
            health = {r["source_id"]: dict(r) for r in c.execute("SELECT source_id,last_status,failures,last_success FROM source_health")}
            inner = settings_ui.page_settings(cfg, base, ctx, pipeline.load_sources(base), ctx.get("units") or {}, ctx.get("secret_status") or S.secret_status(), health)
        return layout(inner, tab, cfg, ctx)
    fn = {"overview": lambda: page_overview(c, cfg, s), "theses": lambda: page_theses(c, cfg, s, q), "positions": lambda: page_positions(c, cfg, s), "dossiers": lambda: page_dossiers(c, cfg, s, q), "decisions": lambda: page_decisions(c, cfg, s, q), "funnel": lambda: page_funnel(c, cfg, s, q), "reports": lambda: page_reports(c, cfg, s, q), "performance": lambda: page_performance(c, cfg, s, q),
          "risk": lambda: page_risk(c, cfg, s), "intelligence": lambda: page_intel(c, cfg, s), "system": lambda: page_system(c, cfg, s)}.get(tab, lambda: page_overview(c, cfg, s))
    return layout((settings_ui.flash_html(ctx.get("flash")) if ctx.get("flash") else "") + fn(), tab, cfg, ctx)


CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
MAX_BODY = 200_000


class Handler(BaseHTTPRequestHandler):
    db_path: str = DB_PATH
    auth: "auth_mod.Auth" = None            # set by serve()
    control_dir: str = ""
    csrf_secret: bytes = _secrets.token_bytes(32)          # per-process; tokens simply change after a restart
    get_cfg = staticmethod(config.load_config)

    # ---------------------------------------------------------------- plumbing
    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "same-origin")   # NOT no-referrer: that makes browsers send "Origin: null" on same-origin form posts
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _redirect(self, loc, headers=None):
        self.send_response(303)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()

    def log_message(self, *a):
        pass

    def _ip(self) -> str:
        return self.client_address[0]

    def _private(self) -> bool:
        try:
            ip = ipaddress.ip_address(self._ip())
            return ip.is_private or ip.is_loopback
        except ValueError:
            return False

    def _token(self) -> Optional[str]:
        try:
            ck = SimpleCookie(self.headers.get("Cookie", ""))
            return ck["pa_session"].value if "pa_session" in ck else None
        except Exception:  # noqa: BLE001
            return None

    def _csrf_token(self) -> str:
        return hmac.new(self.csrf_secret, b"paper-agent-csrf-v1", hashlib.sha256).hexdigest()[:40]

    def _session(self, cfg: dict) -> Optional[dict]:
        """Password mode: a real login session. Open mode (default): a synthetic session whose only job is to carry the CSRF token."""
        if not cfg["runtime"].get("require_password"):
            return {"csrf": self._csrf_token(), "open": True}
        return self.auth.session(self._token())

    def _host_ok(self, cfg: dict) -> bool:
        """Blocks DNS rebinding: a hostile site can make Origin == Host == its own domain, so the Host itself must look like your LAN
        (private IP, localhost, a single-label name like 'orangepizero3', *.local / *.lan / *.home.arpa ...) or be listed in runtime.allowed_hosts."""
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.split("]")[0].lstrip("[") if host.startswith("[") else host.rsplit(":", 1)[0]
        try:
            ip = ipaddress.ip_address(name)
            return ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            pass
        if name == "localhost" or name in [h.lower() for h in cfg["runtime"].get("allowed_hosts", [])]:
            return True
        return "." not in name or name.endswith((".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa"))

    def _same_origin(self) -> bool:
        """CSRF defence that works without a login: browsers always send Origin / Sec-Fetch-Site on cross-site POSTs."""
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host", ""):
            return False
        return True

    def _form(self) -> Dict[str, str]:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return {}
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        return {k: v[-1] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()}

    def _flash_url(self, msg: str, tab: str = "settings") -> str:
        return "/?tab=%s&flash=%s" % (tab, urllib.parse.quote(msg[:300]))

    def _audit(self, action: str, detail=None):
        try:
            c = db.connect(self.db_path)
            try:
                db.audit(c, "dashboard", action, self._ip(), detail or {})
            finally:
                c.close()
        except Exception:  # noqa: BLE001
            pass

    def _ctx(self, sess: Optional[dict], q=None, extra=None) -> dict:
        ctx = {"authed": bool(sess), "csrf": sess["csrf"] if sess else "", "flash": (q or {}).get("flash", [""])[0][:300],
               "auth_required": bool(self.get_cfg()["runtime"].get("require_password"))}
        if sess:
            from . import doctor
            ctx["control"] = control.last_result(self.control_dir or None)
            ctx["units"] = {u: doctor._unit_state(u) for u in control.RESTARTABLE}
            ctx["control_path"] = doctor._unit_state("paper-agent-control.path")
            ctx["schedule_status"] = control.schedule_status(self.get_cfg().get("schedule", {}))
            ctx["meta"] = S.load_meta()
            ctx["pending"] = S.pending_suggestions()
            ctx["secret_status"] = S.secret_status()
        ctx.update(extra or {})
        return ctx

    def _page(self, code, inner_or_none, tab="overview", ctx=None, cfg=None, raw_html=None):
        cfg = cfg or self.get_cfg()
        body = layout(raw_html, tab, cfg, ctx) if raw_html is not None else inner_or_none
        self._send(code, body)

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        cfg = self.get_cfg()
        try:
            if u.path in ("/login", "/setup"):
                if not cfg["runtime"].get("require_password"):
                    return self._redirect("/?tab=settings")
                if u.path == "/login" and not self.auth.configured():
                    return self._redirect("/setup")
                if u.path == "/setup" and self.auth.configured():
                    return self._redirect("/login")
                if not self._private():
                    return self._send(403, "forbidden")
                f = (q.get("flash") or [""])[0]
                inner = settings_ui.page_setup(f) if u.path == "/setup" else settings_ui.page_login(f, locked=not self.auth.allowed(self._ip()))
                return self._page(200, None, "settings", {"authed": False}, cfg, raw_html=inner)
            c = db.connect(self.db_path, readonly=True)
            try:
                if u.path == "/health":
                    last = c.execute("SELECT started,status FROM cycle_runs ORDER BY id DESC LIMIT 1").fetchone()
                    age = (utcnow() - parse_ts(last["started"])).total_seconds() if last else None
                    return self._send(200, json.dumps({"ok": bool(last) and age is not None and age < 1800, "last_cycle": dict(last) if last else None, "age_seconds": age}), "application/json")
                if u.path == "/api/summary":
                    s = build_state(c, cfg)
                    return self._send(200, json.dumps({"equity": s["eq"], "regime": s["reg"].get("regime"), "positions": len(s["hs"])}, default=str), "application/json")
                tab = (q.get("tab") or ["overview"])[0]
                sess = self._session(cfg)
                if tab == "settings" and not sess:
                    return self._redirect("/setup" if not self.auth.configured() else "/login")
                return self._send(200, render(c, cfg, tab, q, self._ctx(sess, q)))
            finally:
                c.close()
        except Exception as e:  # noqa: BLE001
            return self._send(500, "<pre>dashboard error: %s</pre>" % E(e))

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        if not self._private():
            return self._send(403, "forbidden")
        cfg = self.get_cfg()
        if not self._host_ok(cfg):
            return self._send(403, "refused: the Host header '%s' does not look like your local network. If you reach the dashboard through a custom hostname or proxy, add it in Settings -> System -> Allowed hostnames." % E(self.headers.get("Host", "")))
        if not self._same_origin():
            return self._send(403, "refused: cross-site request (Origin '%s' does not match Host '%s'). If you reach the dashboard through a proxy or custom hostname, add it in Settings -> System -> Allowed hostnames." % (E(self.headers.get("Origin", "-")), E(self.headers.get("Host", ""))))
        path = urllib.parse.urlparse(self.path)
        form = self._form()
        try:
            if path.path in ("/setup", "/login", "/logout") and not cfg["runtime"].get("require_password"):
                return self._redirect("/?tab=settings" if path.path != "/logout" else "/")
            if path.path == "/setup":
                if self.auth.configured():
                    return self._redirect("/login")
                pw, conf = form.get("password", ""), form.get("confirm", "")
                if pw != conf:
                    return self._page(400, None, "settings", {"authed": False}, cfg, raw_html=settings_ui.page_setup("Passwords do not match."))
                try:
                    self.auth.set_password(pw)
                except ValueError as e:
                    return self._page(400, None, "settings", {"authed": False}, cfg, raw_html=settings_ui.page_setup(str(e)))
                tok, _ = self.auth.new_session(self._ip())
                self._audit("admin_password_created")
                return self._redirect("/?tab=settings", [("Set-Cookie", "pa_session=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d" % (tok, auth_mod.SESSION_SECONDS))])
            if path.path == "/login":
                r = self.auth.login(self._ip(), form.get("password", ""))
                if not r:
                    self._audit("login_failed")
                    inner = settings_ui.page_login("Wrong password." if self.auth.allowed(self._ip()) else "", locked=not self.auth.allowed(self._ip()))
                    return self._page(401, None, "settings", {"authed": False}, cfg, raw_html=inner)
                self._audit("login")
                return self._redirect("/?tab=settings", [("Set-Cookie", "pa_session=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d" % (r[0], auth_mod.SESSION_SECONDS))])
            # everything below needs a session AND a valid CSRF token
            sess = self._session(cfg)
            if not sess:
                return self._redirect("/login")
            if not auth_mod.Auth.csrf_ok(sess, form.get("csrf")):
                return self._send(403, "invalid or missing CSRF token (reload the page and try again)")
            if path.path == "/logout":
                self.auth.logout(self._token())
                return self._redirect("/", [("Set-Cookie", "pa_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")])
            if path.path in ("/control/pause", "/control/resume"):
                pf_ = cfg["runtime"]["pause_file"]
                if path.path.endswith("pause"):
                    os.makedirs(os.path.dirname(pf_), exist_ok=True)
                    with open(pf_, "w") as pf_fh:
                        pf_fh.write(iso())
                else:
                    try:
                        os.remove(pf_)
                    except OSError:
                        pass
                self._audit(path.path.strip("/").replace("/", "_"))
                return self._redirect("/")
            if path.path == "/settings/save":
                return self._save_settings(form, cfg, sess)
            if path.path == "/settings/secrets":
                changed, errors = S.apply_secret_form(form)
                S.record_secret_change(changed)
                if "FINNHUB_API_KEY" in changed:
                    try:
                        from . import keys
                        keys.resolve_finnhub()
                    except Exception:  # noqa: BLE001
                        pass
                self._audit("secrets_changed", {"keys": changed})
                if errors:
                    return self._settings_page(400, cfg, sess, {"errors": errors})
                return self._redirect(self._flash_url("Saved keys: %s" % (", ".join(changed) if changed else "nothing changed")))
            if path.path == "/settings/disable-dead":
                from . import pipeline
                base = config.load_config(with_overrides=False)
                c = db.connect(self.db_path, readonly=True)
                try:
                    health = {r["source_id"]: dict(r) for r in c.execute("SELECT source_id,last_status,failures,last_success FROM source_health")}
                finally:
                    c.close()
                dead = settings_ui.dead_feed_ids(pipeline.load_sources(base), cfg, health)
                new = json.loads(json.dumps(cfg))
                for sid in dead:
                    new.setdefault("source_overrides", {}).setdefault(sid, {})["enabled"] = False
                S.save_overrides_tracked(S.overrides_for(new, base), "user", "disabled dead feeds")
                self._audit("dead_feeds_disabled", {"feeds": dead})
                return self._redirect(self._flash_url("Disabled %d dead feed(s): %s" % (len(dead), ", ".join(dead) or "none")))
            if path.path in ("/settings/deploy-accept", "/settings/deploy-dismiss"):
                accept = path.path.endswith("accept")
                ok, msg = S.resolve_suggestion(form.get("id", ""), accept)
                self._audit("release_suggestion_" + ("accepted" if accept else "dismissed"), {"id": form.get("id", "")[:80], "ok": ok})
                return self._redirect(self._flash_url(("Applied the release value." if accept else "Dismissed; your value stays.") if ok else "Could not apply: " + msg))
            if path.path == "/settings/reset":
                if form.get("confirm") != "1":
                    return self._redirect(self._flash_url("Tick the confirmation box to reset."))
                S.record_changes(S.load_overrides(), {}, "user", "reset to defaults")
                S.reset_overrides()
                self._audit("settings_reset")
                try:
                    control.send_request("set_schedule", {k: v for k, v in config.DEFAULTS["schedule"].items()}, self.control_dir or None)
                except OSError:
                    pass
                return self._redirect(self._flash_url("All dashboard settings reset to defaults."))
            if path.path == "/service":
                act, unit = form.get("action", ""), form.get("unit", "")
                if act == "reapply_schedule":
                    try:
                        rid = control.send_request("set_schedule", dict(cfg["schedule"]), self.control_dir or None)
                    except OSError as e:
                        return self._redirect(self._flash_url("Could not send the request: %s" % e))
                    self._audit("service_request", {"action": "set_schedule"})
                    return self._redirect(self._flash_url("Schedule re-apply requested (%s). Refresh in a few seconds." % rid))
                ok = (act == "restart" and unit in control.RESTARTABLE) or (act == "start" and unit in control.STARTABLE) or act == "restart_all"
                if not ok:
                    return self._redirect(self._flash_url("Not an allowed service action."))
                try:
                    rid = control.send_request(act, {"unit": unit} if unit else {}, self.control_dir or None)
                except OSError as e:
                    return self._redirect(self._flash_url("Could not send the request: %s" % e))
                self._audit("service_request", {"action": act, "unit": unit})
                return self._redirect(self._flash_url("Request sent (%s). The result appears under Services within a few seconds; refresh to see it." % rid))
            if path.path == "/test/llm":
                tier = (urllib.parse.parse_qs(path.query).get("tier") or ["triage"])[0]
                return self._settings_page(200, cfg, sess, {"test_output": self._test_llm(cfg, tier if tier in ("triage", "analyst") else "triage")})
            if path.path == "/test/preflight":
                return self._settings_page(200, cfg, sess, {"test_output": self._run_preflight(cfg)})
            if path.path == "/test/doctor":
                return self._settings_page(200, cfg, sess, {"test_output": self._run_doctor(cfg)})
            return self._send(404, "not found")
        except Exception as e:  # noqa: BLE001
            return self._send(500, "<pre>dashboard error: %s</pre>" % E(e))

    def _settings_page(self, code, cfg, sess, extra):
        c = db.connect(self.db_path, readonly=True)
        try:
            self._send(code, render(c, cfg, "settings", {}, self._ctx(sess, None, extra)))
        finally:
            c.close()

    def _save_settings(self, form, cfg, sess):
        from . import pipeline
        base = config.load_config(with_overrides=False)
        new, errors, warnings = S.apply_form(form, cfg, base, {s["id"]: s.get("interval", 600) for s in pipeline.load_sources(base)})
        if errors:
            return self._settings_page(400, cfg, sess, {"errors": errors, "warnings": warnings, "raw": form})
        ov = S.overrides_for(new, base)
        S.save_overrides_tracked(ov, "user")                       # stamps every changed setting with (time, who)
        changed = sorted(k for k in (f.path for f in S.FIELDS) if S.get_path(new, k) != S.get_path(cfg, k))
        self._audit("settings_saved", {"changed": changed})
        msg = "Settings saved (%d changed)." % len(changed)
        if any(new["schedule"][k] != cfg["schedule"][k] for k in new["schedule"]):
            try:
                control.send_request("set_schedule", dict(new["schedule"]), self.control_dir or None)
                msg += " Timer schedule update requested."
            except OSError as e:
                msg += " (Could not request the schedule change: %s)" % e
        if new["runtime"]["bind_port"] != cfg["runtime"]["bind_port"]:
            msg += " Restart the dashboard for the new port."
        if warnings:
            msg += " Note: " + " ".join(warnings)
        return self._redirect(self._flash_url(msg))

    def _test_llm(self, cfg, tier: str) -> str:
        from . import event_intel, pipeline
        c = db.connect(self.db_path)
        try:
            client = llm_client.LLMClient(c, cfg)
            if not client.enabled(tier):
                return "LLM is OFF for the %s tier: enable it in the LLM analyst settings and save a key for its provider." % tier
            r = event_intel.analyze(client, pipeline.load_universe(), {"title": "Lockheed Martin wins $2.4 billion Pentagon contract for missile defense", "summary": "The Navy awarded the multi-year contract.", "tickers": []}, tier)
            u = client.usage_summary()
            last = c.execute("SELECT model,input_tokens,output_tokens,cost_usd,ok,error FROM llm_usage ORDER BY id DESC LIMIT 1").fetchone()
            head = "OK: valid structured analysis returned." if r else "FAILED: the call did not return a valid analysis (%s)." % (last["error"] if last else "no call made")
            return "%s\nmodel=%s tokens in/out=%s/%s cost=$%.5f\nspend today $%.4f of $%.2f\n\n%s" % (head, last["model"] if last else "?", last["input_tokens"] if last else 0, last["output_tokens"] if last else 0,
                                                                                                    last["cost_usd"] if last else 0, u["today_usd"], u["daily_budget"], json.dumps(r, indent=1, default=str)[:1500] if r else "")
        finally:
            c.close()

    def _run_preflight(self, cfg) -> str:
        from . import pipeline, preflight
        lines: List[str] = []
        c = db.connect(self.db_path, readonly=True)
        try:
            preflight.run(c, cfg, pipeline.load_universe(), config.CONFIG_DIR, srcs=pipeline.load_sources(cfg), out=lines.append)
        finally:
            c.close()
        return "\n".join(lines)

    def _run_doctor(self, cfg) -> str:
        from . import doctor, pipeline
        lines: List[str] = []
        c = db.connect(self.db_path, readonly=True)
        try:
            doctor.run(c, cfg, config.CONFIG_DIR, live=True, check_units=True, fix=False, srcs=pipeline.load_sources(cfg), out=lines.append)
        finally:
            c.close()
        return "\n".join(lines)


def serve(cfg: dict, db_path: str = DB_PATH) -> None:
    Handler.db_path = db_path
    Handler.auth = auth_mod.Auth()
    Handler.control_dir = config.CONTROL_DIR
    Handler.csrf_secret = _secrets.token_bytes(32)
    srv = ThreadingHTTPServer((cfg["runtime"]["bind_host"], int(cfg["runtime"]["bind_port"])), Handler)
    srv.serve_forever()
