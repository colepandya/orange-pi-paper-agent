"""Build 50: a 30-minute opening delay, the smaller put trial tier with its limits and breaker, refusal reasons for every approved idea, and the option scorecard. Numbers are worked out by hand."""
import datetime as dt
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
import test_options as TO  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import config, dashboard, db, ev, options as O, settings as S, settings_ui as U  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

T_OPEN = TL.T_OPEN
EQUITY = 1_000_000.0


class Base(TO.Base):
    def setUp(self):
        super().setUp()
        d = config.load_config("/nonexistent", with_overrides=False)["instruments"]               # the shared options world lowers the minimum so its ideas qualify: pin the REAL defaults here
        for k in ("options_min_ev", "options_per_trade_pct", "options_max_premium_pct", "options_max_horizon_days", "options_trial_enabled", "options_trial_min_ev", "options_trial_per_trade_pct", "options_trial_puts_only",
                  "options_trial_breaker_trades", "options_trial_breaker_pct", "options_min_price", "options_min_adv_usd", "options_max_iv_rank"):
            self.cfg["instruments"][k] = d[k]

    def idea(self, ev_net, verdict="SHORT", **c_over):
        c = {"ticker": "NVDA", "event_type": "guidance_raise", "direction": 1.0, "magnitude": 1.0, "conf": 0.9, "credibility": 0.95, "age_hours": 0.5, "horizon": 3, "headline": "NVIDIA raises outlook"}
        c.update(c_over)
        f = ev.build(self.c, c, self.cfg, {"regime": "neutral", "risk_mult": 0.8}, T_OPEN, None)
        f = dict(f, ev_net=ev_net, verdict=verdict, horizon_days=3)
        comp = dict(self.c.execute("SELECT * FROM companies WHERE ticker='NVDA'").fetchone())
        return c, f, comp

    def plan(self, ev_net, verdict="SHORT", why=None, comp_over=None, price=120.0):
        c, f, comp = self.idea(ev_net, verdict)
        comp.update(comp_over or {})
        return O.plan_for(self.c, self.cfg, f, c, comp, price, EQUITY, T_OPEN, why)

    def add_closed(self, pnl, trial=True, kind="PUT"):
        self.c.execute("INSERT INTO option_positions(underlying,kind,strike,expiry,contracts,multiplier,opened,entry_premium,entry_underlying,entry_iv,cost,decision_id,forecast_id,status,closed,exit_premium,exit_underlying,pnl,review_code,note) "
                       "VALUES('NVDA',?,100,'2026-12-18',1,100,?,5.0,100,0.4,500,0,0,'CLOSED',?,5.0,100,?,'X',?)", (kind, iso(T_OPEN), iso(T_OPEN), pnl, "[TRIAL, half size] test" if trial else "a put: test"))
        self.c.commit()


class TestOpeningDelay(unittest.TestCase):
    def test_the_default_is_thirty_minutes_and_the_setting_still_goes_from_zero_to_sixty(self):
        d = config.load_config("/nonexistent", with_overrides=False)
        self.assertEqual(d["risk"]["open_delay_minutes"], 30)
        f = {x.path: x for x in S.FIELDS}["risk.open_delay_minutes"]
        self.assertEqual((f.lo, f.hi), (0, 60))


class TestPutTier(Base):
    def test_a_strong_idea_is_a_normal_option_at_the_normal_size(self):
        p = self.plan(0.05)
        self.assertEqual(p["tier"], "core")
        self.assertFalse(p["why"].startswith("[TRIAL"))
        self.assertLessEqual(p["contracts"] * 100 * p["ask"], 0.012 * EQUITY)
        self.assertGreater(p["contracts"] * 100 * p["ask"], 0.012 * EQUITY - 100 * p["ask"] - 1)          # within one contract of the 1.2% budget

    def test_a_weaker_idea_becomes_a_trial_put_at_half_the_size(self):
        p = self.plan(0.025)
        self.assertEqual(p["tier"], "trial")
        self.assertTrue(p["why"].startswith("[TRIAL, half size] "))
        self.assertEqual(p["kind"], "PUT")
        spent = p["contracts"] * 100 * p["ask"]
        self.assertLessEqual(spent, 0.006 * EQUITY)
        self.assertGreater(spent, 0.006 * EQUITY - 100 * p["ask"] - 1)

    def test_below_the_trial_floor_there_is_no_option_and_the_reason_says_so(self):
        why = []
        self.assertIsNone(self.plan(0.015, why=why))
        self.assertIn("below even the 2.0% the smaller put trial needs", why[0])

    def test_the_trial_is_for_puts_only_unless_the_owner_says_otherwise(self):
        why = []
        self.assertIsNone(self.plan(0.025, verdict="BUY", why=why))
        self.assertIn("puts only", why[0])
        self.cfg["instruments"]["options_trial_puts_only"] = False
        self.assertEqual(self.plan(0.025, verdict="BUY")["tier"], "trial")

    def test_switching_the_trial_off_restores_the_old_rule(self):
        self.cfg["instruments"]["options_trial_enabled"] = False
        why = []
        self.assertIsNone(self.plan(0.025, why=why))
        self.assertIn("below the 3% an option needs", why[0])
        self.assertEqual(self.plan(0.05)["tier"], "core")

    def test_the_trial_stays_within_two_points_of_the_normal_minimum_so_a_raised_minimum_is_respected(self):
        self.cfg["instruments"]["options_min_ev"] = 0.9                                             # an owner who means "no options"
        self.assertIsNone(self.plan(0.5))
        self.cfg["instruments"]["options_min_ev"] = 0.05
        self.assertEqual(self.plan(0.035)["tier"], "trial")                                         # floor is 3%: one point under 5%... two points below the minimum
        self.assertIsNone(self.plan(0.025))                                                          # 2.5% is more than two points under 5%

    def test_the_trial_shares_the_overall_options_cap(self):
        c, f, comp = self.idea(0.025)
        self.c.execute("INSERT INTO option_positions(underlying,kind,strike,expiry,contracts,multiplier,opened,entry_premium,entry_underlying,entry_iv,cost,decision_id,forecast_id,status,theme,sector) VALUES('AMD','PUT',100,'2026-12-18',1,100,?,5,100,0.4,49000,0,0,'OPEN','t','s')", (iso(T_OPEN),))
        self.c.commit()                                                                              # 49,000 of the 50,000 (5%) sleeve is already used
        why = []
        p = O.plan_for(self.c, self.cfg, f, c, comp, 120.0, EQUITY, T_OPEN, why)
        self.assertTrue(p is None or p["contracts"] * 100 * p["ask"] <= 1000 + 1)
        if p is None:
            self.assertIn("does not buy one contract", why[0])


class TestTheBreaker(Base):
    def test_it_waits_for_enough_trades_then_pauses_on_losses_and_recovers(self):
        for _ in range(7):
            self.add_closed(-2000.0)
        self.assertEqual(self.plan(0.025)["tier"], "trial")                                         # only 7 trades: not enough to judge
        self.add_closed(-2000.0)                                                                    # 8 trades, -16,000: more than 0.5% of 1,000,000
        why = []
        self.assertIsNone(self.plan(0.025, why=why))
        self.assertIn("paused", why[0])
        self.assertIn("$16000", why[0])
        self.assertEqual(self.plan(0.05)["tier"], "core")                                           # the normal tier is untouched
        for _ in range(8):
            self.add_closed(+1000.0)                                                                # the last 8 are now all winners
        self.assertEqual(self.plan(0.025)["tier"], "trial")

    def test_a_small_loss_does_not_pause_it(self):
        for _ in range(8):
            self.add_closed(-500.0)                                                                 # -4,000: under the 5,000 limit
        self.assertEqual(self.plan(0.025)["tier"], "trial")

    def test_only_trial_trades_count_toward_the_trial_breaker(self):
        for _ in range(8):
            self.add_closed(-5000.0, trial=False)
        self.assertEqual(self.plan(0.025)["tier"], "trial")


class TestEveryRefusalIsRecorded(Base):
    def test_each_reason_an_approved_idea_did_not_become_an_option_is_stated(self):
        cases = []
        why = []
        self.plan(0.05, why=why, comp_over={"adv_usd": 1e6})
        cases.append(why[0])
        why = []
        self.plan(0.05, why=why, price=10.0)
        cases.append(why[0])
        c, f, comp = self.idea(0.05)
        f["horizon_days"] = 9
        why = []
        O.plan_for(self.c, self.cfg, f, c, comp, 120.0, EQUITY, T_OPEN, why)
        cases.append(why[0])
        for text, needle in zip(cases, ("options need a stock over", "options need a stock over", "longer than the 5")):
            self.assertIn(needle, text)

    def test_an_idea_with_an_open_option_says_so(self):
        self.c.execute("INSERT INTO option_positions(underlying,kind,strike,expiry,contracts,multiplier,opened,entry_premium,entry_underlying,entry_iv,cost,decision_id,forecast_id,status,theme,sector) VALUES('NVDA','PUT',100,'2026-12-18',1,100,?,5,100,0.4,500,0,0,'OPEN','t','s')", (iso(T_OPEN),))
        self.c.commit()
        why = []
        self.assertIsNone(self.plan(0.05, why=why))
        self.assertIn("already open", why[0])

    def test_the_decision_keeps_the_reason_when_the_idea_stays_a_stock_trade(self):
        c, f, comp = self.idea(0.015)
        reason = {}
        r = O.maybe_open(self.c, self.cfg, c, f, {"price": 120.0}, 1, 1, T_OPEN, "AI", comp, "Tech", reason, EQUITY, lambda *a, **k: None)
        self.assertIsNone(r)
        self.assertIn("below even", reason["option_considered"])

    def test_a_refusal_list_is_optional_so_existing_callers_work_unchanged(self):
        self.assertIsNotNone(self.plan(0.05))
        c, f, comp = self.idea(0.001)
        self.assertIsNone(O.plan_for(self.c, self.cfg, f, c, comp, 120.0, EQUITY, T_OPEN))


class TestTheScorecard(Base):
    def test_it_counts_each_kind_and_tier_separately_and_never_overstates_a_few_trades(self):
        self.add_closed(+5322.0, trial=False)                                                         # the real META put
        self.add_closed(-300.0)
        self.add_closed(-200.0)
        sc = {(g["kind"], g["tier"]): g for g in O.scorecard(self.c)}
        core, trial = sc[("PUT", "core")], sc[("PUT", "trial")]
        self.assertEqual((core["closed"], core["wins"], round(core["pnl"])), (1, 1, 5322))
        self.assertEqual((trial["closed"], trial["wins"], round(trial["pnl"])), (2, 0, -500))
        self.assertAlmostEqual(core["return_on_premium"], 5322.0 / 500.0)
        for g in (core, trial):
            self.assertIn("anecdote", g["evidence"])

    def test_the_evidence_label_grows_only_with_the_number_of_closed_trades(self):
        for _ in range(10):
            self.add_closed(10.0, trial=False)
        self.assertEqual(O.scorecard(self.c)[0]["evidence"], "informational")
        for _ in range(20):
            self.add_closed(10.0, trial=False)
        self.assertEqual(O.scorecard(self.c)[0]["evidence"], "preliminary")

    def test_the_overview_shows_it_with_the_warning_that_a_few_trades_prove_nothing(self):
        self.add_closed(+5322.0, trial=False)
        self.add_closed(-300.0)
        page = re.sub(r"<[^>]+>", " ", dashboard.options_section(self.c, T_OPEN))
        for need in ("Option scorecard", "smaller trial", "normal", "Evidence", "does not show that puts win"):
            if need in ("normal",):
                continue
            self.assertIn(need, page)
        self.assertIn("anecdote", page)

    def test_an_empty_book_has_an_empty_scorecard(self):
        self.assertEqual(O.scorecard(self.c), [])


class TestSettings(unittest.TestCase):
    def test_the_trial_settings_exist_with_the_stated_defaults_and_a_valid_layout(self):
        d = config.load_config("/nonexistent", with_overrides=False)["instruments"]
        self.assertEqual((d["options_trial_enabled"], d["options_trial_min_ev"], d["options_trial_per_trade_pct"], d["options_trial_puts_only"], d["options_trial_breaker_trades"], d["options_trial_breaker_pct"]), (True, 0.02, 0.006, True, 8, 0.005))
        paths = {f.path for f in S.FIELDS}
        for k in ("options_trial_enabled", "options_trial_min_ev", "options_trial_per_trade_pct", "options_trial_puts_only", "options_trial_breaker_trades", "options_trial_breaker_pct"):
            self.assertIn("instruments." + k, paths)
        self.assertEqual(U.layout_problems(), [])
        self.assertLess(d["options_trial_per_trade_pct"], d["options_per_trade_pct"])                  # the trial is the smaller bet


if __name__ == "__main__":
    unittest.main()


class TestContractLevelExpectedReturn(Base):
    """option_ev prices the actual contract under the forecast's scenarios, after decay and the exit spread. Properties that must hold whatever the numbers."""

    def draft(self, kind="PUT"):
        p = self.plan(0.05, verdict="SHORT" if kind == "PUT" else "BUY")
        return {k: p[k] for k in ("kind", "strike", "expiry", "sigma", "ask", "commission")}

    def f(self, scen):
        return {"scenarios": scen, "horizon_days": 3}

    def test_a_put_loses_everything_in_a_world_where_the_stock_rises_and_gains_when_it_falls(self):
        d = self.draft()
        up = O.option_ev(self.cfg, self.f({"x": {"p": 1.0, "ret": -0.20}}), d, 120.0, T_OPEN)               # ret is in the position's favour: -20% means the stock ROSE 20%
        down = O.option_ev(self.cfg, self.f({"x": {"p": 1.0, "ret": 0.20}}), d, 120.0, T_OPEN)
        self.assertLess(up, -0.9)
        self.assertGreater(down, 0.5)

    def test_it_is_the_probability_weighted_average_of_its_scenarios(self):
        d = self.draft()
        a = O.option_ev(self.cfg, self.f({"x": {"p": 1.0, "ret": 0.10}}), d, 120.0, T_OPEN)
        b = O.option_ev(self.cfg, self.f({"x": {"p": 1.0, "ret": -0.05}}), d, 120.0, T_OPEN)
        mix = O.option_ev(self.cfg, self.f({"x": {"p": 0.3, "ret": 0.10}, "y": {"p": 0.7, "ret": -0.05}}), d, 120.0, T_OPEN)
        self.assertAlmostEqual(mix, 0.3 * a + 0.7 * b, places=9)

    def test_a_bigger_fall_is_worth_more_and_a_tail_cannot_carry_the_number(self):
        d = self.draft()
        vals = [O.option_ev(self.cfg, self.f({"x": {"p": 1.0, "ret": r}}), d, 120.0, T_OPEN) for r in (0.0, 0.03, 0.08, 0.2, 0.6)]
        self.assertEqual(vals, sorted(vals))
        self.assertLessEqual(vals[-1], 2.0 + 1e-9)                                                          # capped at +200%

    def test_time_passing_with_no_move_costs_the_buyer_the_spread_and_the_decay(self):
        d = self.draft()
        flat = O.option_ev(self.cfg, self.f({"x": {"p": 1.0, "ret": 0.0}}), d, 120.0, T_OPEN)
        self.assertLess(flat, 0.0)                                                                           # no edge, so a loss: the bid is below the ask and a few days of decay have gone

    def test_the_number_matches_an_independent_pricing_of_one_scenario_after_decay_at_the_bid_with_commissions(self):
        import datetime as dt2
        d = self.draft()
        ret, price = 0.05, 120.0
        then = T_OPEN + dt2.timedelta(days=5)                                                              # a 3-session idea: ceil(3 * 7/5) = 5 calendar days
        px = O.price_option("PUT", price * (1 - ret), d["strike"], dt2.date.fromisoformat(d["expiry"]), d["sigma"], then, self.cfg)
        expect = (px["bid"] * 100 - d["commission"]) / (d["ask"] * 100 + d["commission"]) - 1.0
        got = O.option_ev(self.cfg, self.f({"x": {"p": 1.0, "ret": ret}}), d, price, T_OPEN)
        self.assertAlmostEqual(got, expect, places=9)
        self.assertLess(px["bid"], px["ask"])                                                               # so using the ask would give a different number
        asked = (px["ask"] * 100 - d["commission"]) / (d["ask"] * 100 + d["commission"]) - 1.0
        self.assertGreater(abs(asked - got), 1e-4)
        now_px = O.price_option("PUT", price * (1 - ret), d["strike"], dt2.date.fromisoformat(d["expiry"]), d["sigma"], T_OPEN, self.cfg)
        self.assertNotAlmostEqual(now_px["bid"], px["bid"], places=3)                                       # and ignoring the passage of time would too

    def test_scenario_probabilities_that_do_not_sum_to_one_are_normalised(self):
        d = self.draft()
        half = O.option_ev(self.cfg, self.f({"x": {"p": 0.5, "ret": 0.1}}), d, 120.0, T_OPEN)
        full = O.option_ev(self.cfg, self.f({"x": {"p": 1.0, "ret": 0.1}}), d, 120.0, T_OPEN)
        self.assertAlmostEqual(half, full, places=9)

    def test_it_returns_none_rather_than_guessing_when_it_cannot_compute(self):
        d = self.draft()
        self.assertIsNone(O.option_ev(self.cfg, {"scenarios": {}, "horizon_days": 3}, d, 120.0, T_OPEN))
        self.assertIsNone(O.option_ev(self.cfg, {"scenarios": {"x": {"p": "bad", "ret": 0}}, "horizon_days": 3}, d, 120.0, T_OPEN))
        self.assertIsNone(O.option_ev(self.cfg, self.f({"x": {"p": 0.0, "ret": 0.1}}), d, 120.0, T_OPEN))

    def test_a_smaller_tier_is_refused_when_its_contract_would_not_pay(self):
        c, f, comp = self.idea(0.025)
        f = dict(f, scenarios={"x": {"p": 1.0, "ret": 0.0}})
        why = []
        self.assertIsNone(O.plan_for(self.c, self.cfg, f, c, comp, 120.0, EQUITY, T_OPEN, why))
        self.assertIn("this contract's own expected return", why[0])
        self.cfg["instruments"]["options_min_option_ev"] = -1.0                                             # with the requirement lifted it goes through
        self.assertEqual(O.plan_for(self.c, self.cfg, f, c, comp, 120.0, EQUITY, T_OPEN)["tier"], "trial")

    def test_the_normal_tier_is_not_subject_to_it(self):
        c, f, comp = self.idea(0.05)
        f = dict(f, scenarios={"x": {"p": 1.0, "ret": 0.0}})
        self.assertEqual(O.plan_for(self.c, self.cfg, f, c, comp, 120.0, EQUITY, T_OPEN)["tier"], "core")


class TestRescue(Base):
    def f(self, why, verdict="WATCH", direction="SHORT", ev_net=0.012):
        return {"verdict": verdict, "direction": direction, "ev_net": ev_net, "why": why}

    def test_only_stock_shape_reasons_are_rescuable(self):
        ok = ["swing reward:risk 0.48 below minimum 1.00 (target 1.5% vs stop 3.0%)"]
        self.assertTrue(O.rescuable(self.f(ok), self.cfg))
        self.assertTrue(O.rescuable(self.f(["EV net 0.50% below minimum 1.00%"]), self.cfg))
        self.assertTrue(O.rescuable(self.f(ok + ["EV net 0.50% below minimum 1.00%"]), self.cfg))
        for bad in (["EV net -0.40% <= 0.00"], ["confidence 40% below minimum"], ["P(positive) 30% too low"], ["earnings report due Thu 12 Nov, inside the 3-session hold"], ["ADV $1.0M below minimum"],
                    ["too quiet for a swing: it moves 0.3% a day"], ok + ["confidence 40% below minimum"], []):
            self.assertFalse(O.rescuable(self.f(bad), self.cfg), bad)

    def test_it_needs_a_bearish_idea_with_a_positive_edge_and_the_switches_on(self):
        ok = ["swing reward:risk 0.48 below minimum 1.00 (target 1.5% vs stop 3.0%)"]
        self.assertFalse(O.rescuable(self.f(ok, direction="LONG"), self.cfg))
        self.assertFalse(O.rescuable(self.f(ok, verdict="BUY"), self.cfg))
        self.assertFalse(O.rescuable(self.f(ok, ev_net=0.0), self.cfg))
        self.cfg["instruments"]["options_rescue_enabled"] = False
        self.assertFalse(O.rescuable(self.f(ok), self.cfg))
        self.cfg["instruments"]["options_rescue_enabled"] = True
        self.cfg["instruments"]["options_enabled"] = False
        self.assertFalse(O.rescuable(self.f(ok), self.cfg))

    def test_a_rescued_idea_is_a_half_size_put_labelled_as_rescued_and_skips_the_stock_edge_minimum(self):
        c, f, comp = self.idea(0.012)
        f = dict(f, verdict="SHORT", rescue=True)
        p = O.plan_for(self.c, self.cfg, f, c, comp, 120.0, EQUITY, T_OPEN)
        self.assertEqual((p["tier"], p["kind"]), ("rescue", "PUT"))
        self.assertTrue(p["why"].startswith("[RESCUE, half size] "))
        self.assertLessEqual(p["contracts"] * 100 * p["ask"], 0.006 * EQUITY)
        self.assertIsNotNone(p["option_ev"])
        self.assertGreaterEqual(p["option_ev"], 0.10)

    def test_a_rescued_idea_still_has_to_pay_on_its_own_contract_and_never_on_a_non_positive_edge(self):
        c, f, comp = self.idea(0.012)
        why = []
        flat = dict(f, verdict="SHORT", rescue=True, scenarios={"x": {"p": 1.0, "ret": 0.0}})
        self.assertIsNone(O.plan_for(self.c, self.cfg, flat, c, comp, 120.0, EQUITY, T_OPEN, why))
        self.assertIn("own expected return", why[0])
        why = []
        self.assertIsNone(O.plan_for(self.c, self.cfg, dict(f, verdict="SHORT", rescue=True, ev_net=-0.01), c, comp, 120.0, EQUITY, T_OPEN, why))
        self.assertIn("not positive", why[0])

    def test_the_breaker_covers_rescued_trades_too(self):
        for _ in range(8):
            self.c.execute("INSERT INTO option_positions(underlying,kind,strike,expiry,contracts,multiplier,opened,entry_premium,entry_underlying,entry_iv,cost,decision_id,forecast_id,status,closed,exit_premium,exit_underlying,pnl,review_code,note) "
                           "VALUES('NVDA','PUT',100,'2026-12-18',1,100,?,5,100,0.4,500,0,0,'CLOSED',?,5,100,-2000,'X','[RESCUE, half size] x')", (iso(T_OPEN), iso(T_OPEN)))
        self.c.commit()
        c, f, comp = self.idea(0.012)
        why = []
        self.assertIsNone(O.plan_for(self.c, self.cfg, dict(f, verdict="SHORT", rescue=True), c, comp, 120.0, EQUITY, T_OPEN, why))
        self.assertIn("paused", why[0])

    def test_the_scorecard_keeps_rescued_trades_apart_from_the_others(self):
        self.c.execute("INSERT INTO option_positions(underlying,kind,strike,expiry,contracts,multiplier,opened,entry_premium,entry_underlying,entry_iv,cost,decision_id,forecast_id,status,closed,exit_premium,exit_underlying,pnl,review_code,note) "
                       "VALUES('NVDA','PUT',100,'2026-12-18',1,100,?,5,100,0.4,500,0,0,'CLOSED',?,5,100,250,'X','[RESCUE, half size] x')", (iso(T_OPEN), iso(T_OPEN)))
        self.c.commit()
        self.assertEqual([(g["tier"], g["closed"]) for g in O.scorecard(self.c)], [("rescue", 1)])

    def test_the_new_defaults_and_settings(self):
        d = config.load_config("/nonexistent", with_overrides=False)["instruments"]
        self.assertEqual((d["options_rescue_enabled"], d["options_rescue_per_trade_pct"], d["options_min_option_ev"], d["options_min_adv_usd"]), (True, 0.006, 0.10, 50_000_000))
        paths = {f.path for f in S.FIELDS}
        for k in ("options_rescue_enabled", "options_rescue_per_trade_pct", "options_min_option_ev"):
            self.assertIn("instruments." + k, paths)
        self.assertEqual(U.layout_problems(), [])


class TestRescueThroughARealCycle(TO.Base):
    BEAR = TO.BEAR

    def setUp(self):
        super().setUp()
        self.cfg["risk"]["min_ev_net"] = 0.5                                                       # the STOCK rules now reject every idea for "EV net below minimum"
        self.cfg["instruments"]["options_min_ev"] = 0.9                                            # and the normal option tier is out of reach
        self.cfg["instruments"]["options_trial_enabled"] = False

    def run_it(self, rescue):
        self.cfg["instruments"]["options_rescue_enabled"] = rescue
        self.cycle_with(TL.T_PRE, self.BEAR)
        self.cycle_with(TL.T_OPEN)
        d = self.c.execute("SELECT * FROM decisions WHERE ticker='NVDA' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(d)

    def test_without_the_rescue_path_the_stock_rules_keep_the_idea_out(self):
        d = self.run_it(False)
        self.assertEqual((d["status"], d["action"]), ("REJECTED", "NO_TRADE"))
        self.assertIn("below minimum", d["reason"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM option_positions")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)

    def test_with_the_rescue_path_the_same_idea_becomes_a_half_size_put_and_never_a_stock_short(self):
        d = self.run_it(True)
        import json
        rs = json.loads(d["reason"])
        self.assertEqual((d["status"], d["action"]), ("EXECUTED", "OPTION_PUT"))
        self.assertEqual(rs["rescue"]["stock_verdict"], "WATCH")
        self.assertTrue(any("below minimum" in w for w in rs["rescue"]["stock_blocked_by"]))
        o = dict(self.one("SELECT * FROM option_positions"))
        self.assertTrue(o["note"].startswith("[RESCUE, half size] "))
        self.assertLessEqual(o["cost"], 0.006 * 1_000_000 + 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)                            # an option, not shares sold short
        self.assertEqual(self.c.execute("SELECT verdict FROM forecasts WHERE decision_id=?", (d["id"],)).fetchone()[0], "WATCH")      # the audit trail keeps what the stock rules said
        self.options_books()

    def test_a_rescued_idea_that_finds_no_qualifying_put_is_rejected_and_says_why(self):
        self.cfg["instruments"]["options_min_option_ev"] = 5.0                                       # no contract can promise a 500% return
        d = self.run_it(True)
        self.assertEqual((d["status"], d["action"]), ("REJECTED", "NO_TRADE"))
        self.assertIn("no put qualified", d["reason"])
        self.assertIn("own expected return", d["reason"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM option_positions")[0], 0)

    def test_a_bullish_idea_is_never_rescued(self):
        self.cfg["instruments"]["options_rescue_enabled"] = True
        self.cycle_with(TL.T_PRE, TL.NVDA_NEWS)
        self.cycle_with(TL.T_OPEN)
        self.assertEqual(self.one("SELECT COUNT(*) FROM option_positions")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)


class TestNothingMalformedCanBreakADecisionOrAPage(Base):
    def test_rescuable_and_option_ev_never_raise_on_junk(self):
        for f in (None, "x", [], {}, {"verdict": "WATCH", "direction": "SHORT", "ev_net": "bad", "why": ["swing reward:risk 0.4 below minimum 1.0"]},
                  {"verdict": "WATCH", "direction": "SHORT", "ev_net": float("nan"), "why": "swing reward:risk"}, {"verdict": "WATCH", "direction": "SHORT", "ev_net": 0.01, "why": [None, 3]}):
            self.assertFalse(O.rescuable(f, self.cfg), f)
        self.assertFalse(O.rescuable({"verdict": "WATCH"}, None))
        self.assertFalse(O.rescuable({"verdict": "WATCH", "direction": "SHORT", "ev_net": 0.01, "why": ["EV net 0.5% below minimum 1.0%"]}, {"instruments": None}))
        d = {"kind": "PUT", "strike": 100.0, "expiry": "2026-12-18", "sigma": 0.4, "ask": 5.0, "commission": 0.65}
        ok = {"scenarios": {"a": {"p": 1, "ret": 0.1}}}
        for f, plan in ((None, d), ({}, d), ({"scenarios": "x"}, d), ({"scenarios": {"a": None}}, d), ({"scenarios": {"a": {"p": 1, "ret": "x"}}}, d), (ok, None), (ok, {}), (ok, dict(d, expiry="garbage")), (ok, dict(d, ask="x"))):
            self.assertIsNone(O.option_ev(self.cfg, f, plan, 120.0, T_OPEN), (f, plan))                  # structurally malformed: unknown, never an error
        for plan, price in ((dict(d, sigma=0.0), 120.0), (dict(d, strike=0), 120.0), (d, 0.0), (d, float("nan")), (d, 1e18)):
            r = O.option_ev(self.cfg, ok, plan, price, T_OPEN)                                             # odd numbers: never raises; a number or None
            self.assertTrue(r is None or isinstance(r, float))

    def test_one_malformed_option_record_cannot_take_the_overview_down(self):
        self.c.execute("INSERT INTO option_positions(underlying,kind,strike,expiry,contracts,multiplier,opened,entry_premium,entry_underlying,entry_iv,cost,decision_id,forecast_id,status,theme,sector) VALUES('NVDA','PUT',100,'2026-12-18',1,100,?,5,100,0.4,NULL,0,0,'OPEN','t','s')", (iso(T_OPEN),))
        self.c.commit()
        page = re.sub(r"<[^>]+>", " ", dashboard.options_section(self.c, T_OPEN))
        self.assertIn("could not be displayed", page)
        full = dashboard.render(self.c, self.cfg, "overview", {}, {"authed": True})
        self.assertIn("Open positions", full)                                                            # the rest of the page is intact

    def test_a_well_formed_book_is_still_displayed_normally(self):
        self.add_closed(+100.0, trial=False)
        page = re.sub(r"<[^>]+>", " ", dashboard.options_section(self.c, T_OPEN))
        self.assertNotIn("could not be displayed", page)
        self.assertIn("Option scorecard", page)
