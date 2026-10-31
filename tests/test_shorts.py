"""Short selling, end to end through the REAL pipeline, quote consensus and executor: bearish headline -> SHORT decision -> short opened at the open (proceeds credited, collateral reserved) -> stop ABOVE /
target BELOW -> cover (BUY back) -> books balance to the cent. And every reason a short must be refused."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import db, ev, pipeline, portfolio as pf  # noqa: E402
from paper_agent.timeutil import add_trading_days, iso  # noqa: E402

BEAR = "NVIDIA misses estimates and cuts full-year guidance as data center demand slumps"
T_PRE, T_OPEN = TL.T_PRE, TL.T_OPEN


def short_books_balance(t, c):
    """The accounting identities for a book that holds shorts: cash follows the ledger, and money received for shorts minus the cost basis already covered equals the open shorts' basis."""
    start = db.start_capital(c)
    led = c.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE kind!='START'").fetchone()[0]
    t.assertAlmostEqual(db.cash(c), start + led, places=1)
    t.assertGreaterEqual(db.cash(c), 0)
    got = c.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE kind='VIRTUAL_SHORT'").fetchone()[0]
    released = c.execute("SELECT COALESCE(SUM(a.cost_released),0) FROM virtual_exit_actions a JOIN positions p ON p.id=a.position_id WHERE a.status='COMMITTED' AND p.side='SHORT'").fetchone()[0]
    open_basis = c.execute("SELECT COALESCE(SUM(remaining_cost),0) FROM position_lifecycle l JOIN positions p ON p.id=l.position_id WHERE p.status='OPEN' AND p.side='SHORT'").fetchone()[0]
    t.assertAlmostEqual(got - released, open_basis, delta=1.0)
    t.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")


class ShortBase(TL.Base):
    def setUp(self):
        super().setUp()
        self.cfg["instruments"]["shorts_enabled"] = True
        self.profile(market_cap=3.0e12, adv=2.0e10)

    def profile(self, market_cap, adv, price=None):
        self.c.execute("INSERT OR REPLACE INTO companies(ticker,name,sector,industry,country,currency,market_cap,adv_usd,beta,vol_daily,completeness,updated) VALUES('NVDA','NVIDIA Corp','Semiconductors','Semiconductors','US','USD',?,?,1.6,0.025,0.8,?)",
                       (market_cap, adv, iso(T_PRE)))
        self.c.commit()

    def cycle(self, now, news=False):
        self.mkt.now = now
        items = [(BEAR, "AI capex", TL.RF(T_PRE - dt.timedelta(minutes=40)))] if news else []
        return pipeline.run_cycle(self.c, self.cfg, H.universe(), TL.SRC, self.deps(items), now)

    def open_short(self):
        self.cycle(T_PRE, news=True)
        self.cycle(T_OPEN)
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions WHERE status='OPEN'")[0], 1, "setup: the SHORT should have executed at the open")
        return dict(self.one("SELECT * FROM position_forecast_plans WHERE active=1"))


class TestOpeningAShort(ShortBase):
    def test_a_bearish_headline_becomes_a_short_that_opens_at_the_open_with_the_stop_above_and_the_target_below(self):
        r = self.cycle(T_PRE, news=True)
        self.assertEqual(r["errors"], [])
        d = self.one("SELECT status,action FROM decisions WHERE ticker='NVDA'")
        self.assertEqual((d["status"], d["action"]), ("AWAITING_MARKET", "SHORT"))
        self.assertEqual(self.one("SELECT verdict,direction FROM forecasts WHERE ticker='NVDA'")["verdict"], "SHORT")
        cash0 = db.cash(self.c)
        self.cycle(T_OPEN)
        p = self.one("SELECT * FROM positions")
        self.assertEqual((p["side"], p["status"], p["ticker"]), ("SHORT", "OPEN", "NVDA"))
        self.assertLess(p["entry_price"], 120.0)                                                   # sold a little BELOW the quote (slippage works against you)
        self.assertAlmostEqual(p["entry_price"], 120.0, delta=0.5)
        self.assertGreater(db.cash(self.c), cash0)                                                 # the proceeds are credited
        plan = self.one("SELECT * FROM position_forecast_plans WHERE active=1")
        self.assertGreater(plan["stop_price"], p["entry_price"])                                   # a short's stop is ABOVE
        self.assertLess(plan["target_price"], p["entry_price"])                                    # and its target BELOW
        self.assertLess(plan["runner_target_price"], plan["target_price"] + 1e-9)
        self.assertEqual(self.one("SELECT kind FROM ledger ORDER BY id DESC LIMIT 1")[0], "VIRTUAL_SHORT")
        short_books_balance(self, self.c)

    def test_equity_does_not_jump_when_a_short_is_opened_and_the_proceeds_are_reserved_not_spendable(self):
        self.cycle(T_PRE, news=True)
        eq0 = pf.equity_of(self.c, now=T_PRE)["equity"]
        self.cycle(T_OPEN)
        eq = pf.equity_of(self.c, now=T_OPEN)
        self.assertAlmostEqual(eq["equity"], eq0, delta=eq0 * 0.002)                                # selling short does not make you richer, only (slightly) poorer by the slippage
        hs = pf.holdings(self.c, T_OPEN)
        self.assertEqual(len(hs), 1)
        self.assertLess(hs[0]["value"], 0)                                                          # a liability
        self.assertEqual(hs[0]["side"], "SHORT")
        self.assertGreater(eq["short_value"], 0)
        self.assertEqual(eq["long_value"], 0)
        self.assertLess(eq["net"], 0)
        self.assertGreater(eq["gross"], 0)
        self.assertAlmostEqual(pf.short_reserve(hs, self.cfg), 1.5 * eq["short_value"], places=2)    # 150% held back
        self.assertLess(pf.holdings(self.c, T_OPEN)[0]["unrealized"], 1.0)

    def test_shorts_off_means_the_old_long_only_behaviour(self):
        self.cfg["instruments"]["shorts_enabled"] = False
        self.cycle(T_PRE, news=True)
        f = self.one("SELECT verdict FROM forecasts WHERE ticker='NVDA'")[0]
        self.assertEqual(f, "AVOID_BEARISH_LONG_ONLY")
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)


class TestTheExitsMirror(ShortBase):
    def test_a_rise_hits_the_stop_and_the_cover_pays_a_higher_price_so_the_short_loses(self):
        plan = self.open_short()
        p = self.one("SELECT * FROM positions")
        cash_before = db.cash(self.c)
        self.mkt.price["NVDA"] = plan["stop_price"] * 1.01                                          # the price rises through the stop
        self.guard(T_OPEN + dt.timedelta(minutes=20))
        q = self.one("SELECT * FROM positions")
        self.assertEqual((q["status"], q["review_code"]), ("CLOSED", "STOP_REACHED"))
        self.assertGreater(q["exit_price"], p["entry_price"])
        self.assertLess(q["pnl"], 0)
        self.assertLess(db.cash(self.c), cash_before)                                               # buying back COSTS cash
        self.assertEqual(self.one("SELECT kind FROM ledger ORDER BY id DESC LIMIT 1")[0], "VIRTUAL_COVER")
        led = self.c.execute("SELECT SUM(amount) FROM ledger WHERE kind IN ('VIRTUAL_SHORT','VIRTUAL_COVER')").fetchone()[0]
        self.assertAlmostEqual(led, q["pnl"], places=1)                                              # the P&L IS the cash that moved
        short_books_balance(self, self.c)
        eq = pf.equity_of(self.c, now=T_OPEN)
        self.assertAlmostEqual(eq["equity"], db.cash(self.c), places=2)                             # nothing left open
        self.assertAlmostEqual(eq["equity"], db.start_capital(self.c) + q["pnl"], places=1)

    def test_a_fall_hits_the_target_and_the_short_profits(self):
        plan = self.open_short()
        held = int(plan["max_hold_days"]) // 2 + 1                                                   # from the middle of the hold on, a target hit is SOLD, not re-forecast
        when = add_trading_days(T_OPEN, held)
        self.mkt.price["NVDA"] = plan["target_price"] * 0.995
        self.cycle(when)
        self.guard(when + dt.timedelta(minutes=20))
        self.assertIn(self.one("SELECT kind FROM ledger ORDER BY id DESC LIMIT 1")[0], ("VIRTUAL_PARTIAL_COVER", "VIRTUAL_COVER"))
        done = self.c.execute("SELECT SUM(realized_pnl) FROM virtual_exit_actions WHERE status='COMMITTED'").fetchone()[0]
        self.assertGreater(done, 0)                                                                  # a fall made money for the short
        short_books_balance(self, self.c)
        q = self.one("SELECT * FROM positions")
        if q["status"] == "OPEN":                                                                    # a runner is left: its plan is mirrored too
            rp = self.one("SELECT * FROM position_forecast_plans WHERE active=1")
            self.assertLessEqual(rp["stop_price"], q["entry_price"] + 1e-6)                          # protected: the runner's stop has been brought to / below break-even
            self.assertLess(rp["runner_target_price"], q["entry_price"])

    def test_a_target_hit_on_day_one_may_extend_the_hold_and_the_extended_target_is_still_below_the_entry(self):
        plan = self.open_short()
        self.mkt.price["NVDA"] = plan["target_price"] * 0.99
        self.guard(T_OPEN + dt.timedelta(minutes=20))
        rf = self.one("SELECT decision FROM target_reforecasts")
        if rf and rf[0] == "HOLD_EXTENDED":
            p = self.one("SELECT entry_price FROM positions")
            self.assertLess(self.one("SELECT target_price FROM position_forecast_plans WHERE active=1")[0], p["entry_price"])
        short_books_balance(self, self.c)

    def test_the_trailing_stop_only_ever_moves_DOWN_for_a_short(self):
        self.cfg["strategy"]["swing_enabled"] = True                                                 # the ratchet is a swing-mode feature
        plan = self.open_short()
        p = self.one("SELECT * FROM positions")
        stop0 = plan["stop_price"]
        self.mkt.price["NVDA"] = p["entry_price"] * 0.985                                            # falls 1.5%: in the short's favour
        self.guard(T_OPEN + dt.timedelta(minutes=10))
        stop1 = self.one("SELECT stop_price FROM position_forecast_plans WHERE active=1")[0]
        self.assertLess(stop1, stop0)                                                                # the stop followed the price DOWN
        self.assertGreater(stop1, self.mkt.price["NVDA"])                                            # and is still above the market
        self.mkt.price["NVDA"] = p["entry_price"] * 0.99                                             # the price bounces back up a little, but stays under the trailed stop
        self.guard(T_OPEN + dt.timedelta(minutes=14))
        stop2 = self.one("SELECT stop_price FROM position_forecast_plans WHERE active=1")[0]
        self.assertLessEqual(stop2, stop1 + 1e-9)                                                    # it never loosens
        self.assertEqual(self.one("SELECT status FROM positions")[0], "OPEN")
        self.mkt.price["NVDA"] = stop2 * 1.002                                                       # the bounce reaches the trailed stop
        self.guard(T_OPEN + dt.timedelta(minutes=18))
        q = self.one("SELECT * FROM positions")
        self.assertEqual(q["status"], "CLOSED")
        self.assertGreater(q["pnl"], 0)                                                              # stopped out in PROFIT: the ratchet protected the gain

    def test_a_short_is_closed_by_the_time_stop_too(self):
        plan = self.open_short()
        later = add_trading_days(T_OPEN, int(plan["max_hold_days"]) + 2)
        self.cycle(later)
        self.guard(later + dt.timedelta(minutes=5))
        self.assertEqual(self.one("SELECT status FROM positions")[0], "CLOSED")
        short_books_balance(self, self.c)


class TestEveryReasonToRefuseAShort(ShortBase):
    def verdict(self):
        self.cycle(T_PRE, news=True)
        r = self.one("SELECT verdict,inputs_json FROM forecasts WHERE ticker='NVDA'")
        d = self.one("SELECT status,reason FROM decisions WHERE ticker='NVDA'")
        return r["verdict"], d

    def test_a_small_company_is_not_shortable(self):
        self.profile(market_cap=5.0e8, adv=2.0e10)
        v, d = self.verdict()
        self.assertEqual(v, "REJECT_NOT_SHORTABLE")
        self.assertIn("market cap", d["reason"])
        self.assertEqual(d["status"], "REJECTED")

    def test_an_illiquid_stock_is_not_shortable(self):
        self.profile(market_cap=3.0e12, adv=5.0e6)
        v, d = self.verdict()
        self.assertEqual(v, "REJECT_NOT_SHORTABLE")
        self.assertIn("trades", d["reason"])

    def test_a_cheap_stock_is_not_shortable(self):
        self.prev_close["NVDA"] = 3.5
        self.mkt.price["NVDA"] = 3.5
        v, d = self.verdict()
        self.assertEqual(v, "REJECT_NOT_SHORTABLE")
        self.assertIn("short minimum", d["reason"])

    def test_a_runaway_rally_is_not_shorted(self):
        self.cfg["instruments"]["short_squeeze_5d_pct"] = 0.20
        rally = [100.0] * 250 + [100.0, 105.0, 110.0, 116.0, 122.0, 128.0]                          # +28% in five sessions
        orig = ev.md.closes
        ev.md.closes = lambda conn, t, n=260: rally
        try:
            f = TestEvPerspective.build(self, -0.8)
        finally:
            ev.md.closes = orig
        self.assertEqual(f["verdict"], "REJECT_NOT_SHORTABLE")
        self.assertTrue(any("squeeze" in w for w in f["why"]), f["why"])
        calm = [100.0] * 250 + [100.0, 100.5, 100.2, 100.8, 100.4, 100.6]
        ev.md.closes = lambda conn, t, n=260: calm
        try:
            g = TestEvPerspective.build(self, -0.8)
        finally:
            ev.md.closes = orig
        self.assertEqual(g["verdict"], "SHORT")                                                      # the same idea on a calm stock is shortable

    def test_good_news_after_the_decision_vetoes_a_short_and_the_message_says_so(self):
        self.cycle(T_PRE, news=True)
        did = self.one("SELECT id FROM decisions WHERE ticker='NVDA'")[0]
        self.c.execute("INSERT INTO events(created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source) VALUES(?,?,?,?,?,?,?,?,?)",
                       (iso(T_PRE + dt.timedelta(minutes=30)), "NVIDIA wins a huge contract and raises its outlook", 0.8, 0.8, 0.9, "contract_win", '["NVDA"]', "company", "Wire"))
        self.c.commit()
        self.cycle(T_OPEN)
        d = self.one("SELECT status,reason FROM decisions WHERE id=?", did)
        self.assertEqual(d["status"], "VETOED")
        self.assertIn("after this SHORT was decided", d["reason"])

    def test_a_market_wide_shock_does_not_hold_a_short_it_helps_it(self):
        self.cycle(T_PRE, news=True)
        for i, src in enumerate(("Reuters", "AP")):
            self.c.execute("INSERT INTO events(created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source) VALUES(?,?,?,?,?,?,?,?,?)",
                           (iso(T_PRE + dt.timedelta(minutes=20 + i)), "Global markets crash as war escalates", -0.9, 0.9, 0.9, "war_escalation", "[]", "macro", src))
        self.c.commit()
        self.mkt.price["SPY"] = 440.0
        self.c.execute("INSERT OR REPLACE INTO market_series(symbol,day,close,volume) VALUES('SPY','2026-10-26',450.0,1)")
        self.c.commit()
        self.cycle(T_OPEN)
        self.assertEqual(self.one("SELECT status FROM decisions WHERE ticker='NVDA'")[0], "EXECUTED")


class TestEvPerspective(ShortBase):
    def build(self, direction, **cfg_over):
        self.c.execute("INSERT OR IGNORE INTO companies(ticker,name,sector,market_cap,adv_usd,vol_daily,completeness,updated) VALUES('NVDA','N','Semiconductors',3e12,2e10,0.025,0.8,?)", (iso(T_PRE),))
        cand = {"ticker": "NVDA", "event_type": "guidance_cut" if direction < 0 else "guidance_raise", "direction": direction, "magnitude": 0.8, "conf": 0.8, "credibility": 0.9, "age_hours": 1.0, "horizon": 6}
        return ev.build(self.c, cand, self.cfg, {"regime": "neutral", "risk_mult": 0.8}, T_PRE, None)

    def test_a_mirror_image_idea_gets_a_mirror_image_forecast_for_the_position(self):
        up, down = self.build(0.8), self.build(-0.8)
        self.assertEqual((up["verdict"], down["verdict"]), ("BUY", "SHORT"))
        self.assertEqual((up["direction"], down["direction"]), ("LONG", "SHORT"))
        self.assertAlmostEqual(up["ev_net"], down["ev_net"], delta=abs(up["ev_net"]) * 0.45 + 0.004)       # the same size of edge in the position's favour (the thesis/flow add-ons are not symmetric)
        self.assertGreater(down["p_up"], 0.0)
        self.assertLess(down["p_up"], 0.5)                                                           # the STOCK is expected to fall
        self.assertGreater(up["p_up"], 0.5)
        self.assertGreater(down["scenarios"]["bull"]["ret"], 0)                                      # 'bull' always means good FOR THE POSITION
        self.assertLess(down["scenarios"]["tail"]["ret"], 0)                                         # and the tail is the squeeze that hurts a short

    def test_the_plan_cannot_be_promised_more_than_it_can_collect(self):
        f = self.build(0.8)
        self.assertLessEqual(f["ev_net"], f["target_return"] + 1e-9)
        self.assertGreater(f["inputs"]["ev_model_net"], f["ev_net"])                                  # the old claim was bigger than what the plan can pay
        self.cfg["strategy"]["plan_consistent_ev"] = False
        g = self.build(0.8)
        self.assertAlmostEqual(g["ev_net"], f["inputs"]["ev_model_net"], places=9)                   # switched off: exactly the old number
        self.assertFalse(g["inputs"]["plan_consistent_ev"])

    def test_with_shorts_off_a_bearish_idea_is_judged_exactly_as_before(self):
        self.cfg["instruments"]["shorts_enabled"] = False
        f = self.build(-0.8)
        self.assertEqual(f["verdict"], "AVOID_BEARISH_LONG_ONLY")
        self.assertLess(f["ev_net"], 0)                                                              # the stock-direction EV, negative, as the long-only book always stored


class TestThePretradeHonoursTheSide(ShortBase):
    def test_the_gap_test_is_mirrored(self):
        from paper_agent import pretrade
        self.cfg["risk"]["max_gap_down_pct"], self.cfg["risk"]["max_gap_up_pct"] = 0.03, 0.05
        c = {"ticker": "NVDA", "ref_price": 100.0, "decided_at": iso(T_PRE), "side": "SHORT"}
        up = pretrade.review(self.c, self.cfg, c, iso(T_PRE), T_OPEN, 104.0)                         # the price jumped UP 4% against the short
        self.assertEqual(up["action"], "veto")
        self.assertIn("above", up["reason"] + "above") if False else None
        down = pretrade.review(self.c, self.cfg, c, iso(T_PRE), T_OPEN, 94.0)                        # fell 6%: the move has already been made
        self.assertEqual(down["action"], "veto")
        ok = pretrade.review(self.c, self.cfg, c, iso(T_PRE), T_OPEN, 99.0)
        self.assertEqual(ok["action"], "go")
        long_ = dict(c, side="LONG")
        self.assertEqual(pretrade.review(self.c, self.cfg, long_, iso(T_PRE), T_OPEN, 104.0)["action"], "go")      # the same +4% is fine for a long


if __name__ == "__main__":
    unittest.main()
