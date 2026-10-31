"""Gaps found by mutation testing of the options engine, the graph and the correlation edges."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_options as TO  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import db, graph, options as O, pipeline, portfolio as pf  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

T_PRE, T_OPEN = TL.T_PRE, TL.T_OPEN
SECOND = "NVIDIA wins a huge new data center order and lifts its outlook again"


class TestWhereOptionsAreNotUsed(TO.Base):
    def stock_instead(self):
        self.cycle_with(T_PRE, TL.NVDA_NEWS)
        self.cycle_with(T_OPEN)
        self.assertEqual(self.one("SELECT COUNT(*) FROM option_positions")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 1)

    def test_an_illiquid_stock_stays_a_stock(self):
        self.profile(3.0e12, 5.0e7)                                                                  # trades $50M a day: under the $100M option floor
        self.stock_instead()

    def test_a_low_priced_stock_stays_a_stock(self):
        self.cfg["instruments"]["options_min_price"] = 500.0
        self.stock_instead()

    def test_a_stock_with_no_volume_data_stays_a_stock(self):
        self.profile(3.0e12, None)
        self.c.execute("UPDATE companies SET adv_usd=NULL")
        self.c.commit()
        self.cycle_with(T_PRE, TL.NVDA_NEWS)
        self.cycle_with(T_OPEN)
        self.assertEqual(self.one("SELECT COUNT(*) FROM option_positions")[0], 0)


class TestTheMoneyIsExact(TO.Base):
    def test_the_premium_and_the_commission_are_both_paid_on_the_way_in_and_the_way_out(self):
        o = self.open_option()
        comm = self.cfg["instruments"]["options_commission"]
        self.assertAlmostEqual(o["cost"], round(o["contracts"] * 100 * o["entry_premium"] + o["contracts"] * comm, 2), places=2)
        self.mkt.price["NVDA"] = o["stop_underlying"] * 0.99
        self.guard(T_OPEN + dt.timedelta(minutes=20))
        q = self.one("SELECT * FROM option_positions")
        proceeds = self.c.execute("SELECT amount FROM ledger WHERE kind='OPTION_SELL'").fetchone()[0]
        self.assertAlmostEqual(proceeds, round(q["contracts"] * 100 * q["exit_premium"] - q["contracts"] * comm, 2), places=2)


class TestAPutIsStoppedFromAbove(TO.Base):
    def test_a_rise_through_a_puts_stop_closes_it(self):
        self.cfg["instruments"]["shorts_enabled"] = False
        o = self.open_option(TO.BEAR)
        self.assertEqual(o["kind"], "PUT")
        self.assertGreater(o["stop_underlying"], o["entry_underlying"])                              # a put's stop is ABOVE the price
        self.assertLess(o["target_underlying"], o["entry_underlying"])
        self.mkt.price["NVDA"] = o["stop_underlying"] * 1.005
        self.guard(T_OPEN + dt.timedelta(minutes=20))
        q = self.one("SELECT * FROM option_positions")
        self.assertEqual((q["status"], q["review_code"]), ("CLOSED", "UNDERLYING_STOP"))
        self.assertLess(q["pnl"], 0)

    def test_a_fall_to_a_puts_target_makes_money(self):
        self.cfg["instruments"]["shorts_enabled"] = False
        o = self.open_option(TO.BEAR)
        self.mkt.price["NVDA"] = o["target_underlying"] * 0.99
        self.guard(T_OPEN + dt.timedelta(minutes=20))
        q = self.one("SELECT * FROM option_positions")
        self.assertEqual(q["status"], "CLOSED")
        self.assertGreater(q["pnl"], 0)


class TestNoTradingOnAStalePrice(TO.Base):
    def test_a_price_that_would_trigger_an_exit_is_not_acted_on_unless_it_is_verified(self):
        o = self.open_option()
        for p in self.mkt.age:
            self.mkt.age[p] = 900                                                                      # no fresh verified quote
        self.c.execute("UPDATE quote_cache SET price=?", (o["stop_underlying"] * 0.8,))                  # the last KNOWN price is far through the stop
        self.c.commit()
        self.mkt.price["NVDA"] = o["stop_underlying"] * 0.8
        self.guard(T_OPEN + dt.timedelta(minutes=20))
        q = self.one("SELECT * FROM option_positions")
        self.assertEqual(q["status"], "OPEN")                                                          # it is marked (equity stays current) but never sold on an unverified price
        self.assertLess(q["last_mark"], o["last_mark"])


class TestTheCashReserveAndDuplicates(TO.Base):
    def test_an_option_cannot_be_bought_with_the_cash_that_must_stay_in_reserve(self):
        self.c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,status,sector,side) VALUES('LMT','t',?,850.0,1000,850000,'OPEN','Defense','LONG')", (iso(T_PRE),))
        self.c.execute("INSERT INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(1,'OPEN',1000,850000,?)", (iso(T_PRE),))
        self.c.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)", (iso(T_PRE), "VIRTUAL_BUY", -850000.0, "LMT:0", 150000.0))
        self.c.commit()                                                                                 # cash 150k of 1,000k equity: already under the 20% reserve
        self.cycle_with(T_PRE, TL.NVDA_NEWS)
        self.cycle_with(T_OPEN)
        self.assertEqual(self.one("SELECT COUNT(*) FROM option_positions")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions WHERE ticker='NVDA'")[0], 0)

    def test_a_second_idea_on_a_stock_that_already_has_an_option_is_a_duplicate_not_a_second_bet(self):
        self.open_option()
        self.mkt.now = T_OPEN + dt.timedelta(minutes=30)
        items = [(SECOND, "AI capex", TL.RF(T_OPEN + dt.timedelta(minutes=25)))]
        pipeline.run_cycle(self.c, self.cfg, H.universe(), TL.SRC, self.deps(items), T_OPEN + dt.timedelta(minutes=30))
        self.assertEqual(self.one("SELECT COUNT(*) FROM option_positions")[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)                              # and the SHARES were not bought on top of the option
        st = [r[0] for r in self.c.execute("SELECT status FROM decisions WHERE ticker='NVDA' ORDER BY id")]
        self.assertEqual(st[0], "EXECUTED")
        self.assertIn("DUPLICATE", st[1:])


class TestTheSecondLineOfDefence(TO.Base):
    """Sizing and the duplicate gate normally stop these first; the option code re-checks because the moments differ. Called directly, bypassing the first line."""

    def idea(self):
        from paper_agent import ev
        c = {"ticker": "NVDA", "event_type": "guidance_raise", "direction": 1.0, "magnitude": 1.0, "conf": 0.9, "credibility": 0.95, "age_hours": 0.5, "horizon": 3, "headline": "NVIDIA raises outlook"}
        f = ev.build(self.c, c, self.cfg, {"regime": "neutral", "risk_mult": 0.8}, T_OPEN, None)
        comp = dict(self.c.execute("SELECT * FROM companies WHERE ticker='NVDA'").fetchone())
        return c, f, comp

    def decision(self):
        self.c.execute("INSERT INTO decisions(created,ticker,action,status,reason) VALUES(?,?,?,?,?)", (iso(T_OPEN), "NVDA", "BUY", "PROPOSED", "{}"))
        self.c.commit()
        return self.c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_the_option_purchase_itself_refuses_to_break_the_cash_reserve(self):
        from paper_agent import trader
        c, f, comp = self.idea()
        plan = O.plan_for(self.c, self.cfg, f, c, comp, 120.0, 1_000_000.0, T_OPEN)
        self.assertIsNotNone(plan, "setup: an idea strong enough for an option")
        self.c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,status,sector,side) VALUES('LMT','t',?,850.0,1000,850000,'OPEN','Defense','LONG')", (iso(T_PRE),))
        self.c.execute("INSERT INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(1,'OPEN',1000,850000,?)", (iso(T_PRE),))
        self.c.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)", (iso(T_PRE), "VIRTUAL_BUY", -850000.0, "LMT:0", 150000.0))
        self.c.commit()                                                                                 # cash 150k of 1,000k: already below the 20% reserve
        did = self.decision()
        r = O.open_option(self.c, self.cfg, c, f, plan, 120.0, did, 0, T_OPEN, "AI", "Tech", {}, trader._set_decision)
        self.assertEqual(r["status"], "CAPACITY_DEFERRED")
        self.assertEqual(self.one("SELECT COUNT(*) FROM option_positions")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM ledger WHERE kind='OPTION_BUY'")[0], 0)

    def test_the_option_purchase_respects_the_collateral_held_against_a_short(self):
        from paper_agent import trader
        c, f, comp = self.idea()
        plan = O.plan_for(self.c, self.cfg, f, c, comp, 120.0, 1_000_000.0, T_OPEN)
        self.assertIsNotNone(plan)
        self.c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,status,sector,side) VALUES('XYZ','t',?,100.0,2000,200000,'OPEN','Tech','SHORT')", (iso(T_PRE),))
        self.c.execute("INSERT INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(1,'OPEN',2000,200000,?)", (iso(T_PRE),))
        self.c.execute("INSERT OR REPLACE INTO quote_cache(ticker,price,source,quoted_at,updated_epoch) VALUES('XYZ',100.0,'T',?,0)", (iso(T_PRE),))
        self.c.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)", (iso(T_PRE), "VIRTUAL_SHORT", 200000.0, "XYZ:0", 1200000.0))
        self.c.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)", (iso(T_PRE), "RESET", -900000.0, "x", 300000.0))
        self.c.commit()                                                                                 # cash 300k, equity ~100k, 300k of the cash is the short's collateral
        hs = pf.holdings(self.c, T_OPEN)
        floor = self.cfg["risk"]["min_cash_pct"] * pf.equity_of(self.c, hs, T_OPEN)["equity"] * 0.99
        cost = plan["contracts"] * 100 * plan["ask"]
        self.assertGreater(300000.0 - cost, floor)                                                       # without the collateral it would look affordable...
        self.assertLess(300000.0 - cost - pf.short_reserve(hs, self.cfg), floor)                         # ...with it, it is not
        did = self.decision()
        r = O.open_option(self.c, self.cfg, c, f, plan, 120.0, did, 0, T_OPEN, "AI", "Tech", {}, trader._set_decision)
        self.assertEqual(r["status"], "CAPACITY_DEFERRED")

    def test_a_stock_with_an_open_option_gets_no_second_plan(self):
        self.open_option()
        c, f, comp = self.idea()
        self.assertIsNone(O.plan_for(self.c, self.cfg, f, c, comp, 120.0, 1_000_000.0, T_OPEN))


class TestALongCannotSpendShortCollateralAtTheMomentMoneyMoves(TO.Base):
    def test_exactly_the_amount_that_is_free_only_if_the_collateral_were_ignored(self):
        import test_shorts as TS
        from paper_agent import sizing, trader
        self.cfg["instruments"]["shorts_enabled"] = True
        self.cfg["instruments"]["options_enabled"] = False
        self.c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,status,sector,side) VALUES('XYZ','t',?,100.0,2000,200000,'OPEN','Tech','SHORT')", (iso(T_PRE),))
        self.c.execute("INSERT INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(1,'OPEN',2000,200000,?)", (iso(T_PRE),))
        self.c.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)", (iso(T_PRE), "VIRTUAL_SHORT", 200000.0, "XYZ:0", 1200000.0))
        self.c.execute("INSERT OR REPLACE INTO quote_cache(ticker,price,source,quoted_at,updated_epoch) VALUES('XYZ',100.0,'T',?,0)", (iso(T_PRE),))
        self.c.commit()
        hs = pf.holdings(self.c, T_OPEN)
        eq = pf.equity_of(self.c, hs, T_OPEN)
        reserve = pf.short_reserve(hs, self.cfg)                                                       # 300,000 held against a 200,000 short
        floor = self.cfg["risk"]["min_cash_pct"] * eq["equity"] * 0.99
        amount = eq["cash"] - floor - 0.5 * 200000.0                                                    # leaves cash >= floor, but cash - reserve < floor
        self.assertGreater(eq["cash"] - amount, floor)
        self.assertLess(eq["cash"] - amount - reserve, floor)
        orig = sizing.size_position
        sizing.size_position = lambda *a, **k: {"pct": amount / eq["equity"], "amount": amount, "reasons": [], "binding": "x", "ok": True}
        try:
            self.mkt.price["LMT"] = 450.0
            self.prev_close["LMT"] = 450.0
            c = {"ticker": "LMT", "event_type": "guidance_raise", "direction": 1.0, "magnitude": 1.0, "conf": 0.9, "credibility": 0.95, "age_hours": 0.5, "horizon": 6, "origin": "event", "headline": "Lockheed raises its outlook", "source": "Wire"}
            q = lambda conn, t, purpose: {"status": "PASS", "execution_eligible": True, "price": 450.0, "providers_used": ["X"], "quoted_at": iso(T_OPEN), "age_seconds": 3}      # noqa: E731
            r = trader.process_candidate(self.c, self.cfg, H.universe(), c, {"regime": "neutral", "risk_mult": 0.8}, T_OPEN, q, self.series)
        finally:
            sizing.size_position = orig
        self.assertEqual(r["status"], "CAPACITY_DEFERRED")


class TestOppositeMovesAreNotNeighbours(unittest.TestCase):
    def test_a_stock_that_moves_the_other_way_is_not_linked_as_a_co_mover(self):
        import random
        c, cfg = H.fresh_db(), H.cfg()
        rnd = random.Random(5)
        a = [rnd.gauss(0, 0.01) for _ in range(80)]
        for sym, rets in (("AAA", a), ("CCC", [-x for x in a])):
            p, base = 100.0, dt.date(2026, 8, 1)
            for i, r in enumerate(rets):
                p *= 1 + r
                c.execute("INSERT OR REPLACE INTO market_series(symbol,day,close,volume) VALUES(?,?,?,?)", (sym, (base + dt.timedelta(days=i)).isoformat(), p, 1000))
        c.commit()
        graph.refresh_data_edges(c, cfg, T_PRE, force=True)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM relations WHERE source='data'").fetchone()[0], 0)          # corr = -1: a hedge, not a neighbour


if __name__ == "__main__":
    unittest.main()
