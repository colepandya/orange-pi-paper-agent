"""Old failures must not raise false flags forever - but they must never be deleted, and a NEW failure must always show."""
import contextlib
import datetime as dt
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import db, doctor, funnel, pipeline, preflight  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

NOW = H.NOW
ERR = {"stages": {"entries": {"error": "OperationalError: no such column: backoff_until", "where": "quotes.py:129 _budget_ok <- trader.py:190 process_candidate"}}}


def add(c, minutes_ago, status="ERROR", rep=ERR):
    c.execute("INSERT INTO cycle_runs(started,finished,status,mode,report_json) VALUES(?,?,?,?,?)", (iso(NOW - dt.timedelta(minutes=minutes_ago)), iso(NOW), status, "full", json.dumps(rep)))


def set_meta(c, k, v):
    c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))


class TestBaseline(unittest.TestCase):
    def test_errors_from_before_the_current_build_started_are_history_not_flags(self):
        c = H.fresh_db()
        for m in (300, 280, 260, 240):
            add(c, m)                                                       # old failures
        set_meta(c, "build_since", iso(NOW - dt.timedelta(minutes=60)))     # this build started an hour ago
        add(c, 20, "OK", {})
        r = funnel.report(c, 24, NOW)
        self.assertEqual((r["error_cycles_now"], r["error_cycles_history"]), (0, 4))
        self.assertEqual(r["stage_errors"], [])
        self.assertFalse([f for f in r["findings"] if f["severity"] in ("HIGH", "MEDIUM")])
        self.assertTrue([f for f in r["findings"] if "predate the current build" in f["text"]])        # ...but it is still mentioned, not hidden
        self.assertEqual(c.execute("SELECT COUNT(*) FROM cycle_runs WHERE status='ERROR'").fetchone()[0], 4)   # and NOTHING was deleted

    def test_a_new_error_after_the_baseline_is_always_reported(self):
        c = H.fresh_db()
        add(c, 300)
        set_meta(c, "build_since", iso(NOW - dt.timedelta(minutes=60)))
        add(c, 10)                                                          # a fresh failure on the new build
        r = funnel.report(c, 24, NOW)
        self.assertEqual((r["error_cycles_now"], r["error_cycles_history"]), (1, 1))
        f = [x for x in r["findings"] if "since the current build started" in x["text"]]
        self.assertEqual(f[0]["severity"], "MEDIUM")
        self.assertIn("quotes.py:129 _budget_ok", f[0]["text"])             # with the exact location

    def test_the_operator_can_acknowledge_everything_so_far(self):
        c = H.fresh_db()
        for m in (50, 40, 30):
            add(c, m)
        set_meta(c, "errors_ack_at", iso(NOW - dt.timedelta(minutes=5)))
        self.assertEqual(funnel.report(c, 24, NOW)["error_cycles_now"], 0)
        add(c, 2)
        self.assertEqual(funnel.report(c, 24, NOW)["error_cycles_now"], 1)  # an error AFTER the acknowledgement still shows

    def test_a_baseline_in_the_future_is_ignored(self):
        """(Also keeps the suite deterministic: tests use a fake clock that is earlier than the real build_since.)"""
        c = H.fresh_db()
        add(c, 30)
        set_meta(c, "build_since", iso(NOW + dt.timedelta(days=3)))
        self.assertIsNone(db.error_baseline(c, NOW))
        self.assertEqual(funnel.report(c, 24, NOW)["error_cycles_now"], 1)

    def test_preflight_and_doctor_use_the_same_baseline(self):
        c = H.fresh_db()
        for m in (90, 80, 70, 60):
            add(c, m)
        add(c, 5, "OK", {})
        cfg = H.cfg()
        cfg["runtime"]["min_available_memory_mb"] = 120
        cfg["runtime"]["bind_port"] = 1
        before = preflight.run(c, cfg, H.universe(), now=NOW, live=False, check_units=False, mem_mb=lambda: 600, out=lambda s: None)
        self.assertEqual(next(x["status"] for x in before["checks"] if x["name"].startswith("Recent cycles")), "FAIL")
        set_meta(c, "build_since", iso(NOW - dt.timedelta(minutes=30)))
        after = preflight.run(c, cfg, H.universe(), now=NOW, live=False, check_units=False, mem_mb=lambda: 600, out=lambda s: None)
        self.assertEqual(next(x["status"] for x in after["checks"] if x["name"].startswith("Recent cycles")), "PASS")
        lines = []
        r = doctor.run(c, cfg, "/nonexistent", srcs=[], live=False, check_units=False, now=NOW, out=lines.append)
        self.assertFalse([i for i in r["issues"] if "stage errors" in i["message"]])

    def test_migrate_records_the_build_and_resets_the_baseline_only_when_the_build_changes(self):
        c = H.fresh_db()
        b1 = c.execute("SELECT value FROM meta WHERE key='build_since'").fetchone()[0]
        self.assertEqual(c.execute("SELECT value FROM meta WHERE key='build'").fetchone()[0], db.package_build())
        db.migrate(c)                                                       # same build again (every process start): baseline must NOT move
        self.assertEqual(c.execute("SELECT value FROM meta WHERE key='build_since'").fetchone()[0], b1)
        set_meta(c, "build", "old")
        db.migrate(c)                                                       # a different build: the baseline moves forward
        self.assertEqual(c.execute("SELECT value FROM meta WHERE key='build'").fetchone()[0], db.package_build())


class TestErrorLocation(unittest.TestCase):
    def test_a_stage_error_records_the_exact_file_line_and_function(self):
        c = H.fresh_db()
        with mock.patch.object(pipeline.regime_engine, "refresh", side_effect=ValueError("boom")):
            r = pipeline.run_cycle(c, H.cfg(), H.universe(), [], H.deps([], {}), NOW)
        st = r["stages"]["regime"]
        self.assertIn("ValueError: boom", st["error"])
        self.assertRegex(st["where"], r"\w+\.py:\d+ \w+")
        self.assertIn("Traceback", st["trace"])

    def test_the_funnel_shows_where_it_failed(self):
        c = H.fresh_db()
        add(c, 10)
        self.assertIn("[quotes.py:129 _budget_ok", funnel.text(funnel.report(c, 24, NOW)))


class TestCommands(unittest.TestCase):
    def run_cli(self, *argv):
        from paper_agent import __main__ as cli
        c = H.fresh_db()
        add(c, 30)
        c.commit()
        path = c.execute("PRAGMA database_list").fetchone()[2]
        old = cli.DB_PATH
        cli.DB_PATH = path
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(list(argv))
        finally:
            cli.DB_PATH = old
        return rc, buf.getvalue(), path

    def test_errors_command_prints_the_stored_traceback_location(self):
        rc, out, _ = self.run_cli("errors")
        self.assertEqual(rc, 0)
        self.assertIn("no such column: backoff_until", out)
        self.assertIn("quotes.py:129 _budget_ok", out)

    def test_acknowledge_errors_hides_history_but_keeps_the_rows(self):
        rc, out, path = self.run_cli("acknowledge-errors")
        self.assertEqual(rc, 0)
        self.assertIn("1 earlier failed cycle", out)
        import sqlite3
        c = sqlite3.connect(path)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM cycle_runs WHERE status='ERROR'").fetchone()[0], 1)      # not deleted
        self.assertTrue(c.execute("SELECT value FROM meta WHERE key='errors_ack_at'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
