"""Build 53: allow the ideas that clear the quality bar but not the full-size bar (expected value under 0.6% or reward:risk under 1), and trade them SMALL. Numbers are worked out by hand."""
import datetime as dt
import json
import os
import re
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
import test_guards as TG  # noqa: E402
from paper_agent import config, dashboard, ev, opportunity, performance, settings as S, settings_ui as U, sizing  # noqa: E402
from paper_agent.timeutil import iso, utcnow, NY  # noqa: E402

DEF = lambda: config.load_config("/nonexistent", with_overrides=False)  # noqa: E731


class TestTheLowEdgeRule(unittest.TestCase):
    def test_each_reason_alone_and_both(self):
        c = DEF()
        both = sizing.low_edge({"ev_net": 0.003, "target_return": 0.02, "stop_return": -0.03}, c)             # EV 0.3% < 0.6%;  2% / 3% = 0.667 < 1
        self.assertEqual(both["cap_pct"], 0.005)
        self.assertEqual(len(both["why"]), 2)
        self.assertIn("0.30%", both["why"][0])
        self.assertIn("0.67 times its stop", both["why"][1])
        ev_only = sizing.low_edge({"ev_net": 0.003, "target_return": 0.045, "stop_return": -0.03}, c)        # 1.5 : 1 is fine
        self.assertEqual(len(ev_only["why"]), 1)
        rr_only = sizing.low_edge({"ev_net": 0.02, "target_return": 0.015, "stop_return": -0.03}, c)         # 0.5 : 1 is not
        self.assertEqual(len(rr_only["why"]), 1)
        self.assertIn("0.50 times", rr_only["why"][0])

    def test_an_ordinary_idea_is_not_low_edge_and_the_boundaries_are_inclusive_of_full_size(self):
        c = DEF()
        self.assertIsNone(sizing.low_edge({"ev_net": 0.01, "target_return": 0.045, "stop_return": -0.03}, c))
        self.assertIsNone(sizing.low_edge({"ev_net": 0.006, "target_return": 0.03, "stop_return": -0.03}, c))   # exactly 0.6% and exactly 1:1 are full size

    def test_switches_and_junk(self):
        c = DEF()
        c["risk"]["low_edge_max_pct"] = 0.0
        self.assertIsNone(sizing.low_edge({"ev_net": 0.001, "target_return": 0.01, "stop_return": -0.03}, c))     # 0 switches it off
        c = DEF()
        c["risk"]["low_edge_max_pct"] = 0.01
        self.assertEqual(sizing.low_edge({"ev_net": 0.001, "target_return": 0.045, "stop_return": -0.03}, c)["cap_pct"], 0.01)
        for f in (None, "x", {}, {"ev_net": "bad"}, {"ev_net": float("nan")}, {"ev_net": 0.001, "target_return": "x"}):
            self.assertIsNone(sizing.low_edge(f, DEF()), f)
        self.assertEqual(sizing.low_edge({"ev_net": 0.001}, None)["cap_pct"], 0.005)                      # no configuration: the shipped defaults apply
        self.assertEqual(len(sizing.low_edge({"ev_net": 0.001, "target_return": 0.0, "stop_return": 0.0}, DEF())["why"]), 1)       # no stop: no ratio to judge


class TestTheOpportunityTestAcceptsASmallPosition(unittest.TestCase):
    def test_free_capacity_equal_to_the_low_edge_minimum_is_enough(self):
        c = H.fresh_db()
        cfg = DEF()
        f = {"horizon_days": 3, "ev_net": 0.004, "sigma_h": 0.02}
        state = {"holdings": [], "equity": 1e6, "cash": 1e6}
        cfg["risk"]["min_position_pct"] = 0.01
        old = opportunity.evaluate(c, f, True, state, cfg, utcnow(), headroom_pct=0.005)                      # 0.5% against a 1% minimum
        self.assertFalse(old["ok"])
        self.assertIn("no capacity", old["reason"])
        new = opportunity.evaluate(c, f, True, state, cfg, utcnow(), headroom_pct=0.005, min_pct=0.005)
        self.assertTrue(new["ok"])
        self.assertFalse(opportunity.evaluate(c, f, True, state, cfg, utcnow(), headroom_pct=0.004, min_pct=0.005)["ok"])


def run_cycle(w, patch=None, news=None):
    news = news or [TG.TL.NVDA_NEWS]
    if patch:
        with patch:
            w.cyc(TG.TL.T_PRE, news)
            w.cyc(TG.TL.T_OPEN)
            w.cyc(TG.TL.T_OPEN + dt.timedelta(minutes=45))
    else:
        w.cyc(TG.TL.T_PRE, news)
        w.cyc(TG.TL.T_OPEN)
        w.cyc(TG.TL.T_OPEN + dt.timedelta(minutes=45))


def forecast_as(ev_net, target=0.045, stop=-0.03):
    real = ev.build

    def wrapped(*a, **k):
        f = real(*a, **k)
        return dict(f, ev_net=ev_net, ev_gross=ev_net + 0.003, target_return=target, stop_return=stop, verdict="BUY" if f["verdict"] in ("BUY", "WATCH") else f["verdict"], why=[])
    return unittest.mock.patch.object(ev, "build", wrapped)


class TestThroughARealCycle(unittest.TestCase):
    def world(self, cap=0.005):
        w = TG.World()
        w.setUp()
        self.addCleanup(w.tearDown)
        w.cfg["risk"]["low_edge_max_pct"] = cap
        w.cfg["risk"]["cash_hurdle_margin"] = 0.001                                                     # the shipped margin (the shared world keeps the old 0.2%)
        return w

    def cost(self, w):
        r = w.one("SELECT cost FROM positions WHERE ticker='NVDA' ORDER BY id LIMIT 1")
        return r[0] if r else None

    def test_a_low_edge_idea_trades_but_never_above_the_cap(self):
        w = self.world()
        run_cycle(w, forecast_as(0.003))
        cost = self.cost(w)
        self.assertIsNotNone(cost, "the idea must trade")
        self.assertLessEqual(cost, 0.005 * 1_000_000 * 1.01)
        d = dict(w.c.execute("SELECT * FROM decisions WHERE ticker='NVDA' AND status='EXECUTED' ORDER BY id LIMIT 1").fetchone())
        rs = json.loads(d["reason"])
        self.assertEqual(rs["low_edge"]["cap_pct"], 0.005)
        self.assertTrue(any("capped at 0.50%" in x for x in rs["sizing"]))

    def test_an_ordinary_idea_is_unaffected_and_is_bigger(self):
        w = self.world()
        run_cycle(w, forecast_as(0.02))
        cost = self.cost(w)
        self.assertIsNotNone(cost)
        self.assertGreater(cost, 0.005 * 1_000_000 * 1.01)
        d = dict(w.c.execute("SELECT * FROM decisions WHERE ticker='NVDA' AND status='EXECUTED' ORDER BY id LIMIT 1").fetchone())
        self.assertNotIn("low_edge", json.loads(d["reason"]))

    def test_a_poor_reward_to_risk_alone_makes_it_small(self):
        w = self.world()
        run_cycle(w, forecast_as(0.02, target=0.015, stop=-0.03))
        self.assertLessEqual(self.cost(w), 0.005 * 1_000_000 * 1.01)

    def test_with_the_cap_switched_off_a_weak_idea_is_rejected_exactly_as_before(self):
        w = self.world(cap=0.0)
        run_cycle(w, forecast_as(0.003))
        self.assertIsNone(self.cost(w))                                                                  # its natural size (about 0.57%) is under the 1% minimum position
        d = dict(w.c.execute("SELECT * FROM decisions WHERE ticker='NVDA' ORDER BY id LIMIT 1").fetchone())
        self.assertEqual(d["status"], "AWAITING_CAPACITY")                                                 # Build 55: a full book is a wait, not a rejection
        self.assertIn("no capacity", d["reason"])
        w2 = self.world(cap=0.005)
        run_cycle(w2, forecast_as(0.003))
        self.assertIsNotNone(self.cost(w2))                                                              # the same idea trades with the cap on

    def test_the_cap_is_whatever_the_owner_sets_and_a_smaller_natural_size_is_kept(self):
        w = self.world(cap=0.012)
        run_cycle(w, forecast_as(0.003))
        cost = self.cost(w)
        self.assertIsNotNone(cost)
        self.assertLessEqual(cost, 0.012 * 1_000_000 * 1.01)                                             # never above the cap
        self.assertLess(cost, 0.0075 * 1_000_000)                                                        # and not inflated up to it: the natural size (about 0.57%) stands
        self.assertGreater(cost, 0.0045 * 1_000_000)                                                     # 2.5% x 0.47 x 0.63 x 0.85 x 0.90 = about 0.57%, above the 0.25% floor


class TestTheCashHurdleMargin(unittest.TestCase):
    def test_the_numbers(self):
        self.assertAlmostEqual(opportunity.cash_hurdle(3), 0.04 * 3 / 252 + 0.002)                              # the old fixed margin is still the code default
        self.assertAlmostEqual(opportunity.cash_hurdle(3, 0.001), 0.04 * 3 / 252 + 0.001)
        self.assertAlmostEqual(opportunity.cash_hurdle(5, 0.0), 0.04 * 5 / 252)

    def test_the_configured_margin_decides_whether_an_idea_beats_cash(self):
        c = H.fresh_db()
        f = {"horizon_days": 3, "ev_net": 0.0020, "sigma_h": 0.02}
        state = {"holdings": [], "equity": 1e6, "cash": 1e6}
        cfg = DEF()
        cfg["risk"]["cash_hurdle_margin"] = 0.002                                                                # hurdle 0.25%: 0.20% fails
        self.assertIn("does not beat cash hurdle", opportunity.evaluate(c, f, True, state, cfg, utcnow(), headroom_pct=0.005, min_pct=0.005)["reason"])
        cfg["risk"]["cash_hurdle_margin"] = 0.001                                                                # hurdle 0.15%: 0.20% passes
        self.assertTrue(opportunity.evaluate(c, f, True, state, cfg, utcnow(), headroom_pct=0.005, min_pct=0.005)["ok"])


class TestTheTradesTodayTile(unittest.TestCase):
    def test_it_counts_today_in_new_york_against_the_target(self):
        c = H.fresh_db()
        cfg = DEF()
        now = dt.datetime(2026, 11, 10, 18, 0, tzinfo=dt.timezone.utc)                                          # 13:00 in New York on the 10th
        for created, status in (("2026-11-10T14:35:00+00:00", "EXECUTED"), ("2026-11-10T15:00:00+00:00", "EXECUTED"), ("2026-11-10T15:10:00+00:00", "REJECTED"),
                                ("2026-11-09T20:00:00+00:00", "EXECUTED"), ("2026-11-10T03:00:00+00:00", "EXECUTED")):                         # 03:00Z is 22:00 on the 9th in New York
            c.execute("INSERT INTO decisions(created,ticker,action,status,reason) VALUES(?,?,?,?,?)", (created, "AAA", "BUY", status, "{}"))
        c.commit()
        self.assertEqual(dashboard.trades_today_text(c, cfg, now), "2 / 30")
        cfg["portfolio"]["daily_trade_target"] = 0
        self.assertEqual(dashboard.trades_today_text(c, cfg, now), "2")
        self.assertEqual(dashboard.trades_today_text(None, cfg, now), "n/a")

    def test_the_overview_shows_the_tile(self):
        c = H.fresh_db()
        page = re.sub(r"<[^>]+>", " ", dashboard.render(c, DEF(), "overview", {}, {"authed": True}))
        self.assertIn("Trades today", page)


class TestTheSizeTierInThePerformanceData(unittest.TestCase):
    def test_closed_trades_carry_their_tier_and_the_page_shows_both(self):
        c = H.fresh_db()
        for i, (reason, pnl) in enumerate((({"low_edge": {"cap_pct": 0.005, "why": ["x"]}}, -20.0), ({}, 300.0), ({"low_edge": {"cap_pct": 0.005, "why": ["y"]}}, 15.0)), 1):
            c.execute("INSERT INTO decisions(created,ticker,action,status,reason,amount) VALUES(?,?,?,?,?,?)", ("2026-11-03T15:00:00+00:00", "T%d" % i, "BUY", "EXECUTED", json.dumps(reason), 5000.0))
            did = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,status,sector,side,closed,exit_price,pnl,review_code,decision_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      ("T%d" % i, "t", "2026-11-03T15:00:00+00:00", 10.0, 500, 5000.0, "CLOSED", "S", "LONG", "2026-11-03T18:00:00+00:00", 10.0, pnl, "TARGET_REACHED", did))
        c.commit()
        tiers = sorted(t["size_tier"] for t in performance.closed_trades(c))
        self.assertEqual(tiers, ["full size", "low-edge", "low-edge"])
        page = re.sub(r"<[^>]+>", " ", dashboard.render(c, DEF(), "performance", {"days": ["all"]}, {"authed": True}))
        self.assertIn("By size tier", page)
        self.assertIn("low-edge", page)


class TestAMalformedDecisionCannotBreakThePerformanceData(unittest.TestCase):
    def test_every_odd_shape_of_the_stored_reason_is_survived(self):
        c = H.fresh_db()
        shapes = [None, "", "[]", "123", "null", "not json", json.dumps({"candidate": "x"}), json.dumps({"candidate": ["a"]}), json.dumps({"candidate": None, "low_edge": {}}), json.dumps({"candidate": {"event_type": 5}, "low_edge": {"cap_pct": 0.005}})]
        for i, rs in enumerate(shapes, 1):
            c.execute("INSERT INTO decisions(created,ticker,action,status,reason,amount) VALUES(?,?,?,?,?,?)", ("2026-11-03T15:00:00+00:00", "T%d" % i, "BUY", "EXECUTED", rs, 5000.0))
            did = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,status,sector,side,closed,exit_price,pnl,review_code,decision_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      ("T%d" % i, "t", "2026-11-03T15:00:00+00:00", 10.0, 500, 5000.0, "CLOSED", "S", "LONG", "2026-11-03T18:00:00+00:00", 10.0, 1.0, "TARGET_REACHED", did))
        c.execute("UPDATE decisions SET amount='not a number' WHERE ticker='T1'")                       # a text value where an amount belongs
        c.commit()
        trades = performance.closed_trades(c)
        self.assertEqual(len(trades), len(shapes))                                                       # none dropped, none fatal
        self.assertEqual(sum(1 for t in trades if t["size_tier"] == "low-edge"), 1)                      # only the one with a real low_edge record
        page = dashboard.render(c, DEF(), "performance", {"days": ["all"]}, {"authed": True})
        self.assertIn("By size tier", page)


class TestSettingsAndDefaults(unittest.TestCase):
    def test_the_shipped_defaults(self):
        d = DEF()
        self.assertEqual(d["risk"]["cash_hurdle_margin"], 0.001)
        self.assertEqual((d["risk"]["min_ev_net"], d["risk"]["full_size_min_ev"], d["risk"]["full_size_min_rr"], d["risk"]["low_edge_max_pct"], d["strategy"]["min_reward_risk"], d["portfolio"]["daily_trade_target"], d["portfolio"]["entry_score"]),
                         (0.0, 0.006, 1.0, 0.005, 0.4, 30, 65))
        self.assertGreater(d["risk"]["low_edge_max_pct"], 0.0)
        self.assertLessEqual(d["risk"]["low_edge_max_pct"], d["risk"]["min_position_pct"] * 1.0001)             # (Build 55 lowered the usual minimum position to 0.5%, level with the low-edge cap)

    def test_the_settings_exist_with_bounds_that_admit_the_defaults_and_the_layout_is_valid(self):
        f = {x.path: x for x in S.FIELDS}
        for p in ("risk.full_size_min_ev", "risk.full_size_min_rr", "risk.low_edge_max_pct", "portfolio.daily_trade_target"):
            self.assertIn(p, f)
        self.assertLessEqual(f["strategy.min_reward_risk"].lo, 0.4)
        self.assertEqual(U.layout_problems(), [])


if __name__ == "__main__":
    unittest.main()
