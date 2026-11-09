"""Build 51: reversal puts (the model's own forecast of a fall after bullish news) and the shadow book's scoring of the model's own forecast direction. Numbers are worked out by hand."""
import datetime as dt
import json
import os
import re
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
import test_build49 as T49  # noqa: E402
import test_build50 as T50  # noqa: E402
import test_options as TO  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import config, dashboard, ev, monitor, options as O, settings as S, settings_ui as U, shadow  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

EQUITY = T50.EQUITY
T_OPEN = TL.T_OPEN


def neg_long(ev_net=-0.082, ret=-0.09):
    return {"verdict": "REJECT_NEGATIVE_EV", "direction": "LONG", "ev_net": ev_net, "scenarios": {"x": {"p": 1.0, "ret": ret}}, "why": ["EV net %.2f%% <= 0.00%%" % (ev_net * 100)]}


class TestReversalView(unittest.TestCase):
    def cfg(self):
        c = config.load_config("/nonexistent", with_overrides=False)
        c["instruments"]["options_reversal_enabled"] = True                                          # (it ships OFF: the one-week evidence is confounded by a falling market)
        c["instruments"]["options_reversal_min_move"] = 0.04
        return c

    def test_the_numbers(self):
        v = O.reversal_view(neg_long(), self.cfg())
        cost = 2 * 8 / 1e4 + 0.0005                                                                 # 0.0021 a leg-pair
        self.assertAlmostEqual(v["ev_net"], (0.082 - 2 * cost) * 0.5)                               # (8.2% fall - two round trips) at half belief = 3.89%
        self.assertAlmostEqual(v["scenarios"]["x"]["ret"], 0.045)                                   # a 9% fall in the long's scenario, believed at half, is a +4.5% for the put side
        self.assertEqual((v["verdict"], v["direction"], v["rescue"], v["reversal"]), ("SHORT", "SHORT", True, True))

    def test_only_a_large_enough_forecast_fall_on_a_bullish_idea_qualifies(self):
        c = self.cfg()
        self.assertIsNone(O.reversal_view(neg_long(-0.03, -0.04), c))                               # 3% - costs is under the 4% needed
        self.assertIsNotNone(O.reversal_view(neg_long(-0.05, -0.06), c))
        self.assertIsNone(O.reversal_view(dict(neg_long(), direction="SHORT"), c))                  # a bearish idea predicted to RISE is not this feature
        self.assertIsNone(O.reversal_view(dict(neg_long(), verdict="WATCH"), c))
        self.assertIsNone(O.reversal_view(dict(neg_long(), verdict="BUY"), c))

    def test_it_ships_off_and_does_nothing_until_switched_on(self):
        shipped = config.load_config("/nonexistent", with_overrides=False)
        self.assertIsNone(O.reversal_view(neg_long(), shipped))
        shipped["instruments"]["options_reversal_enabled"] = True
        self.assertIsNone(O.reversal_view(neg_long(-0.05, -0.06), shipped))                         # and the shipped threshold is 8%, where the (weak) evidence is
        self.assertIsNone(O.reversal_view(neg_long(-0.082), shipped))                               # 8.2% less two round trips of cost is 7.8%: under 8%
        self.assertIsNotNone(O.reversal_view(neg_long(-0.09, -0.10), shipped))                       # 9% less costs is 8.6%

    def test_switches_and_haircut(self):
        c = self.cfg()
        c["instruments"]["options_reversal_enabled"] = False
        self.assertIsNone(O.reversal_view(neg_long(), c))
        c = self.cfg()
        c["instruments"]["options_enabled"] = False
        self.assertIsNone(O.reversal_view(neg_long(), c))
        c = self.cfg()
        c["instruments"]["options_reversal_haircut"] = 1.0
        self.assertAlmostEqual(O.reversal_view(neg_long(), c)["scenarios"]["x"]["ret"], 0.09)
        c["instruments"]["options_reversal_haircut"] = 0.0
        self.assertIsNone(O.reversal_view(neg_long(), c))

    def test_junk_never_raises(self):
        c = self.cfg()
        for f in (None, "x", [], {}, dict(neg_long(), ev_net="bad"), dict(neg_long(), ev_net=float("nan")), dict(neg_long(), scenarios="x"), dict(neg_long(), scenarios={}), dict(neg_long(), scenarios={"a": {"p": "x", "ret": 0}}), dict(neg_long(), scenarios={"a": None})):
            self.assertIsNone(O.reversal_view(f, c), f)
        self.assertIsNone(O.reversal_view(neg_long(), None))
        self.assertIsNone(O.reversal_view(neg_long(), {"instruments": None}))
        self.assertIsNone(O.reversal_view(neg_long(), dict(self.cfg(), portfolio={"slippage_bps": "x"})))


class TestReversalTier(T50.Base):
    def view(self, ev_net=0.012):
        c, f, comp = self.idea(ev_net)
        scen = {k: {"p": v["p"], "ret": v["ret"]} for k, v in f["scenarios"].items()}
        return c, dict(f, verdict="SHORT", rescue=True, reversal=True, scenarios=scen), comp

    def test_a_reversal_put_is_half_size_labelled_and_pays_on_its_own_contract(self):
        c, f, comp = self.view()
        p = O.plan_for(self.c, self.cfg, f, c, comp, 120.0, EQUITY, T_OPEN)
        self.assertEqual((p["tier"], p["kind"]), ("reversal", "PUT"))
        self.assertTrue(p["why"].startswith("[REVERSAL, half size] "))
        self.assertLessEqual(p["contracts"] * 100 * p["ask"], 0.006 * EQUITY)
        self.assertGreaterEqual(p["option_ev"], 0.10)

    def test_the_cooldown_after_a_reversal_put_closes(self):
        c, f, comp = self.view()
        self.c.execute("INSERT INTO option_positions(underlying,kind,strike,expiry,contracts,multiplier,opened,entry_premium,entry_underlying,entry_iv,cost,decision_id,forecast_id,status,closed,exit_premium,exit_underlying,pnl,review_code,note) "
                       "VALUES('NVDA','PUT',100,'2026-12-18',1,100,?,5,100,0.4,500,0,0,'CLOSED',?,5,100,-50,'X','[REVERSAL, half size] x')", (iso(T_OPEN - dt.timedelta(hours=30)), iso(T_OPEN - dt.timedelta(hours=10))))
        self.c.commit()
        why = []
        self.assertIsNone(O.plan_for(self.c, self.cfg, f, c, comp, 120.0, EQUITY, T_OPEN, why))
        self.assertIn("closed less than 24 hours ago", why[0])
        self.cfg["instruments"]["options_reversal_cooldown_hours"] = 5
        self.assertEqual(O.plan_for(self.c, self.cfg, f, c, comp, 120.0, EQUITY, T_OPEN)["tier"], "reversal")        # closed 10 hours ago: past a 5-hour cooldown
        self.cfg["instruments"]["options_reversal_cooldown_hours"] = 0
        self.assertEqual(O.plan_for(self.c, self.cfg, f, c, comp, 120.0, EQUITY, T_OPEN)["tier"], "reversal")

    def test_the_breaker_and_the_scorecard_include_reversal_trades(self):
        for _ in range(8):
            self.c.execute("INSERT INTO option_positions(underlying,kind,strike,expiry,contracts,multiplier,opened,entry_premium,entry_underlying,entry_iv,cost,decision_id,forecast_id,status,closed,exit_premium,exit_underlying,pnl,review_code,note) "
                           "VALUES('AMD','PUT',100,'2026-12-18',1,100,?,5,100,0.4,500,0,0,'CLOSED',?,5,100,-2000,'X','[REVERSAL, half size] x')", (iso(T_OPEN), iso(T_OPEN)))
        self.c.commit()
        c, f, comp = self.view()
        why = []
        self.assertIsNone(O.plan_for(self.c, self.cfg, f, c, comp, 120.0, EQUITY, T_OPEN, why))
        self.assertIn("paused", why[0])
        self.assertEqual([(g["tier"], g["closed"]) for g in O.scorecard(self.c)], [("reversal", 8)])

    def test_the_dashboard_names_the_tier(self):
        self.add_closed(-100.0)
        self.c.execute("UPDATE option_positions SET note='[REVERSAL, half size] x'")
        self.c.commit()
        self.assertIn("reversal: the model forecast a fall", re.sub(r"<[^>]+>", " ", dashboard.options_section(self.c, T_OPEN)))

    def test_new_settings(self):
        d = config.load_config("/nonexistent", with_overrides=False)["instruments"]
        self.assertEqual((d["options_reversal_enabled"], d["options_reversal_min_move"], d["options_reversal_haircut"], d["options_reversal_per_trade_pct"], d["options_reversal_cooldown_hours"]), (False, 0.08, 0.5, 0.006, 24))
        paths = {f.path for f in S.FIELDS}
        for k in ("enabled", "min_move", "haircut", "per_trade_pct", "cooldown_hours"):
            self.assertIn("instruments.options_reversal_" + k, paths)
        self.assertEqual(U.layout_problems(), [])


class TestReversalThroughARealCycle(TO.Base):
    """The model forecasts a fall after bullish news: the stock rules say 'buying loses money' and stop; with the feature on the same forecast becomes a half-size put."""

    def setUp(self):
        super().setUp()
        real = ev.build

        def forecast_a_fall(*a, **k):
            f = real(*a, **k)
            scen = {key: {"p": v["p"], "ret": -0.09 + (0.0 if key == "base" else (0.03 if key == "bull" else -0.03 if key == "bear" else -0.1 if key == "tail" else -0.2))} for key, v in f["scenarios"].items()}
            ev_gross = sum(v["p"] * v["ret"] for v in scen.values())
            return dict(f, verdict="REJECT_NEGATIVE_EV", direction="LONG", ev_gross=ev_gross, ev_net=ev_gross - 0.0021, scenarios=scen, why=["EV net %.2f%% <= 0.00%%" % ((ev_gross - 0.0021) * 100)])
        self.patch = unittest.mock.patch.object(ev, "build", forecast_a_fall)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.cfg["instruments"]["options_min_ev"] = 0.9
        self.cfg["instruments"]["options_reversal_min_move"] = 0.04

    def run_it(self, enabled):
        self.cfg["instruments"]["options_reversal_enabled"] = enabled
        self.cycle_with(TL.T_PRE, TL.NVDA_NEWS)                                                        # BULLISH news
        self.cycle_with(TL.T_OPEN)
        return dict(self.c.execute("SELECT * FROM decisions WHERE ticker='NVDA' ORDER BY id DESC LIMIT 1").fetchone())

    def test_off_the_bot_just_passes_as_it_always_did(self):
        d = self.run_it(False)
        self.assertEqual((d["status"], d["action"]), ("REJECTED", "NO_TRADE"))
        self.assertEqual(self.one("SELECT COUNT(*) FROM option_positions")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)

    def test_on_the_models_forecast_fall_becomes_a_half_size_put_never_a_stock_short(self):
        d = self.run_it(True)
        rs = json.loads(d["reason"])
        self.assertEqual((d["status"], d["action"]), ("EXECUTED", "OPTION_PUT"))
        self.assertGreater(rs["reversal"]["model_expects_the_stock_to_fall_pct"], 4.0)
        o = dict(self.one("SELECT * FROM option_positions"))
        self.assertTrue(o["note"].startswith("[REVERSAL, half size] "))
        self.assertLessEqual(o["cost"], 0.006 * 1_000_000 + 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)
        self.assertEqual(self.c.execute("SELECT verdict FROM forecasts WHERE decision_id=?", (d["id"],)).fetchone()[0], "REJECT_NEGATIVE_EV")      # the audit trail keeps the forecast as it was
        self.options_books()

    def test_a_monitor_conflict_does_not_block_an_option_the_monitor_does_not_manage(self):
        with unittest.mock.patch.object(monitor, "entry_check", lambda *a, **k: {"would_exit": True, "why": "victim of a strong thesis", "health": 10.0, "state": "x", "signals": []}):
            d = self.run_it(True)
        self.assertEqual(d["status"], "EXECUTED")
        self.assertIn("monitor_check_ignored", d["reason"])

    def test_with_no_qualifying_contract_it_is_rejected_with_the_reason_and_never_shorted(self):
        self.cfg["instruments"]["options_min_option_ev"] = 5.0
        d = self.run_it(True)
        self.assertEqual((d["status"], d["action"]), ("REJECTED", "NO_TRADE"))
        self.assertIn("no put qualified", d["reason"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)


class TestTheModelForecastScoredInItsOwnDirection(unittest.TestCase):
    """Hand-worked: two stocks, both bullish news with a forecast of a big fall."""

    def setUp(self):
        T49.BARS["TK2"] = {"2026-10-20": (100, 101, 94, 96), "2026-10-21": (96, 97, 93, 94), "2026-10-22": (94, 95, 93, 95), "2026-10-23": (95, 96, 94, 95), "2026-10-26": (95, 96, 94, 95)}
        self.addCleanup(lambda: T49.BARS.pop("TK2", None))

    def world(self, specs):
        c, cfg = T49.world([{"at": "2026-10-20T12:00:00+00:00", "tk": tk, "dir": 0.6} for tk, _fd, _ev in specs])
        for i, (tk, fdir, fev) in enumerate(specs, 1):
            c.execute("INSERT INTO forecasts(created,ticker,decision_id,direction,horizon_days,p_up,ev_gross,ev_net,risk_score,confidence,target_return,stop_return,regime,verdict,scenarios_json,inputs_json,hash) VALUES(?,?,?,?,3,0.5,0,?,10,0.7,0,-0.03,'x','REJECT_NEGATIVE_EV','{}','{}','h%d')" % i, ("2026-10-20T12:00:00+00:00", tk, i, fdir, fev))
        c.commit()
        return c, cfg

    def test_the_arithmetic(self):
        c, cfg = self.world([("TKR", "LONG", -0.09), ("TK2", "LONG", -0.09)])
        m = shadow.model_report(c, cfg, 3)
        g = {(x["band"], x["predicted"]): x for x in m["groups"]}["|EV| 8% or more", "FALL"]
        # TKR: entry 100 -> close 22 Oct 99 = -1%; SPY 400 -> 396 = -1%:   predicted fall right by 0% vs market, minus 0.3% cost = -0.3%
        # TK2: entry 100 -> close 22 Oct 95 = -5%; against SPY -1%:       right by 4%, minus 0.3% = +3.7%
        self.assertEqual(g["observations"], 2)
        self.assertAlmostEqual(g["mean_pct"], (-0.3 + 3.7) / 2, places=3)
        self.assertEqual(g["hit_rate"], 0.5)
        self.assertAlmostEqual(g["mean_predicted_move_pct"], 9.0)

    def test_a_forecast_of_a_rise_is_scored_the_other_way_and_bands_are_separate(self):
        c, cfg = self.world([("TKR", "SHORT", -0.05), ("TK2", "LONG", 0.02)])
        g = {(x["band"], x["predicted"]): x for x in shadow.model_report(c, cfg, 3)["groups"]}
        rise = g["|EV| 4% to 8%", "RISE"]                                                          # a SHORT with negative EV: forecast to go UP; TKR fell 1% vs market -1%: wrong by 0, minus cost
        self.assertAlmostEqual(rise["mean_pct"], -0.3, places=3)
        small = g["|EV| 1% to 4%", "RISE"]                                                         # a LONG with +2% EV: forecast to rise; TK2 fell 5% vs market -1%: wrong by 4%
        self.assertAlmostEqual(small["mean_pct"], -4.3, places=3)

    def test_two_days_of_one_stock_in_one_week_are_one_observation(self):
        c, cfg = self.world([("TK2", "LONG", -0.09), ("TK2", "LONG", -0.10)])
        g = shadow.model_report(c, cfg, 3)["groups"]
        self.assertEqual(sum(x["observations"] for x in g), 1)

    def test_different_days_of_the_same_week_for_one_stock_are_still_one_observation(self):
        c, cfg = T49.world([{"at": "2026-10-20T12:00:00+00:00", "tk": "TK2", "dir": 0.6}, {"at": "2026-10-21T12:00:00+00:00", "tk": "TK2", "dir": 0.6}])      # Tuesday and Wednesday: overlapping 3-session windows
        for i in (1, 2):
            c.execute("INSERT INTO forecasts(created,ticker,decision_id,direction,horizon_days,p_up,ev_gross,ev_net,risk_score,confidence,target_return,stop_return,regime,verdict,scenarios_json,inputs_json,hash) VALUES(?,?,?,?,3,0.5,0,?,10,0.7,0,-0.03,'x','REJECT_NEGATIVE_EV','{}','{}','w%d')" % i, ("2026-10-20T12:00:00+00:00", "TK2", i, "LONG", -0.09))
        c.commit()
        self.assertEqual(c.execute("SELECT COUNT(DISTINCT entry_day) FROM shadow_candidates WHERE entry_day IS NOT NULL").fetchone()[0], 2)           # two real entry days
        self.assertEqual(sum(x["observations"] for x in shadow.model_report(c, cfg, 3)["groups"]), 1)                                                   # one observation

    def test_it_appears_in_the_report_text_and_the_dashboard_and_is_empty_without_forecasts(self):
        c, cfg = self.world([("TKR", "LONG", -0.09), ("TK2", "LONG", -0.09)])
        text = shadow.format_report(shadow.report(c, cfg))
        self.assertIn("DOES THE MODEL'S OWN FORECAST OF A BIG MOVE COME TRUE", text)
        page = re.sub(r"<[^>]+>", " ", dashboard.shadow_section(c, cfg))
        self.assertIn("Does the model's own forecast of a big move come true", page)
        e = H.fresh_db()
        self.assertEqual(shadow.model_report(e, H.cfg(), 3).get("groups", []), [])

    def test_junk_rows_are_skipped_not_fatal(self):
        c, cfg = self.world([("TKR", "LONG", -0.09), ("TK2", "LONG", -0.09)])
        c.execute("UPDATE forecasts SET ev_net='not a number' WHERE decision_id=1")
        c.commit()
        m = shadow.model_report(c, cfg, 3)
        self.assertEqual(sum(g["observations"] for g in m["groups"]), 1)                              # the bad row is skipped, the good one counted


if __name__ == "__main__":
    unittest.main()
