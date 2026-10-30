"""Orchestration of the OMEGA pipeline:
Data Collection -> Event -> Evidence -> Thesis (Bayesian) -> Narrative -> Company Intelligence -> Portfolio Intelligence
-> Expected Value -> Capital Allocation -> Monitoring -> Execution -> Learning -> Research gaps.

Each stage is isolated: a failing stage is recorded in the cycle report and the rest of the cycle still runs (the legacy
runner skipped everything - including thesis reassessment - whenever the market was closed or a single quote failed)."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import time
import traceback
from typing import Callable, Dict, List, Optional

from . import (candqueue, config, earnings, prices, company, contradiction, db, event_intel, evidence, executor, flows, gaps, learning, llm_client, marketdata as md, monitor, narrative, net, portfolio as pf,
               quotes, regime as regime_engine, sources, thesis, trader)
from .config import load_json, CONFIG_DIR
from .entities import Universe
from .timeutil import iso, market_state, utcnow


class Deps:
    """Injectable I/O so the whole pipeline can be run offline in tests."""

    def __init__(self, fetch=None, fetch_json=None, quote_fn=None, sleep=None, analyst=None, series_fetch=None, llm_transport=None, article_fetch=None):
        self.fetch = fetch or net.get
        self.fetch_json = fetch_json or net.get_json
        self.quote_fn = quote_fn
        self.sleep = sleep or time.sleep
        self.analyst = analyst
        self.series_fetch = series_fetch   # None => real network for Yahoo bars
        self.llm_transport = llm_transport  # None => real HTTPS to the configured LLM provider
        self.article_fetch = article_fetch  # None => the hardened real fetcher (public hosts only); tests inject a fake


def resources_ok(cfg: dict) -> Optional[str]:
    try:
        avail = None
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) // 1024
        if avail is not None and avail < cfg["runtime"]["min_available_memory_mb"]:
            return "low memory: %d MB available" % avail
        if os.cpu_count() and os.getloadavg()[0] > os.cpu_count() * cfg["runtime"]["max_load_per_core"]:
            return "high load %.2f" % os.getloadavg()[0]
    except (OSError, AttributeError):
        pass
    return None


class Lock:
    def __init__(self, path: str, blocking: bool = False):
        self.path, self.blocking, self.f = path, blocking, None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.f = open(self.path, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
        except OSError:
            self.f.close()
            raise BlockingIOError("another cycle holds the lock")
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        finally:
            self.f.close()


def _common_words_path() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_PATH) or ".", "common-words.json")


def load_universe() -> Universe:
    common = load_json(_common_words_path(), [])
    return Universe(load_json(os.path.join(CONFIG_DIR, "watchlist.json"), {}), set(common) if isinstance(common, list) else set())


def refresh_common_words(conn, limit: int = 40000) -> int:
    """Learn which words are ordinary English from the news we have actually stored (cheap, run daily)."""
    from .entities import build_common_words
    rows = conn.execute("SELECT title,summary FROM news ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    words = build_common_words([(r["title"] or "") + " " + (r["summary"] or "")[:400] for r in rows])
    path = _common_words_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(words), f)
    os.replace(tmp, path)
    return len(words)


def load_sources(cfg: Optional[dict] = None) -> List[dict]:
    d = load_json(os.path.join(CONFIG_DIR, "sources.json"), {"sources": []})
    srcs = [dict(s) for s in (d["sources"] if isinstance(d, dict) else d)]
    for s in srcs:                                   # per-feed overrides made in the dashboard
        o = ((cfg or {}).get("source_overrides") or {}).get(s["id"], {})
        if "enabled" in o:
            s["enabled"] = bool(o["enabled"])
        if "interval" in o:
            s["interval"] = int(o["interval"])
    return srcs


_CKPT: Optional[Callable] = None            # set by run_cycle: persists the report so far (a killed cycle then shows where it died)


def _stage(report: dict, name: str, fn: Callable, *a, **kw):
    t0 = time.time()
    report["current"] = name
    if _CKPT:
        try:
            _CKPT()
        except Exception:  # noqa: BLE001 - bookkeeping must never break a cycle
            pass
    try:
        out = fn(*a, **kw)
        report["stages"][name] = {"ok": True, "seconds": round(time.time() - t0, 2)}
        return out
    except Exception as e:  # noqa: BLE001
        frames = [f for f in traceback.extract_tb(e.__traceback__) if "paper_agent" in f.filename]
        where = " <- ".join("%s:%d %s" % (os.path.basename(f.filename), f.lineno, f.name) for f in reversed(frames[-3:]))      # innermost first
        report["stages"][name] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "where": where, "trace": traceback.format_exc()[-900:]}
        report["errors"].append(name)
        return None


def run_cycle(conn, cfg: dict, universe: Optional[Universe] = None, srcs: Optional[List[dict]] = None, deps: Optional[Deps] = None, now=None, mode: str = "full") -> dict:
    deps = deps or Deps()
    now = now or utcnow()
    universe = universe if universe is not None else load_universe()
    srcs = srcs if srcs is not None else load_sources(cfg)
    report: Dict = {"started": iso(now), "mode": mode, "stages": {}, "errors": [], "market": market_state(now)}
    # a cycle that never finished (timeout / out-of-memory / reboot) is marked KILLED so it is visible instead of looking "running" forever
    conn.execute("UPDATE cycle_runs SET status='KILLED' WHERE status='RUNNING' AND started<?", (iso(now - dt.timedelta(minutes=12)),))
    run_id = conn.execute("INSERT INTO cycle_runs(started,status,mode) VALUES(?,?,?)", (iso(now), "RUNNING", mode)).lastrowid
    t_start = time.time()
    budget = float(cfg["runtime"].get("cycle_budget_seconds", 300))
    over_budget = lambda frac=1.0: (time.time() - t_start) > budget * frac  # noqa: E731

    def _save() -> None:
        conn.execute("UPDATE cycle_runs SET report_json=? WHERE id=?", (json.dumps(report, default=str), run_id))
    global _CKPT
    _CKPT = _save
    bad = resources_ok(cfg)
    if bad:
        report["skipped"] = bad
        conn.execute("UPDATE cycle_runs SET finished=?,status='SKIPPED',report_json=? WHERE id=?", (iso(), json.dumps(report), run_id))
        _CKPT = None
        return report
    quote_fn = deps.quote_fn or (lambda c, t, purpose: quotes.get_quote(c, t, cfg, purpose, now=now, fetch=deps.fetch_json))
    thesis.seed(conn)
    icfg = cfg["intelligence"]
    client = llm_client.LLMClient(conn, cfg, deps.llm_transport)
    analyst = deps.analyst or event_intel.make_analyst(conn, cfg, universe, deps.llm_transport)
    intel = {"client": client} if client.enabled("triage") else None
    report["llm"] = {"enabled": bool(intel)}

    def heal_schedule():
        from . import control
        st = control.schedule_status(cfg.get("schedule", {}))
        drift = [k for k, v in st.items() if not v["in_sync"]]
        if drift and db.due(conn, "schedule_reconcile", 0.5, now):
            try:
                control.send_request("set_schedule", dict(cfg["schedule"]))
            except OSError:
                pass                                    # control dir missing/unwritable: reported by the doctor, must never fail a cycle
            db.mark_run(conn, "schedule_reconcile", ",".join(drift), now)
        return {"drift": drift}
    report["schedule"] = _stage(report, "schedule", heal_schedule)
    if db.due(conn, "common_words", 24, now):                     # keeps company matching precise as the news corpus grows
        report["common_words"] = _stage(report, "common_words", refresh_common_words, conn)
        db.mark_run(conn, "common_words", "", now)

    report["earnings"] = _stage(report, "earnings", earnings.refresh, conn, cfg, now, deps.fetch_json)                # who reports in the next 10 days
    report["quote_health"] = _stage(report, "quote_health", quotes.probe, conn, cfg, now, deps.fetch_json)           # keeps the System tab's provider table true
    report["prices"] = _stage(report, "prices", prices.run, conn, cfg, now, deps.series_fetch)       # pre-market / after-hours prices (your schedule, Settings -> Refresh times)

    # ---- world state
    reg = regime_engine.current(conn)
    if db.due(conn, "regime", icfg["regime_refresh_hours"], now):
        r = _stage(report, "regime", regime_engine.refresh, conn, deps.series_fetch, now, icfg["regime_refresh_hours"])
        if r:
            reg = {"regime": r["regime"], "probs": r["probs"], "risk_mult": r["risk_mult"]}
            db.mark_run(conn, "regime", r["regime"], now)
    report["regime"] = reg.get("regime")

    # ---- collection
    held = [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM positions WHERE status='OPEN'")]

    def collect():
        out = sources.collect(conn, [s for s in srcs if icfg["gdelt_enabled"] or s["type"] != "gdelt"], deps.fetch, clock=lambda: now.timestamp(), sleep=deps.sleep)
        names = list(dict.fromkeys(held + [r[0] for r in conn.execute("SELECT ticker FROM decisions WHERE status IN ('AWAITING_MARKET','AWAITING_CAPACITY')")]))[: icfg["finnhub_company_news_max"]]
        extra = 0
        if os.getenv("FINNHUB_API_KEY") and db.due(conn, "company_news", 0.25, now):
            src = {"id": "finnhub_company", "name": "Finnhub Company News", "trust": 0.7}
            for t in names:
                try:
                    extra += sources.store_items(conn, src, sources.finnhub_company_news(t, deps.fetch), now)
                except Exception:  # noqa: BLE001
                    pass
            db.mark_run(conn, "company_news", "", now)
        out["company_news_new"] = extra
        out["contradiction"] = contradiction.search(conn, cfg, deps.fetch, now, sleep=deps.sleep)
        return out
    report["collect"] = _stage(report, "collect", collect)

    # ---- event / evidence / thesis / narrative
    ev = _stage(report, "evidence", evidence.process_new_news, conn, universe, cfg, now, analyst, 600, deps.article_fetch)
    report["evidence"] = ev["stats"] if ev else {}
    report["articles"] = {k: v for k, v in ((ev or {}).get("articles") or {}).items() if k != "notes"}
    cands = candqueue.load(conn, now, icfg["trade_event_max_age_hours"])          # durable: includes leftovers from a cycle that was killed before it reached entries
    report["queue"] = candqueue.counts(conn)
    _stage(report, "flows", flows.record_event_flows, conn, iso(now - dt.timedelta(days=3)))
    _stage(report, "thesis", thesis.recompute, conn, now)
    _stage(report, "narrative", narrative.update, conn, now)

    # ---- company intelligence (budgeted)
    def refresh_companies():
        want = list(dict.fromkeys(held + [c["ticker"] for c in cands]))
        n = 0
        for t in want:
            if over_budget(0.4):                    # slow network calls must not starve the decisions that follow
                report.setdefault("budget", []).append("companies: stopped early at %ds" % (time.time() - t_start))
                break
            row = conn.execute("SELECT updated FROM companies WHERE ticker=?", (t,)).fetchone()
            fresh = False
            if row and row["updated"]:
                from .timeutil import parse_ts
                fresh = (now - parse_ts(row["updated"])).total_seconds() < icfg["company_refresh_hours"] * 3600
            if fresh or n >= icfg["max_company_refresh_per_cycle"]:
                continue
            n += 1
            try:                                    # one bad company must not abort the others (it used to fail the whole stage)
                md.refresh_symbol(conn, t, 12, "1y", deps.series_fetch, now)
                company.refresh_company(conn, t, universe.meta(t), cfg, deps.fetch_json, now, use_network=True)
                if os.getenv("FINNHUB_API_KEY"):
                    try:
                        flows.fetch_insiders(conn, t, deps.fetch_json)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as e:  # noqa: BLE001
                report.setdefault("company_errors", []).append("%s: %s: %s" % (t, type(e).__name__, str(e)[:100]))
        for etf in flows.ETF_LIST[:8] if db.due(conn, "etf_series", 12, now) else []:
            md.refresh_symbol(conn, etf, 12, "1y", deps.series_fetch, now)
        db.mark_run(conn, "etf_series", "", now)
        return n
    _stage(report, "companies", refresh_companies)

    # ---- company knowledge base (SEC filings read by the LLM; skipped when the LLM is off)
    def refresh_knowledge():
        from . import filings
        if not intel:
            return {"skipped": "llm off"}
        done = {}
        for t in filings.due_tickers(conn, cfg, held, [c["ticker"] for c in cands], now):
            if over_budget(0.5):
                report.setdefault("budget", []).append("knowledge: stopped early at %ds" % (time.time() - t_start))
                break
            meta = universe.meta(t)
            try:
                done[t] = filings.refresh(conn, cfg, client, t, meta.get("cik"), deps.fetch, now, sleep=deps.sleep)
            except Exception as e:  # noqa: BLE001
                done[t] = {"error": "%s: %s" % (type(e).__name__, e)}
            db.mark_run(conn, "kb:" + t, "", now)
        return done
    report["knowledge"] = _stage(report, "knowledge", refresh_knowledge)

    # ---- capital allocation
    cands = cands + trader.thesis_candidates(conn, cfg, reg, universe, now)
    ent = _stage(report, "entries", trader.run_entries, conn, cfg, universe, cands, reg, now, quote_fn, deps.series_fetch if deps.series_fetch else None, intel, t_start + budget * 1.2)
    report["entries"] = ent

    # ---- portfolio intelligence -> monitoring -> execution
    snap = _stage(report, "portfolio", pf.snapshot, conn, cfg, reg, now)
    mon = _stage(report, "monitor", monitor.monitor, conn, cfg, reg, now, snap["stress"] if snap else None, snap["holdings"] if snap else None)
    report["monitor"] = {"signals": mon["signals"], "plans_changed": mon["plans_changed"]} if mon else {}
    exe = _stage(report, "executor", executor.run, conn, cfg, now, quote_fn)
    report["executor"] = {k: exe[k] for k in ("checked", "committed", "extended", "blocked", "errors")} if exe else {}
    snap2 = _stage(report, "portfolio_final", pf.snapshot, conn, cfg, reg, now)
    if snap2:
        report["portfolio"] = {"equity": round(snap2["equity"], 2), "cash": round(snap2["cash"], 2), "drawdown": round(snap2["drawdown"], 4), "survival": round(snap2["stress"]["survival_probability"], 4)}

    # ---- learning + gaps
    if mode == "full":
        _stage(report, "thesis_predictions", learning.record_thesis_predictions, conn, now)
        if db.due(conn, "learning", 6, now):
            lr = _stage(report, "learning", learning.resolve_due, conn, cfg, now, deps.series_fetch)
            rc = _stage(report, "recalibrate", learning.recalibrate, conn, cfg, now)
            db.mark_run(conn, "learning", "", now)
            report["learning"] = {"resolved": lr, "recalibrated": bool(rc and (rc["calibration"] or rc["event_types"] or rc["theses"]))}
        _stage(report, "gaps", gaps.scan, conn, cfg, now)
        _stage(report, "gaps_resolve", gaps.resolve_auto, conn, cfg, universe, deps.fetch_json, deps.fetch, now)
    if intel:
        report["llm"] = client.usage_summary()
    report["finished"] = iso()
    report["current"] = None
    report["seconds"] = round(time.time() - t_start, 1)
    _CKPT = None
    conn.execute("UPDATE cycle_runs SET finished=?,status=?,report_json=? WHERE id=?", (iso(), "ERROR" if report["errors"] else "OK", json.dumps(report, default=str), run_id))
    conn.execute("DELETE FROM cycle_runs WHERE id<?", (run_id - 2000,))
    return report


def run_guard(conn, cfg: dict, deps: Optional[Deps] = None, now=None) -> dict:
    """Fast path every ~2 min: only manage exits on fresh quotes. Does nothing outside regular hours."""
    deps = deps or Deps()
    now = now or utcnow()
    if not market_state(now)["execution"]:
        return {"skipped": "market %s" % market_state(now)["session"]}
    if not conn.execute("SELECT 1 FROM positions WHERE status='OPEN' LIMIT 1").fetchone():
        return {"skipped": "no open positions"}
    quote_fn = deps.quote_fn or (lambda c, t, purpose: quotes.get_quote(c, t, cfg, purpose, now=now, fetch=deps.fetch_json))
    return executor.run(conn, cfg, now, quote_fn)
