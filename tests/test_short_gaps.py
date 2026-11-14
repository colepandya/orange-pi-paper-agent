"""Gaps found by mutation testing: each test here exists because deliberately breaking one short-selling rule went unnoticed until it was written."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_shorts as TS  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import db, ev, executor, monitor, sizing, trader  # noqa: E402
from paper_agent.timeutil import add_trading_days, iso  # noqa: E402

T_PRE, T_OPEN = TL.T_PRE, TL.T_OPEN


class TestCoveringCostsMore(TS.ShortBase):
    def test_a_short_is_covered_ABOVE_the_quote_and_a_long_is_sold_BELOW_it(self):
        plan = self.open_short()
        self.mkt.price["NVDA"] = plan["stop_price"] * 1.01
        self.guard(T_OPEN + dt.timedelta(minutes=20))
        a = self.one("SELECT fill_price,observed_price FROM virtual_exit_actions")
        self.assertGreater(a["fill_price"], a["observed_price"])                                     # buying back pays up
        self.assertAlmostEqual(a["fill_price"] / a["observed_price"] - 1, self.cfg["portfolio"]["slippage_bps"] / 1e4, places=6)


class TestTheReforecastReadsAShortsReturnCorrectly(TS.ShortBase):
    def test_a_fall_is_a_gain_for_the_reforecast(self):
        plan = self.open_short()
        p = self.one("SELECT * FROM positions")
        q = {"price": plan["target_price"] * 0.99, "status": "PASS"}
        with db.tx(self.c):
            r = executor.rapid_reforecast(self.c, p, plan, q, 0, T_OPEN, self.cfg)
        row = self.one("SELECT observed_return,velocity_ratio,decision FROM target_reforecasts")
        self.assertGreater(row["observed_return"], 0.02)                                              # the short is UP, not down
        self.assertGreaterEqual(row["velocity_ratio"], 1.0)
        if r["hold"]:
            self.assertLess(self.one("SELECT target_price FROM position_forecast_plans WHERE active=1")[0], p["entry_price"])


class TestTheRunnerOfAShortIsOnTheRightSide(TS.ShortBase):
    def test_after_the_partial_cover_the_runner_has_its_stop_below_the_entry_and_its_target_further_down(self):
        self.cfg["strategy"]["swing_enabled"] = True                                                  # swing mode banks part at the target and runs the rest
        plan = self.open_short()
        p = self.one("SELECT * FROM positions")
        when = add_trading_days(T_OPEN, 2)
        self.mkt.price["NVDA"] = plan["target_price"] * 0.995
        self.cycle(when)
        self.guard(when + dt.timedelta(minutes=20))
        q = self.one("SELECT * FROM positions")
        self.assertEqual((q["status"], q["review_code"]), ("OPEN", "RUNNER_OPEN"), "setup: a runner should be left")
        rp = self.one("SELECT * FROM position_forecast_plans WHERE active=1")
        self.assertLess(rp["target_price"], p["entry_price"])                                         # still a short: the target is BELOW
        self.assertLess(rp["stop_price"], p["entry_price"] * 1.001)                                   # protected: at or under break-even, never above it
        self.assertLess(rp["runner_target_price"], p["entry_price"])
        TS.short_books_balance(self, self.c)


class TestTheCollateralIsAlsoCheckedAtTheMoneyMoves(TS.ShortBase):
    """Sizing normally keeps a short within its collateral; the entry re-checks it as a second line, because sizing and entry are different moments."""

    def huge(self, amount):
        orig = sizing.size_position
        sizing.size_position = lambda *a, **k: {"pct": 0.9, "amount": amount, "reasons": [], "binding": "x", "ok": True}
        return orig

    def test_a_short_whose_collateral_cannot_be_found_is_deferred(self):
        orig = self.huge(1_800_000.0)
        try:
            self.cycle(T_PRE, news=True)
            self.cycle(T_OPEN)
        finally:
            sizing.size_position = orig
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)
        self.assertEqual(self.one("SELECT status FROM decisions WHERE ticker='NVDA'")[0], "CAPACITY_DEFERRED")

    def test_a_long_cannot_spend_money_that_is_reserved_against_a_short(self):
        self.open_short()
        self.mkt.price["LMT"] = 450.0
        self.prev_close["LMT"] = 450.0
        orig = self.huge(850_000.0)
        try:
            c = {"ticker": "LMT", "event_type": "guidance_raise", "direction": 1.0, "magnitude": 1.0, "conf": 0.9, "credibility": 0.95, "age_hours": 0.5, "horizon": 6, "origin": "event", "headline": "Lockheed raises its outlook", "source": "Wire"}
            q = lambda conn, t, purpose: {"status": "PASS", "execution_eligible": True, "price": 450.0, "providers_used": ["X"], "quoted_at": iso(T_OPEN), "age_seconds": 3}      # noqa: E731
            r = trader.process_candidate(self.c, self.cfg, H.universe(), c, {"regime": "neutral", "risk_mult": 0.8}, T_OPEN, q, self.series)
        finally:
            sizing.size_position = orig
        self.assertEqual(r["status"], "CAPACITY_DEFERRED")                                           # the cash is there, but 150% of the short is not free to spend


class TestTheEdgeIsExactlyTheClippedSum(unittest.TestCase):
    def test_the_formula_with_different_gap_allowances(self):
        c, cfg = H.fresh_db(), H.cfg()
        cfg["instruments"]["shorts_enabled"] = True
        c.execute("INSERT OR REPLACE INTO companies(ticker,name,sector,market_cap,adv_usd,vol_daily,completeness,updated) VALUES('NVDA','N','Semiconductors',3e12,2e10,0.025,0.8,?)", (iso(T_PRE),))
        for direction in (0.9, -0.9):
            for gap in (0.0, 0.5, 2.0):
                cfg["strategy"]["plan_gap_allowance"] = gap
                cand = {"ticker": "NVDA", "event_type": "guidance_raise" if direction > 0 else "guidance_cut", "direction": direction, "magnitude": 0.9, "conf": 0.9, "credibility": 0.9, "age_hours": 1.0, "horizon": 6}
                f = ev.build(c, cand, cfg, {"regime": "neutral", "risk_mult": 0.8}, T_PRE, None)
                self.assertTrue(f["target_return"], f)
                stop, target, cost = abs(f["stop_return"]), f["target_return"], f["inputs"]["cost"]
                expect = sum(v["p"] * max(-stop * (1 + gap), min(target, v["ret"])) for v in f["scenarios"].values()) - cost
                self.assertAlmostEqual(f["ev_net"], expect, places=12, msg=(direction, gap))
            cfg["strategy"]["plan_gap_allowance"] = 0.0
            tight = ev.build(c, cand, cfg, {"regime": "neutral", "risk_mult": 0.8}, T_PRE, None)["ev_net"]
            cfg["strategy"]["plan_gap_allowance"] = 2.0
            loose = ev.build(c, cand, cfg, {"regime": "neutral", "risk_mult": 0.8}, T_PRE, None)["ev_net"]
            self.assertGreater(tight, loose)                                                            # allowing for stops that slip makes the edge smaller

    def test_an_unknown_market_cap_is_not_shortable(self):
        c, cfg = H.fresh_db(), H.cfg()
        cfg["instruments"]["shorts_enabled"] = True
        c.execute("INSERT OR REPLACE INTO companies(ticker,name,sector,market_cap,adv_usd,vol_daily,completeness,updated) VALUES('NVDA','N','Semiconductors',NULL,2e10,0.025,0.8,?)", (iso(T_PRE),))
        f = ev.build(c, {"ticker": "NVDA", "event_type": "guidance_cut", "direction": -0.9, "magnitude": 0.9, "conf": 0.9, "credibility": 0.9, "age_hours": 1.0, "horizon": 6}, cfg, {"regime": "neutral", "risk_mult": 0.8}, T_PRE, None)
        self.assertEqual(f["verdict"], "REJECT_NOT_SHORTABLE")
        self.assertTrue(any("unknown" in w for w in f["why"]), f["why"])


class TestTheMonitorJudgesAShortFromItsOwnSide(unittest.TestCase):
    def setUp(self):
        self.c, self.cfg = H.fresh_db(), H.cfg()
        for tk, side in (("NVDA", "LONG"), ("LMT", "SHORT")):
            self.c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,status,thesis_key,sector,side) VALUES(?,?,?,100,100,10000,'OPEN','tk1','X',?)", (tk, "t", iso(T_OPEN), side))
            pid = self.c.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.c.execute("INSERT INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(?,?,?,?,?)", (pid, "OPEN", 100, 10000, iso(T_OPEN)))
        self.c.commit()
        from paper_agent import monitor as m
        self.m = m
        self.orig = m.evidence_engine.ticker_evidence

    def tearDown(self):
        self.m.evidence_engine.ticker_evidence = self.orig

    def thesis(self, state, net=0.0, trend=0.0):
        self.c.execute("INSERT INTO theses(key,name,prior,probability,confidence_quality,uncertainty,trend,state,support_score,contradiction_score,net_strength,evidence_mass,active) VALUES('tk1','Test thesis',0.5,0.6,0.5,0.3,?,?,0,0,?,1.0,1)", (trend, state, net))
        self.c.commit()

    def run_monitor(self, ticker_net):
        self.m.evidence_engine.ticker_evidence = lambda conn, t, now: {"net": ticker_net}
        self.m.monitor(self.c, self.cfg, {"regime": "neutral", "risk_mult": 0.8}, T_OPEN + dt.timedelta(hours=1))
        h = {r["ticker"]: r["health"] for r in self.c.execute("SELECT p.ticker,h.health FROM position_health h JOIN positions p ON p.id=h.position_id")}
        sig = {r["ticker"]: r["trigger"] for r in self.c.execute("SELECT p.ticker,s.trigger FROM exit_signals s JOIN positions p ON p.id=s.position_id")}
        return h, sig

    def test_bearish_evidence_is_good_for_a_short_and_bad_for_a_long(self):
        h, sig = self.run_monitor(-1.5)
        self.assertLess(h["NVDA"], 25)
        self.assertGreater(h["LMT"], 75)                                                              # the very news the short was opened on SUPPORTS it
        self.assertIn("NVDA", sig)
        self.assertNotIn("LMT", sig)

    def test_a_broken_bullish_thesis_exits_a_long_but_is_confirmation_for_a_short(self):
        self.thesis("Broken", net=-0.5)
        h, sig = self.run_monitor(0.0)
        self.assertEqual(sig.get("NVDA"), "THESIS_BROKEN")
        self.assertNotIn("LMT", sig)

    def test_a_revised_plan_for_a_short_keeps_its_levels_below_the_entry(self):
        for pid, tk in ((1, "NVDA"), (2, "LMT")):
            self.c.execute("INSERT INTO position_forecast_plans(position_id,decision_id,created,plan_version,active,event_type,forecast_decision,target_return,stop_return,target_price,stop_price,max_hold_days,estimated_exit,target_probability,confidence,completeness,"
                           "partial_exit_pct,runner_target_return,runner_target_price,runner_stop_return,runner_stop_price,bear_return,base_return,bull_return,expected_return,residual_return,priced_in,rationale,invalidation_json,forecast_hash) "
                           "VALUES(?,0,?,'5.0.0',1,'x','BUY',0.03,-0.02,?,?,5,?,0.5,70,80,1.0,0.03,?,-0.02,?,-0.03,0.01,0.05,0.02,0.02,0,'t','[]','h')",
                           (pid, iso(T_OPEN), 103.0 if tk == "NVDA" else 97.0, 98.0 if tk == "NVDA" else 102.0, iso(T_OPEN + dt.timedelta(days=5)), 103.0, 98.0))
        self.c.execute("INSERT INTO events(created,event_type,scope,direction,magnitude,tickers_json,regions_json,novelty,credibility,stale) VALUES(?,?,?,?,?,?,?,?,?,0)", (iso(T_OPEN + dt.timedelta(minutes=30)), "guidance_cut", "company", -1.0, 0.9, '["NVDA","LMT"]', "[]", 1.0, 0.9))
        eid = self.c.execute("SELECT last_insert_rowid()").fetchone()[0]
        for tk in ("NVDA", "LMT"):
            self.c.execute("INSERT INTO evidence(event_id,ticker,created,credibility,strength,novelty,predictive_power,direction,event_type,decay_days,expiry_date) VALUES(?,?,?,0.9,0.9,1.0,0.5,-1.0,'guidance_cut',5,?)", (eid, tk, iso(T_OPEN + dt.timedelta(minutes=30)), iso(T_OPEN + dt.timedelta(days=5))))
        self.c.commit()
        self.run_monitor(0.0)
        short = self.c.execute("SELECT p.entry_price,f.target_price,f.stop_price FROM position_forecast_plans f JOIN positions p ON p.id=f.position_id WHERE p.ticker='LMT' AND f.active=1").fetchone()
        self.assertLess(short["target_price"], short["entry_price"])                                    # bearish news EXPANDS a short's target: further down
        self.assertGreater(short["stop_price"], short["entry_price"])
        long_ = self.c.execute("SELECT p.entry_price,f.target_price,f.stop_price FROM position_forecast_plans f JOIN positions p ON p.id=f.position_id WHERE p.ticker='NVDA' AND f.active=1").fetchone()
        self.assertGreater(long_["target_price"], long_["entry_price"])
        self.assertLess(long_["stop_price"], long_["entry_price"])


if __name__ == "__main__":
    unittest.main()
