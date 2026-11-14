"""Swing profile: in and out within ~1-3 sessions, tight risk, profits protected. (Before: holds up to 15 days, stops 2-10%, nothing protected a profit.)"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import config, ev, settings as S  # noqa: E402

T_OPEN = TL.T_OPEN


class Swing(TL.Base):
    swing = True

    def setUp(self):
        super().setUp()
        self.cfg["strategy"]["swing_enabled"] = self.swing

    def runTest(self):
        pass

    def entry(self):
        return float(self.one("SELECT entry_price FROM positions")[0])

    def plan(self):
        return dict(self.one("SELECT * FROM position_forecast_plans WHERE active=1"))

    def revisions(self):
        return [r[0] for r in self.c.execute("SELECT reason FROM forecast_revisions WHERE field='stop_price' AND reason LIKE 'Swing protection%'")]


class TestEntryEconomics(unittest.TestCase):
    def forecast(self, **cfg_changes):
        s = Swing()
        s.setUp()
        try:
            for k, v in cfg_changes.items():
                s.cfg["strategy"][k] = v
            s.cycle(TL.T_PRE, news=True)
            r = s.one("SELECT reason FROM decisions WHERE ticker='NVDA' ORDER BY id LIMIT 1")[0]
            f = s.one("SELECT * FROM forecasts ORDER BY id DESC LIMIT 1")
            return dict(f), r
        finally:
            s.tearDown()

    def test_the_swing_plan_is_short_with_a_tight_stop_and_a_reachable_target(self):
        s = Swing()
        s.setUp()
        try:
            plan = s.open_position()
            sw = s.cfg["strategy"]
            self.assertLessEqual(plan["max_hold_days"], sw["max_hold_days"])
            self.assertTrue(sw["stop_min_pct"] - 1e-9 <= -plan["stop_return"] <= sw["stop_max_pct"] + 1e-9, plan["stop_return"])
            self.assertTrue(sw["target_min_pct"] - 1e-9 <= plan["target_return"] <= sw["target_max_pct"] + 1e-9, plan["target_return"])
            self.assertGreaterEqual(plan["target_return"] / -plan["stop_return"], sw["min_reward_risk"])
            self.assertIn("Swing plan", plan["rationale"])
        finally:
            s.tearDown()

    def test_the_old_profile_is_still_available_and_is_much_wider_and_longer(self):
        old = Swing()
        old.swing = False
        old.setUp()
        new = Swing()
        new.setUp()
        try:
            a, b = old.open_position(), new.open_position()
            self.assertGreater(a["max_hold_days"], b["max_hold_days"])
            self.assertGreater(-a["stop_return"], -b["stop_return"])
            self.assertNotIn("Swing plan", a["rationale"])
        finally:
            old.tearDown()
            new.tearDown()

    def test_only_the_part_of_the_move_that_fits_the_shorter_window_is_counted(self):
        f_swing, _ = self.forecast()
        f_classic, _ = self.forecast(swing_enabled=False)
        self.assertLess(f_swing["ev_net"], f_classic["ev_net"])                    # a shorter hold captures less of a long-running story, and the EV says so honestly
        self.assertLess(f_swing["horizon_days"], f_classic["horizon_days"])

    def test_a_trade_whose_target_is_too_small_for_its_stop_is_skipped(self):
        f, r = self.forecast(min_reward_risk=4.0)
        self.assertEqual(f["verdict"] if "verdict" in f else "", "WATCH") if "verdict" in f else None
        self.assertIn("reward:risk", r)

    def test_a_stock_that_barely_moves_is_skipped(self):
        f, r = self.forecast(min_daily_vol_pct=0.5)                                # nothing moves 50% a day
        self.assertIn("too quiet for a swing", r)

    def test_every_swing_setting_is_editable_in_the_dashboard_and_validated(self):
        base = config.load_config("/nonexistent", with_overrides=False)
        fields = {f.path for f in S.FIELDS if f.group == "Swing strategy"}
        self.assertEqual(fields, {"strategy." + k for k in base["strategy"]})      # every strategy key has a dashboard control, none are hidden
        for path, bad in (("strategy.max_hold_days", "99"), ("strategy.stop_max_pct", "0.9"), ("strategy.min_reward_risk", "0"), ("strategy.trail_pct", "-1")):
            _, err, _ = S.apply_form({"f." + path: bad}, base, base, {})
            self.assertTrue(err, path)


class TestProfitProtection(unittest.TestCase):
    def setUp(self):
        self.s = Swing()
        self.s.setUp()
        self.plan = self.s.open_position()
        self.entry = self.s.entry()
        self.t = T_OPEN + dt.timedelta(minutes=30)

    def tearDown(self):
        self.s.tearDown()

    def px(self, pct):
        return self.entry * (1 + pct)

    def test_nothing_moves_before_the_trigger(self):
        self.s.mkt.price["NVDA"] = self.px(0.004)
        self.s.guard(self.t)
        self.assertAlmostEqual(self.s.plan()["stop_price"], self.plan["stop_price"], places=6)
        self.assertEqual(self.s.revisions(), [])

    def test_after_a_one_percent_gain_the_stop_moves_to_break_even_and_a_pullback_sells_in_profit(self):
        self.s.mkt.price["NVDA"] = self.px(0.012)
        r = self.s.guard(self.t)
        self.assertEqual(r["committed"], 0)                                          # below the target: no sale, but the stop has been raised
        stop = self.s.plan()["stop_price"]
        self.assertGreater(stop, self.entry)                                         # break-even or better: this winner can no longer become a loser
        self.assertGreater(stop, self.plan["stop_price"])
        self.assertTrue(self.s.revisions() and "locks in" in self.s.revisions()[0])  # and the dashboard's revision log says why
        self.s.mkt.price["NVDA"] = stop - 0.05                                       # the pullback
        r = self.s.guard(self.t + dt.timedelta(minutes=30))
        self.assertEqual(r["committed"], 1)
        row = self.s.one("SELECT status,review_code,exit_price FROM positions")
        self.assertEqual((row["status"], row["review_code"]), ("CLOSED", "STOP_REACHED"))
        self.assertGreaterEqual(row["exit_price"], self.entry * 0.999)               # sold at ~break-even instead of the original -1.2% stop

    def test_the_stop_trails_the_high_and_never_moves_down(self):
        self.s.mkt.price["NVDA"] = self.px(0.018)
        self.s.guard(self.t)
        hi_stop = self.s.plan()["stop_price"]
        self.assertAlmostEqual(hi_stop, self.px(0.018) * (1 - self.s.cfg["strategy"]["trail_pct"]), places=2)    # trails 1% below the high
        self.s.mkt.price["NVDA"] = self.px(0.014)                                    # pulls back but stays above the stop
        self.s.guard(self.t + dt.timedelta(minutes=10))
        self.assertAlmostEqual(self.s.plan()["stop_price"], hi_stop, places=6)       # a lower price must NOT lower the stop
        self.s.mkt.price["NVDA"] = self.px(0.021)                                    # new high (still below the 2.3% target)
        self.s.guard(self.t + dt.timedelta(minutes=20))
        self.assertGreater(self.s.plan()["stop_price"], hi_stop)

    def test_widening_the_trail_setting_mid_trade_never_lowers_a_stop_that_is_already_protecting_profit(self):
        self.s.mkt.price["NVDA"] = self.px(0.018)
        self.s.guard(self.t)
        protected = self.s.plan()["stop_price"]
        self.s.cfg["strategy"]["trail_pct"] = 0.03                                   # the owner loosens the trail in Settings: the formula now gives a LOWER stop
        self.s.cfg["strategy"]["breakeven_lock_pct"] = 0.0
        self.s.guard(self.t + dt.timedelta(minutes=10))
        self.assertAlmostEqual(self.s.plan()["stop_price"], protected, places=6)     # ...but a stop only ever moves up

    def test_a_loser_is_not_given_a_free_pass(self):
        self.s.mkt.price["NVDA"] = self.plan["stop_price"] - 0.05                    # straight down: the ORIGINAL tight stop applies
        self.s.guard(self.t)
        self.assertEqual(self.s.one("SELECT review_code FROM positions")[0], "STOP_REACHED")
        self.assertLess(self.s.one("SELECT pnl FROM positions")[0], 0)

    def test_the_target_still_takes_a_partial_profit_and_the_runner_is_capped(self):
        self.s.c.execute("UPDATE position_forecast_plans SET confidence=10")         # follow the plan
        self.s.mkt.price["NVDA"] = self.plan["target_price"] + 0.1
        r = self.s.guard(self.t)
        self.assertEqual(r["committed"], 1)
        self.assertEqual(self.s.one("SELECT phase FROM position_lifecycle")[0], "RUNNER_OPEN")
        runner = self.s.plan()
        self.assertLessEqual(runner["target_return"], self.s.cfg["strategy"]["runner_cap_pct"] + 1e-9)
        TL.books_balance(self.s, self.s.c)

    def test_the_time_stop_sells_after_the_maximum_hold(self):
        self.s.guard(T_OPEN + dt.timedelta(days=3, minutes=30))                      # Friday: the 3rd session after entry, flat price
        self.assertEqual(self.s.one("SELECT review_code FROM positions")[0], "TIME_EXPIRED")


class TestDayTradeMode(unittest.TestCase):
    def run_at(self, minutes_after_open, flatten=True):
        s = Swing()
        s.setUp()
        try:
            s.cfg["strategy"].update(flatten_at_close=flatten, flatten_minutes_before_close=10)
            s.open_position()
            r = s.guard(dt.datetime(2026, 10, 27, 19, 0, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=minutes_after_open))     # 15:00 New York + n
            return r["committed"], s.one("SELECT status,review_code FROM positions")
        finally:
            s.tearDown()

    def test_nothing_is_held_overnight_when_the_owner_asks_for_day_trading(self):
        committed, row = self.run_at(55)                                             # 15:55 New York: 5 minutes to the close
        self.assertEqual((committed, row["status"], row["review_code"]), (1, "CLOSED", "EOD_FLATTEN"))

    def test_it_does_not_flatten_early_or_when_switched_off(self):
        self.assertEqual(self.run_at(30)[0], 0)                                      # 15:30: 30 minutes left
        self.assertEqual(self.run_at(55, flatten=False)[0], 0)


class TestWholeSystemOnTheSwingProfile(unittest.TestCase):
    """The shipped defaults (swing ON) against the simulated internet: trade, protect, sell, and keep the books straight."""

    def test_a_full_morning_trades_with_swing_plans_and_a_crash_is_sold_at_the_tight_stops(self):
        import fake_internet as FI  # noqa: F401
        import test_fake_internet as TF
        from paper_agent import pipeline
        tf = TF.TestWholeSystemAgainstTheSimulatedInternet("test_a_clean_morning_runs_without_a_single_stage_error_and_trades")
        tf.setUp()
        try:
            c, errors, net = tf.morning(seed=1, cycles=12, swing=True)
            self.assertEqual(errors, [])
            plans = [dict(r) for r in c.execute("SELECT max_hold_days,stop_return,target_return FROM position_forecast_plans WHERE active=1")]
            self.assertTrue(plans)
            self.assertTrue(all(p["max_hold_days"] <= 4 and p["stop_return"] >= -0.03 - 1e-9 for p in plans), plans)         # never wider than 3%; hold <= 3 sessions + 1 for a runner (winners may have been ratcheted ABOVE entry)
            TL.books_balance(self, c)
            original = [r[0] for r in c.execute("SELECT id FROM positions WHERE status='OPEN'")]
            for t in {r[0] for r in c.execute("SELECT ticker FROM positions WHERE status='OPEN'")}:
                tf.co[t]["price"] *= 0.93                                           # -7%: through every swing stop (max 3%)
            cfg = H.cfg()
            cfg["runtime"]["min_available_memory_mb"] = 0
            cfg["strategy"]["swing_enabled"] = True
            with net:
                for k in range(12, 15):
                    now = TF.T0 + dt.timedelta(minutes=5 * k)
                    net.now = now
                    r = pipeline.run_cycle(c, cfg, tf.uni, TF.SRC, pipeline.Deps(sleep=lambda s: None), now)
                    errors += r["errors"]
            self.assertEqual(errors, [])
            still = [r[0] for r in c.execute("SELECT id FROM positions WHERE status='OPEN' AND id IN (%s)" % ",".join("?" * len(original)), original)]
            self.assertEqual(still, [])                                             # every original position was sold
            TL.books_balance(self, c)
        finally:
            tf.tearDown()


if __name__ == "__main__":
    unittest.main()
