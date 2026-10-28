"""Read-only dashboard (plus pause/resume). No network I/O while rendering a page. Times shown in NZ time."""
from __future__ import annotations

import html
import ipaddress
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List

from . import db, learning, portfolio as pf, regime as regime_engine
from .config import DB_PATH, load_config
from .timeutil import market_state, parse_ts, to_nz, utcnow, iso

E = lambda x: html.escape("" if x is None else str(x))  # noqa: E731
CSS = """:root{--bg:#fff;--fg:#14181f;--mut:#5b6573;--card:#f5f7fa;--line:#dde2ea;--ok:#0a7d3b;--bad:#b3261e;--warn:#9a6700;--acc:#1f5fbf}
@media(prefers-color-scheme:dark){:root{--bg:#0e1116;--fg:#e6e9ee;--mut:#98a2b3;--card:#171c24;--line:#2a313c;--ok:#4cc38a;--bad:#ff7b72;--warn:#e3b341;--acc:#6ea8ff}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;flex-wrap:wrap;align-items:center}
nav a{margin-right:12px;color:var(--acc);text-decoration:none;font-weight:600}nav a.on{border-bottom:2px solid var(--acc)}
main{padding:14px 16px;max-width:1300px;margin:auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}.card b{display:block;font-size:20px}.card span{color:var(--mut);font-size:12px}
table{border-collapse:collapse;width:100%;margin:6px 0 16px}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--mut);font-size:12px}
.wrap{overflow-x:auto}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.mut{color:var(--mut)}h2{margin:18px 0 4px;font-size:16px}
details summary{cursor:pointer;color:var(--acc)}pre{white-space:pre-wrap;background:var(--card);padding:8px;border-radius:6px;font-size:12px}button{padding:5px 10px}"""
TABS = ["overview", "theses", "positions", "decisions", "risk", "intelligence", "system"]


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


def page_overview(c, cfg, s):
    eq, reg, snap = s["eq"], s["reg"], s["snap"]
    pnl = eq["equity"] - s["start"]
    last = c.execute("SELECT * FROM cycle_runs ORDER BY id DESC LIMIT 1").fetchone()
    paused = os.path.exists(cfg["runtime"]["pause_file"])
    top = sorted(reg.get("probs", {}).items(), key=lambda x: -x[1])[:3]
    out = ['<div class=grid>', card("Equity (mark-to-market)", money(eq["equity"])), card("P&L vs start", "%s (%s)" % (money(pnl), pct(pnl / s["start"] if s["start"] else 0, 2)), cls(pnl)),
           card("Cash", money(eq["cash"])), card("Invested", pct(eq["gross"])), card("Drawdown", pct(snap["drawdown"] if snap else 0), "bad" if snap and snap["drawdown"] > 0.05 else ""),
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


def page_positions(c, cfg, s):
    rows = []
    for h in s["hs"]:
        p = c.execute("SELECT * FROM positions WHERE id=?", (h["position_id"],)).fetchone()
        plan = c.execute("SELECT * FROM position_forecast_plans WHERE position_id=? AND active=1 ORDER BY id DESC LIMIT 1", (h["position_id"],)).fetchone()
        ph = c.execute("SELECT * FROM position_health WHERE position_id=? ORDER BY id DESC LIMIT 1", (h["position_id"],)).fetchone()
        sig = [r[0] for r in c.execute("SELECT trigger FROM exit_signals WHERE position_id=? AND status='OPEN'", (h["position_id"],))]
        unreal = h["value"] - h["cost"]
        rows.append([h["ticker"], h["phase"], "%.0f" % h["shares"], "%.2f" % p["entry_price"], "%.2f (%s)" % (h["price"], h["price_source"][:14]), Raw('<span class=%s>%s (%s)</span>' % (cls(unreal), money(unreal), pct(unreal / h["cost"] if h["cost"] else 0))),
                     h["thesis_key"] or "", "%.0f" % ph["health"] if ph else "n/a", ("%+.1f%% / %+.1f%% / %dd" % (plan["target_return"] * 100, plan["stop_return"] * 100, plan["max_hold_days"])) if plan else "no plan", ", ".join(sig) or "-", to_nz(p["opened"], "%d %b")])
    out = [table(["Ticker", "Phase", "Shares", "Entry", "Price", "Unrealised", "Thesis", "Health", "Target/Stop/Hold", "Signals", "Opened"], rows)]
    cl = c.execute("SELECT ticker,opened,closed,entry_price,exit_price,pnl,review_code FROM positions WHERE status='CLOSED' ORDER BY id DESC LIMIT 25").fetchall()
    out.append("<h2>Closed positions</h2>" + table(["Ticker", "Opened", "Closed", "Entry", "Exit", "P&L", "Exit reason"], [[r["ticker"], to_nz(r["opened"], "%d %b"), to_nz(r["closed"], "%d %b"), "%.2f" % r["entry_price"], "%.2f" % (r["exit_price"] or 0), Raw('<span class=%s>%s</span>' % (cls(r["pnl"]), money(r["pnl"]))), r["review_code"]] for r in cl]))
    return "".join(out)


def page_decisions(c, cfg, s, q):
    st = (q.get("status") or [""])[0]
    tk = (q.get("ticker") or [""])[0].upper()
    sql, args = "SELECT * FROM decisions WHERE 1=1", []
    if st:
        sql += " AND status=?"; args.append(st)
    if tk:
        sql += " AND ticker=?"; args.append(tk)
    rows = c.execute(sql + " ORDER BY id DESC LIMIT 120", args).fetchall()
    stats = [r[0] for r in c.execute("SELECT DISTINCT status FROM decisions")]
    out = ["<form>Status <select name=status onchange=this.form.submit()><option value=''>all</option>%s</select> Ticker <input name=ticker value='%s' size=6><input type=hidden name=tab value=decisions><button>Filter</button></form>" % ("".join("<option%s>%s</option>" % (" selected" if x == st else "", E(x)) for x in stats), E(tk))]
    body = []
    for r in rows:
        try:
            rs = json.loads(r["reason"])
        except ValueError:
            rs = {}
        f = c.execute("SELECT scenarios_json,inputs_json,verdict,horizon_days FROM forecasts WHERE id=?", (r["forecast_id"],)).fetchone() if r["forecast_id"] else None
        det = ""
        if f:
            sc = json.loads(f["scenarios_json"])
            det = "<details><summary>why</summary><pre>%s</pre></details>" % E(json.dumps({"gates": rs.get("gates"), "forecast": rs.get("forecast"), "sizing": rs.get("sizing"), "opportunity": rs.get("opportunity"),
                                                                                         "scenarios": {k: "p=%.3f ret=%+.2f%%" % (v["p"], v["ret"] * 100) for k, v in sc.items()}, "headline": (rs.get("candidate") or {}).get("headline")}, indent=1, default=str))
        body.append([to_nz(r["created"], "%d %b %H:%M"), r["ticker"], r["status"], r["action"], "%.0f" % (r["score"] or 0), pct(r["ev_net"], 2), r["thesis_key"] or "", money(r["amount"]) if r["amount"] else "", Raw(det)])
    out.append(table(["Time", "Ticker", "Status", "Action", "Elig.", "EV net", "Thesis", "Amount", "Audit"], body))
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
    out = ["<h2>Sources</h2>" + table(["Source", "Last success", "Status", "Failures", "Items", "Error"], [[r["name"], to_nz(r["last_success"], "%d %b %H:%M") if r["last_success"] else "never", r["last_status"], r["failures"], r["items_total"], (r["last_error"] or "")[:60]] for r in c.execute("SELECT * FROM source_health ORDER BY failures DESC,name")])]
    out.append("<h2>Quote providers</h2>" + table(["Provider", "State", "OK", "Fail", "Rate-limited", "Auth fail", "Last success"], [[r["provider"], r["state"], r["successes"], r["failures"], r["rate_limits"], r["auth_failures"], to_nz(r["last_success"], "%d %b %H:%M") if r["last_success"] else "never"] for r in c.execute("SELECT * FROM provider_health")]))
    out.append("<h2>Recent cycles</h2>" + table(["Started", "Mode", "Status", "Errors"], [[to_nz(r["started"], "%d %b %H:%M"), r["mode"], r["status"], ", ".join(json.loads(r["report_json"] or "{}").get("errors", []))] for r in c.execute("SELECT * FROM cycle_runs ORDER BY id DESC LIMIT 15")]))
    out.append("<h2>Executor runs</h2>" + table(["Time", "Status", "Checked", "Committed", "Blocked", "Errors"], [[to_nz(r["created"], "%d %b %H:%M"), r["status"], r["positions_checked"], r["actions_committed"], r["blocked_stale"], r["errors"]] for r in c.execute("SELECT * FROM executor_runs ORDER BY id DESC LIMIT 10")]))
    out.append("<h2>Audit log</h2>" + table(["Time", "Actor", "Action", "Subject"], [[to_nz(r["ts"], "%d %b %H:%M"), r["actor"], r["action"], r["subject"]] for r in c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 15")]))
    return "".join(out)


def render(c, cfg, tab: str, q: Dict) -> str:
    s = build_state(c, cfg)
    fn = {"overview": lambda: page_overview(c, cfg, s), "theses": lambda: page_theses(c, cfg, s, q), "positions": lambda: page_positions(c, cfg, s), "decisions": lambda: page_decisions(c, cfg, s, q),
          "risk": lambda: page_risk(c, cfg, s), "intelligence": lambda: page_intel(c, cfg, s), "system": lambda: page_system(c, cfg, s)}.get(tab, lambda: page_overview(c, cfg, s))
    paused = os.path.exists(cfg["runtime"]["pause_file"])
    nav = "".join('<a class="%s" href="?tab=%s">%s</a>' % ("on" if t == tab else "", t, t.title()) for t in TABS)
    ctl = '<form method=post action="/control/%s" style="margin-left:auto"><button>%s</button></form>' % ("resume" if paused else "pause", "Resume trading" if paused else "Pause trading")
    return "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Paper Agent</title><style>%s</style><header><b>Paper Agent v5</b><nav>%s</nav>%s</header><main>%s</main>" % (CSS, nav, ctl, fn())


class Handler(BaseHTTPRequestHandler):
    cfg: dict = {}
    db_path: str = DB_PATH

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            c = db.connect(self.db_path, readonly=True)
            try:
                if u.path == "/health":
                    last = c.execute("SELECT started,status FROM cycle_runs ORDER BY id DESC LIMIT 1").fetchone()
                    age = (utcnow() - parse_ts(last["started"])).total_seconds() if last else None
                    return self._send(200, json.dumps({"ok": bool(last) and age is not None and age < 1800, "last_cycle": dict(last) if last else None, "age_seconds": age}), "application/json")
                if u.path == "/api/summary":
                    s = build_state(c, self.cfg)
                    return self._send(200, json.dumps({"equity": s["eq"], "regime": s["reg"].get("regime"), "positions": len(s["hs"])}, default=str), "application/json")
                return self._send(200, render(c, self.cfg, (q.get("tab") or ["overview"])[0], q))
            finally:
                c.close()
        except Exception as e:  # noqa: BLE001
            return self._send(500, "<pre>dashboard error: %s</pre>" % E(e))

    def do_POST(self):
        try:
            ip = ipaddress.ip_address(self.client_address[0])
            if not (ip.is_private or ip.is_loopback):
                return self._send(403, "forbidden")
        except ValueError:
            return self._send(403, "forbidden")
        pf_ = self.cfg["runtime"]["pause_file"]
        if self.path == "/control/pause":
            os.makedirs(os.path.dirname(pf_), exist_ok=True)
            open(pf_, "w").write(iso())
        elif self.path == "/control/resume":
            try:
                os.remove(pf_)
            except OSError:
                pass
        else:
            return self._send(404, "not found")
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()


def serve(cfg: dict, db_path: str = DB_PATH) -> None:
    Handler.cfg, Handler.db_path = cfg, db_path
    srv = ThreadingHTTPServer((cfg["runtime"]["bind_host"], int(cfg["runtime"]["bind_port"])), Handler)
    srv.serve_forever()
