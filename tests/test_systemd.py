"""Guards for the failures seen on the Pi: restart stampede, unit/CLI mismatches, schedule drift."""
import glob
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import control  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def units(pattern):
    for p in sorted(glob.glob(os.path.join(ROOT, "systemd", pattern))):
        with open(p) as f:
            yield os.path.basename(p), f.read()


class TestUnitFiles(unittest.TestCase):
    def test_no_timer_is_anchored_to_boot(self):
        """REGRESSION: OnBootSec makes a RESTARTED timer fire immediately (boot was long ago). 'Restart everything' then started the cycle, guard,
        update (installer + tests!), backup and universe jobs simultaneously and ran a 730 MB Pi out of memory."""
        for name, text in units("*.timer"):
            self.assertNotRegex(text, r"(?m)^\s*OnBootSec\s*=", name)       # the directive, not a comment that mentions it
        for name in ("paper-agent-cycle.timer", "paper-agent-guard.timer", "paper-agent-update.timer"):
            self.assertRegex(dict(units("*.timer"))[name], r"(?m)^OnActiveSec=", name)

    def test_every_service_runs_a_real_cli_command(self):
        with open(os.path.join(ROOT, "paper_agent", "__main__.py")) as f:
            src = f.read()
        known = set(re.findall(r'"([a-z][a-z-]+)"', src[src.index("def main"):src.index("args = ap.parse_args")]))
        for name, text in units("*.service"):
            for m in re.finditer(r"ExecStart=.*-m paper_agent(?:\.control)?\s*([a-z-]*)", text):
                cmd = m.group(1)
                if "paper_agent.control" in m.group(0):
                    continue
                self.assertIn(cmd, known, "%s runs unknown command %r" % (name, cmd))

    def test_timers_point_at_existing_services(self):
        svc = {n for n, _ in units("*.service")}
        for name, text in units("*.timer"):
            self.assertIn(name.replace(".timer", ".service"), svc, name)

    def test_update_job_is_not_restartable_and_is_memory_guarded(self):
        self.assertNotIn("paper-agent-update.timer", control.RESTARTABLE)
        self.assertNotIn("paper-agent-backup.timer", control.RESTARTABLE)
        self.assertNotIn("paper-agent-universe.timer", control.RESTARTABLE)
        self.assertIn("paper-agent-update.service", control.HEAVY)


class TestControlSafety(unittest.TestCase):
    def setUp(self):
        self.calls, self.etc = [], tempfile.mkdtemp()
        self.run = lambda cmd, **kw: (self.calls.append(cmd), mock.Mock(returncode=0, stdout="", stderr=""))[1]

    def test_restart_all_touches_only_guard_cycle_dashboard_in_a_safe_order(self):
        r = control.execute({"id": "1", "action": "restart_all", "args": {}}, self.run, self.etc)
        self.assertTrue(r["ok"])
        self.assertEqual([c[-1] for c in self.calls], ["paper-agent-guard.timer", "paper-agent-cycle.timer", "paper-agent-dashboard.service"])
        flat = " ".join(" ".join(c) for c in self.calls)
        for job in ("update", "backup", "universe"):
            self.assertNotIn(job, flat)

    def test_job_timers_cannot_be_restarted_from_the_dashboard(self):
        for u in ("paper-agent-update.timer", "paper-agent-backup.timer", "paper-agent-universe.timer"):
            self.assertFalse(control.execute({"action": "restart", "args": {"unit": u}}, self.run, self.etc)["ok"], u)
        self.assertEqual(self.calls, [])

    def test_heavy_jobs_are_refused_when_memory_is_low_and_allowed_when_it_is_not(self):
        r = control.execute({"action": "start", "args": {"unit": "paper-agent-update.service"}}, self.run, self.etc, mem_mb=lambda: 180)
        self.assertFalse(r["ok"])
        self.assertIn("180 MB", r["output"])
        self.assertEqual(self.calls, [])
        self.assertTrue(control.execute({"action": "start", "args": {"unit": "paper-agent-update.service"}}, self.run, self.etc, mem_mb=lambda: 500)["ok"])
        self.assertTrue(control.execute({"action": "start", "args": {"unit": "paper-agent-update.service"}}, self.run, self.etc, mem_mb=lambda: None)["ok"])    # unknown RAM: do not block


class TestScheduleReconcile(unittest.TestCase):
    def setUp(self):
        self.calls, self.etc = [], tempfile.mkdtemp()
        self.run = lambda cmd, **kw: (self.calls.append(cmd), mock.Mock(returncode=0, stdout="", stderr=""))[1]

    def dropin(self, unit):
        p = os.path.join(self.etc, unit + ".d", "schedule.conf")
        if os.path.exists(p):
            with open(p) as f:
                return f.read()

    def test_settings_become_effective_systemd_config(self):
        r = control.reconcile({"cycle_minutes": 15, "guard_minutes": 2, "update_hours": 6}, self.run, self.etc)
        self.assertEqual(sorted(r["changed"]), ["paper-agent-cycle.timer", "paper-agent-update.timer"])      # guard is at its default: untouched
        self.assertEqual(self.dropin("paper-agent-cycle.timer"), "[Timer]\nOnUnitInactiveSec=\nOnUnitInactiveSec=15min\n")
        self.assertEqual(self.dropin("paper-agent-update.timer"), "[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=6h\n")
        self.assertIsNone(self.dropin("paper-agent-guard.timer"))
        self.assertEqual([c for c in self.calls if c[:2] == ["systemctl", "restart"]], [["systemctl", "restart", "--no-block", "paper-agent-cycle.timer"], ["systemctl", "restart", "--no-block", "paper-agent-update.timer"]])

    def test_idempotent_no_churn_when_already_in_effect(self):
        control.reconcile({"cycle_minutes": 15}, self.run, self.etc)
        self.calls.clear()
        r = control.reconcile({"cycle_minutes": 15}, self.run, self.etc)
        self.assertEqual((r["changed"], self.calls), ([], []))                  # nothing restarted, daemon not reloaded

    def test_back_to_default_removes_the_dropin(self):
        control.reconcile({"cycle_minutes": 15}, self.run, self.etc)
        r = control.reconcile({"cycle_minutes": 10}, self.run, self.etc)
        self.assertEqual(r["changed"], ["paper-agent-cycle.timer"])
        self.assertIsNone(self.dropin("paper-agent-cycle.timer"))

    def test_invalid_values_are_ignored(self):
        r = control.reconcile({"cycle_minutes": 1, "guard_minutes": "x; reboot", "update_hours": 9999}, self.run, self.etc)
        self.assertEqual((r["changed"], os.listdir(self.etc)), ([], []))

    def test_status_reports_drift_so_it_is_never_silent(self):
        st = control.schedule_status({"cycle_minutes": 15, "guard_minutes": 2, "update_hours": 3}, self.etc)
        self.assertFalse(st["cycle_minutes"]["in_sync"])
        self.assertEqual((st["cycle_minutes"]["configured"], st["cycle_minutes"]["applied"]), (15, 10))
        self.assertTrue(st["guard_minutes"]["in_sync"])
        control.reconcile({"cycle_minutes": 15}, self.run, self.etc)
        self.assertTrue(control.schedule_status({"cycle_minutes": 15}, self.etc)["cycle_minutes"]["in_sync"])

    def test_cycle_self_heals_drift_by_requesting_reconcile_at_most_every_30_minutes(self):
        import datetime as dt
        from paper_agent import config, pipeline
        c, cfg = H.fresh_db(), H.cfg()
        cfg["schedule"]["cycle_minutes"] = 20
        d = tempfile.mkdtemp()
        old = config.CONTROL_DIR
        config.CONTROL_DIR = d
        try:
            with mock.patch("paper_agent.control.applied_schedule", return_value=dict(control.DEFAULT_SCHEDULE)):
                r = pipeline.run_cycle(c, cfg, H.universe(), [], H.deps([], {}), H.NOW)
                self.assertEqual(r["schedule"]["drift"], ["cycle_minutes"])
                self.assertTrue(os.path.exists(os.path.join(d, "request.json")))
                os.remove(os.path.join(d, "request.json"))
                pipeline.run_cycle(c, cfg, H.universe(), [], H.deps([], {}), H.NOW + dt.timedelta(minutes=10))
                self.assertFalse(os.path.exists(os.path.join(d, "request.json")))      # rate-limited
        finally:
            config.CONTROL_DIR = old


if __name__ == "__main__":
    unittest.main()
