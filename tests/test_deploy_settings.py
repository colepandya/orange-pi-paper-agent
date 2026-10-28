"""Deployments must NEVER overwrite a setting the user changed. Releases may suggest, or force a value explicitly and only once."""
import datetime as dt
import io
import contextlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_settings_ui as TS  # noqa: E402
from paper_agent import config, settings as S  # noqa: E402

T0 = dt.datetime(2026, 10, 28, 12, 0, tzinfo=dt.timezone.utc)
ISO = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S+00:00")  # noqa: E731


class Sandbox(unittest.TestCase):
    """Temp state + config dirs; nothing touches the host."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._old = (config.SETTINGS_PATH, config.SECRETS_PATH, config.CONFIG_DIR)
        config.SETTINGS_PATH, config.SECRETS_PATH, config.CONFIG_DIR = os.path.join(self.d, "settings.json"), os.path.join(self.d, "secrets.env"), os.path.join(self.d, "etc")

    def tearDown(self):
        config.SETTINGS_PATH, config.SECRETS_PATH, config.CONFIG_DIR = self._old

    def entry(self, eid, key, value, **kw):
        return {"id": eid, "key": key, "value": value, "released": kw.pop("released", ISO(T0 + dt.timedelta(days=1))), **kw}

    def user_sets(self, ov, at=T0):
        S.save_overrides_tracked(ov, "user", now=at)

    def eff(self, path):
        return S.get_path(config.load_config(), path)


class TestModifiedOnTracking(Sandbox):
    def test_user_changes_are_stamped_with_time_and_author(self):
        self.user_sets({"risk": {"max_position_pct": 0.08}})
        m = S.load_meta()["risk.max_position_pct"]
        self.assertEqual((m["by"], m["modified"]), ("user", ISO(T0)))

    def test_only_what_actually_changed_is_restamped(self):
        self.user_sets({"risk": {"max_position_pct": 0.08, "min_cash_pct": 0.25}}, T0)
        S.save_overrides_tracked({"risk": {"max_position_pct": 0.08, "min_cash_pct": 0.30}}, "user", now=T0 + dt.timedelta(hours=1))
        meta = S.load_meta()
        self.assertEqual(meta["risk.max_position_pct"]["modified"], ISO(T0))                     # untouched: keeps its original stamp
        self.assertEqual(meta["risk.min_cash_pct"]["modified"], ISO(T0 + dt.timedelta(hours=1)))

    def test_removal_is_recorded_too(self):
        self.user_sets({"risk": {"max_position_pct": 0.08}})
        S.save_overrides_tracked({}, "user", "reset", now=T0 + dt.timedelta(hours=1))
        self.assertTrue(S.load_meta()["risk.max_position_pct"]["removed"])


class TestReleaseEntries(Sandbox):
    def test_untouched_setting_gets_the_release_value(self):
        rep = S.apply_deploy([self.entry("r1", "risk.max_position_pct", 0.07, note="safer default")], T0)
        self.assertEqual(rep[0]["status"], "applied")
        self.assertEqual(self.eff("risk.max_position_pct"), 0.07)
        self.assertEqual(S.load_meta()["risk.max_position_pct"]["by"], "deploy")

    def test_a_setting_the_user_changed_is_NEVER_overwritten(self):
        self.user_sets({"risk": {"max_position_pct": 0.08}}, T0)
        rep = S.apply_deploy([self.entry("r1", "risk.max_position_pct", 0.05)], T0 + dt.timedelta(days=2))
        self.assertEqual(rep[0]["status"], "suggested")
        self.assertEqual(self.eff("risk.max_position_pct"), 0.08)                                # unchanged
        self.assertEqual(S.load_meta()["risk.max_position_pct"]["by"], "user")
        p = S.pending_suggestions()
        self.assertEqual((p[0]["key"], p[0]["value"], p[0]["your_value"]), ("risk.max_position_pct", 0.05, 0.08))

    def test_repeated_deploys_do_not_re_suggest_or_re_apply(self):
        self.user_sets({"risk": {"max_position_pct": 0.08}}, T0)
        e = [self.entry("r1", "risk.max_position_pct", 0.05), self.entry("r2", "risk.min_cash_pct", 0.3)]
        S.apply_deploy(e, T0)
        again = S.apply_deploy(e, T0)
        self.assertEqual([r["status"] for r in again], ["already_decided", "already_decided"])
        self.assertEqual(len(S.pending_suggestions()), 1)

    def test_a_release_older_than_the_users_change_is_dropped_silently(self):
        """The timestamp rule: your later decision beats an older release."""
        self.user_sets({"risk": {"max_position_pct": 0.08}}, T0 + dt.timedelta(days=3))
        rep = S.apply_deploy([self.entry("old", "risk.max_position_pct", 0.05, released=ISO(T0))], T0 + dt.timedelta(days=4))
        self.assertEqual(rep[0]["status"], "superseded_by_user")
        self.assertEqual(S.pending_suggestions(), [])
        self.assertEqual(self.eff("risk.max_position_pct"), 0.08)

    def test_force_overrides_exactly_once(self):
        self.user_sets({"risk": {"max_position_pct": 0.08}}, T0)
        rep = S.apply_deploy([self.entry("fix1", "risk.max_position_pct", 0.05, force=True, note="critical fix")], T0 + dt.timedelta(days=2))
        self.assertEqual((rep[0]["status"], rep[0]["detail"]), ("applied", "forced"))
        self.assertEqual(self.eff("risk.max_position_pct"), 0.05)
        self.user_sets({"risk": {"max_position_pct": 0.09}}, T0 + dt.timedelta(days=3))           # the user sets it again afterwards
        again = S.apply_deploy([self.entry("fix1", "risk.max_position_pct", 0.05, force=True)], T0 + dt.timedelta(days=4))     # same release redeployed
        self.assertEqual(again[0]["status"], "already_decided")
        self.assertEqual(self.eff("risk.max_position_pct"), 0.09)                                 # the forced value is NOT forced back
        # a NEW entry id can force again, deliberately
        S.apply_deploy([self.entry("fix2", "risk.max_position_pct", 0.05, force=True)], T0 + dt.timedelta(days=5))
        self.assertEqual(self.eff("risk.max_position_pct"), 0.05)

    def test_user_reset_counts_as_a_user_decision(self):
        self.user_sets({"risk": {"max_position_pct": 0.08}}, T0)
        S.save_overrides_tracked({}, "user", "reset", now=T0 + dt.timedelta(hours=1))
        rep = S.apply_deploy([self.entry("r1", "risk.max_position_pct", 0.05)], T0 + dt.timedelta(days=2))
        self.assertEqual(rep[0]["status"], "suggested")

    def test_invalid_entries_are_rejected_and_never_applied(self):
        entries = [self.entry("a", "no.such.setting", 1), self.entry("b", "risk.max_position_pct", 0.9), self.entry("c", "risk.max_position_pct", "lots"),
                   self.entry("d", "risk.max_new_per_cycle", 2.5), self.entry("e", "intelligence.llm_enabled", "yes"), {"id": "f", "key": "risk.min_cash_pct"}, self.entry("g", "runtime.contact_email", "nope")]
        rep = S.apply_deploy(entries, T0)
        self.assertEqual([r["status"] for r in rep], ["rejected"] * 7)
        self.assertEqual(S.load_overrides(), {})

    def test_cross_field_rules_apply_to_releases_too(self):
        self.user_sets({"risk": {"max_issuer_pct": 0.10}}, T0 - dt.timedelta(days=9))
        S.apply_deploy([self.entry("x", "risk.max_position_pct", 0.2, force=True)], T0)           # position 20% > company cap 10%
        self.assertEqual(self.eff("risk.max_position_pct"), config.DEFAULTS["risk"]["max_position_pct"])
        with open(S.applied_path()) as fh:
            self.assertEqual(json.load(fh)["x"]["status"], "rejected")

    def test_value_equal_to_the_shipped_default_removes_the_override(self):
        self.user_sets({"risk": {"max_position_pct": 0.08}}, T0)
        S.apply_deploy([self.entry("back", "risk.max_position_pct", config.DEFAULTS["risk"]["max_position_pct"], force=True)], T0 + dt.timedelta(days=2))
        self.assertNotIn("risk", S.load_overrides())

    def test_all_untouched_entries_apply_together_and_user_ones_are_suggested(self):
        self.user_sets({"risk": {"min_cash_pct": 0.15}}, T0)
        rep = S.apply_deploy([self.entry("a", "risk.max_position_pct", 0.07), self.entry("b", "risk.min_cash_pct", 0.25), self.entry("c", "schedule.guard_minutes", 3)], T0 + dt.timedelta(days=2))
        self.assertEqual({r["id"]: r["status"] for r in rep}, {"a": "applied", "b": "suggested", "c": "applied"})
        self.assertEqual((self.eff("risk.min_cash_pct"), self.eff("risk.max_position_pct"), self.eff("schedule.guard_minutes")), (0.15, 0.07, 3))

    def test_a_preexisting_inconsistency_does_not_block_unrelated_changes(self):
        """REGRESSION (found by these tests): cross-checks ran on the whole config, so one existing violation rejected EVERY change."""
        self.user_sets({"risk": {"min_cash_pct": 0.35}}, T0)                                   # 35% cash + 80% max invested > 100%: inconsistent on its own
        rep = S.apply_deploy([self.entry("a", "risk.max_position_pct", 0.07), self.entry("c", "schedule.guard_minutes", 3)], T0 + dt.timedelta(days=2))
        self.assertEqual([r["status"] for r in rep], ["applied", "applied"])
        rep = S.apply_deploy([self.entry("bad", "risk.max_gross_pct", 0.9, force=True)], T0 + dt.timedelta(days=3))         # but it may not make things WORSE
        self.assertEqual(rep[0]["status"], "applied")                                           # same violation already existed (not newly introduced)
        rep = S.apply_deploy([self.entry("worse", "risk.max_position_pct", 0.5, force=True)], T0 + dt.timedelta(days=4))   # 50% position > 15% company cap: genuinely new
        self.assertEqual(rep[0]["status"], "rejected")

    def test_accept_and_dismiss(self):
        self.user_sets({"risk": {"max_position_pct": 0.08}, "schedule": {"guard_minutes": 3}}, T0)
        S.apply_deploy([self.entry("r1", "risk.max_position_pct", 0.05), self.entry("r2", "schedule.guard_minutes", 4)], T0 + dt.timedelta(days=2))
        self.assertEqual(sorted(p["id"] for p in S.pending_suggestions()), ["r1", "r2"])
        ok, _ = S.resolve_suggestion("r1", accept=True)
        self.assertTrue(ok)
        self.assertEqual(self.eff("risk.max_position_pct"), 0.05)
        self.assertEqual(S.load_meta()["risk.max_position_pct"]["by"], "user")                    # YOUR explicit decision
        ok, _ = S.resolve_suggestion("r2", accept=False)
        self.assertTrue(ok)
        self.assertEqual(self.eff("schedule.guard_minutes"), 3)
        self.assertEqual(S.pending_suggestions(), [])
        self.assertFalse(S.resolve_suggestion("nope", True)[0])
        self.assertEqual(S.apply_deploy([self.entry("r2", "schedule.guard_minutes", 4)], T0)[0]["status"], "already_decided")      # dismissed stays dismissed


class TestCLI(Sandbox):
    def test_apply_deploy_command_reports_and_tolerates_missing_files(self):
        from paper_agent import __main__ as cli
        f = os.path.join(self.d, "deploy.json")
        with open(f, "w") as fh:
            json.dump({"entries": [self.entry("c1", "risk.max_position_pct", 0.06)]}, fh)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["apply-deploy", f]), 0)
            self.assertEqual(cli.main(["apply-deploy", os.path.join(self.d, "missing.json")]), 0)
        self.assertIn("applied", buf.getvalue())
        self.assertIn("user-changed settings were left alone", buf.getvalue())
        self.assertEqual(self.eff("risk.max_position_pct"), 0.06)

    def test_the_shipped_deploy_file_is_valid_and_changes_nothing_by_default(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "deploy-settings.json")) as fh:
            self.assertEqual(json.load(fh)["entries"], [])


class TestDashboardIntegration(TS.HTTPHarness, unittest.TestCase):
    def test_saving_in_the_dashboard_stamps_the_change_and_the_page_shows_it(self):
        tok, _ = self.csrf()
        self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": "8"})
        m = S.load_meta()["risk.max_position_pct"]
        self.assertEqual(m["by"], "user")
        _, _, page = self.req("GET", "/?tab=settings")
        self.assertIn("changed", page)
        self.assertIn("by you", page)
        self.assertIn("Recent setting changes", page)

    def test_release_suggestion_is_shown_and_can_be_accepted_or_dismissed(self):
        tok, _ = self.csrf()
        self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": "8", "f.risk.min_cash_pct": "15"})
        S.apply_deploy([{"id": "r1", "key": "risk.max_position_pct", "value": 0.05, "released": "2099-01-01T00:00:00+00:00", "note": "release prefers 5%"},
                        {"id": "r2", "key": "risk.min_cash_pct", "value": 0.18, "released": "2099-01-01T00:00:00+00:00"}])
        _, _, page = self.req("GET", "/?tab=settings")
        for needle in ("The latest release suggests 2 setting change", "release prefers 5%", "Use release value", "Keep mine"):
            self.assertIn(needle, page)
        self.assertEqual(self.req("POST", "/settings/deploy-accept", {"id": "r1"})[0], 403)                # CSRF still required
        self.assertEqual(self.req("POST", "/settings/deploy-accept", {"csrf": tok, "id": "r1"})[0], 303)
        self.assertEqual(self.req("POST", "/settings/deploy-dismiss", {"csrf": tok, "id": "r2"})[0], 303)
        cfg = config.load_config()
        self.assertEqual((cfg["risk"]["max_position_pct"], cfg["risk"]["min_cash_pct"]), (0.05, 0.15))
        self.assertNotIn("The latest release suggests", self.req("GET", "/?tab=settings")[2])

    def test_saving_a_key_records_when_it_was_updated_and_never_shows_it(self):
        tok, _ = self.csrf()
        self.req("POST", "/settings/secrets", {"csrf": tok, "secret.ANTHROPIC_API_KEY": TS.SECRET})
        self.assertEqual(S.load_meta()["secret.ANTHROPIC_API_KEY"]["by"], "user")
        _, _, page = self.req("GET", "/?tab=settings")
        self.assertIn("updated", page)
        self.assertNotIn(TS.SECRET, page)

    def test_reset_is_recorded_and_a_later_release_can_only_suggest(self):
        tok, _ = self.csrf()
        self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": "8"})
        self.req("POST", "/settings/reset", {"csrf": tok, "confirm": "1"})
        self.assertTrue(S.load_meta()["risk.max_position_pct"]["removed"])
        self.assertEqual(S.apply_deploy([{"id": "r9", "key": "risk.max_position_pct", "value": 0.05, "released": "2099-01-01T00:00:00+00:00"}])[0]["status"], "suggested")


if __name__ == "__main__":
    unittest.main()
