"""Nobody is awake at the open: the system writes its own report (and can push it to a phone). A report must ALWAYS be written, and its verdict must be honest."""
import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import fake_internet as FI  # noqa: E402
import test_fake_internet as TF  # noqa: E402
import test_preflight as TP  # noqa: E402
from paper_agent import config, dashboard, notify, pipeline, report, settings as S  # noqa: E402
from paper_agent.timeutil import iso, last_market_open  # noqa: E402

UTC = dt.timezone.utc
T = dt.datetime(2026, 10, 27, 14, 5, tzinfo=UTC)          # 10:05 New York, 35 minutes after the open


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._old = config.SETTINGS_PATH
        config.SETTINGS_PATH = os.path.join(self.d, "settings.json")          # reports live beside the other state files

    def tearDown(self):
        config.SETTINGS_PATH = self._old

    def cfg(self):
        c = TP.cfg_real()
        c["runtime"]["bind_port"] = 1
        return c

    def build(self, c, now=T, live=False, **kw):
        return report.build(c, self.cfg(), TP.big_uni(), [], now, live=live, check_units=False, **kw)

    def add_decision(self, c, status, n=1, why="x", ticker="NVDA"):
        for i in range(n):
            c.execute("INSERT INTO decisions(created,ticker,action,status,reason) VALUES(?,?,?,?,?)", (iso(T - dt.timedelta(minutes=30)), ticker, "BUY", status, json.dumps({"gates": [why]})))


class TestVerdicts(Base):
    def test_label_follows_the_market_clock(self):
        lo = last_market_open(T)
        for delta, want in ((-60, "preopen"), (10, "open"), (120, "midday"), (400, "close")):
            self.assertEqual(report.label_for(lo + dt.timedelta(minutes=delta)), want, delta)

    def test_a_clean_session_with_trades_is_ok_and_lists_what_was_bought(self):
        tf = TF.TestWholeSystemAgainstTheSimulatedInternet("test_a_clean_morning_runs_without_a_single_stage_error_and_trades")
        tf.setUp()
        try:
            c, errors, net = tf.morning(seed=1, cycles=14)
            now = TF.T0 + dt.timedelta(minutes=5 * 14)
            with net:
                net.now = now
                rep = report.build(c, self.cfg(), TP.big_uni(), [], now, live=True, check_units=False)
        finally:
            tf.tearDown()
        self.assertEqual(rep["verdict"], "OK", rep["text"][:1500])
        self.assertIn("OPENED THIS SESSION", rep["text"])
        self.assertIn("Nothing needs your attention", rep["text"])
        self.assertRegex(rep["summary"], r"\d+ position\(s\) opened")

    def test_buys_waiting_but_nothing_executed_long_after_the_open_is_a_failure(self):
        c = H.fresh_db()
        c.execute("INSERT INTO cycle_runs(started,finished,status,mode) VALUES(?,?,?,?)", (iso(T - dt.timedelta(minutes=3)), iso(T), "OK", "full"))
        self.add_decision(c, "AWAITING_MARKET", 8, "market session AFTER_HOURS; will retry")
        rep = self.build(c)
        self.assertEqual(rep["verdict"], "FAILED")
        self.assertIn("NOTHING has executed", rep["text"])

    def test_buys_stuck_on_prices_are_called_out_with_the_reason(self):
        c = H.fresh_db()
        c.execute("INSERT INTO cycle_runs(started,finished,status,mode) VALUES(?,?,?,?)", (iso(T - dt.timedelta(minutes=3)), iso(T), "OK", "full"))
        c.execute("INSERT INTO positions(ticker,opened,entry_price,shares,cost,status) VALUES('AAPL',?,100,10,1000,'OPEN')", (iso(T - dt.timedelta(minutes=10)),))
        self.add_decision(c, "AWAITING_QUOTE", 3, "market is open but no verified price: TWELVE_DATA stale (900s old, limit 90s)")
        rep = self.build(c)
        self.assertIn(rep["verdict"], ("ATTENTION", "FAILED"))
        self.assertIn("waiting for a verified price", rep["text"])
        self.assertIn("TWELVE_DATA stale", rep["text"])

    def test_stage_errors_during_the_session_fail_the_report_with_the_location(self):
        c = H.fresh_db()
        for i in range(3):
            c.execute("INSERT INTO cycle_runs(started,finished,status,mode,report_json) VALUES(?,?,?,?,?)", (iso(T - dt.timedelta(minutes=5 * (i + 1))), iso(T), "ERROR", "full",
                      json.dumps({"stages": {"entries": {"error": "OperationalError: no such column: x", "where": "quotes.py:129 _budget_ok"}}})))
        rep = self.build(c)
        self.assertEqual(rep["verdict"], "FAILED")
        mine = [x for x in rep["failed"] if "stage errors since this build started" in x]            # the report's OWN reason, not just the checklist's
        self.assertTrue(mine and "quotes.py:129 _budget_ok" in mine[0], rep["failed"])

    def test_errors_from_before_this_build_do_not_fail_the_report(self):
        c = H.fresh_db()
        c.execute("INSERT INTO cycle_runs(started,finished,status,mode,report_json) VALUES(?,?,?,?,?)", (iso(T - dt.timedelta(minutes=50)), iso(T), "ERROR", "full",
                  json.dumps({"stages": {"entries": {"error": "OperationalError: old"}}})))
        c.execute("INSERT OR REPLACE INTO meta VALUES('build_since',?)", (iso(T - dt.timedelta(minutes=20)),))
        c.execute("INSERT INTO cycle_runs(started,finished,status,mode) VALUES(?,?,?,?)", (iso(T - dt.timedelta(minutes=3)), iso(T), "OK", "full"))
        self.assertNotIn("stage errors", " ".join(self.build(c)["failed"]))

    def test_a_database_with_missing_columns_fails_the_report(self):
        c = H.fresh_db()
        c.execute("DROP TABLE provider_health")
        c.execute("CREATE TABLE provider_health(provider TEXT PRIMARY KEY, requests INTEGER)")
        rep = self.build(c)
        self.assertEqual(rep["verdict"], "FAILED")
        self.assertIn("schema", rep["text"].lower())

    def test_benign_warnings_alone_do_not_raise_attention(self):
        c = H.fresh_db()
        c.execute("INSERT INTO cycle_runs(started,finished,status,mode) VALUES(?,?,?,?)", (iso(T - dt.timedelta(minutes=3)), iso(T), "OK", "full"))
        c.execute("INSERT INTO events(created,event_type,scope,direction,magnitude,gate) VALUES(?,?,?,?,?,?)", (iso(T - dt.timedelta(minutes=30)), "earnings_beat", "company", .8, .5, "candidate"))
        os.environ["TWELVE_DATA_API_KEY"] = os.environ["FINNHUB_API_KEY"] = os.environ["ALPACA_API_KEY"] = "x" * 20
        rep = self.build(c)
        self.assertNotIn("Contact email", " ".join(rep["problems"]))


class TestFiles(Base):
    def rep(self, label="open", verdict="OK"):
        return {"verdict": verdict, "label": label, "text": "VERDICT: %s | x\nbody" % verdict, "headline": "VERDICT: %s | x" % verdict, "summary": "s", "failed": [], "problems": []}

    def test_written_with_a_latest_pointer_and_old_ones_are_pruned(self):
        for i in range(report.KEEP + 12):
            report.write(self.rep(), T + dt.timedelta(minutes=i))
        d = report.reports_dir()
        files = [f for f in os.listdir(d) if f != "latest.txt"]
        self.assertEqual(len(files), report.KEEP)
        self.assertTrue(os.path.exists(os.path.join(d, "latest.txt")))
        self.assertEqual(report.list_reports(5)[0]["file"], sorted(files)[-1])

    def test_path_traversal_is_refused(self):
        report.write(self.rep(), T)
        for bad in ("../settings.json", "/etc/passwd", "a/b.txt", "..", "x.sh"):
            self.assertIsNone(report.read(bad), bad)
        self.assertIn("VERDICT", report.read("latest.txt"))

    def test_the_dashboard_tab_shows_the_latest_report_and_the_history(self):
        report.write(self.rep(verdict="FAILED"), T)
        report.write(self.rep(verdict="OK"), T + dt.timedelta(hours=1))
        html = dashboard.render(H.fresh_db(), H.cfg(), "reports", {})
        self.assertIn("VERDICT: OK", html)
        self.assertIn("Recent reports", html)
        old = sorted(f for f in os.listdir(report.reports_dir()) if f != "latest.txt")[0]
        self.assertIn("VERDICT: FAILED", dashboard.render(H.fresh_db(), H.cfg(), "reports", {"file": [old]}))
        config.SETTINGS_PATH = os.path.join(tempfile.mkdtemp(), "empty", "settings.json")                # a state dir with no reports yet
        self.assertIn("No report yet", dashboard.render(H.fresh_db(), H.cfg(), "reports", {}))


class TestPush(Base):
    def test_push_goes_to_the_topic_with_the_right_priority(self):
        calls = []
        cfg = {"notifications": {"ntfy_topic": "my-secret-topic-123"}}
        for verdict, prio in (("FAILED", "urgent"), ("ATTENTION", "high"), ("OK", "default")):
            calls.clear()
            ok = report.push(cfg, {"verdict": verdict, "label": "open", "summary": "2 opened", "failed": ["boom"], "problems": []}, lambda url, **kw: calls.append((url, kw)))
            self.assertTrue(ok)
            self.assertEqual(calls[0][0], "https://ntfy.sh/my-secret-topic-123")
            self.assertEqual(calls[0][1]["headers"]["Priority"], prio)
            self.assertIn(b"2 opened", calls[0][1]["data"])

    def test_no_topic_means_no_network_call_and_failures_never_raise(self):
        calls = []
        self.assertFalse(notify.push({"notifications": {"ntfy_topic": ""}}, "t", "b", post=lambda *a, **k: calls.append(1)))
        self.assertEqual(calls, [])

        def boom(*a, **k):
            raise OSError("no network")
        self.assertFalse(notify.push({"notifications": {"ntfy_topic": "abcdefgh12"}}, "t", "b", post=boom))

    def test_topic_setting_is_validated(self):
        base = config.load_config("/nonexistent", with_overrides=False)
        for v, ok in (("my-secret-topic-123", True), ("", True), ("short", False), ("has space in it", False), ("bad/slash123", False), ("x" * 70, False)):
            _, err, _ = S.apply_form({"f.notifications.ntfy_topic": v}, base, base, {})
            self.assertEqual(not err, ok, v)


class TestCommandAndSchedule(Base):
    def run_cli(self, build_raises=False):
        from paper_agent import __main__ as cli
        c = H.fresh_db()
        c.commit()
        path = c.execute("PRAGMA database_list").fetchone()[2]
        old = (cli.DB_PATH, cli.pipeline.load_universe, cli.pipeline.load_sources)
        cli.DB_PATH, cli.pipeline.load_universe, cli.pipeline.load_sources = path, (lambda: TP.big_uni()), (lambda cfg=None: [])
        patcher = mock.patch.object(report, "build", side_effect=RuntimeError("kaboom")) if build_raises else contextlib.nullcontext()
        try:
            buf = io.StringIO()
            with patcher, contextlib.redirect_stdout(buf):
                rc = cli.main(["report", "--no-push", "--offline"])
        finally:
            cli.DB_PATH, cli.pipeline.load_universe, cli.pipeline.load_sources = old
        return rc, buf.getvalue()

    def test_the_command_writes_a_report(self):
        rc, out = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertIn("VERDICT:", out)
        self.assertIn("[report saved:", out)
        self.assertTrue(report.read("latest.txt"))

    def test_a_report_is_written_even_when_building_it_crashes(self):
        rc, out = self.run_cli(build_raises=True)
        self.assertEqual(rc, 0)
        text = report.read("latest.txt")
        self.assertIn("VERDICT: FAILED", text)
        self.assertIn("RuntimeError: kaboom", text)                                   # the crash itself is the report: silence would look like success

    def test_the_timer_runs_four_times_a_session_in_new_york_time(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "systemd", "paper-agent-report.timer")) as f:
            text = f.read()
        cal = [l for l in text.splitlines() if l.startswith("OnCalendar=")]
        self.assertEqual(len(cal), 4)
        self.assertTrue(all(l.endswith("America/New_York") and "Mon..Fri" in l for l in cal))
        self.assertNotRegex(text, r"(?m)^\s*OnBootSec\s*=")
        with open(os.path.join(root, "scripts", "install.sh")) as f:
            inst = f.read()
        self.assertIn("report.timer", inst)


if __name__ == "__main__":
    unittest.main()
