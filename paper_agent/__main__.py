"""CLI:  python3 -m paper_agent <command>"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import VERSION, backup as backup_mod, config, db, net, pipeline, universe as universe_mod
from .config import CONFIG_DIR, DB_PATH, STATE_DIR


def _setup():
    config.load_secrets()
    cfg = config.load_config()
    net.set_contact(cfg["runtime"]["contact_email"])
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="paper_agent", description="Paper Agent v%s (virtual money only)" % VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("migrate", "cycle", "guard", "serve", "learn", "probe", "universe", "backup", "status", "keys"):
        sub.add_parser(name)
    qp = sub.add_parser("quote", help="show what every quote provider returns for a ticker")
    qp.add_argument("ticker")
    args = ap.parse_args(argv)
    cfg = _setup()
    if args.cmd == "keys":   # which provider keys are present (names only, never values)
        for k in ("TWELVE_DATA_API_KEY", "FINNHUB_API_KEY", "ALPACA_API_KEY", "ALPACA_API_SECRET", "MASSIVE_API_KEY", "ALPHA_VANTAGE_API_KEY", "TIINGO_API_KEY", "EODHD_API_KEY", "ANTHROPIC_API_KEY"):
            print("%-24s %s" % (k, "set" if os.getenv(k) else "-"))
        return 0
    if args.cmd == "quote":
        from . import quotes
        c = quotes.consensus(None, args.ticker.upper(), cfg, providers=None, fetch=net.get_json)
        for o in c["observations"]:
            print("%-14s price=%-10s age=%-8s accepted=%-5s %s" % (o["provider"], o["price"], None if o["age_seconds"] is None else round(o["age_seconds"]), o["accepted"], o["reject_reason"] or ""))
        print("consensus: status=%s price=%s providers=%s execution_eligible=%s (session %s)" % (c["status"], c["price"], c["providers_used"], c["execution_eligible"], c["session"]))
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
        for s in pipeline.load_sources():
            if s["type"] == "finnhub_general" and not os.getenv("FINNHUB_API_KEY"):
                print("%-22s SKIP (no key)" % s["id"])
                continue
            try:
                items, st = sources.fetch_source(s, net.get)
                print("%-22s OK   %3d items (HTTP %s)" % (s["id"], len(items), st))
            except Exception as e:  # noqa: BLE001
                print("%-22s FAIL %s" % (s["id"], e))
        return 0
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
        from . import llm
        try:
            with pipeline.Lock(cfg["runtime"]["lock_file"]):
                r = pipeline.run_cycle(conn, cfg, deps=pipeline.Deps(analyst=llm.make_analyst(cfg)))
        except BlockingIOError:
            print("another cycle is running; exiting")
            return 0
        print(json.dumps({k: r.get(k) for k in ("started", "regime", "errors", "entries", "executor", "portfolio", "skipped")}, default=str))
        return 1 if r.get("errors") else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
