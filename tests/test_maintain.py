"""Database clean-up. Built from what was found in the owner's REAL database: 44 tables from the retired v4 app, 4,962 v4 decisions (95% of the table), and news/events that only ever grow.
Everything destructive is archived and verified first; nothing a live row points at is deleted; trading records are never touched."""
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import backup as backup_mod, config, dashboard, db, doctor, maintain, settings as S  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 29, 9, 30, tzinfo=UTC)         # 05:30 New York: pre-market, maintenance is allowed
OPEN = dt.datetime(2026, 10, 29, 15, 0, tzinfo=UTC)        # 11:00 New York: the regular session
OLD, RECENT = iso(NOW - dt.timedelta(days=60)), iso(NOW - dt.timedelta(days=2))
LEGACY = {"shadow_plans": 600, "decision_lineage": 400, "reset_plans_archive": 20, "v34_meta": 1}


def build():
    """A database shaped like the owner's."""
    p = os.path.join(tempfile.mkdtemp(), "paper-agent.db")
    c = db.open_db(p)
    for t, n in LEGACY.items():
        c.execute('CREATE TABLE "%s"(id INTEGER PRIMARY KEY,created TEXT,blob TEXT)' % t)
        for _ in range(n):
            c.execute('INSERT INTO "%s"(created,blob) VALUES(?,?)' % t, (OLD, "x" * 300))
    for i in range(40):                                                     # v4 decisions: none of the columns the current app fills in
        c.execute("INSERT INTO decisions(created,ticker,action,status,reason) VALUES(?,?,?,?,?)", (OLD, "T%d" % i, "BUY", ("REJECTED", "EXECUTION_ERROR", "EXECUTED", "BASELINE")[i % 4], "No verified company mapped; " + "y" * 200))
    ev = {}
    for kind, n, ts in (("old_free", 60, OLD), ("old_decision", 4, OLD), ("old_queue", 3, OLD), ("old_evidence", 3, OLD), ("old_forecast", 3, OLD), ("recent", 12, RECENT)):
        ev[kind] = []
        for i in range(n):
            uid = "%s-%d" % (kind, i)
            c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES(?,?,?,?,?,?,?,0.8)", (uid, "s", "title " + uid, "summary " * 20, "u", ts, ts))
            c.execute("INSERT INTO events(news_uid,created,published,source,headline,event_type,scope,direction,magnitude,tickers_json) VALUES(?,?,?,?,?,?,?,?,?,?)", (uid, ts, ts, "s", "h " + uid, "general_news", "company", 0.1, 0.1, "[]"))
            ev[kind].append(c.execute("SELECT MAX(id) FROM events").fetchone()[0])
    c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES('orphan-news','s','t','s','u',?,?,0.8)", (OLD, OLD))              # old news no event ever used
    c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES('decision-news','s','t','s','u',?,?,0.8)", (OLD, OLD))             # old news ONLY a current decision points at
    for i, eid in enumerate(ev["old_decision"]):                              # CURRENT decisions that rest on old events
        c.execute("INSERT INTO decisions(created,ticker,action,status,reason,event_id,origin) VALUES(?,?,?,?,?,?,?)", (RECENT, "NVDA", "BUY", "AWAITING_MARKET", "{}", eid, "event"))
    c.execute("UPDATE decisions SET news_uid='decision-news' WHERE id=(SELECT MAX(id) FROM decisions)")
    c.execute("INSERT INTO decisions(created,ticker,action,status,reason,origin,thesis_key) VALUES(?,?,?,?,?,?,?)", (RECENT, "MSFT", "BUY", "AWAITING_MARKET", "{}", "thesis", "ai_infrastructure"))      # current, thesis-driven: no event_id
    for eid in ev["old_queue"]:
        c.execute("INSERT INTO candidate_queue(event_id,created,ticker,payload_json) VALUES(?,?,?,?)", (eid, OLD, "NVDA", "{}"))
    for eid in ev["old_evidence"]:
        c.execute("INSERT INTO evidence(event_id,created,credibility,strength,novelty,predictive_power,direction,event_type,decay_days,expiry_date) VALUES(?,?,?,?,?,?,?,?,?,?)", (eid, OLD, .8, .5, .5, .5, .5, "x", 5, RECENT))
    for eid in ev["old_forecast"]:
        c.execute("INSERT INTO forecasts(created,ticker,direction,horizon_days,verdict,scenarios_json,inputs_json,hash,event_id) VALUES(?,?,?,?,?,?,?,?,?)", (OLD, "NVDA", "UP", 3, "BUY", "{}", "{}", "h%d" % eid, eid))
    for _ in range(5):                                                        # narrative states: 5 old rows of thesis A (all old), 1 recent row of thesis B
        c.execute("INSERT INTO narrative_states(thesis_key,ts) VALUES('A',?)", (OLD,))
    c.execute("INSERT INTO narrative_states(thesis_key,ts) VALUES('B',?)", (RECENT,))
    for ts in (OLD, RECENT):
        c.execute("INSERT INTO executor_runs(created,status,positions_checked,actions_committed,holds_extended,blocked_stale,errors,detail_json) VALUES(?,'OK',0,0,0,0,0,'{}')", (ts,))
        c.execute("INSERT INTO position_monitor_runs(created,status,open_positions,events_reviewed,plans_changed,detail_json) VALUES(?,'OK',0,0,0,'{}')", (ts,))
    ok = json.dumps({"errors": [], "stages": {"evidence": {"ok": True}}, "pad": "z" * 900, "regime": "risk_on"})
    bad = json.dumps({"errors": ["entries"], "stages": {"entries": {"error": "OperationalError: boom", "trace": "t" * 900}}})
    for status, ts, rep in (("OK", OLD, ok), ("ERROR", OLD, bad), ("OK", RECENT, ok)):
        c.execute("INSERT INTO cycle_runs(started,finished,status,mode,report_json) VALUES(?,?,?,?,?)", (ts, ts, status, "full", rep))
    for i in range(30):
        c.execute("INSERT INTO portfolio_snapshots(ts,equity) VALUES(?,?)", (OLD, 1e6 + i))             # the drawdown peak lives here: must never be trimmed
    c.execute("INSERT INTO positions(ticker,opened,entry_price,shares,cost,status) VALUES('AAPL',?,100,10,1000,'OPEN')", (RECENT,))
    c.commit()
    return c, p, ev


def digest(c, t):
    h = hashlib.sha256()
    for r in c.execute('SELECT * FROM "%s" ORDER BY 1' % t):
        h.update(repr(tuple(r)).encode())
    return h.hexdigest()


def n(c, t, where="1=1"):
    return c.execute('SELECT COUNT(*) FROM "%s" WHERE %s' % (t, where)).fetchone()[0]


def all_tables(c):
    return [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


class Base(unittest.TestCase):
    def setUp(self):
        self.c, self.path, self.ev = build()
        self.backups, self.archive = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.cfg = H.cfg()

    def go(self, **kw):
        return maintain.apply(self.c, self.cfg, self.path, kw.pop("now", NOW), backup_dir=self.backups, archive_dir=self.archive, **kw)


class TestPlan(Base):
    def test_the_plan_is_accurate_and_changes_nothing(self):
        before = {t: digest(self.c, t) for t in all_tables(self.c)}
        pl = maintain.plan(self.c, self.cfg, NOW, self.path)
        self.assertEqual({t["name"]: t["rows"] for t in pl["legacy_tables"]}, LEGACY)
        self.assertEqual(pl["legacy_decisions"]["rows"], 40)
        self.assertEqual(pl["retention"]["events"], 60)                              # 60 free; the 13 that a decision / queue row / evidence / forecast points at are protected
        self.assertEqual(pl["retention"]["news"], 60 + 1)                            # the news of those 60 events + 'orphan-news'; 'decision-news' is protected by a current decision
        self.assertEqual(pl["retention"]["narrative_states"], 4)                     # 5 old rows of thesis A: the newest is kept
        self.assertEqual((pl["retention"]["executor_runs"], pl["retention"]["position_monitor_runs"]), (1, 1))
        self.assertEqual(pl["trim_reports"], 1)                                      # only the OLD *successful* cycle
        self.assertFalse(pl["nothing_to_do"])
        self.assertEqual(before, {t: digest(self.c, t) for t in all_tables(self.c)})
        self.assertIn("nothing has been changed", maintain.format_plan(pl))

    def test_what_the_plan_promised_is_exactly_what_apply_does(self):
        pl = maintain.plan(self.c, self.cfg, NOW, self.path)
        rep = self.go()
        self.assertEqual({k: rep["deleted"][k] for k in ("events", "news", "narrative_states", "executor_runs", "position_monitor_runs")},
                         {k: pl["retention"][k] for k in ("events", "news", "narrative_states", "executor_runs", "position_monitor_runs")})
        self.assertEqual((rep["deleted"]["legacy_decisions"], rep["trimmed_reports"], sorted(rep["dropped_tables"])), (40, 1, sorted(LEGACY)))


class TestArchiveThenDrop(Base):
    def test_legacy_data_is_archived_verified_then_removed(self):
        rep = self.go()
        a = sqlite3.connect(rep["archive"])
        for t, rows in LEGACY.items():
            self.assertEqual(n(a, t), rows, t)                                       # every legacy row is in the archive...
            self.assertNotIn(t, all_tables(self.c))                                  # ...and gone from the live database
        self.assertEqual(n(a, "legacy_decisions"), 40)
        self.assertEqual(sorted(r[0] for r in a.execute("SELECT DISTINCT status FROM legacy_decisions")), ["BASELINE", "EXECUTED", "EXECUTION_ERROR", "REJECTED"])
        self.assertEqual({r[0]: r[3] for r in a.execute("SELECT * FROM _schema")}, dict(LEGACY, legacy_decisions=40))
        self.assertEqual(n(self.c, "decisions", "origin IS NULL AND event_id IS NULL"), 0)
        self.assertEqual(n(self.c, "decisions"), 5)                                  # the current app's decisions (incl. the thesis-driven one with no event) are untouched

    def test_a_verified_backup_comes_first_and_can_restore_everything(self):
        rep = self.go()
        b = sqlite3.connect(rep["backup"])
        self.assertEqual(b.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual((n(b, "shadow_plans"), n(b, "decisions"), n(b, "events")), (600, 45, 85))               # exactly the state before the clean-up
        shutil_copy = os.path.join(tempfile.mkdtemp(), "restored.db")
        with open(rep["backup"], "rb") as f, open(shutil_copy, "wb") as g:
            g.write(f.read())
        self.assertEqual(n(db.connect(shutil_copy), "decisions"), 45)               # restoring = copying the file back

    def test_a_failed_verification_drops_nothing_and_leaves_no_archive_behind(self):
        real = sqlite3.connect

        def blind(path, *a, **k):                                                    # the archive "on disk" cannot be read back
            if isinstance(path, str) and "legacy-v4" in path and "mode=ro" in path:
                return real(":memory:")
            return real(path, *a, **k)
        with mock.patch.object(maintain.sqlite3, "connect", blind), self.assertRaises(Exception):
            self.go()
        self.assertEqual(sorted(t for t in LEGACY if t in all_tables(self.c)), sorted(LEGACY))
        self.assertEqual(n(self.c, "decisions"), 45)
        self.assertEqual([f for f in os.listdir(self.archive) if f.endswith(".db")], [])

    def test_a_table_the_current_code_still_mentions_is_never_dropped(self):
        with mock.patch.object(maintain, "_code_sources", return_value={"x.py": 'conn.execute("SELECT * FROM v34_meta")'}):
            pl = maintain.plan(self.c, self.cfg, NOW, self.path)
            self.assertEqual([k["name"] for k in pl["kept_tables"]], ["v34_meta"])
            self.assertNotIn("v34_meta", [t["name"] for t in pl["legacy_tables"]])
            rep = self.go()
        self.assertIn("v34_meta", all_tables(self.c))
        self.assertNotIn("v34_meta", rep["dropped_tables"])

    def test_it_is_idempotent(self):
        self.go()
        self.assertTrue(maintain.plan(self.c, self.cfg, NOW, self.path)["nothing_to_do"])
        rep = self.go()
        self.assertIsNone(rep["backup"])                                             # nothing to do: no needless backup, no changes


class TestRetentionNeverDeletesWhatIsInUse(Base):
    def test_events_and_news_that_anything_live_points_at_survive(self):
        self.go()
        alive = {r[0] for r in self.c.execute("SELECT id FROM events")}
        for kind in ("old_decision", "old_queue", "old_evidence", "old_forecast", "recent"):
            self.assertTrue(set(self.ev[kind]) <= alive, kind)
        self.assertFalse(set(self.ev["old_free"]) & alive)
        uids = {r[0] for r in self.c.execute("SELECT uid FROM news")}
        self.assertIn("decision-news", uids)                                         # pointed at only by a current decision
        self.assertTrue(all("old_decision-%d" % i in uids for i in range(4)))        # the news of a protected event is protected too
        self.assertNotIn("orphan-news", uids)
        self.assertFalse([u for u in uids if u.startswith("old_free")])

    def test_trading_records_are_never_touched(self):
        guarded = ("positions", "ledger", "portfolio_snapshots", "forecasts", "evidence", "candidate_queue", "theses", "companies", "market_series", "position_forecast_plans", "position_lifecycle")
        before = {t: digest(self.c, t) for t in guarded}
        self.go()
        self.assertEqual(before, {t: digest(self.c, t) for t in guarded})
        self.assertEqual(n(self.c, "portfolio_snapshots"), 30)                       # the drawdown peak is read from here

    def test_retention_follows_the_owners_settings(self):
        rep = self.go(news_days=90, log_days=90)                                    # everything is 60 days old: nothing qualifies at 90
        self.assertEqual((rep["deleted"]["events"], rep["deleted"]["news"], rep["deleted"]["narrative_states"], rep["trimmed_reports"]), (0, 0, 0, 1))
        rep = self.go(news_days=7, log_days=7)
        self.assertGreater(rep["deleted"]["events"] + rep["deleted"]["narrative_states"], 0)

    def test_the_newest_state_of_every_thesis_survives(self):
        self.go()
        self.assertEqual({r[0]: r[1] for r in self.c.execute("SELECT thesis_key,COUNT(*) FROM narrative_states GROUP BY thesis_key")}, {"A": 1, "B": 1})

    def test_old_successful_cycle_reports_shrink_but_errors_are_kept_in_full(self):
        self.go()
        rows = {(r["status"], r["started"]): json.loads(r["report_json"]) for r in self.c.execute("SELECT * FROM cycle_runs")}
        self.assertTrue(rows[("OK", OLD)].get("trimmed"))
        self.assertEqual(rows[("OK", OLD)]["regime"], "risk_on")
        self.assertNotIn("pad", rows[("OK", OLD)])
        self.assertEqual(rows[("ERROR", OLD)]["stages"]["entries"]["trace"], "t" * 900)           # a failure is evidence: never shrunk
        self.assertIn("pad", rows[("OK", RECENT)])

    def test_retention_only_leaves_the_legacy_data_alone(self):
        rep = self.go(do_archive=False)
        self.assertEqual(sorted(t for t in LEGACY if t in all_tables(self.c)), sorted(LEGACY))
        self.assertEqual(n(self.c, "decisions"), 45)
        self.assertIsNone(rep["archive"])
        self.assertGreater(rep["deleted"]["events"], 0)


class TestSafety(Base):
    def test_it_refuses_while_the_us_market_is_open(self):
        with self.assertRaises(maintain.MaintenanceError) as cm:
            self.go(now=OPEN)
        self.assertIn("US market is open", str(cm.exception))
        self.assertEqual(n(self.c, "decisions"), 45)                                 # and changed nothing
        self.assertEqual(self.go(now=OPEN, force=True)["integrity"], "ok")

    def test_the_database_is_healthy_smaller_and_consistent_afterwards(self):
        size0 = os.path.getsize(self.path)
        rep = self.go()
        self.assertEqual(rep["integrity"], "ok")
        self.assertEqual(rep["vacuum"], "done")
        self.assertLess(os.path.getsize(self.path), size0 * 0.8)                     # the space really came back
        self.assertEqual(db.schema_drift(self.c), {})
        again = db.open_db(self.path)                                                # every process start runs migrations: they must not bring the legacy tables back
        self.assertEqual([t for t in LEGACY if t in all_tables(again)], [])
        self.assertEqual(self.c.execute("SELECT value FROM meta WHERE key='last_maintenance'").fetchone()[0], iso(NOW))

    def test_the_two_tables_nothing_uses_are_no_longer_part_of_the_schema_and_two_indexes_are(self):
        schema = db.expected_schema()
        self.assertNotIn("event_impact_forecasts", schema)
        self.assertNotIn("position_event_links", schema)
        idx = {r[0] for r in self.c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"ix_forecasts_decision", "ix_pcalls_ts"} <= idx)


class TestSurfaces(Base):
    def test_the_system_tab_and_the_doctor_say_whats_left_over(self):
        html = dashboard.render(self.c, self.cfg, "system", {})
        self.assertIn("Left over from the old v4 app", html)
        self.assertIn("4 tables and 40 old decisions", html)
        self.assertIn("db-maintain plan", html)
        lines = []
        cfg = H.cfg()
        cfg["runtime"]["bind_port"] = 1
        doctor.run(self.c, cfg, "/nonexistent", srcs=[], live=False, check_units=False, now=NOW, out=lines.append)
        self.assertRegex("\n".join(lines), r"\[database\] .*4 legacy table\(s\), 40 old v4 decision\(s\) -> run: python3 -m paper_agent db-maintain plan")
        self.go()
        html = dashboard.render(self.c, self.cfg, "system", {})
        self.assertIn("(nothing to clean)", html)
        self.assertNotIn("db-maintain plan", html)

    def test_the_settings_exist_with_sane_defaults_and_validation(self):
        base = config.load_config("/nonexistent", with_overrides=False)
        self.assertEqual(base["maintenance"], {"weekly_cleanup": True, "news_days": 30, "log_days": 30})
        self.assertTrue({"maintenance.weekly_cleanup", "maintenance.news_days", "maintenance.log_days"} <= {f.path for f in S.FIELDS})
        for bad in ("3", "9999"):
            self.assertTrue(S.apply_form({"f.maintenance.news_days": bad}, base, base, {})[1], bad)

    def test_the_weekly_timer_runs_on_saturday_new_york_time_and_only_expires_old_data(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        def read(*parts):
            with open(os.path.join(root, *parts)) as f:
                return f.read()
        t = read("systemd", "paper-agent-maintain.timer")
        self.assertIn("OnCalendar=Sat 10:00:00 America/New_York", t)
        self.assertNotIn("OnBootSec", t)
        self.assertIn("db-maintain apply --retention-only --quiet", read("systemd", "paper-agent-maintain.service"))   # the weekly job NEVER archives/drops: that is a deliberate, one-off, human decision
        self.assertIn("maintain", read("scripts", "install.sh"))


class TestCommand(Base):
    def run_cli(self, *argv, at=NOW, cfg_override=None):
        from paper_agent import __main__ as cli
        self.c.commit()
        out, real, saved = io.StringIO(), maintain.apply, cli.DB_PATH
        cli.DB_PATH = self.path
        patches = [mock.patch.object(cli, "utcnow", return_value=at), mock.patch.object(backup_mod, "BACKUP_DIR", self.backups, create=True),
                   mock.patch.object(maintain, "apply", side_effect=lambda *a, **k: real(*a, **dict(k, archive_dir=self.archive)))]        # keep the archive inside the test's temp dir
        if cfg_override is not None:
            patches.append(mock.patch.object(cli.config, "load_config", return_value=cfg_override))
        try:
            with contextlib.ExitStack() as st, contextlib.redirect_stdout(out):
                for pt in patches:
                    st.enter_context(pt)
                rc = cli.main(list(argv))
        finally:
            cli.DB_PATH = saved
        return rc, out.getvalue()

    def test_plan_is_the_default_and_apply_does_it(self):
        rc, out = self.run_cli("db-maintain")
        self.assertEqual(rc, 0)
        self.assertIn("DATABASE CLEAN-UP PLAN", out)
        self.assertIn("db-maintain apply", out)
        self.assertEqual(n(sqlite3.connect(self.path), "shadow_plans"), 600)           # plan changed nothing
        rc, out = self.run_cli("db-maintain", "apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("DATABASE CLEAN-UP DONE", out)
        self.assertIn("integrity: ok", out)
        self.assertNotIn("shadow_plans", all_tables(sqlite3.connect(self.path)))

    def test_the_weekly_job_expires_old_data_but_never_archives_or_drops_legacy_data(self):
        """This is the unattended job. Dropping tables is a deliberate, one-off, human decision: the weekly flag must never do it."""
        rc, out = self.run_cli("db-maintain", "apply", "--retention-only", "--quiet")
        self.assertEqual(rc, 0, out)
        c = sqlite3.connect(self.path)
        self.assertEqual({t: n(c, t) for t in LEGACY}, LEGACY)                        # every legacy table and row is still there
        self.assertEqual(n(c, "decisions"), 45)
        self.assertLess(n(c, "events"), 85)                                           # ...while old, unreferenced events were expired
        self.assertEqual([f for f in os.listdir(self.archive) if f.endswith(".db")], [])

    def test_it_refuses_during_the_session_with_a_clear_message(self):
        rc, out = self.run_cli("db-maintain", "apply", at=OPEN)
        self.assertEqual(rc, 1)
        self.assertIn("NOT DONE", out)
        self.assertIn("US market is open", out)

    def test_the_weekly_run_does_nothing_when_switched_off(self):
        rc, out = self.run_cli("db-maintain", "apply", "--retention-only", cfg_override=dict(H.cfg(), maintenance={"weekly_cleanup": False, "news_days": 30, "log_days": 30}))
        self.assertEqual((rc, "switched off" in out), (0, True))
        self.assertEqual(n(sqlite3.connect(self.path), "events"), 85)


if __name__ == "__main__":
    unittest.main()
