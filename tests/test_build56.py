"""Build 56: a new position gets a grace period before an evidence (RISK_ESCALATION) exit applies."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
import test_build53 as T53  # noqa: E402
import test_guards as TG  # noqa: E402
from paper_agent import config, executor, settings as S, settings_ui as U  # noqa: E402
from paper_agent.timeutil import iso, parse_ts  # noqa: E402

NOW = parse_ts("2026-11-12T15:30:00+00:00")
DEF = lambda: config.load_config("/nonexistent", with_overrides=False)  # noqa: E731


def pos(minutes_old, side="LONG", entry=100.0):
    return {"opened": iso(NOW - dt.timedelta(minutes=minutes_old)), "entry_price": entry, "side": side}


class TestTheWaitRule(unittest.TestCase):
    def waits(self, p, price, cfg=None):
        return executor.escalation_waits(p, price, DEF() if cfg is None else cfg, NOW)

    def test_a_young_position_with_a_small_loss_waits(self):
        self.assertTrue(self.waits(pos(30), 99.7))                                                          # -0.3% after 30 minutes: your GOOGL / MSFT case

    def test_the_loss_boundary_is_one_percent(self):
        self.assertTrue(self.waits(pos(30), 99.01))                                                         # -0.99%: still waits
        self.assertFalse(self.waits(pos(30), 98.99))                                                        # -1.01%: acts at once
        self.assertFalse(self.waits(pos(30), 98.0))

    def test_the_time_boundary_is_sixty_minutes(self):
        self.assertTrue(self.waits(pos(59), 99.7))
        self.assertFalse(self.waits(pos(60), 99.7))                                                         # at exactly the grace it applies
        self.assertFalse(self.waits(pos(61), 99.7))

    def test_a_profit_while_young_also_waits(self):
        self.assertTrue(self.waits(pos(10), 103.0))

    def test_a_short_loses_when_the_price_RISES(self):
        s = pos(30, side="SHORT")
        self.assertTrue(self.waits(s, 100.3))                                                               # +0.3% against a short: a small loss
        self.assertFalse(self.waits(s, 101.5))                                                              # +1.5% against a short: a real loss
        self.assertTrue(self.waits(s, 98.0))                                                                # price down: the short is winning

    def test_the_settings_change_it(self):
        c = DEF()
        c["strategy"]["escalation_grace_minutes"] = 0
        self.assertFalse(self.waits(pos(1), 99.9, c))                                                       # 0 = act at once (the old behaviour)
        c = DEF()
        c["strategy"]["escalation_grace_minutes"] = 120
        self.assertTrue(self.waits(pos(100), 99.7, c))
        c["strategy"]["escalation_grace_loss_pct"] = 0.003
        self.assertFalse(self.waits(pos(100), 99.6, c))                                                     # a tighter loss limit
        self.assertTrue(self.waits(pos(100), 99.8, c))

    def test_no_configuration_and_junk_never_wait_and_never_raise(self):
        self.assertFalse(executor.escalation_waits(pos(30), 99.7, None, NOW))
        for bad_cfg in ({"strategy": {"escalation_grace_minutes": "x"}}, {"strategy": {"escalation_grace_loss_pct": None}}, {"strategy": {"escalation_grace_minutes": float("inf")}},
                        {"strategy": {"escalation_grace_minutes": float("nan")}}, {"strategy": None}):
            executor.escalation_waits(pos(30), 99.7, bad_cfg, NOW)
        for bad_p in ({}, {"opened": None, "entry_price": 1}, {"opened": "junk", "entry_price": 1}, pos(30, entry=0), pos(30, entry=-1), {"opened": iso(NOW), "entry_price": "x", "side": "LONG"}):
            self.assertFalse(executor.escalation_waits(bad_p, 99.7, DEF(), NOW))
        for bad_px in (0, -5, None, "x", float("nan")):
            self.assertFalse(executor.escalation_waits(pos(30), bad_px, DEF(), NOW))


class TestThroughTheRealGuard(unittest.TestCase):
    def world(self, trigger="RISK_ESCALATION", severity=0.8, extra=None, grace=None):
        w = TG.World()
        w.setUp()
        self.addCleanup(w.tearDown)
        if grace is not None:
            w.cfg["strategy"]["escalation_grace_minutes"] = grace
        T53.run_cycle(w)
        p = dict(w.c.execute("SELECT * FROM positions WHERE ticker='NVDA'").fetchone())
        for trig, sev in [(trigger, severity)] + (extra or []):
            w.c.execute("INSERT INTO exit_signals(position_id,created,trigger,severity,reason,status) VALUES(?,?,?,?,?,'OPEN')", (p["id"], p["opened"], trig, sev, "test: %s" % trig))
        w.c.commit()
        return w, p

    def guard(self, w, p, minutes, move):
        executor.run(w.c, w.cfg, parse_ts(p["opened"]) + dt.timedelta(minutes=minutes), H.Quotes({"NVDA": round(p["entry_price"] * (1 + move), 4)}))
        return dict(w.c.execute("SELECT status,review_code FROM positions WHERE id=?", (p["id"],)).fetchone())

    def test_a_small_loss_in_the_first_hour_is_held_not_sold(self):
        w, p = self.world()
        self.assertEqual(self.guard(w, p, 20, -0.003)["status"], "OPEN")

    def test_the_same_signal_applies_once_the_grace_has_passed(self):
        w, p = self.world()
        r = self.guard(w, p, 70, -0.003)
        self.assertEqual((r["status"], r["review_code"]), ("CLOSED", "RISK_ESCALATION"))

    def test_a_real_loss_applies_at_once(self):
        w, p = self.world()
        r = self.guard(w, p, 20, -0.015)                                                                    # -1.5%: not near the 3% stop, but past the 1% grace limit
        self.assertEqual((r["status"], r["review_code"]), ("CLOSED", "RISK_ESCALATION"))

    def test_a_winning_position_is_held_too(self):
        w, p = self.world()
        self.assertEqual(self.guard(w, p, 20, 0.005)["status"], "OPEN")

    def test_a_broken_thesis_is_never_delayed(self):
        w, p = self.world(trigger="THESIS_BROKEN", severity=0.75)
        r = self.guard(w, p, 20, -0.003)
        self.assertEqual((r["status"], r["review_code"]), ("CLOSED", "THESIS_INVALIDATED"))

    def test_a_waiting_escalation_does_not_hide_a_thesis_exit_behind_it(self):
        w, p = self.world(trigger="RISK_ESCALATION", severity=0.8, extra=[("THESIS_BROKEN", 0.75)])        # the higher-severity one waits; the next one acts
        r = self.guard(w, p, 20, -0.003)
        self.assertEqual((r["status"], r["review_code"]), ("CLOSED", "THESIS_INVALIDATED"))

    def test_only_the_evidence_exit_is_delayed_not_a_rotation(self):
        w, p = self.world(trigger="SUPERIOR_OPPORTUNITY", severity=0.5)
        r = self.guard(w, p, 20, -0.003)
        self.assertEqual((r["status"], r["review_code"]), ("CLOSED", "SUPERIOR_OPPORTUNITY"))

    def test_with_the_grace_off_it_acts_at_once_as_before(self):
        w, p = self.world(grace=0)
        r = self.guard(w, p, 20, -0.003)
        self.assertEqual((r["status"], r["review_code"]), ("CLOSED", "RISK_ESCALATION"))


class TestSettings(unittest.TestCase):
    def test_defaults_bounds_layout(self):
        d = DEF()
        self.assertEqual((d["strategy"]["escalation_grace_minutes"], d["strategy"]["escalation_grace_loss_pct"]), (60, 0.01))
        f = {x.path: x for x in S.FIELDS}
        for p in ("strategy.escalation_grace_minutes", "strategy.escalation_grace_loss_pct"):
            self.assertIn(p, f)
        self.assertEqual((f["strategy.escalation_grace_minutes"].lo, f["strategy.escalation_grace_minutes"].hi), (0, 480))
        self.assertEqual(U.layout_problems(), [])


if __name__ == "__main__":
    unittest.main()
