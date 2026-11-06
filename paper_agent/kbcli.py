"""Command-line tools for the company knowledge base and the independent evaluation. Heavy work (bootstrap, a full SEC refresh) is run HERE, outside the trading cycle."""
from __future__ import annotations

import json
import os
import resource
import time
from typing import Optional

from . import config, db, kb, kbbench, kbeval, kbevent, kbsnap, whybundle
from .timeutil import iso, utcnow

EVALSET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evalset")
DATASETS = ("real_news_v1", "real_news_holdout_v1", "real_news_family_v1", "real_news_validation_v1")                       # every labelled set that ships, in the order they were drawn


def _universe():
    from . import pipeline
    return pipeline.load_universe()


def _previous_watch():
    p = os.path.join(config.CONFIG_DIR, "watchlist.json.previous")
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _dump(x) -> str:
    return json.dumps(x, indent=1, default=str)


def run(args, conn, cfg) -> int:
    now = utcnow()
    if args.cmd == "company":
        cf = whybundle.case_file(conn, cfg, ticker=args.ticker, now=now)
        for s in cf["sections"]:
            print("\n== %s [%s]\n%s" % (s["title"], s["status"], s["summary"]))
            for r in s["rows"][:14]:
                print("   %-34s %s" % (str(r[0])[:34], " | ".join(str(x) for x in r[1:])[:150]))
            for m in s["missing"][:6]:
                print("   MISSING: " + str(m))
        return 0
    if args.cmd == "eval":
        files = [args.file] if args.file else [os.path.join(EVALSET, f + ".json") for f in DATASETS]
        from .entities import Universe
        U = _universe()
        for fpath in files:
            with open(fpath) as f:
                data = json.load(f)
            r = kbeval.run(conn, cfg, U, data, now)
            print("\n=== %s: %d labelled cases (labeller: %s)" % (data.get("dataset_version"), len(data["cases"]), data.get("labeler")))
            for k, v in r["metrics"].items():
                if k.startswith("_") or k == "by_stratum":
                    continue
                if v.get("value") is not None and "k" in v:
                    print("  %-34s %.3f  (%d/%d)  95%% interval %s" % (k, v["value"], v["k"], v["n"], v.get("ci95")))
                elif k == "n_cases":
                    print("  %-34s %d (gold companies outside the universe: %d)" % (k, v["value"], v.get("gold_companies_outside_universe", 0)))
                else:
                    print("  %-34s %s" % (k, {a: b for a, b in v.items() if a != "detail"}))
            if getattr(args, "save", False):
                kbeval.save_results(conn, "cli-%s" % iso(now), {k: v for k, v in r["metrics"].items() if not k.startswith("_")}, "R2", now)
                kbeval.load_labels(conn, data=data)
                conn.commit()
        print("\nNo combined accuracy figure is given on purpose: each aspect above stands alone, with its own sample size.")
        return 0
    what = args.what
    uni = _universe()
    watch = uni.watch
    if what == "reconcile":
        rep = kb.reconcile(watch, _previous_watch())
        path = os.path.join(config.STATE_DIR, "kb-reconciliation.json")
        try:
            os.makedirs(config.STATE_DIR, exist_ok=True)
            with open(path, "w") as f:
                json.dump(rep, f, indent=1)
        except OSError:
            path = "(could not write the file)"
        if args.json:
            print(_dump(rep))
        else:
            for k, v in rep.items():
                print("%-42s %s" % (k, (len(v) if isinstance(v, (list, dict)) and len(v) > 12 else v) if not isinstance(v, str) else v[:100]))
            print("\nfull report: %s" % path)
        return 0
    if what == "bootstrap":
        t0 = time.time()
        r = kb.bootstrap(conn, watch, now, previous=_previous_watch(), dry_run=args.dry_run, max_batches=args.max_batches)
        print(_dump(r))
        if not args.dry_run:
            kbevent.seed_rules(conn)
            print("relationship seed:", kbsnap.import_relationship_seed(conn, now))
            print("exposure seed:", kbsnap.import_exposure_seed(conn, now))
            print("priorities:", kb.assign_priorities(conn, now))
            conn.commit()
            print("universe preserved exactly:", not kb.verify_universe(conn, watch), "| problems:", kb.verify_universe(conn, watch)[:5])
            print("runtime %.1fs, peak memory %.0f MB" % (time.time() - t0, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0))
        return 0
    if what == "refresh":
        from . import net
        only = [t.strip().upper() for t in args.only.split(",") if t.strip()] or None
        cfg2 = cfg if args.budget is None else dict(cfg, kb=dict(cfg.get("kb") or {}, max_network_seconds=float(args.budget)))
        r = kb.refresh(conn, cfg2, now, net.get_json, limit=args.limit, dry_run=args.dry_run, only=only)
        print(_dump(r))
        if args.facts and r.get("status") != "source_backoff":
            f = kb.refresh_facts(conn, cfg2, now, net.get_json, limit=args.facts, budget_s=args.budget, dry_run=args.dry_run)
            print("financial facts:", _dump(f))
            if f.get("status") not in ("ok", "dry_run", "stopped", "source_backoff", "disabled", "off"):
                return 1
        return 0 if r.get("status") in ("ok", "dry_run", "stopped", "source_backoff", "disabled") else 1
    if what == "coverage":
        s = kb.coverage_summary(conn)
        print("COVERAGE BY DATA CATEGORY (entities with at least one record; %d active entities, %d operating or unclassified)" % (s["active_entities"], s["operating_or_unclassified"]))
        for r in s["categories"]:
            print("  %-46s %6d  %6.1f%% of all  %6.1f%% of operating" % (r["category"], r["entities"], 100 * (r["share_of_all_entities"] or 0), 100 * (r["share_of_operating_companies"] or 0)))
        print("\n" + s["caution"] + "\n")
        print(_dump(kb.coverage_report(conn, watch)))
        return 0
    if what == "prune":
        return run_prune(conn, cfg)
    if what == "universe-report":
        print(_dump(kb.universe_quality(watch, _previous_watch())))
        return 0
    if what == "universe-check":
        from . import net, universe as _u
        payload, _ = net.get_json(_u.URL)
        print(_dump(kb.universe_check(payload, [t.strip() for t in args.tickers.split(",") if t.strip()], watch)))
        return 0
    if what == "unresolved":
        print(_dump(kb.unresolved_report(conn)))
        return 0
    if what == "provenance":
        print(_dump(kb.provenance_report(conn)))
        return 0
    if what == "stale":
        print(_dump(kb.stale_report(conn, cfg, now)))
        return 0
    if what == "resources":
        print(_dump(kbbench.resources(conn, cfg, watch)))
        return 0
    return 2


def after_universe(conn, cfg: dict, watch: dict, now, fetch_json, sleep=time.sleep) -> dict:
    """The weekly follow-up to the universe refresh, run from the same service OUTSIDE the trading cycle: identity re-synced to the new universe, seeds and priorities refreshed, then a larger (but still time-bounded)
    SEC refresh of the companies that matter most. Returns a summary; raises nothing a caller must handle."""
    from . import kbstage, kbevent, kbsnap
    out: dict = {}
    for name, fn in (("identity", lambda: kbstage.sync_identity(conn, watch, now)), ("rules", lambda: kbevent.seed_rules(conn)),
                     ("seeds", lambda: {"relationships": kbsnap.import_relationship_seed(conn, now)["imported"], "exposures": kbsnap.import_exposure_seed(conn, now)["imported"]}), ("priorities", lambda: kb.assign_priorities(conn, now))):
        try:
            out[name] = fn()
        except Exception as ex:  # noqa: BLE001
            out.setdefault("errors", []).append("%s: %s: %s" % (name, type(ex).__name__, str(ex)[:100]))
    big = dict(cfg, kb=dict(cfg.get("kb") or {}, refresh_companies_per_cycle=500, max_network_seconds=900))
    try:
        out["refresh"] = kb.refresh(conn, big, now, fetch_json, sleep=sleep)
    except Exception as ex:  # noqa: BLE001
        out.setdefault("errors", []).append("refresh: %s: %s" % (type(ex).__name__, str(ex)[:100]))
    try:
        n_facts = int((cfg.get("kb") or {}).get("weekly_facts_companies", 60))
        if n_facts > 0 and (out.get("refresh") or {}).get("status") != "source_backoff":
            out["facts"] = kb.refresh_facts(conn, big, now, fetch_json, limit=n_facts, budget_s=900, sleep=sleep)
    except Exception as ex:  # noqa: BLE001
        out.setdefault("errors", []).append("facts: %s: %s" % (type(ex).__name__, str(ex)[:100]))
    conn.commit()
    return out


def run_prune(conn, cfg) -> int:
    print(_dump(kb.prune_filings(conn, cfg)))
    return 0


READ_ONLY_KB = {"reconcile", "coverage", "unresolved", "provenance", "stale", "universe-report", "universe-check"}


def is_read_only(args) -> bool:
    """Commands that only LOOK: they need no write access to the database, so any user on the machine may run them. Everything that writes (bootstrap, a real refresh, resources, saving an evaluation) belongs to the bot's own user."""
    if args.cmd == "company":
        return True
    if args.cmd == "eval":
        return not getattr(args, "save", False)
    what = getattr(args, "what", None)
    if what in READ_ONLY_KB:
        return True
    return what in ("bootstrap", "refresh") and bool(getattr(args, "dry_run", False))


def permission_message(args, ex) -> str:
    cmd = "python3 -m paper_agent %s" % " ".join(a for a in (args.cmd, getattr(args, "what", None) or getattr(args, "ticker", None)) if a)
    why = ("The bot keeps its database in WAL mode, so even reading it needs the bot's own user (%s)." % str(ex)[:60]) if is_read_only(args) else ("This command writes to the bot's database, which only its own user may do (%s)." % str(ex)[:60])
    return "%s\nRun it as the bot's own user:\n  sudo -u paperagent env PYTHONPATH=/opt/paper-agent %s" % (why, cmd)
