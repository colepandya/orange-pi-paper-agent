"""One-shot health report: everything that can be wrong on the Pi, checked live, with a suggested fix for each issue."""
from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import urllib.request
from typing import Callable, Dict, List, Optional

from . import VERSION, db, llm_client, marketdata, net, portfolio as pf, quotes, regime, sources
from .timeutil import age_seconds, market_state, to_nz, utcnow

UNITS = ["paper-agent-cycle.timer", "paper-agent-guard.timer", "paper-agent-dashboard.service", "paper-agent-backup.timer", "paper-agent-universe.timer", "paper-agent-update.timer"]
KEYS = ["TWELVE_DATA_API_KEY", "FINNHUB_API_KEY", "ALPACA_API_KEY", "ALPACA_API_SECRET", "MASSIVE_API_KEY", "ALPHA_VANTAGE_API_KEY", "TIINGO_API_KEY", "EODHD_API_KEY"]


def _unit_state(unit: str) -> str:
    try:
        return subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "n/a"


def run(conn, cfg: dict, config_dir: str, fetch: Callable = net.get, fetch_json: Callable = net.get_json, live: bool = True, check_units: bool = True,
        fix: bool = False, srcs: Optional[List[dict]] = None, out: Callable = print) -> dict:
    issues: List[dict] = []

    def issue(sev: str, msg: str, fixcmd: str = "") -> None:
        issues.append({"severity": sev, "message": msg, "fix": fixcmd})

    now = utcnow()
    out("=== PAPER AGENT DOCTOR v%s  %s ===" % (VERSION, to_nz(now)))
    # ---- database
    integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
    eq = pf.equity_of(conn)
    opens = conn.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
    out("[db] integrity=%s schema=%s equity=$%s cash=$%s open_positions=%d regime=%s paused=%s" % (
        integ, conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "{:,.0f}".format(eq["equity"]), "{:,.0f}".format(eq["cash"]), opens,
        regime.current(conn)["regime"], os.path.exists(cfg["runtime"]["pause_file"])))
    if integ != "ok":
        issue("CRITICAL", "database integrity check failed: %s" % integ, "restore a backup: ls /var/lib/paper-agent/backups")
    # ---- keys
    have = [k for k in KEYS if os.getenv(k)]
    out("[keys] set: %s | missing: %s" % (", ".join(have) or "none", ", ".join(k for k in KEYS if k not in have) or "none"))
    if len(have) < 3:
        issue("HIGH", "fewer than 3 quote-provider keys loaded; two providers must agree before any trade", "edit /etc/paper-agent/market-data.env")
    # ---- SEC contact (fair-access policy asks for a real contact in the User-Agent)
    if "example" in str(cfg["runtime"].get("contact_email", "")).lower():
        issue("MEDIUM", "runtime.contact_email is still a placeholder; SEC (filings, company facts, universe) expects a real contact in the User-Agent and may throttle or block otherwise",
              'edit /etc/paper-agent/config.json -> "runtime": {"contact_email": "you@yourdomain.com"}')
    # ---- LLM analyst
    cl = llm_client.LLMClient(conn, cfg)
    u = cl.usage_summary()
    on = cfg["intelligence"].get("llm_enabled")
    out("[llm] %s | tiers: %s | today $%.3f/$%.2f month $%.2f/$%.0f | calls today %d (failed %d, cached %d)" % (
        "ON" if on and cl.enabled("triage") else "OFF (deterministic engine only)", ", ".join("%s=%s" % (k, v["model"]) for k, v in cfg["intelligence"]["llm_tiers"].items()),
        u["today_usd"], u["daily_budget"], u["month_usd"], u["monthly_budget"], u["calls_today"], u["failures_today"], u["cache_hits_today"]))
    if on and not cl.enabled("triage"):
        issue("MEDIUM", "llm_enabled is true but no API key is loaded for the triage tier", "sudo /opt/paper-agent/scripts/enable-llm.sh")
    if u["calls_today"] >= 10 and u["failures_today"] / max(1, u["calls_today"]) > 0.5:
        issue("HIGH", "more than half of today's LLM calls failed or were rejected", "run: python3 -m paper_agent llm-test; check provider key, model id and quota")
    if u["today_usd"] >= u["daily_budget"]:
        issue("LOW", "daily LLM budget reached; the system is running on the deterministic engine until tomorrow", "raise intelligence.llm_daily_budget_usd if intended")
    # ---- universe
    wl = os.path.join(config_dir, "watchlist.json")
    try:
        n = len(json.load(open(wl)))
    except (OSError, ValueError):
        n = 0
    out("[universe] %d companies" % n)
    if n < 2000:
        issue("HIGH", "company universe missing/small (%d); news cannot be mapped to tickers" % n, "sudo env PYTHONPATH=/opt/paper-agent python3 -m paper_agent universe")
        if fix:
            try:
                from . import universe
                out("   fix: " + str(universe.refresh(wl, fetch_json)))
            except Exception as e:  # noqa: BLE001
                out("   fix failed: %s" % e)
    # ---- services
    if check_units:
        states = {u: _unit_state(u) for u in UNITS}
        out("[services] " + ", ".join("%s=%s" % (u.replace("paper-agent-", ""), s) for u, s in states.items()))
        for u, s in states.items():
            if s not in ("active", "n/a"):
                issue("HIGH", "%s is %s" % (u, s), "sudo systemctl enable --now %s" % u)
                if fix:
                    subprocess.run(["systemctl", "enable", "--now", u], capture_output=True, timeout=30)
    # ---- last cycle
    last = conn.execute("SELECT * FROM cycle_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not last:
        out("[cycle] never run")
        issue("MEDIUM", "no cycle has run yet", "sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent cycle")
    else:
        age = age_seconds(last["started"], now) or 0
        rep = json.loads(last["report_json"] or "{}")
        out("[cycle] last=%s ago=%dm status=%s errors=%s evidence=%s entries=%s" % (to_nz(last["started"]), age // 60, last["status"], rep.get("errors") or "none", rep.get("evidence", {}).get("events"), (rep.get("entries") or {}).get("by_status")))
        for name in rep.get("errors") or []:
            st = (rep.get("stages") or {}).get(name, {})
            issue("HIGH", "stage '%s' failed: %s" % (name, st.get("error")), "paste this report to Claude")
        if age > 1800 and check_units:
            issue("HIGH", "last cycle was %d minutes ago (timer expected every 10)" % (age // 60), "journalctl -u paper-agent-cycle -n 40 --no-pager")
    if opens and conn.execute("SELECT COUNT(*) FROM positions p LEFT JOIN position_forecast_plans f ON f.position_id=p.id AND f.active=1 WHERE p.status='OPEN' AND f.id IS NULL").fetchone()[0]:
        issue("MEDIUM", "open position(s) without an active plan (created automatically on next cycle)", "")
    # ---- live feeds
    if live:
        srcs = srcs if srcs is not None else []
        todo = [s for s in srcs if s.get("enabled", True) and not (s["type"] == "finnhub_general" and not os.getenv("FINNHUB_API_KEY"))]

        def probe(s):
            try:
                items, st = sources.fetch_source(s, fetch, None)
                return s["id"], True, "%d items" % len(items)
            except Exception as e:  # noqa: BLE001
                return s["id"], False, str(e)[:90]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            res = list(ex.map(probe, todo))
        bad = [r for r in res if not r[1]]
        out("[feeds] %d/%d reachable" % (len(res) - len(bad), len(res)))
        for sid, ok, msg in bad[:8]:
            out("   FAIL %-22s %s" % (sid, msg))
        if len(bad) > 8:
            out("   ... and %d more failing (same error pattern: %s)" % (len(bad) - 8, bad[0][2][:50]))
        if todo and len(bad) > len(todo) * 0.5:
            issue("HIGH", "more than half the news feeds are failing (network/DNS?)", "ping -c2 1.1.1.1; ping -c2 bbc.co.uk")
        elif bad:
            issue("LOW", "%d feed(s) unreachable: %s (they back off automatically)" % (len(bad), ", ".join(r[0] for r in bad[:8])), "")
        # ---- quote providers
        try:
            q = quotes.consensus(None, "SPY", cfg, now, None, fetch_json)
            out("[quotes] SPY consensus=%s price=%s session=%s" % (q["status"], q["price"], q["session"]))
            usable = 0
            for o in q["observations"]:
                ok = o.get("price") is not None and not o.get("error")
                usable += ok
                out("   %-14s %s  price=%s age=%ss %s" % (o["provider"], "OK  " if ok else "FAIL", o["price"], None if o["age_seconds"] is None else int(o["age_seconds"]), o["reject_reason"] or ""))
            if usable < 2:
                issue("HIGH", "fewer than 2 quote providers return data; no trade can execute", "check keys / provider status above")
            if market_state(now)["execution"] and q["status"] != "PASS":
                issue("HIGH", "market is open but quote consensus is %s; trades will be blocked" % q["status"], "see provider lines above")
        except Exception as e:  # noqa: BLE001
            issue("HIGH", "quote check crashed: %s" % e, "paste this report to Claude")
        try:
            bars = marketdata.parse_chart(marketdata.fetch_chart("SPY", "5d", fetch_json))
            out("[yahoo chart] OK (%d bars) - regime/volatility data reachable" % len(bars))
        except Exception as e:  # noqa: BLE001
            out("[yahoo chart] FAIL %s" % e)
            issue("MEDIUM", "Yahoo chart endpoint unreachable: %s (regime, volatility and outcome scoring degrade)" % e, "")
        # ---- dashboard
        try:
            body = urllib.request.urlopen("http://127.0.0.1:%d/health" % cfg["runtime"]["bind_port"], timeout=5).read().decode()
            out("[dashboard] %s" % body[:120])
        except Exception as e:  # noqa: BLE001
            out("[dashboard] not responding: %s" % e)
            if check_units:
                issue("HIGH", "dashboard not responding on port %d" % cfg["runtime"]["bind_port"], "sudo systemctl restart paper-agent-dashboard")
    out("--- VERDICT: %s ---" % ("ALL GOOD" if not [i for i in issues if i["severity"] in ("CRITICAL", "HIGH")] else "%d ISSUE(S) NEED ATTENTION" % len([i for i in issues if i["severity"] in ("CRITICAL", "HIGH")])))
    for i in sorted(issues, key=lambda x: ["CRITICAL", "HIGH", "MEDIUM", "LOW"].index(x["severity"])):
        out("  [%s] %s%s" % (i["severity"], i["message"], ("\n         fix: " + i["fix"]) if i["fix"] else ""))
    return {"issues": issues}
