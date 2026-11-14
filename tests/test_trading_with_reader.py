"""The article reader sits in FRONT of buying. It must never stop a good trade from being bought and sold, in any mode, and a pages-all-fail day (the common case) must behave exactly like before."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_reader as TR  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import db, net  # noqa: E402


def lifecycle(mode, pages, exit_kind):
    """news -> (article) -> queued before the open -> bought at the open -> price moves -> sold. Returns the world and what happened."""
    w = TR.World()
    w.setUp()
    w.cfg["reader"]["mode"] = mode
    w.pages = pages
    w.cyc(TL.T_PRE, [TR.HEAD_BULL])
    queued = w.c.execute("SELECT status FROM decisions WHERE action='BUY' ORDER BY id LIMIT 1").fetchone()
    rep = w.cyc(TL.T_OPEN)
    pos = w.c.execute("SELECT * FROM positions ORDER BY id LIMIT 1").fetchone()
    plan = w.c.execute("SELECT stop_price,target_price,max_hold_days FROM position_forecast_plans ORDER BY id LIMIT 1").fetchone() if pos else None
    out = {"queued": queued[0] if queued else None, "opened": bool(pos), "report": rep}
    if pos:
        t = TL.T_OPEN + dt.timedelta(minutes=30)
        w.mkt.price["NVDA"] = plan["stop_price"] - 0.5 if exit_kind == "stop" else plan["target_price"] + 0.5
        w.mkt.now = t
        from paper_agent import pipeline
        pipeline.run_guard(w.c, w.cfg, w.deps(), t)
        out["after"] = w.c.execute("SELECT status,review_code,pnl FROM positions ORDER BY id LIMIT 1").fetchone()
    return w, out


class TestBuysAndSellsWorkWithTheReaderOn(unittest.TestCase):
    def test_an_agreeing_article_the_trade_is_bought_and_a_stop_loss_sells_it_in_every_mode(self):
        for mode in ("off", "shadow", "guard", "full"):
            w, o = lifecycle(mode, {"http://x/0": TR.page(TR.BULL_BODY)}, "stop")
            try:
                self.assertEqual((o["queued"], o["opened"]), ("AWAITING_MARKET", True), mode)                  # waits for the open, then buys
                self.assertEqual((o["after"]["status"], o["after"]["review_code"]), ("CLOSED", "STOP_REACHED"), mode)
                self.assertLess(o["after"]["pnl"], 0)
                self.assertAlmostEqual(db.cash(w.c) - 1_000_000, o["after"]["pnl"], places=1)                  # cash moved by exactly the realised result
                TL.books_balance(w, w.c)
                if mode != "off":
                    self.assertEqual(o["report"]["articles"]["mode"], mode)
            finally:
                w.tearDown()

    def test_a_target_hit_is_handled_and_the_position_is_then_sold_with_balanced_books(self):
        from paper_agent import pipeline
        from paper_agent.timeutil import add_trading_days, market_state, parse_ts
        for mode in ("guard", "full"):
            w, o = lifecycle(mode, {"http://x/0": TR.page(TR.BULL_BODY)}, "target")
            try:
                self.assertTrue(o["opened"], mode)
                r = pipeline.run_guard(w.c, w.cfg, w.deps(), TL.T_OPEN + dt.timedelta(minutes=45))
                self.assertEqual(r["errors"], 0, mode)                                                            # at the target the swing plan banks, extends or ratchets: never an error
                opened = w.c.execute("SELECT opened FROM positions").fetchone()[0]
                plan = w.c.execute("SELECT max_hold_days FROM position_forecast_plans ORDER BY id LIMIT 1").fetchone()
                later = add_trading_days(parse_ts(opened), int(plan["max_hold_days"]) + 2).replace(hour=15, minute=0, second=0, microsecond=0)
                self.assertTrue(market_state(later)["execution"])
                w.mkt.price["NVDA"] = 120.0
                w.mkt.now = later
                pipeline.run_guard(w.c, w.cfg, w.deps(), later)
                self.assertEqual(w.c.execute("SELECT status FROM positions ORDER BY id LIMIT 1").fetchone()[0], "CLOSED", mode)
                self.assertGreater(w.c.execute("SELECT COUNT(*) FROM ledger WHERE kind LIKE 'VIRTUAL_SELL%'").fetchone()[0], 0, mode)
                TL.books_balance(w, w.c)
            finally:
                w.tearDown()

    def test_when_every_page_fails_to_load_the_day_is_exactly_like_before(self):
        base = None
        for mode in ("off", "guard", "full"):
            w, o = lifecycle(mode, {}, "stop")                                                                      # every article is a 404
            try:
                snap = (o["queued"], o["opened"], o["after"]["review_code"], round(o["after"]["pnl"], 2))
                base = base or snap
                self.assertEqual(snap, base, mode)                                                                  # identical trade, identical result
                TL.books_balance(w, w.c)
            finally:
                w.tearDown()

    def test_a_contradicting_article_stops_the_buy_and_leaves_the_books_untouched(self):
        for mode in ("guard", "full"):
            w, o = lifecycle(mode, {"http://x/0": TR.page(TR.BEAR_BODY)}, "stop")
            try:
                self.assertEqual((o["queued"], o["opened"]), (None, False), mode)
                self.assertEqual(db.cash(w.c), 1_000_000, mode)
                self.assertEqual(w.c.execute("SELECT COUNT(*) FROM ledger WHERE kind LIKE 'VIRTUAL_%'").fetchone()[0], 0)
            finally:
                w.tearDown()

    def test_a_vague_headline_is_bought_and_sold_only_in_full_mode_and_the_books_balance(self):
        for mode, bought in (("guard", False), ("full", True)):
            w = TR.World()
            w.setUp()
            try:
                w.cfg["reader"]["mode"] = mode
                w.pages = {"http://x/0": TR.page(TR.BULL_BODY)}
                w.cyc(TL.T_PRE, [TR.HEAD_VAGUE])
                w.cyc(TL.T_OPEN)
                self.assertEqual(bool(w.c.execute("SELECT 1 FROM positions").fetchone()), bought, mode)
                if bought:
                    plan = w.c.execute("SELECT stop_price FROM position_forecast_plans ORDER BY id LIMIT 1").fetchone()
                    w.mkt.price["NVDA"] = plan["stop_price"] - 0.5
                    t = TL.T_OPEN + dt.timedelta(minutes=30)
                    w.mkt.now = t
                    from paper_agent import pipeline
                    pipeline.run_guard(w.c, w.cfg, w.deps(), t)
                    self.assertEqual(w.c.execute("SELECT status FROM positions").fetchone()[0], "CLOSED")
                TL.books_balance(w, w.c)
            finally:
                w.tearDown()

    def test_even_a_crash_in_the_whole_reader_step_cannot_stop_a_trade(self):
        from unittest import mock
        from paper_agent import reader
        w = TR.World()
        w.setUp()
        try:
            w.cfg["reader"]["mode"] = "full"
            with mock.patch.object(reader, "enrich", side_effect=RuntimeError("the reader itself blew up")):
                rep = w.cyc(TL.T_PRE, [TR.HEAD_BULL])
            w.cyc(TL.T_OPEN)
            self.assertEqual(rep["errors"], [])                                                                       # the stage did not fail
            self.assertEqual(rep["articles"]["mode"], "error")                                                          # ...and the report says the reader failed
            self.assertEqual(w.c.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 1)                          # the trade happened anyway
        finally:
            w.tearDown()

    def test_a_crashing_article_fetcher_cannot_stop_a_trade(self):
        w = TR.World()
        w.setUp()
        try:
            w.cfg["reader"]["mode"] = "full"
            w.pages = None                                                                                          # the fake fetcher itself raises (TypeError) on every call
            w.cyc(TL.T_PRE, [TR.HEAD_BULL])
            w.cyc(TL.T_OPEN)
            self.assertEqual(w.c.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 1)
        finally:
            w.tearDown()


if __name__ == "__main__":
    unittest.main()
