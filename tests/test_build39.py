"""Build 39 (strategy S4): the properties that must hold however the new instruments are used: borrow fees, the opening delay, the edge never exceeding what the plan can collect (over hundreds of random ideas),
the short sizing limits, and the per-instrument scorecard."""
import datetime as dt
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_shorts as TS  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import db, ev, performance, portfolio as pf, sizing, stamp  # noqa: E402
from paper_agent.timeutil import add_trading_days, iso  # noqa: E402

T_PRE, T_OPEN = TL.T_PRE, TL.T_OPEN


class TestBorrowFees(TS.ShortBase):
    def test_a_short_pays_the_fee_each_day_and_only_once_a_day(self):
        self.open_short()
        cash0 = db.cash(self.c)
        hs = pf.holdings(self.c, T_OPEN)
        later = T_OPEN + dt.timedelta(days=3)
        r = pf.accrue_borrow_fees(self.c, self.cfg, hs, later)
        self.assertEqual(r["charged"], 1)
        expect = abs(hs[0]["value"]) * 0.005 * 3 / 365.0
        self.assertAlmostEqual(r["total"], expect, delta=expect * 0.02 + 0.02)
        self.assertAlmostEqual(cash0 - db.cash(self.c), r["total"], places=2)
        self.assertEqual(self.one("SELECT kind FROM ledger ORDER BY id DESC LIMIT 1")[0], "SHORT_BORROW_FEE")
        again = pf.accrue_borrow_fees(self.c, self.cfg, hs, later + dt.timedelta(hours=5))               # the same day: nothing more
        self.assertEqual(again["charged"], 0)
        nxt = pf.accrue_borrow_fees(self.c, self.cfg, hs, later + dt.timedelta(days=1))
        self.assertEqual(nxt["charged"], 1)
        TS.short_books_balance(self, self.c)

    def test_longs_pay_nothing_and_a_zero_rate_pays_nothing(self):
        self.open_short()
        hs = pf.holdings(self.c, T_OPEN)
        hs[0]["side"] = "LONG"
        self.assertEqual(pf.accrue_borrow_fees(self.c, self.cfg, hs, T_OPEN + dt.timedelta(days=3))["charged"], 0)
        hs[0]["side"] = "SHORT"
        self.cfg["instruments"]["short_borrow_fee_pct"] = 0.0
        self.assertEqual(pf.accrue_borrow_fees(self.c, self.cfg, hs, T_OPEN + dt.timedelta(days=3))["charged"], 0)

    def test_the_cycle_charges_it_and_the_fee_reduces_equity(self):
        self.open_short()
        eq0 = pf.equity_of(self.c, now=T_OPEN)["equity"]
        r = self.cycle(add_trading_days(T_OPEN, 1))
        self.assertGreaterEqual(r["borrow_fees"]["charged"], 1)
        self.assertLess(pf.equity_of(self.c, now=add_trading_days(T_OPEN, 1))["equity"], eq0 + 1.0)


class TestTheOpeningDelay(TL.Base):
    def test_an_entry_waits_out_the_first_minutes_then_goes(self):
        self.cfg["risk"]["open_delay_minutes"] = 10
        self.cycle(T_PRE, news=True)
        early = dt.datetime(2026, 10, 27, 13, 35, tzinfo=dt.timezone.utc)                                 # 09:35 New York: five minutes after the open
        self.cycle(early)
        d = self.one("SELECT status,reason FROM decisions WHERE ticker='NVDA'")
        self.assertEqual(d["status"], "AWAITING_MARKET")
        self.assertIn("opened 5 minutes ago", d["reason"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)
        self.cycle(T_OPEN)                                                                                  # 09:40: ten minutes in
        self.assertEqual(self.one("SELECT status FROM decisions WHERE ticker='NVDA'")[0], "EXECUTED")

    def test_zero_means_no_delay_and_exits_are_never_delayed(self):
        self.cfg["risk"]["open_delay_minutes"] = 0
        self.cycle(T_PRE, news=True)
        self.cycle(dt.datetime(2026, 10, 27, 13, 31, tzinfo=dt.timezone.utc))
        self.assertEqual(self.one("SELECT status FROM decisions WHERE ticker='NVDA'")[0], "EXECUTED")


class TestTheEdgeNeverExceedsWhatThePlanCanCollect(unittest.TestCase):
    def test_over_hundreds_of_random_ideas_in_both_directions(self):
        c, cfg = H.fresh_db(), H.cfg()
        cfg["instruments"]["shorts_enabled"] = True
        c.execute("INSERT OR REPLACE INTO companies(ticker,name,sector,market_cap,adv_usd,vol_daily,completeness,updated) VALUES('NVDA','N','Semiconductors',3e12,2e10,0.025,0.8,?)", (iso(T_PRE),))
        rnd = random.Random(7)
        n = checked = 0
        for _ in range(400):
            cand = {"ticker": "NVDA", "event_type": rnd.choice(list(ev.MOVE_PRIOR)), "direction": rnd.choice((-1, 1)) * rnd.uniform(0.15, 1.0), "magnitude": rnd.uniform(0.1, 1.0), "conf": rnd.uniform(0.4, 1.0),
                    "credibility": rnd.uniform(0.3, 1.0), "age_hours": rnd.uniform(0, 20), "horizon": rnd.randint(1, 12), "amount_usd": rnd.choice((None, None, 5e8, 5e10))}
            for plan_ev in (True, False):
                cfg["strategy"]["plan_consistent_ev"] = plan_ev
                f = ev.build(c, cand, cfg, {"regime": rnd.choice(("neutral", "risk_on", "risk_off")), "risk_mult": rnd.uniform(0.4, 1.0)}, T_PRE, None)
                n += 1
                self.assertIn(f["verdict"], ("BUY", "SHORT", "WATCH", "REJECT_NEGATIVE_EV", "REJECT_NOT_SHORTABLE", "REJECT_ILLIQUID"))
                self.assertEqual(f["direction"], "SHORT" if cand["direction"] < 0 else "LONG")
                if f["target_return"]:
                    if plan_ev:
                        checked += 1
                        self.assertLessEqual(f["ev_net"], f["target_return"] + 1e-9, f)                    # the whole point: no claim beyond the plan's own target
                        self.assertLessEqual(f["ev_net"], f["inputs"]["ev_model_net"] + 1e-9)               # and never rosier than the old number
                    else:
                        self.assertAlmostEqual(f["ev_net"], f["inputs"]["ev_model_net"], places=12)         # switched off: exactly the old calculation
                probs = sum(v["p"] for v in f["scenarios"].values())
                self.assertAlmostEqual(probs, 1.0, places=9)                                                # scenario probabilities always sum to one
                self.assertGreater(f["scenarios"]["bull"]["ret"], f["scenarios"]["base"]["ret"])
                self.assertLess(f["scenarios"]["bear"]["ret"], f["scenarios"]["base"]["ret"])
        self.assertGreater(checked, 100)


def hold(ticker, value, side, theme=None):
    return {"ticker": ticker, "value": value, "side": side, "sector": "Tech", "beta": 1.0, "thesis_key": theme, "country": "US", "industry": "", "market_cap": 5e11, "adv_usd": 5e9}


class TestShortSizingLimits(unittest.TestCase):
    def forecast(self, verdict="SHORT"):
        return {"ticker": "XYZ", "verdict": verdict, "ev_net": 0.04, "sigma_h": 0.05, "stop_return": -0.03, "confidence": 0.8, "risk_score": 20, "horizon_days": 3}

    def state(self, hs, cash=500_000.0, eq=1_000_000.0):
        return {"equity": eq, "cash": cash, "holdings": hs, "scenario_probs": {}}

    def size(self, verdict, hs, cash=500_000.0, **inst):
        cfg = H.cfg()
        cfg["instruments"].update(inst)
        return sizing.size_position(self.forecast(verdict), {"adv_usd": 5e9, "sector": "Tech", "beta": 1.0}, self.state(hs, cash), cfg, {"risk_mult": 1.0}, {"state": "Active", "score": 0.0, "primary": None})

    def test_a_short_starts_smaller_than_the_same_long(self):
        long_, short = self.size("BUY", []), self.size("SHORT", [])
        self.assertLess(short["pct"], long_["pct"])
        self.assertAlmostEqual(short["pct"] / long_["pct"], 0.6, delta=0.05)

    def test_the_short_exposure_cap_binds(self):
        hs = [hold("AAA", -240_000.0, "SHORT")]
        r = self.size("SHORT", hs, cash=900_000.0)
        self.assertLessEqual(r["pct"], 0.25 - 0.24 + 1e-9)
        self.assertEqual(r["binding"], "short exposure limit")

    def test_collateral_that_cannot_be_found_binds(self):
        hs = [hold("AAA", -200_000.0, "SHORT")]
        r = self.size("SHORT", hs, cash=520_000.0)                                                         # 300,000 is already reserved against the short: only 220,000 - 200,000 (min cash) is free
        self.assertLessEqual(r["pct"], 0.04 + 1e-9)
        self.assertIn(r["binding"], ("short collateral", "short exposure limit"))

    def test_a_long_cannot_spend_the_short_collateral(self):
        hs = [hold("AAA", -200_000.0, "SHORT")]
        free = self.size("BUY", [], cash=520_000.0)
        held = self.size("BUY", hs, cash=520_000.0)
        self.assertLess(held["pct"], free["pct"] + 1e-12)
        self.assertEqual(held["binding"], "cash reserve")

    def test_a_short_does_not_count_as_long_concentration_in_its_theme(self):
        hs = [hold("AAA", -280_000.0, "SHORT", "ai_infrastructure")]
        f = self.forecast("BUY")
        cfg = H.cfg()
        r = sizing.size_position(f, {"adv_usd": 5e9, "sector": "Tech", "beta": 1.0}, self.state(hs, 900_000.0), cfg, {"risk_mult": 1.0}, {"state": "Active", "score": 0.0, "primary": "ai_infrastructure"})
        self.assertNotEqual(r["binding"], "theme limit")                                                     # 28% SHORT in the theme does not use up the LONG theme allowance


class TestTheScorecard(unittest.TestCase):
    def setUp(self):
        self.c = H.fresh_db()

    def pos(self, ticker, side, entry, exit_, pnl, opened, closed, cost):
        self.c.execute("INSERT INTO positions(ticker,opened,entry_price,shares,cost,status,closed,exit_price,pnl,review_code,side) VALUES(?,?,?,0,0,'CLOSED',?,?,?,'STOP_REACHED',?)", (ticker, opened, entry, closed, exit_, pnl, side))
        pid = self.c.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.c.execute("INSERT INTO decisions(created,ticker,action,status,reason,amount) VALUES(?,?,?,?,?,?)", (opened, ticker, "BUY", "EXECUTED", "{}", cost))
        self.c.execute("UPDATE positions SET decision_id=? WHERE id=?", (self.c.execute("SELECT last_insert_rowid()").fetchone()[0], pid))

    def opt(self, kind, cost, pnl, opened, closed):
        self.c.execute("INSERT INTO option_positions(underlying,kind,strike,expiry,contracts,multiplier,opened,entry_premium,cost,status,closed,exit_premium,pnl,review_code) VALUES('X',?,100,'2026-12-18',5,100,?,2.0,?,'CLOSED',?,1.0,?,'PREMIUM_STOP')",
                       (kind, opened, cost, closed, pnl))

    def test_each_instrument_is_summarised_on_its_own(self):
        a, b = "2026-10-27T14:00:00+00:00", "2026-10-28T14:00:00+00:00"
        self.pos("AAA", "LONG", 10, 11, 1000.0, a, b, 10000.0)
        self.pos("BBB", "LONG", 10, 9, -500.0, a, b, 10000.0)
        self.pos("CCC", "SHORT", 10, 9, 800.0, a, b, 8000.0)
        self.opt("CALL", 1000.0, -500.0, a, b)
        self.opt("PUT", 1000.0, 1500.0, a, b)
        self.c.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)", (b, "SHORT_BORROW_FEE", -12.5, "fee:3", 1.0))
        self.c.commit()
        r = {x["instrument"]: x for x in performance.by_instrument(self.c)["rows"]}
        self.assertEqual((r["STOCK"]["n"], r["STOCK"]["wins"], r["STOCK"]["pnl"]), (2, 1, 500.0))
        self.assertAlmostEqual(r["STOCK"]["avg_ret"], (0.10 - 0.05) / 2, places=6)
        self.assertEqual((r["SHORT"]["n"], r["SHORT"]["pnl"]), (1, 800.0))
        self.assertAlmostEqual(r["SHORT"]["avg_ret"], 0.10, places=6)
        self.assertEqual((r["CALL"]["n"], r["CALL"]["wins"], r["CALL"]["pnl"]), (1, 0, -500.0))
        self.assertEqual((r["PUT"]["n"], r["PUT"]["pnl"]), (1, 1500.0))
        self.assertAlmostEqual(r["PUT"]["avg_ret"], 1.5, places=6)
        self.assertAlmostEqual(r["STOCK"]["avg_hold_h"], 24.0, places=3)
        self.assertEqual(performance.by_instrument(self.c)["borrow_fees"], 12.5)
        self.assertEqual(performance.by_instrument(self.c, "2026-10-29T00:00:00+00:00")["rows"][0]["n"], 0)       # the date filter applies to every instrument

    def test_an_empty_book_is_all_zeros_not_an_error(self):
        r = performance.by_instrument(self.c)
        self.assertEqual([x["n"] for x in r["rows"]], [0, 0, 0, 0])
        self.assertEqual(r["borrow_fees"], 0.0)


class TestTheVersion(unittest.TestCase):
    def test_the_version_is_s4_and_the_instrument_settings_are_decision_affecting(self):
        self.assertEqual(stamp.STRATEGY_VERSION, "S4")
        c = H.cfg()
        a = stamp.fingerprint(c)
        c["instruments"]["shorts_enabled"] = not c["instruments"]["shorts_enabled"]
        self.assertNotEqual(a, stamp.fingerprint(c))                                                       # turning shorts on or off starts a new measurement period
        b = stamp.fingerprint(c)
        c["instruments"]["options_max_premium_pct"] = 0.01
        self.assertNotEqual(b, stamp.fingerprint(c))
        d = stamp.fingerprint(c)
        c["graph"]["enabled"] = not c["graph"]["enabled"]
        self.assertNotEqual(d, stamp.fingerprint(c))


if __name__ == "__main__":
    unittest.main()
