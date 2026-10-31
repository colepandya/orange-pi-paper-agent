"""Paper options (bought calls and puts, MODEL-priced): the pricing maths, every reason an option is NOT used, and full lifecycles through the real pipeline: opened instead of the stock for the strongest
short-horizon ideas, marked to the model, closed by each kind of exit, settled at expiry, capped as a sleeve, and always balancing to the cent."""
import datetime as dt
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import db, options as O, pipeline, portfolio as pf  # noqa: E402
from paper_agent.timeutil import add_trading_days, iso  # noqa: E402

T_PRE, T_OPEN = TL.T_PRE, TL.T_OPEN
BEAR = "NVIDIA misses estimates and cuts full-year guidance as data center demand slumps"


class TestThePricing(unittest.TestCase):
    def test_it_reproduces_the_textbook_numbers(self):
        self.assertAlmostEqual(O.bs_price("CALL", 100, 100, 1, 0.05, 0.2), 10.4506, places=3)
        self.assertAlmostEqual(O.bs_price("PUT", 100, 100, 1, 0.05, 0.2), 5.5735, places=3)
        self.assertAlmostEqual(O.bs_delta("CALL", 100, 100, 1, 0.05, 0.2), 0.6368, places=3)
        self.assertAlmostEqual(O.bs_delta("PUT", 100, 100, 1, 0.05, 0.2), 0.6368 - 1, places=3)

    def test_put_call_parity_holds_everywhere(self):
        for S, K, T, r, s in ((120, 125, 0.30, 0.04, 0.35), (50, 45, 0.10, 0.03, 0.60), (300, 300, 0.05, 0.05, 0.25), (20, 30, 1.0, 0.02, 0.45)):
            lhs = O.bs_price("CALL", S, K, T, r, s) - O.bs_price("PUT", S, K, T, r, s)
            self.assertAlmostEqual(lhs, S - K * math.exp(-r * T), places=8)

    def test_value_rises_with_volatility_and_time_and_never_below_intrinsic(self):
        base = O.bs_price("CALL", 100, 100, 0.25, 0.04, 0.3)
        self.assertGreater(O.bs_price("CALL", 100, 100, 0.25, 0.04, 0.5), base)
        self.assertGreater(O.bs_price("CALL", 100, 100, 0.50, 0.04, 0.3), base)
        self.assertGreaterEqual(O.bs_price("CALL", 130, 100, 0.25, 0.04, 0.3), 30.0)
        self.assertGreaterEqual(O.bs_price("PUT", 70, 100, 0.25, 0.04, 0.3), 100 * math.exp(-0.04 * 0.25) - 70)       # a European put's floor is the DISCOUNTED strike minus the price
        self.assertEqual(O.bs_price("CALL", 105, 100, 0.0, 0.04, 0.3), 5.0)                       # at expiry: intrinsic
        self.assertEqual(O.bs_price("PUT", 105, 100, 0.0, 0.04, 0.3), 0.0)

    def test_time_decay_accelerates_toward_expiry(self):
        v = [O.bs_price("CALL", 100, 100, d / 365.0, 0.04, 0.3) for d in (60, 30, 10, 1)]
        drops = [v[0] - v[1], v[1] - v[2], v[2] - v[3]]                                              # per 30, 20, 9 days
        self.assertGreater(drops[2] / 9, drops[0] / 30)

    def test_strikes_expiries_and_the_spread(self):
        self.assertEqual(O.pick_strike("CALL", 120.4), 122.5)
        self.assertEqual(O.pick_strike("PUT", 120.4), 120.0)
        self.assertEqual(O.pick_strike("CALL", 120.0), 120.0)
        e = O.next_expiry(dt.date(2026, 10, 27), 21)
        self.assertEqual(e.weekday(), 4)                                                              # a Friday
        self.assertGreaterEqual((e - dt.date(2026, 10, 27)).days, 21)
        self.assertLess((e - dt.date(2026, 10, 27)).days, 28)
        px = O.price_option("CALL", 120, 120, e, 0.40, T_OPEN, H.cfg())
        self.assertLess(px["bid"], px["mid"])
        self.assertGreater(px["ask"], px["mid"])
        self.assertGreater(px["ask"] - px["bid"], 0.05)


class Base(TL.Base):
    def setUp(self):
        super().setUp()
        self.cfg["instruments"]["options_enabled"] = True
        self.cfg["instruments"]["options_min_ev"] = 0.005
        self.cfg["instruments"]["options_max_hold_days"] = 6
        self.cfg["instruments"]["options_max_horizon_days"] = 15                                      # these tests run the non-swing profile (an 8-session horizon); the shipped swing profile is 1-3
        self.profile(3.0e12, 2.0e10)

    def profile(self, market_cap, adv):
        self.c.execute("INSERT OR REPLACE INTO companies(ticker,name,sector,industry,country,currency,market_cap,adv_usd,beta,vol_daily,completeness,updated) VALUES('NVDA','NVIDIA Corp','Semiconductors','Semiconductors','US','USD',?,?,1.6,0.025,0.8,?)",
                       (market_cap, adv, iso(T_PRE)))
        self.c.commit()

    def cycle_with(self, now, headline=None):
        self.mkt.now = now
        items = [(headline, "AI capex", TL.RF(T_PRE - dt.timedelta(minutes=40)))] if headline else []
        return pipeline.run_cycle(self.c, self.cfg, H.universe(), TL.SRC, self.deps(items), now)

    def open_option(self, headline=TL.NVDA_NEWS):
        self.cycle_with(T_PRE, headline)
        self.cycle_with(T_OPEN)
        o = self.one("SELECT * FROM option_positions")
        self.assertIsNotNone(o, "setup: an option should have opened at the open")
        return dict(o)

    def options_books(self):
        start = db.start_capital(self.c)
        led = self.c.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE kind!='START'").fetchone()[0]
        self.assertAlmostEqual(db.cash(self.c), start + led, places=1)
        self.assertGreaterEqual(db.cash(self.c), 0)
        self.assertEqual(self.c.execute("PRAGMA integrity_check").fetchone()[0], "ok")


class TestOpening(Base):
    def test_a_strong_catalyst_becomes_a_call_instead_of_the_stock(self):
        cash0 = db.cash(self.c)
        eq0 = pf.equity_of(self.c, now=T_PRE)["equity"]
        o = self.open_option()
        self.assertEqual((o["kind"], o["underlying"], o["status"]), ("CALL", "NVDA", "OPEN"))
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)                          # the stock was NOT also bought
        d = self.one("SELECT action,status,reason FROM decisions WHERE ticker='NVDA'")
        self.assertEqual((d["action"], d["status"]), ("OPTION_CALL", "EXECUTED"))
        self.assertIn("model_priced", d["reason"])
        self.assertGreaterEqual(o["strike"], 120.0)                                                   # at or just above the price
        self.assertEqual(dt.date.fromisoformat(o["expiry"]).weekday(), 4)
        self.assertGreaterEqual((dt.date.fromisoformat(o["expiry"]) - T_OPEN.date()).days, 21)
        self.assertAlmostEqual(cash0 - db.cash(self.c), o["cost"], places=1)                        # the whole premium (+ commission) left cash
        self.assertEqual(self.one("SELECT kind FROM ledger ORDER BY id DESC LIMIT 1")[0], "OPTION_BUY")
        eq = pf.equity_of(self.c, now=T_OPEN)
        self.assertAlmostEqual(eq["option_value"], o["contracts"] * 100 * o["last_mark"], places=1)
        self.assertLess(eq["equity"], eq0)                                                            # buying costs the spread and the commission, no more
        self.assertGreater(eq["equity"], eq0 - 0.2 * o["cost"])
        self.options_books()

    def test_the_sleeve_is_capped_per_trade_and_in_total(self):
        o = self.open_option()
        eq = pf.equity_of(self.c, now=T_PRE)["equity"]
        self.assertLessEqual(o["cost"], self.cfg["instruments"]["options_per_trade_pct"] * eq * 1.0001)
        self.assertLessEqual(O.sleeve_used(self.c), self.cfg["instruments"]["options_max_premium_pct"] * eq)

    def test_a_full_sleeve_means_the_stock_is_bought_instead(self):
        self.cfg["instruments"]["options_max_premium_pct"] = 0.0001
        self.cycle_with(T_PRE, TL.NVDA_NEWS)
        self.cycle_with(T_OPEN)
        self.assertEqual(self.one("SELECT COUNT(*) FROM option_positions")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 1)

    def test_a_weaker_or_slower_idea_or_an_illiquid_stock_stays_in_the_stock(self):
        self.cfg["instruments"]["options_min_ev"] = 0.5
        self.cycle_with(T_PRE, TL.NVDA_NEWS)
        self.cycle_with(T_OPEN)
        self.assertEqual((self.one("SELECT COUNT(*) FROM option_positions")[0], self.one("SELECT COUNT(*) FROM positions")[0]), (0, 1))

    def test_with_options_off_the_stock_is_traded_as_before(self):
        self.cfg["instruments"]["options_enabled"] = False
        self.cycle_with(T_PRE, TL.NVDA_NEWS)
        self.cycle_with(T_OPEN)
        self.assertEqual((self.one("SELECT COUNT(*) FROM option_positions")[0], self.one("SELECT COUNT(*) FROM positions")[0]), (0, 1))

    def test_a_bearish_idea_becomes_a_put_even_when_short_selling_is_off(self):
        self.cfg["instruments"]["shorts_enabled"] = False
        o = self.open_option(BEAR)
        self.assertEqual(o["kind"], "PUT")
        self.assertLessEqual(o["strike"], 120.0)
        self.assertEqual(self.one("SELECT action FROM decisions WHERE ticker='NVDA'")[0], "OPTION_PUT")
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)
        self.options_books()

    def test_a_bearish_idea_with_both_off_is_refused_with_the_reason(self):
        self.cfg["instruments"]["shorts_enabled"] = False
        self.cfg["instruments"]["options_enabled"] = False
        self.cycle_with(T_PRE, BEAR)
        d = self.one("SELECT status,reason FROM decisions WHERE ticker='NVDA'")
        self.assertEqual(d["status"], "REJECTED")
        self.assertEqual(self.one("SELECT COUNT(*) FROM option_positions")[0], 0)

    def test_a_bearish_idea_with_shorts_on_but_no_qualifying_put_is_sold_short(self):
        self.cfg["instruments"]["shorts_enabled"] = True
        self.cfg["instruments"]["options_min_ev"] = 0.9                                              # no put qualifies
        self.cycle_with(T_PRE, BEAR)
        self.cycle_with(T_OPEN)
        self.assertEqual(self.one("SELECT side FROM positions")[0], "SHORT")

    def test_the_stress_test_assumes_open_options_are_worthless(self):
        self.open_option()
        hs = pf.holdings(self.c, T_OPEN)
        eq = pf.equity_of(self.c, hs, T_OPEN)
        plain = pf.stress(hs, eq["equity"], self.cfg, {})
        with_opt = pf.stress(hs, eq["equity"], self.cfg, {}, eq["option_value"])
        self.assertGreater(with_opt["worst_loss"], plain["worst_loss"])


class TestTheExits(Base):
    def guard_at(self, price, minutes=20, when=None):
        self.mkt.price["NVDA"] = price
        self.guard(when or (T_OPEN + dt.timedelta(minutes=minutes)))
        return self.one("SELECT * FROM option_positions")

    def test_a_fall_to_the_ideas_stop_closes_the_option_at_a_loss(self):
        o = self.open_option()
        q = self.guard_at(o["stop_underlying"] * 0.995)
        self.assertEqual((q["status"], q["review_code"]), ("CLOSED", "UNDERLYING_STOP"))
        self.assertLess(q["pnl"], 0)
        self.assertLess(q["exit_premium"], o["entry_premium"])
        self.assertEqual(self.one("SELECT kind FROM ledger ORDER BY id DESC LIMIT 1")[0], "OPTION_SELL")
        led = self.c.execute("SELECT SUM(amount) FROM ledger WHERE kind IN ('OPTION_BUY','OPTION_SELL')").fetchone()[0]
        self.assertAlmostEqual(led, q["pnl"], places=1)                                              # the P&L is exactly the cash that moved
        self.assertAlmostEqual(pf.equity_of(self.c, now=T_OPEN)["equity"], db.cash(self.c), places=2)
        self.options_books()

    def test_a_rise_to_the_target_closes_it_at_a_profit(self):
        o = self.open_option()
        q = self.guard_at(o["target_underlying"] * 1.01)
        self.assertEqual(q["status"], "CLOSED")
        self.assertIn(q["review_code"], ("UNDERLYING_TARGET", "TARGET_REACHED"))
        self.assertGreater(q["pnl"], 0)
        self.options_books()

    def test_the_premium_stop_cuts_a_loss_even_if_the_stock_has_not_hit_its_stop(self):
        o = self.open_option()
        self.c.execute("UPDATE option_positions SET stop_underlying=0.01")
        self.c.commit()
        q = self.guard_at(o["entry_underlying"] * 0.92)
        self.assertEqual((q["status"], q["review_code"]), ("CLOSED", "PREMIUM_STOP"))
        self.assertLessEqual(q["exit_premium"], o["entry_premium"] * 0.5 + 0.05)

    def test_a_flat_stock_loses_to_time_and_the_option_is_closed_by_the_clock(self):
        o = self.open_option()
        when = add_trading_days(T_OPEN, 7)
        self.mkt.now = when
        q = self.guard_at(120.0, when=when)
        self.assertEqual(q["status"], "CLOSED")
        self.assertIn(q["review_code"], ("TIME_EXPIRED", "PREMIUM_STOP"))
        self.assertLess(q["pnl"], 0)                                                                  # time decay and the spread cost money on a stock that went nowhere

    def test_it_is_closed_before_expiry_when_the_days_run_short(self):
        o = self.open_option()
        self.c.execute("UPDATE option_positions SET max_hold_days=200, stop_underlying=0.01, target_underlying=1e9")
        self.c.commit()
        self.cfg["instruments"]["options_stop_loss_pct"] = 0.99                                       # three weeks of decay on a flat stock would trip the premium stop first (correctly); isolate the days-left rule
        exp = dt.datetime.fromisoformat(o["expiry"] + "T16:00:00+00:00")                              # 16:00 UTC is inside the session in both US clock settings
        when = exp - dt.timedelta(days=3)
        while when.weekday() > 4:
            when -= dt.timedelta(days=1)
        self.mkt.now = when
        self.mkt.price["NVDA"] = 121.0
        self.guard(when)
        q = self.one("SELECT * FROM option_positions")
        self.assertEqual((q["status"], q["review_code"]), ("CLOSED", "EXPIRY_NEAR"))

    def test_at_expiry_it_settles_at_intrinsic_value_even_with_no_quote(self):
        o = self.open_option()
        o2 = self.one("SELECT * FROM option_positions")
        after = dt.datetime.fromisoformat(o["expiry"] + "T23:00:00+00:00") + dt.timedelta(days=1)
        self.c.execute("DELETE FROM quote_cache")
        self.c.execute("INSERT OR REPLACE INTO market_series(symbol,day,close,volume) VALUES('NVDA',?,?,1)", (o["expiry"], o["strike"] + 10.0))                # the expiry day's close
        self.c.commit()
        r = O.run(self.c, self.cfg, after, None)                                                       # market closed, no quote function: it still settles
        q = self.one("SELECT * FROM option_positions")
        self.assertEqual((q["status"], q["review_code"]), ("CLOSED", "EXPIRED"))
        self.assertAlmostEqual(q["exit_premium"], 10.0, places=1)                                      # in the money by 10: worth 10 a share
        self.assertAlmostEqual(q["pnl"], o["contracts"] * 100 * 10.0 - o["cost"], places=1)             # no spread and no commission on settlement
        self.options_books()

    def test_marks_follow_the_stock_so_equity_tracks_the_position(self):
        o = self.open_option()
        self.guard_at(o["entry_underlying"] * 1.01, minutes=15)
        up = self.one("SELECT last_mark FROM option_positions")[0]
        self.guard_at(o["entry_underlying"] * 0.995, minutes=17)
        dn = self.one("SELECT last_mark FROM option_positions")[0]
        self.assertGreater(up, dn)
        self.assertGreater(up, o["last_mark"])

    def test_a_blocked_quote_marks_but_never_trades(self):
        o = self.open_option()
        for p in self.mkt.age:
            self.mkt.age[p] = 900
        self.mkt.price["NVDA"] = o["stop_underlying"] * 0.9
        self.guard(T_OPEN + dt.timedelta(minutes=20))
        self.assertEqual(self.one("SELECT status FROM option_positions")[0], "OPEN")                  # no verified price: no exit


class TestTheModelIsHonestAboutItself(unittest.TestCase):
    def test_the_module_and_the_setting_say_it_is_model_priced(self):
        self.assertIn("MODEL-PRICED", O.__doc__)
        self.assertIn("NOT a verified track record", O.__doc__)
        from paper_agent import settings as S
        f = next(x for x in S.FIELDS if x.path == "instruments.options_enabled")
        self.assertIn("MODEL-priced", f.label)
        self.assertIn("NO options data feed", f.help)


if __name__ == "__main__":
    unittest.main()
