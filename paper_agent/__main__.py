"""CLI:  python3 -m paper_agent <command>"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import VERSION, backup as backup_mod, config, db, net, pipeline, universe as universe_mod
from .config import CONFIG_DIR, DB_PATH, STATE_DIR
from .timeutil import utcnow


def _setup():
    config.load_secrets()
    try:
        from . import keys
        keys.resolve_finnhub()                      # a pasted 40-character Finnhub value is split and the working half is used
    except Exception:  # noqa: BLE001
        pass
    cfg = config.load_config()
    net.set_contact(cfg["runtime"]["contact_email"])
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="paper_agent", description="Paper Agent v%s (virtual money only)" % VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("migrate", "cycle", "guard", "serve", "learn", "probe", "universe", "backup", "status", "keys", "llm-test", "llm-eval"):
        sub.add_parser(name)
    pp2 = sub.add_parser("preflight", help="pre-open checklist: READY or the exact blockers")
    pp2.add_argument("--offline", action="store_true")
    rp = sub.add_parser("recover", help="re-evaluate recent events whose candidates were lost when a cycle died")
    rp.add_argument("--hours", type=float, default=24)
    sub.add_parser("common-words", help="learn which words are ordinary English from the stored news")
    rp2 = sub.add_parser("report", help="write the open/midday/close report (also pushed to your phone if a ntfy topic is set)")
    rp2.add_argument("--no-push", action="store_true")
    rp2.add_argument("--offline", action="store_true")
    sub.add_parser("acknowledge-errors", help="treat all errors so far as history (kept, but no longer flagged)")
    ep = sub.add_parser("errors", help="print the stored traceback of the most recent failed stages")
    ep.add_argument("--limit", type=int, default=3)
    sub.add_parser("recall-check", help="test company recognition on realistic headlines using the real watchlist")
    fp = sub.add_parser("funnel", help="why did / did not news become decisions")
    fp.add_argument("--hours", type=float, default=24)
    for name in ("apply-deploy",):
        pp = sub.add_parser(name)
        pp.add_argument("file", nargs="?", default="")
    for name in ():
        sub.add_parser(name)
    dp = sub.add_parser("doctor", help="full health report")
    dp.add_argument("--fix", action="store_true")
    dp.add_argument("--offline", action="store_true")
    qp = sub.add_parser("quote", help="show what every quote provider returns for a ticker")
    qp.add_argument("ticker")
    args = ap.parse_args(argv)
    cfg = _setup()
    if args.cmd == "keys":   # which provider keys are present (names only, never values)
        for k in ("TWELVE_DATA_API_KEY", "FINNHUB_API_KEY", "ALPACA_API_KEY", "ALPACA_API_SECRET", "MASSIVE_API_KEY", "ALPHA_VANTAGE_API_KEY", "TIINGO_API_KEY", "EODHD_API_KEY", "ANTHROPIC_API_KEY"):
            print("%-24s %s" % (k, "set" if os.getenv(k) else "-"))
        return 0
    if args.cmd == "doctor":
        from . import doctor
        conn = db.open_db(DB_PATH, cfg["portfolio"]["starting_cash"])
        r = doctor.run(conn, cfg, CONFIG_DIR, live=not args.offline, fix=args.fix, srcs=pipeline.load_sources(cfg))
        return 1 if any(i["severity"] in ("CRITICAL", "HIGH") for i in r["issues"]) else 0
    if args.cmd == "quote":
        from . import quotes
        c = quotes.consensus(None, args.ticker.upper(), cfg, providers=None, fetch=net.get_json)
        for o in c["observations"]:
            print("%-14s price=%-10s age=%-8s accepted=%-5s %s" % (o["provider"], o["price"], None if o["age_seconds"] is None else round(o["age_seconds"]), o["accepted"], o["reject_reason"] or ""))
        print("consensus: status=%s price=%s providers=%s execution_eligible=%s (session %s)" % (c["status"], c["price"], c["providers_used"], c["execution_eligible"], c["session"]))
        return 0
    if args.cmd == "preflight":
        from . import preflight
        conn = db.open_db(DB_PATH, cfg["portfolio"]["starting_cash"])
        r = preflight.run(conn, cfg, pipeline.load_universe(), CONFIG_DIR, srcs=pipeline.load_sources(cfg), live=not args.offline)
        return 0 if r["ready"] else 1
    if args.cmd == "recover":
        from . import evidence
        conn = db.open_db(DB_PATH, cfg["portfolio"]["starting_cash"])
        res = evidence.recover(conn, pipeline.load_universe(), cfg, utcnow(), args.hours)
        print("recover: examined %(examined)d unrecorded events from the last %(h)gh -> queued %(queued)d trade candidate(s) for the next cycle; reasons: %(by_gate)s" % {**res, "h": args.hours})
        return 0
    if args.cmd == "common-words":
        conn = db.open_db(DB_PATH, cfg["portfolio"]["starting_cash"])
        print("common words learned from stored news: %d" % pipeline.refresh_common_words(conn))
        return 0
    if args.cmd == "report":
        from . import report
        conn = db.open_db(DB_PATH, cfg["portfolio"]["starting_cash"])
        now = utcnow()
        try:
            rep = report.build(conn, cfg, pipeline.load_universe(), pipeline.load_sources(cfg), now, live=not args.offline)
        except Exception as e:  # noqa: BLE001 - a report must ALWAYS be written, even if building it failed
            import traceback
            text = "VERDICT: FAILED  |  the report itself crashed: %s: %s\n\n%s" % (type(e).__name__, e, traceback.format_exc()[-1500:])
            rep = {"verdict": "FAILED", "label": "error", "text": text, "headline": text.splitlines()[0], "summary": text.splitlines()[0], "failed": [text.splitlines()[0]], "problems": []}
        path = report.write(rep, now)
        pushed = False if args.no_push else report.push(cfg, rep)
        print(rep["text"])
        print("\n[report saved: %s%s]" % (path, "; pushed to phone" if pushed else ""))
        return 0
    if args.cmd == "acknowledge-errors":
        conn = db.open_db(DB_PATH, cfg["portfolio"]["starting_cash"])
        n = conn.execute("SELECT COUNT(*) FROM cycle_runs WHERE status='ERROR'").fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO meta VALUES('errors_ack_at',?)", (utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00"),))
        print("acknowledged: %d earlier failed cycle(s) are now history (kept in the database, no longer flagged). New errors will still be reported." % n)
        return 0
    if args.cmd == "errors":
        conn = db.open_db(DB_PATH, cfg["portfolio"]["starting_cash"])
        seen = set()
        for r in conn.execute("SELECT started,report_json FROM cycle_runs WHERE status='ERROR' ORDER BY id DESC LIMIT 200"):
            for name, st in (json.loads(r["report_json"] or "{}").get("stages") or {}).items():
                if isinstance(st, dict) and st.get("error") and (name, st["error"]) not in seen and len(seen) < args.limit:
                    seen.add((name, st["error"]))
                    print("=== %s  stage %s: %s\n    where: %s\n%s\n" % (r["started"], name, st["error"], st.get("where") or "(not recorded by that build)", st.get("trace") or ""))
        if not seen:
            print("no failed stages recorded")
        return 0
    if args.cmd == "recall-check":
        from . import recall
        res = recall.evaluate(pipeline.load_universe(), icfg=cfg["intelligence"])
        print(recall.text(res))
        return 0 if res["recall"] >= 0.85 and not res["false_positives"] else 1
    if args.cmd == "funnel":
        from . import funnel
        conn = db.open_db(DB_PATH, cfg["portfolio"]["starting_cash"])
        print(funnel.text(funnel.report(conn, args.hours)))
        return 0
    if args.cmd == "apply-deploy":
        from . import settings as _S
        path = args.file or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy-settings.json")
        try:
            with open(path) as fh:
                entries = json.load(fh).get("entries", [])
        except (OSError, ValueError):
            entries = []
        rep = _S.apply_deploy(entries)
        for r in rep:
            print("%-22s %-28s %s" % (r["status"], r["key"], r.get("detail", "")))
        print("release settings: %d entr%s processed; user-changed settings were left alone" % (len(rep), "y" if len(rep) == 1 else "ies"))
        return 0
    if args.cmd == "universe":
        print(universe_mod.refresh(os.path.join(CONFIG_DIR, "watchlist.json")))
        return 0
    if args.cmd == "backup":
        print(backup_mod.backup(DB_PATH, os.path.join(STATE_DIR, "backups")))
        return 0
    if args.cmd == "serve":
        from . import dashboard
        c = db.open_db(DB_PATH, cfg["portfolio"]["starting_cash"])
        c.close()
        dashboard.serve(cfg, DB_PATH)
        return 0
    conn = db.open_db(DB_PATH, cfg["portfolio"]["starting_cash"])
    if args.cmd == "migrate":
        print(json.dumps(db.migrate(conn, cfg["portfolio"]["starting_cash"])))
        return 0
    if args.cmd == "status":
        from . import portfolio as pf, regime
        eq = pf.equity_of(conn)
        last = conn.execute("SELECT started,status FROM cycle_runs ORDER BY id DESC LIMIT 1").fetchone()
        print(json.dumps({"equity": round(eq["equity"], 2), "cash": round(eq["cash"], 2), "regime": regime.current(conn)["regime"], "last_cycle": dict(last) if last else None,
                          "open_positions": conn.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0], "paused": os.path.exists(cfg["runtime"]["pause_file"])}, indent=1))
        return 0
    if args.cmd == "probe":
        from . import sources
        for s in pipeline.load_sources(cfg):
            if s["type"] == "finnhub_general" and not os.getenv("FINNHUB_API_KEY"):
                print("%-22s SKIP (no key)" % s["id"])
                continue
            try:
                items, st = sources.fetch_source(s, net.get)
                print("%-22s OK   %3d items (HTTP %s)" % (s["id"], len(items), st))
            except Exception as e:  # noqa: BLE001
                print("%-22s FAIL %s" % (s["id"], e))
        return 0
    if args.cmd in ("llm-test", "llm-eval"):
        from . import event_intel, golden, llm_client
        client = llm_client.LLMClient(conn, cfg)
        if not client.enabled("analyst"):
            print("LLM disabled: set intelligence.llm_enabled=true in /etc/paper-agent/config.json and provide the provider key (see `keys`).")
            return 1
        uni = pipeline.load_universe()
        if args.cmd == "llm-test":
            r = event_intel.analyze(client, uni, {"title": "Lockheed Martin wins $2.4 billion Pentagon contract for missile defense", "summary": "The Navy awarded the multi-year contract.", "tickers": []}, "triage")
            print(json.dumps(r, indent=1, default=str) if r else "no valid result (see llm_usage table / doctor)")
            print(json.dumps(client.usage_summary()))
            return 0 if r else 1
        res = golden.run(client, uni)
        return 0 if res["passed"] >= res["total"] * 0.8 else 1
    if args.cmd == "learn":
        from . import learning
        print(json.dumps({"resolved": learning.resolve_due(conn, cfg), "recalibrated": learning.recalibrate(conn, cfg)}, default=str, indent=1))
        return 0
    if args.cmd == "guard":
        try:
            with pipeline.Lock(cfg["runtime"]["lock_file"] + ".guard"):
                r = pipeline.run_guard(conn, cfg, pipeline.Deps(analyst=None))
        except BlockingIOError:
            r = {"skipped": "guard already running"}
        print(json.dumps(r, default=str))
        return 0
    if args.cmd == "cycle":
        try:
            with pipeline.Lock(cfg["runtime"]["lock_file"]):
                r = pipeline.run_cycle(conn, cfg, deps=pipeline.Deps())
        except BlockingIOError:
            print("another cycle is running; exiting")
            return 0
        print(json.dumps({k: r.get(k) for k in ("started", "regime", "errors", "entries", "executor", "portfolio", "skipped")}, default=str))
        return 1 if r.get("errors") else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
