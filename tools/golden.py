#!/usr/bin/env python3
"""Golden-master regression check: proves a code change did NOT change what the bot does.

It replays your REAL database through the real trading code with a fixed clock and invented-but-fixed prices, and writes down exactly what happened
(every decision, position and ledger row). Run `record` once on a build you trust, then run `check` after every refactoring step:
anything that differs is reported, and the step is not accepted until the difference is explained.

    python3 tools/golden.py record --db /path/to/paper-agent-export.db --out golden-baseline.json
    python3 tools/golden.py check  --db /path/to/paper-agent-export.db --baseline golden-baseline.json

Three kinds of scenario, with different rules:
  behaviour  what the bot decides, buys and sells. Must NEVER change in a refactor or clean-up step.
  surface    the settings, defaults and database tables. Changes only when a step says it adds or removes one.
  pages      the dashboard HTML. Changes only in dashboard steps.
Use --allow surface,pages to accept expected changes in those kinds. Nothing here is installed on the Pi; it is a development tool.
"""
import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from paper_agent import config, db, net, pipeline, settings as S, timeutil, trader  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402

UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")
CUTOVER = "2026-10-28T01:18:04"                       # when the v5 app took over from v4 in the owner's database
OPEN_AT = dt.datetime(2026, 10, 29, 13, 45, tzinfo=UTC)
FRESH_AT = dt.datetime(2026, 10, 29, 14, 30, tzinfo=UTC)
PAGES_AT = dt.datetime(2026, 10, 29, 22, 0, tzinfo=UTC)
KIND = {"open_defaults": "behaviour", "open_owner": "behaviour", "lifecycle_defaults": "behaviour", "lifecycle_owner": "behaviour",
        "fresh_arrivals": "behaviour", "thesis_open": "behaviour", "surface": "surface", "pages": "pages"}


def pi_baseline(cfg):
    """How the owner's Pi differs from the code defaults BEFORE any dashboard change. Found by comparing the Settings page with the defaults: only slippage differs
    (15 bps on the Pi, carried over from the old v4 config.json; 8 bps in code). If the Pi's /etc/paper-agent/config.json changes, update this and re-record."""
    cfg["portfolio"]["slippage_bps"] = 15


def owner_settings(cfg):
    """The owner's own non-default risk settings (from the Settings page)."""
    cfg["risk"].update(allow_add_to_position=False, daily_loss_pause_pct=0.03, max_gross_pct=0.95, max_position_pct=0.15, min_cash_pct=0.05)
    cfg["strategy"]["partial_exit_pct"] = 0.6


# ------------------------------------------------------------------------------------------------------ helpers
def legacy_mode(cfg):
    """Build 39's new behaviour, all off: no shorts, no options, no relationship graph, the old (larger) edge claim, no opening delay, no correlation gate."""
    cfg["intelligence"]["engine"] = "llm"
    cfg["instruments"]["shorts_enabled"] = False
    cfg["instruments"]["options_enabled"] = False
    cfg["graph"]["enabled"] = False
    cfg["strategy"]["plan_consistent_ev"] = False
    cfg["risk"]["open_delay_minutes"] = 0
    cfg["risk"]["corr_block"] = 1.01
    cfg["risk"]["corr_haircut_from"] = 1.0


class Work:
    """A private copy of the database (never touches the original) plus a fresh default config."""

    def __init__(self, src, tweak=None):
        self.dir = tempfile.mkdtemp(prefix="golden-")
        self.path = os.path.join(self.dir, "work.db")
        shutil.copy(src, self.path)
        self.c = db.open_db(self.path)
        self.cfg = config.load_config("/nonexistent", with_overrides=False)
        self.cfg["runtime"]["pause_file"] = "/nonexistent"
        pi_baseline(self.cfg)
        if tweak:
            tweak(self.cfg)
        if os.environ.get("GOLDEN_LEGACY"):
            legacy_mode(self.cfg)                                               # every feature added after build 38 switched OFF: the behaviour must equal build 38's exactly
        watch = {r["ticker"]: {"name": r["name"] or r["ticker"], "sector": r["sector"]} for r in self.c.execute("select ticker,name,sector from companies")}
        self.uni = Universe(watch)
        self.px = {r["ticker"]: float(r["price"]) for r in self.c.execute("select ticker,price from quote_cache")}
        self.ledger_start = self.c.execute("select coalesce(max(id),0) from ledger").fetchone()[0]

    def close(self):
        try:
            self.c.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)


def quote(price, at):
    if not price:
        return {"status": "FAILED", "execution_eligible": False, "price": None, "observations": [], "provider_count": 0}
    return {"status": "PASS", "execution_eligible": True, "price": price, "providers_used": ["REPLAY"], "quoted_at": at.isoformat(), "observations": [], "provider_count": 1, "age_seconds": 3}


def offline(*a, **k):
    raise net.FetchError("offline replay", None)


def gate_of(reason):
    try:
        g = json.loads(reason).get("gates") or ["?"]
        return str(g[-1])[:150]
    except (ValueError, AttributeError, TypeError):
        return str(reason)[:150]


def snap(w, decision_sql, args=()):
    """Everything that matters, with ids and timestamps removed so it can be compared across runs."""
    c = w.c
    decisions = sorted([r["ticker"], r["status"], gate_of(r["reason"]), round(r["ev_net"] or 0.0, 5), r["origin"] or "event"] for r in c.execute(decision_sql, args))
    positions = [[r["ticker"], r["status"], r["shares"], round(r["cost"] or 0.0, 2), round(r["entry_price"] or 0.0, 4), round(r["exit_price"] or 0.0, 4), round(r["pnl"] or 0.0, 2), r["review_code"] or ""]
                 for r in c.execute("select * from positions order by id")]
    ledger = [[r["kind"], round(r["amount"], 2), round(r["balance"], 2), (r["ref"] or "").split(":")[0]] for r in c.execute("select * from ledger where id>? order by id", (w.ledger_start,))]
    return {"decisions": decisions, "decision_count": len(decisions), "status_counts": dict(sorted(collections.Counter(d[1] for d in decisions).items())), "positions": positions, "ledger": ledger,
            "cash": round(c.execute("select balance from ledger order by id desc limit 1").fetchone()[0], 2)}


# ------------------------------------------------------------------------------------------------------ behaviour scenarios
def _open(src, tweak):
    w = Work(src, tweak)
    waiting = [r[0] for r in w.c.execute("select id from decisions where status in ('AWAITING_MARKET','AWAITING_QUOTE','AWAITING_CAPACITY','HELD') order by id")]

    def quote_fn(conn, t, purpose="entry"):
        p = w.px.get(t)
        if p is None:
            rr = conn.execute("select reason from decisions where ticker=? and status like 'AWAITING%' order by id desc limit 1", (t,)).fetchone()
            try:
                p = float(json.loads(rr[0]).get("ref_price") or 0) or None
            except Exception:  # noqa: BLE001
                p = None
        return quote(p, OPEN_AT)
    trader.run_entries(w.c, w.cfg, w.uni, [], {"regime": "risk_on", "risk_mult": 1.0}, OPEN_AT, quote_fn, md_fetch=offline)
    return w, waiting


def scenario_open(src, tweak):
    w, waiting = _open(src, tweak)
    try:
        return snap(w, "select * from decisions where id in (%s)" % ",".join(str(i) for i in waiting) if waiting else "select * from decisions where 0")
    finally:
        w.close()


PATHS = {   # return vs entry price at each of 8 checkpoints in each of 6 sessions (invented, fixed)
    "NVDA": [[.002, .004, .006, .009, .011, .014, .015, .016], [.026, .030, .032, .034, .030, .026, .021, .018], [.010, .008, .006, .004, .004, .003, .003, .002], [0] * 8, [0] * 8, [0] * 8],
    "AVGO": [[-.002, -.004, -.006, -.010, -.013, -.016, -.019, -.022]] + [[-.022] * 8] * 5,
    "GRMN": [[.001, .002, .001, -.001, .000, .001, .002, .001], [.002, .001, .000, .001, .002, .001, .000, .001], [.001, .002, .001, .000, .001, .002, .001, .000], [.001] * 8, [.001] * 8, [.001] * 8],
    "FDX": [[.002, .003, .002, .002, .001, .002, .002, .002], [-.036, -.037, -.036, -.038, -.037, -.036, -.037, -.037]] + [[-.037] * 8] * 4,
    "EQIX": [[.002, .004, .005, .006, .008, .009, .010, .011], [.012, .013, .014, .016, .018, .020, .022, .023], [.024, .025, .026, .026, .025, .024, .023, .022], [.02] * 8, [.02] * 8, [.02] * 8],
    "MSFT": [[.004, .008, .012, .015, .018, .020, .019, .018], [.016, .014, .012, .010, .009, .008, .007, .006]] + [[.006] * 8] * 4,
    "SHEL": [[.001, -.002, -.004, -.006, -.007, -.008, -.009, -.009], [-.009, -.007, -.005, -.003, -.002, -.001, .000, .001], [.002, .003, .004, .004, .005, .005, .006, .006], [.006] * 8, [.006] * 8, [.006] * 8],
    "GNRC": [[-.004, -.008, -.012, -.013, -.011, -.009, -.007, -.005], [-.003, .000, .004, .008, .010, .012, .013, .014], [.014, .015, .016, .016, .016, .016, .016, .016], [.016] * 8, [.016] * 8, [.016] * 8],
}
SESSIONS = [dt.date(2026, 10, 29), dt.date(2026, 10, 30), dt.date(2026, 11, 2), dt.date(2026, 11, 3), dt.date(2026, 11, 4), dt.date(2026, 11, 5)]
CHECKPOINTS = [(9, 50), (10, 30), (11, 15), (12, 0), (13, 0), (14, 0), (15, 0), (15, 50)]


def scenario_lifecycle(src, tweak):
    """Buy at the open, then run the exit guard at 8 checkpoints in each of 6 sessions along fixed price paths."""
    w, waiting = _open(src, tweak)
    try:
        held = {r["ticker"]: dict(r) for r in w.c.execute("select * from positions where status='OPEN'")}
        clock = [None]

        def price(t, now):
            if t not in held or t not in PATHS:
                return held[t]["entry_price"] if t in held else None          # a holding without an invented path stays at its entry price
            n = now.astimezone(NY)
            di = SESSIONS.index(n.date()) if n.date() in SESSIONS else len(SESSIONS) - 1
            k = max([i for i, (h, m) in enumerate(CHECKPOINTS) if (n.hour, n.minute) >= (h, m)] or [0])
            return held[t]["entry_price"] * (1 + PATHS[t][di][k])

        def quote_fn(conn, t, purpose="exit"):
            return quote(price(t, clock[0]), clock[0])
        deps = pipeline.Deps(quote_fn=quote_fn)
        errors = 0
        for d in SESSIONS:
            for h, m in CHECKPOINTS:
                if d == SESSIONS[0] and (h, m) < (9, 50):
                    continue
                now = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=NY).astimezone(UTC)
                clock[0] = now
                res = pipeline.run_guard(w.c, w.cfg, deps, now)
                errors += len(res.get("errors") or [])
        out = snap(w, "select * from decisions where id in (%s)" % ",".join(str(i) for i in waiting) if waiting else "select * from decisions where 0")
        out["guard_errors"] = errors
        led = w.c.execute("select * from ledger where id>? order by id", (w.ledger_start,)).fetchall()
        out["books_balance"] = all(abs(b["balance"] - (a["balance"] + b["amount"])) < 0.01 for a, b in zip(led, led[1:]))
        return out
    finally:
        w.close()


def scenario_fresh(src, tweak=None):
    """Brand-new BUY-rated events arriving mid-session (10:30 ET), taken from the owner's real forecasts."""
    s = db.open_db(src)
    cands, seen = [], set()
    for r in s.execute("select id,ticker,reason,event_id,news_uid from decisions where created>=? order by id", (CUTOVER,)):
        try:
            R = json.loads(r["reason"])
        except ValueError:
            continue
        if (R.get("forecast") or {}).get("verdict") != "BUY" or not R.get("candidate"):
            continue
        cand = dict(R["candidate"], ticker=r["ticker"], event_id=r["event_id"], news_uid=r["news_uid"], ref_price=R.get("ref_price"))
        if (cand["ticker"], cand["event_id"]) in seen:
            continue
        seen.add((cand["ticker"], cand["event_id"]))
        cands.append(cand)
    s.close()
    w = Work(src, tweak)
    try:
        w.c.execute("DELETE FROM decisions WHERE created>=?", (CUTOVER,))
        w.c.execute("DELETE FROM candidate_queue")
        w.c.commit()
        refp = {c["ticker"]: c.get("ref_price") or c.get("price") for c in cands}

        def quote_fn(conn, t, purpose="entry"):
            p = refp.get(t) or w.px.get(t)
            if not p:
                row = conn.execute("select close from market_series where symbol=? order by day desc limit 1", (t,)).fetchone()
                p = row[0] if row else None
            return quote(p, FRESH_AT)
        for c in cands:
            c["age_hours"] = min(float(c.get("age_hours") or 0), 1.0)
            c["decided_at"] = FRESH_AT.isoformat()
        trader.run_entries(w.c, w.cfg, w.uni, cands, {"regime": "risk_on", "risk_mult": 1.0}, FRESH_AT, quote_fn, md_fetch=offline)
        out = snap(w, "select * from decisions where created>=?", (FRESH_AT.isoformat().replace("+00:00", ""),))
        out["candidates_offered"] = len(cands)
        return out
    finally:
        w.close()


def scenario_thesis(src, tweak=None):
    """Thesis-driven ideas (no news) from the owner's real thesis table."""
    w = Work(src, tweak)
    try:
        w.c.execute("DELETE FROM decisions WHERE origin='thesis'")
        w.c.execute("DELETE FROM decisions WHERE status IN ('AWAITING_MARKET','HELD','AWAITING_QUOTE','AWAITING_CAPACITY')")
        w.c.commit()
        reg = {"regime": "risk_on", "risk_mult": 1.0}
        cands = trader.thesis_candidates(w.c, w.cfg, reg, w.uni, OPEN_AT)

        def quote_fn(conn, t, purpose="entry"):
            p = w.px.get(t)
            if not p:
                r = conn.execute("select close from market_series where symbol=? order by day desc limit 1", (t,)).fetchone()
                p = float(r[0]) if r else None
            return quote(p, OPEN_AT)
        trader.run_entries(w.c, w.cfg, w.uni, cands, reg, OPEN_AT, quote_fn, md_fetch=offline)
        out = snap(w, "select * from decisions where origin='thesis'")
        out["candidates"] = [c["ticker"] for c in cands]
        return out
    finally:
        w.close()


# ------------------------------------------------------------------------------------------------------ surface and pages
def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def scenario_surface(src, tweak=None):
    cfg = config.load_config("/nonexistent", with_overrides=False)
    con = db.open_db(":memory:")
    schema = {t[0]: [[r[1], r[2], r[3], r[5]] for r in con.execute('PRAGMA table_info("%s")' % t[0])] for t in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")}
    indexes = sorted(r[0] for r in con.execute("select name from sqlite_master where type='index' and name not like 'sqlite_%'"))
    fields = sorted([f.path, f.kind, f.group, repr(S.get_path(cfg, f.path))] for f in S.FIELDS)
    return {"config_defaults": digest(cfg), "settings_fields": fields, "tables": schema, "table_count": len(schema), "indexes": indexes}


def scenario_pages(src, tweak=None):
    from paper_agent import dashboard
    fixed = lambda: PAGES_AT  # noqa: E731
    saved = {}
    for name, mod in list(sys.modules.items()):
        if name.startswith("paper_agent") and hasattr(mod, "utcnow"):
            saved[name] = mod.utcnow
            mod.utcnow = fixed
    w = Work(src)
    try:
        out = {}
        for tab in dashboard.TABS:
            try:
                html = dashboard.render(w.c, w.cfg, tab, {}, {"authed": True})
            except Exception as e:  # noqa: BLE001
                out[tab] = {"error": type(e).__name__}
                continue
            body = re.sub(r"<style.*?</style>", "", html, flags=re.S)
            out[tab] = {"renders": True, "bytes": len(html)} if tab == "system" else {"sha": hashlib.sha256(body.encode()).hexdigest()[:16], "bytes": len(body)}   # the System tab shows live disk / memory: only "it renders" is stable
        return out
    finally:
        w.close()
        for name, f in saved.items():
            sys.modules[name].utcnow = f


SCENARIOS = {
    "open_defaults": lambda src: scenario_open(src, None),
    "open_owner": lambda src: scenario_open(src, owner_settings),
    "lifecycle_defaults": lambda src: scenario_lifecycle(src, None),
    "lifecycle_owner": lambda src: scenario_lifecycle(src, owner_settings),
    "fresh_arrivals": lambda src: scenario_fresh(src),
    "thesis_open": lambda src: scenario_thesis(src),
    "surface": lambda src: scenario_surface(src),
    "pages": lambda src: scenario_pages(src),
}


# ------------------------------------------------------------------------------------------------------ record / check
def diff(a, b, path="", out=None, limit=12):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append("%s.%s: only in the new run" % (path, k))
            elif k not in b:
                out.append("%s.%s: missing from the new run" % (path, k))
            else:
                diff(a[k], b[k], "%s.%s" % (path, k), out, limit)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append("%s: %d items in the baseline, %d now" % (path, len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                diff(x, y, "%s[%d]" % (path, i), out, limit)
    elif a != b:
        out.append("%s: baseline %r -> now %r" % (path, a, b))
    return out


def run(names, src):
    res = {}
    for n in names:
        res[n] = SCENARIOS[n](src)
        sys.stdout.write("  ran %-20s (%s)\n" % (n, KIND[n]))
        sys.stdout.flush()
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["record", "check"])
    ap.add_argument("--db", required=True, help="a copy of the owner's real database (never modified)")
    ap.add_argument("--out", help="record: where to write the baseline")
    ap.add_argument("--baseline", help="check: the baseline file")
    ap.add_argument("--only", help="comma-separated scenario names (default: all)")
    ap.add_argument("--allow", default="", help="check: kinds whose differences are expected in this step (surface,pages)")
    a = ap.parse_args(argv)
    names = a.only.split(",") if a.only else list(SCENARIOS)
    bad = [n for n in names if n not in SCENARIOS]
    if bad:
        ap.error("unknown scenario(s): %s (choose from %s)" % (", ".join(bad), ", ".join(SCENARIOS)))
    build = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "BUILD")).read().strip()
    if a.mode == "record":
        path = a.out or "golden-baseline.json"
        base = json.load(open(path)) if os.path.exists(path) else {"scenarios": {}}
        base["build"] = build
        base["scenarios"].update(run(names, a.db))
        base["digests"] = {n: digest(v) for n, v in base["scenarios"].items()}
        json.dump(base, open(path, "w"), indent=1, sort_keys=True)
        print("recorded %d scenario(s) from build %s into %s" % (len(names), build, path))
        return 0
    base = json.load(open(a.baseline))
    allow = {x for x in a.allow.split(",") if x}
    print("checking build %s against a baseline recorded from build %s" % (build, base.get("build")))
    now = run(names, a.db)
    failed = 0
    for n in names:
        d = diff(base["scenarios"].get(n), now[n])
        ok = not d
        expected = (not ok) and KIND[n] in allow
        print("%-20s %-10s %s" % (n, KIND[n], "same" if ok else ("DIFFERENT (expected: --allow %s)" % KIND[n] if expected else "DIFFERENT")))
        for line in d[:8]:
            print("      " + line[:200])
        failed += 0 if (ok or expected) else 1
    print("\nRESULT: %s" % ("every scenario matches the baseline" if not failed else "%d scenario(s) CHANGED: do not accept this step until each difference is explained" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
