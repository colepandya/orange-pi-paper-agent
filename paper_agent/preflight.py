"""Pre-open checklist: everything that must be true for the bot to trade correctly at the open. One verdict: READY or the exact blockers."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
from typing import Callable, Dict, List, Optional

from . import config, control, db, doctor, funnel, marketdata, net, portfolio as pf, quotes, recall, settings as S, sources
from .timeutil import iso, market_state, next_market_open, parse_ts, to_nz, utcnow

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def run(conn, cfg: dict, universe=None, config_dir: str = "", fetch: Callable = net.get, fetch_json: Callable = net.get_json, srcs: Optional[List[dict]] = None,
        live: bool = True, check_units: bool = True, now=None, mem_mb: Callable = control.mem_available_mb, out: Callable = print) -> dict:
    now = now or utcnow()
    checks: List[dict] = []

    def chk(name: str, status: str, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "status": status, "detail": detail, "fix": fix})

    st = market_state(now)
    nxt = next_market_open(now)
    # ---- 1. database
    integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
    chk("Database", PASS if integ == "ok" else FAIL, "integrity %s, schema %s" % (integ, conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]), "restore a backup from /var/lib/paper-agent/backups" if integ != "ok" else "")
    from . import db as _db
    drift = _db.schema_drift(conn)
    chk("Database schema matches the code", PASS if not drift else FAIL, "all tables and columns present" if not drift else "; ".join("%s missing %s" % (t, ",".join(c)) for t, c in list(drift.items())[:4]),
        "sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent migrate" if drift else "")
    # ---- 2. services + schedule
    if check_units:
        states = {u: doctor._unit_state(u) for u in ("paper-agent-cycle.timer", "paper-agent-guard.timer", "paper-agent-dashboard.service", "paper-agent-control.path")}
        bad = {u: s for u, s in states.items() if s != "active"}
        chk("Services", PASS if not bad else FAIL, ", ".join("%s=%s" % (u.replace("paper-agent-", ""), s) for u, s in states.items()), "sudo systemctl enable --now " + " ".join(bad) if bad else "")
    ss = control.schedule_status(cfg.get("schedule", {}))
    drift = [k for k, v in ss.items() if not v["in_sync"]]
    chk("Timer schedule matches settings", PASS if not drift else WARN, "all in effect" if not drift else "not yet applied: " + ", ".join(drift), "Settings -> Services -> Apply schedule now" if drift else "")
    # ---- 3. resources
    m = mem_mb()
    floor = cfg["runtime"]["min_available_memory_mb"]
    if m is not None:
        chk("Memory", PASS if m >= floor * 2 else (WARN if m >= floor * 1.3 else FAIL), "%d MB available (cycles are skipped below %d MB)" % (m, floor), "sudo bash /opt/paper-agent/scripts/cleanup-ram.sh" if m < floor * 2 else "")
    try:
        du = shutil.disk_usage(os.path.dirname(config.DB_PATH) if os.path.exists(os.path.dirname(config.DB_PATH)) else ".")
        chk("Disk space", PASS if du.free > 1 << 30 else (WARN if du.free > 200 << 20 else FAIL), "%.1f GB free" % (du.free / 1e9), "free space or prune /var/backups/paper-agent" if du.free <= 1 << 30 else "")
    except OSError:
        pass
    # ---- 4. cycles + guard actually running
    last = conn.execute("SELECT started,status FROM cycle_runs ORDER BY id DESC LIMIT 1").fetchone()
    age_m = (now - parse_ts(last["started"])).total_seconds() / 60 if last and parse_ts(last["started"]) else None
    interval = int(cfg["schedule"]["cycle_minutes"])
    if age_m is None:
        chk("Cycle running", FAIL, "no cycle has ever run", "sudo systemctl start paper-agent-cycle.service")
    else:
        ok = age_m <= interval * 2.5 + 2 and last["status"] in ("OK",)
        chk("Cycle running", PASS if ok else (WARN if age_m <= interval * 4 else FAIL), "last cycle %.0f min ago, status %s (every %d min)" % (age_m, last["status"], interval), "sudo /opt/paper-agent/scripts/doctor.sh" if not ok else "")
    killed = conn.execute("SELECT COUNT(*) FROM cycle_runs WHERE (status='KILLED' OR (status='RUNNING' AND started<?)) AND started>?", (iso(now - dt.timedelta(minutes=12)), iso(now - dt.timedelta(hours=2)))).fetchone()[0]
    chk("No killed cycles (last 2h)", PASS if not killed else FAIL, "%d cycle(s) started but never finished" % killed if killed else "none", "see `funnel` for the stage they died in; journalctl -u paper-agent-cycle --since -3h" if killed else "")
    base = _db.error_baseline(conn, now)
    recent = [r["status"] for r in conn.execute("SELECT status FROM cycle_runs WHERE status IN ('OK','ERROR') AND started>? ORDER BY id DESC LIMIT 6", (base or "",))]
    if recent:
        nerr = recent.count("ERROR")
        top = funnel.report(conn, 3, now, samples=0)["stage_errors"][:1]
        chk("Recent cycles free of stage errors", PASS if nerr == 0 else (WARN if nerr == 1 else FAIL), "%d of the last %d cycles had stage errors%s" % (nerr, len(recent), (": " + top[0][0][:120]) if (nerr and top) else ""),
            "run `funnel` for every error message" if nerr else "")
    skipped = conn.execute("SELECT COUNT(*) FROM cycle_runs WHERE status='SKIPPED' AND started>?", (iso(now - dt.timedelta(hours=2)),)).fetchone()[0]
    chk("No skipped cycles (last 2h)", PASS if not skipped else FAIL, "%d skipped" % skipped if skipped else "none skipped", "low memory: sudo bash /opt/paper-agent/scripts/cleanup-ram.sh" if skipped else "")
    opens = conn.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
    if opens:
        g = conn.execute("SELECT created FROM executor_runs ORDER BY id DESC LIMIT 1").fetchone()
        gage = (now - parse_ts(g["created"])).total_seconds() / 60 if g and parse_ts(g["created"]) else None
        need = st["execution"] or (nxt - now).total_seconds() < 1800
        chk("Exit guard (open positions)", PASS if (gage is not None and gage <= 6) or not need else FAIL,
            "%d open position(s); last exit check %s" % (opens, "%.0f min ago" % gage if gage is not None else "never"), "systemctl status paper-agent-guard.timer" if need and (gage is None or gage > 6) else "")
        noplan = conn.execute("SELECT COUNT(*) FROM positions p LEFT JOIN position_forecast_plans f ON f.position_id=p.id AND f.active=1 WHERE p.status='OPEN' AND f.id IS NULL").fetchone()[0]
        chk("Open positions have exit plans", PASS if not noplan else WARN, "all have stop/target" if not noplan else "%d without a plan (the next cycle creates one)" % noplan)
    waiting = conn.execute("SELECT status,COUNT(*) n FROM decisions WHERE status IN ('AWAITING_MARKET','AWAITING_QUOTE','AWAITING_CAPACITY') GROUP BY status").fetchall()
    if waiting:
        wd = {r["status"]: r["n"] for r in waiting}
        stuck_q = wd.get("AWAITING_QUOTE", 0)
        chk("BUY decisions waiting", WARN if (stuck_q and st["execution"]) else PASS, ", ".join("%s: %d" % kv for kv in wd.items()) + (" -> market is open but no verified price" if stuck_q and st["execution"] else ""),
            "see `funnel` for the per-provider reason (stale timestamps / missing keys)" if stuck_q and st["execution"] else "")
    # ---- 5. news + decisions pipeline
    fr = funnel.report(conn, 6, now, samples=1)
    ev3 = conn.execute("SELECT COUNT(*) FROM events WHERE created>?", (iso(now - dt.timedelta(hours=3)),)).fetchone()[0]
    chk("News flowing", PASS if ev3 > 0 else FAIL, "%d events in the last 3h" % ev3, "Settings -> News & data feeds; run the health check" if ev3 == 0 else "")
    highs = [f for f in fr["findings"] if f["severity"] == "HIGH"]
    chk("Decision pipeline", PASS if not highs else FAIL, "; ".join(f["text"][:110] for f in highs) or "%d events, %d candidates, %d decisions (6h)" % (fr["events"], fr["candidates"], fr["decisions_total"]), highs[0]["fix"] if highs else "")
    # ---- 6. config sanity
    ov = S.cross_check(cfg, cfg)
    paused = os.path.exists(cfg["runtime"]["pause_file"])
    chk("Trading not paused", PASS if not paused else WARN, "active" if not paused else "PAUSED by the operator: no new trades", "dashboard -> Resume trading" if paused else "")
    dd = pf.drawdown(conn, pf.equity_of(conn, now=now)["equity"])
    chk("Drawdown pause not triggered", PASS if dd < cfg["risk"]["drawdown_pause_pct"] else WARN, "drawdown %.1f%% (pause at %.0f%%)" % (dd * 100, cfg["risk"]["drawdown_pause_pct"] * 100))
    keys_ok = [k for k in ("TWELVE_DATA_API_KEY", "FINNHUB_API_KEY", "ALPACA_API_KEY", "MASSIVE_API_KEY", "TIINGO_API_KEY", "EODHD_API_KEY") if os.getenv(k)]
    chk("Quote provider keys", PASS if len(keys_ok) >= 3 else (WARN if len(keys_ok) >= 2 else FAIL), "%d configured (+ keyless Yahoo)" % len(keys_ok), "Settings -> API keys" if len(keys_ok) < 3 else "")
    if "example" in str(cfg["runtime"].get("contact_email", "")).lower():
        chk("Contact email", WARN, "still a placeholder (SEC and some news sites expect a real one)", "Settings -> System -> Contact email")
    # ---- 7. company recognition on the REAL watchlist
    if universe is not None and len(getattr(universe, "watch", {})) > 0:
        n = len(universe.watch)
        chk("Company universe", PASS if n >= 2000 else FAIL, "%d companies" % n, "sudo systemctl start paper-agent-universe.service" if n < 2000 else "")
        r = recall.evaluate(universe, icfg=cfg["intelligence"])
        chk("Company recognition (recall check)", PASS if r["tested"] and r["recall"] >= 0.9 else (WARN if r["recall"] >= 0.75 else FAIL),
            "%d/%d realistic headlines recognised, %d false positive(s)" % (r["recognised"], r["tested"], len(r["false_positives"])), "python3 -m paper_agent recall-check   (lists the misses)" if r["recall"] < 0.9 else "")
    # ---- 8. live data (feeds + quotes)
    if live:
        todo = [s for s in (srcs or []) if s.get("enabled", True) and s["type"] not in ("gdelt",) and not (s["type"] == "finnhub_general" and not os.getenv("FINNHUB_API_KEY"))]
        okc = 0
        for s in todo[:25]:
            try:
                sources.fetch_source(s, fetch, None)
                okc += 1
            except Exception:  # noqa: BLE001
                pass
        if todo:
            ratio = okc / min(len(todo), 25)
            chk("News feeds reachable", PASS if ratio >= 0.7 else (WARN if ratio >= 0.4 else FAIL), "%d/%d sampled feeds respond" % (okc, min(len(todo), 25)), "Settings -> News & data feeds" if ratio < 0.7 else "")
        try:
            try:                                                     # through the DATABASE, exactly as a real BUY does (provider budgets / backoff live in tables)
                q = quotes.consensus(conn, "SPY", cfg, now, None, fetch_json)
            except sqlite3.OperationalError as e:
                if "readonly" not in str(e).lower():
                    raise
                q = quotes.consensus(None, "SPY", cfg, now, None, fetch_json)      # read-only connection (dashboard button): cannot record calls
            have = [o["provider"] for o in q["observations"] if o.get("price") is not None and not o.get("error")]
            chk("Quote providers returning data", PASS if len(have) >= 2 else FAIL, ", ".join(have) or "none", "check keys in Settings -> API keys; run the health check" if len(have) < 2 else "")
            if st["execution"]:
                chk("Verified fresh quote right now (SPY)", PASS if q["status"] == "PASS" else FAIL, "%s via %s" % (q["status"], "+".join(q["providers_used"]) or "-"), "trades are blocked until two providers agree on a fresh price" if q["status"] != "PASS" else "")
            else:
                chk("Fresh quote at the open", WARN, "market is %s: freshness can only be proven while it is open. Re-run this check ~5 min after the open." % st["session"])
        except Exception as e:  # noqa: BLE001
            chk("Quote providers returning data", FAIL, "check crashed: %s" % e)
        try:
            marketdata.parse_chart(marketdata.fetch_chart("SPY", "5d", fetch_json))
            chk("Market data (regime, volatility)", PASS, "Yahoo chart reachable")
        except Exception as e:  # noqa: BLE001
            chk("Market data (regime, volatility)", WARN, "Yahoo chart unavailable: %s" % e)
    blockers = [c for c in checks if c["status"] == FAIL]
    warns = [c for c in checks if c["status"] == WARN]
    out("=== PRE-OPEN CHECKLIST  %s | US market: %s | next open: %s (in %.1f h) ===" % (to_nz(now), st["session"], to_nz(nxt), (nxt - now).total_seconds() / 3600))
    for c in checks:
        out("  %-4s %-40s %s%s" % ({"PASS": "OK", "WARN": "WARN", "FAIL": "FAIL"}[c["status"]], c["name"], c["detail"], ("\n          fix: " + c["fix"]) if c["fix"] and c["status"] != PASS else ""))
    out("")
    out("VERDICT: %s" % ("READY FOR MARKET OPEN" + (" (with %d warning(s))" % len(warns) if warns else "") if not blockers else "NOT READY: %d blocker(s): %s" % (len(blockers), "; ".join(c["name"] for c in blockers))))
    return {"ready": not blockers, "checks": checks, "blockers": blockers, "warnings": warns}
