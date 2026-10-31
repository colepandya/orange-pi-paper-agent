"""One-shot health report: everything that can be wrong on the Pi, checked live, with a suggested fix for each issue."""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import time
import urllib.request
from typing import Callable, Dict, List, Optional

from . import VERSION, db, llm_client, marketdata, net, portfolio as pf, quotes, regime, sources
from .timeutil import age_seconds, market_state, to_nz, utcnow

UNITS = ["paper-agent-cycle.timer", "paper-agent-guard.timer", "paper-agent-dashboard.service", "paper-agent-backup.timer", "paper-agent-universe.timer", "paper-agent-update.timer", "paper-agent-open.timer", "paper-agent-report.timer", "paper-agent-maintain.timer", "paper-agent-control.path"]
KEYS = ["TWELVE_DATA_API_KEY", "FINNHUB_API_KEY", "ALPACA_API_KEY", "ALPACA_API_SECRET", "MASSIVE_API_KEY", "ALPHA_VANTAGE_API_KEY", "TIINGO_API_KEY", "EODHD_API_KEY"]


def _unit_state(unit: str) -> str:
    try:
        return subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "n/a"


PROVIDER_HINTS = {
    ("FINNHUB", 401): "Finnhub REJECTED the API key (401). Use the 20-character API key from finnhub.io/dashboard, NOT the webhook secret (a second 20-character string shown next to it). Fix it in Settings -> API keys. Until then company profiles, Finnhub news and insider data are unavailable.",
    ("MASSIVE", 403): "Massive returned 403: its free plan has no real-time last-trade endpoint. This is expected and harmless: it just will not count toward trade execution (Twelve Data, Alpaca and Yahoo still can).",
}


def _classify(msg: str) -> str:
    m = re.search(r"HTTP (\d{3})", msg)
    if m:
        return "HTTP " + m.group(1)
    return "timeout" if "timed out" in msg or "timeout" in msg.lower() else ("DNS/network" if "Name or service" in msg or "URL error" in msg else msg[:30])


def iso_hours_ago(h: float, now=None) -> str:
    import datetime as _dt
    from .timeutil import iso as _iso
    return _iso((now or utcnow()) - _dt.timedelta(hours=h))


def run(conn, cfg: dict, config_dir: str, fetch: Callable = net.get, fetch_json: Callable = net.get_json, live: bool = True, check_units: bool = True,
        fix: bool = False, srcs: Optional[List[dict]] = None, out: Callable = print, sleep: Callable = time.sleep, now=None) -> dict:
    issues: List[dict] = []

    def issue(sev: str, msg: str, fixcmd: str = "") -> None:
        issues.append({"severity": sev, "message": msg, "fix": fixcmd})

    now = now or utcnow()
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
    # ---- schema: a database that began as an OLDER install keeps old tables; a missing column breaks only the code path that needs it (e.g. quotes, only while the market is open)
    try:
        from . import db as _db
        drift = _db.schema_drift(conn)
        out("[schema] %s" % ("matches the code" if not drift else "DRIFT: " + "; ".join("%s missing %s" % (t, ",".join(c)) for t, c in list(drift.items())[:4])))
        if drift:
            issue("HIGH", "the database is missing columns the code needs (%s)" % "; ".join("%s: %s" % (t, ",".join(c)) for t, c in list(drift.items())[:4]),
                  "sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent migrate   (adds them, keeps all data; the installer does this automatically)")
    except Exception as e:  # noqa: BLE001
        out("[schema] check failed: %s" % e)
    try:
        from . import maintain as _mt
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        sm = _mt.summary(conn, path)
        out("[database] %s | %d legacy table(s), %d old v4 decision(s)%s | last clean-up: %s" % ("%.1f MB" % sm["file_mb"] if sm["file_mb"] is not None else "size n/a", sm["legacy_tables"], sm["legacy_decisions"],
                                                                                             " -> run: python3 -m paper_agent db-maintain plan" if (sm["legacy_tables"] or sm["legacy_decisions"]) else "", sm["last"] or "never"))
        from . import backup as _bk
        pr = _bk.projection(conn, path, ["/var/backups/paper-agent", os.path.join(os.path.dirname(path), "backups")], utcnow().strftime("%Y-%m-%dT%H:%M:%S"))
        grow = ("growing %.1f MB/day" % pr["growth_mb_day"]) if pr["growth_mb_day"] is not None else "growth not measured yet (needs two daily backups)"
        left = (" | at this rate the disk lasts about %d more days%s" % (pr["days_left"], " -> LOWER the news / log retention (Settings -> Database)" if pr["days_left"] < 120 else "")) if pr["days_left"] is not None else ""
        out("[disk] database %.0f MB, %s | backups use %.0f MB | %.1f GB free%s" % (pr["db_mb"], grow, pr["backups_mb"], pr["free_mb"] / 1024.0, left))
    except Exception as e:  # noqa: BLE001
        out("[database] check failed: %s" % e)
    # ---- dashboard-managed settings
    from . import config as _cfg
    try:
        if os.path.exists(_cfg.SETTINGS_PATH):
            with open(_cfg.SETTINGS_PATH) as fh:
                ov = json.load(fh)
            out("[settings] dashboard overrides active: %s" % (", ".join(sorted(ov)) or "none"))
    except (OSError, ValueError) as e:
        issue("MEDIUM", "dashboard settings file is unreadable (%s); defaults are being used" % e, "reset it from the dashboard Settings tab, or delete %s" % _cfg.SETTINGS_PATH)
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
        with open(wl) as fh:
            n = len(json.load(fh))
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
    # ---- resources: a low-memory Pi SKIPS cycles entirely (no news processing, no decisions) - make that impossible to miss
    try:
        with open("/proc/meminfo") as fh:
            mem = {l.split(":")[0]: int(l.split()[1]) // 1024 for l in fh if l.split(":")[0] in ("MemAvailable", "MemTotal", "Shmem")}
        out("[memory] available %d / %d MB | RAM-disk (shmem) %d MB | cycle skips below %d MB" % (mem.get("MemAvailable", 0), mem.get("MemTotal", 0), mem.get("Shmem", 0), cfg["runtime"]["min_available_memory_mb"]))
        if mem.get("MemAvailable", 9999) < cfg["runtime"]["min_available_memory_mb"] * 1.5:
            issue("HIGH", "available memory %d MB is close to the cycle's skip threshold (%d MB): cycles may be skipped" % (mem.get("MemAvailable", 0), cfg["runtime"]["min_available_memory_mb"]), "sudo bash /opt/paper-agent/scripts/cleanup-ram.sh")
        if mem.get("Shmem", 0) > 60:
            issue("MEDIUM", "RAM-disk is holding %d MB (leftover temp files from older installs)" % mem["Shmem"], "sudo bash /opt/paper-agent/scripts/cleanup-ram.sh")
    except (OSError, ValueError):
        pass
    runs = conn.execute("SELECT status,COUNT(*) n FROM cycle_runs WHERE started>? GROUP BY status", (iso_hours_ago(24, now),)).fetchall()
    if runs:
        out("[cycles 24h] " + ", ".join("%s=%d" % (r["status"], r["n"]) for r in runs))
        if any(r["status"] == "ERROR" for r in runs):
            try:
                from . import funnel as _fn
                for m, n in _fn.report(conn, 24, now, samples=0)["stage_errors"][:3]:
                    out("   stage error x%d: %s" % (n, m))
            except Exception:  # noqa: BLE001
                pass
        skipped = sum(r["n"] for r in runs if r["status"] == "SKIPPED")
        if skipped:
            why = conn.execute("SELECT report_json FROM cycle_runs WHERE status='SKIPPED' ORDER BY id DESC LIMIT 1").fetchone()
            reason = (json.loads(why["report_json"] or "{}").get("skipped") if why else "") or "?"
            issue("HIGH", "%d cycle(s) in the last 24h were SKIPPED (latest reason: %s); a skipped cycle processes no news and makes no decisions" % (skipped, reason), "sudo bash /opt/paper-agent/scripts/cleanup-ram.sh; check memory above")
    try:
        from . import control as _control
        ss = _control.schedule_status(cfg.get("schedule", {}))
        out("[schedule] " + ", ".join("%s: configured %d / running %d %s" % (k, v["configured"], v["applied"], "ok" if v["in_sync"] else "DRIFT") for k, v in ss.items()))
        for k, v in ss.items():
            if not v["in_sync"]:
                issue("MEDIUM", "%s is set to %d but systemd is running %d" % (k, v["configured"], v["applied"]), "Settings -> Services -> Apply schedule now")
    except Exception:  # noqa: BLE001
        pass
    # ---- decision funnel: why are (or are not) news items becoming decisions?
    try:
        from . import funnel as _funnel
        fr = _funnel.report(conn, 24, now, samples=1)
        out("[funnel 24h] news=%d events=%d candidates=%d decisions=%d trades=%d | last decision %s" % (fr["news"], fr["events"], fr["candidates"], fr["decisions_total"], fr["executed"], to_nz(fr["last_decision"]) if fr["last_decision"] else "never"))
        if fr["gates"]:
            out("   why events dropped: " + ", ".join("%s x%d" % (g, n) for g, n in list(fr["gates"].items())[:5]))
        for f in fr["findings"]:
            if f["severity"] in ("HIGH", "MEDIUM"):
                issue(f["severity"], f["text"], f["fix"])
    except Exception as e:  # noqa: BLE001
        out("[funnel] unavailable: %s" % e)
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
        normal = [s for s in todo if s["type"] != "gdelt"]
        gdelt = [s for s in todo if s["type"] == "gdelt"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            res = list(ex.map(probe, normal))
        for k, s in enumerate(gdelt[:2]):               # GDELT allows 1 request / 5 s: sample two politely instead of hammering all of them
            if k:
                sleep(5.5)
            res.append(probe(s))
        bad = [r for r in res if not r[1]]
        out("[feeds] %d/%d reachable%s" % (len(res) - len(bad), len(res), (" (GDELT: %d of %d sampled)" % (min(2, len(gdelt)), len(gdelt))) if gdelt else ""))
        groups: dict = {}
        for sid, ok, msg in bad:
            groups.setdefault(_classify(msg), []).append(sid)
        for cause, ids in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            out("   %-12s x%-2d %s" % (cause, len(ids), ", ".join(ids)))
        if todo and len(bad) > len(todo) * 0.5:
            issue("HIGH", "more than half the news feeds are failing (network/DNS?)", "ping -c2 1.1.1.1; ping -c2 bbc.co.uk")
        elif bad:
            dead = [sid for c, ids in groups.items() if c in ("HTTP 404", "HTTP 410") for sid in ids]
            issue("LOW", "%d feed(s) unreachable (%s). They back off automatically; permanently dead ones (404/410: %s) can be switched off in Settings -> News & data feeds" % (len(bad), "; ".join("%s x%d" % (c, len(i)) for c, i in groups.items()), ", ".join(dead) or "none"), "")
        # ---- quote providers
        try:
            q = quotes.consensus(None, "SPY", cfg, now, None, fetch_json)
            out("[quotes] SPY consensus=%s price=%s session=%s" % (q["status"], q["price"], q["session"]))
            usable = 0
            for o in q["observations"]:
                ok = o.get("price") is not None and not o.get("error")
                usable += ok
                out("   %-14s %s  price=%s age=%ss %s" % (o["provider"], "OK  " if ok else "FAIL", o["price"], None if o["age_seconds"] is None else int(o["age_seconds"]), o["reject_reason"] or ""))
            for o in q["observations"]:
                hint = PROVIDER_HINTS.get((o["provider"], o.get("http_status")))
                if hint:
                    issue("MEDIUM" if o["provider"] == "FINNHUB" else "LOW", hint, "")
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
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % cfg["runtime"]["bind_port"], timeout=5) as resp:
                body = resp.read().decode()
            out("[dashboard] %s" % body[:120])
        except Exception as e:  # noqa: BLE001
            out("[dashboard] not responding: %s" % e)
            if check_units:
                issue("HIGH", "dashboard not responding on port %d" % cfg["runtime"]["bind_port"], "sudo systemctl restart paper-agent-dashboard")
    out("--- VERDICT: %s ---" % ("ALL GOOD" if not [i for i in issues if i["severity"] in ("CRITICAL", "HIGH")] else "%d ISSUE(S) NEED ATTENTION" % len([i for i in issues if i["severity"] in ("CRITICAL", "HIGH")])))
    for i in sorted(issues, key=lambda x: ["CRITICAL", "HIGH", "MEDIUM", "LOW"].index(x["severity"])):
        out("  [%s] %s%s" % (i["severity"], i["message"], ("\n         fix: " + i["fix"]) if i["fix"] else ""))
    return {"issues": issues}
