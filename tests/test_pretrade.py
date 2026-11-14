"""A BUY decided before the open is re-checked at the open against everything that happened SINCE it was decided. (Before: it executed no matter what, even after a war headline.)"""
import datetime as dt
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import pretrade, funnel, pipeline, report  # noqa: E402

RF = TL.RF
WAR = "China launches missile strikes on Taiwan as global markets plunge"
BAD = "NVIDIA cuts full-year outlook as DOJ opens probe into export violations"


SRC2 = TL.SRC + [dict(TL.SRC[0], id="t2", name="Wire2", url="http://y/rss")]       # a second outlet: a real shock is reported by more than one


class Scenario(TL.Base):
    reaction = -0.02                                                                    # SPY down 2% on the day: a real shock is confirmed by the market itself (tests can override)

    def runTest(self):
        pass

    def setUp(self):
        super().setUp()
        self._rx = mock.patch.object(pretrade, "market_reaction", side_effect=lambda *a, **k: self.reaction)
        self._rx.start()

    def tearDown(self):
        self._rx.stop()
        super().tearDown()

    def cyc(self, now, titles=(), sources=None):
        self.mkt.now = now
        items = [(t, "", RF(now - dt.timedelta(minutes=10))) for t in titles]
        srcs = sources or (SRC2 if WAR in titles else TL.SRC)                          # WAR = a widely reported shock: two outlets carry it
        return pipeline.run_cycle(self.c, self.cfg, H.universe(), srcs, self.deps(items), now)

    def buy(self):
        return self.c.execute("SELECT status,reason FROM decisions WHERE ticker='NVDA' AND action='BUY' ORDER BY id LIMIT 1").fetchone()

    def positions(self):
        return self.c.execute("SELECT COUNT(*) FROM positions WHERE ticker='NVDA'").fetchone()[0]

    def gate(self):
        return json.loads(self.buy()["reason"])["gates"][-1]

    def day(self, later=(), at=30, open_price=None, tweak=None):
        if tweak:
            tweak(self.cfg)
        self.cyc(TL.T_PRE, [TL.NVDA_NEWS])
        self.assertEqual(self.buy()["status"], "AWAITING_MARKET")                      # the BUY is queued before the open
        if later:
            self.cyc(TL.T_PRE + dt.timedelta(minutes=at), later)
        if open_price:
            self.mkt.price["NVDA"] = open_price
        self.cyc(TL.T_OPEN)


class TestLastLook(unittest.TestCase):
    def run_day(self, **kw):
        s = Scenario()
        s.setUp()
        try:
            s.day(**kw)
            return s.buy()["status"], s.positions(), s.gate() if s.buy()["status"] in ("VETOED", "HELD") else ""
        finally:
            s.tearDown()

    def test_nothing_happens_overnight_the_buy_executes(self):
        self.assertEqual(self.run_day()[:2], ("EXECUTED", 1))

    def test_later_bad_news_about_the_same_company_cancels_the_buy(self):
        st, pos, why = self.run_day(later=[BAD])
        self.assertEqual((st, pos), ("VETOED", 0))
        self.assertIn("arrived after this BUY was decided", why)
        self.assertIn("DOJ opens probe", why)                                            # the reason quotes the news

    def test_a_world_event_holds_the_buy_instead_of_buying_into_it(self):
        st, pos, why = self.run_day(later=[WAR])
        self.assertEqual((st, pos), ("HELD", 0))
        self.assertIn("market-wide shock", why)
        self.assertIn("released after", why)

    def test_the_hold_is_released_once_the_shock_has_aged_out_and_the_buy_then_executes(self):
        s = Scenario()
        s.setUp()
        try:
            s.cfg["risk"]["shock_hold_hours"] = 0.75                                     # 45 minutes
            s.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            s.cyc(TL.T_PRE + dt.timedelta(minutes=10), [WAR])                            # shock at ~12:50 (10 min after the BUY)
            s.cyc(TL.T_OPEN)                                                             # shock is ~35 min old -> still held
            self.assertEqual(s.buy()["status"], "HELD")
            s.cyc(TL.T_OPEN + dt.timedelta(minutes=15))                                  # ~50 min old -> released
            self.assertEqual((s.buy()["status"], s.positions()), ("EXECUTED", 1))
        finally:
            s.tearDown()

    def test_an_overnight_crash_cancels_the_buy(self):
        st, pos, why = self.run_day(open_price=109.0)                                    # -9.2% vs the 120.00 previous close
        self.assertEqual((st, pos), ("VETOED", 0))
        self.assertIn("below its previous close", why)

    def test_a_gap_up_that_already_made_the_move_cancels_the_buy(self):
        st, pos, why = self.run_day(open_price=129.6)                                    # +8%
        self.assertEqual((st, pos), ("VETOED", 0))
        self.assertIn("the edge is gone", why)

    def test_a_normal_gap_does_not_interfere(self):
        self.assertEqual(self.run_day(open_price=118.2)[:2], ("EXECUTED", 1))            # -1.5%
        self.assertEqual(self.run_day(open_price=122.4)[:2], ("EXECUTED", 1))            # +2%

    def test_positive_or_unrelated_news_does_not_interfere(self):
        self.assertEqual(self.run_day(later=["NVIDIA raises full-year outlook again on record data center demand"])[:2], ("EXECUTED", 1))
        self.assertEqual(self.run_day(later=["AMD cuts outlook as DOJ opens probe into export violations"])[:2], ("EXECUTED", 1))

    def test_news_that_arrived_BEFORE_the_decision_was_already_known_and_does_not_veto(self):
        s = Scenario()
        s.setUp()
        try:
            s.cyc(TL.T_PRE - dt.timedelta(hours=3), [BAD])                              # old news, before the BUY was decided
            s.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            s.cyc(TL.T_OPEN)
            self.assertNotEqual(s.buy()["status"], "VETOED")
        finally:
            s.tearDown()

    def test_every_check_can_be_switched_off_from_settings(self):
        off = lambda c: c["risk"].update(veto_adverse_news=False, max_gap_down_pct=1.0, max_gap_up_pct=1.0)  # noqa: E731
        self.assertEqual(self.run_day(later=[BAD], tweak=off)[:2], ("EXECUTED", 1))
        self.assertEqual(self.run_day(later=[WAR], tweak=off)[:2], ("EXECUTED", 1))
        self.assertEqual(self.run_day(open_price=100.0, tweak=off)[:2], ("EXECUTED", 1))
        no_hold = lambda c: c["risk"].update(shock_hold_hours=0)  # noqa: E731
        self.assertEqual(self.run_day(later=[WAR], tweak=no_hold)[:2], ("EXECUTED", 1))

    def test_a_vetoed_buy_is_final_and_is_not_retried(self):
        s = Scenario()
        s.setUp()
        try:
            s.day(later=[BAD])
            s.mkt.price["NVDA"] = 120.0
            s.cyc(TL.T_OPEN + dt.timedelta(minutes=20))
            self.assertEqual((s.buy()["status"], s.positions()), ("VETOED", 0))
        finally:
            s.tearDown()

    def test_waiting_and_held_buys_are_visible_in_the_funnel_and_the_report(self):
        s = Scenario()
        s.setUp()
        try:
            s.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            s.cyc(TL.T_PRE + dt.timedelta(minutes=30), [WAR])
            s.cyc(TL.T_OPEN)
            rep = funnel.report(s.c, 6, TL.T_OPEN)
            held = [p for p in rep["pending"] if p["status"] == "HELD"]
            self.assertEqual(len(held), 1)
            self.assertIn("market-wide shock", held[0]["waiting_for"])
        finally:
            s.tearDown()


class TestBuysQueuedByOlderBuilds(unittest.TestCase):
    """REGRESSION: a BUY queued by an older build has no 'previous close' stored, so the gap check silently skipped it and it executed straight into a -9% gap."""

    def legacy(self, w):
        w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
        d = w.c.execute("SELECT id,reason FROM decisions WHERE ticker='NVDA' ORDER BY id LIMIT 1").fetchone()
        r = json.loads(d["reason"])
        r.pop("ref_price", None)
        r.pop("decided_at", None)                                                    # exactly what an older build stored
        w.c.execute("UPDATE decisions SET reason=? WHERE id=?", (json.dumps(r), d["id"]))
        return d["id"]

    def reason(self, w, did):
        return json.loads(w.c.execute("SELECT reason FROM decisions WHERE id=?", (did,)).fetchone()[0])

    def test_the_reference_is_filled_in_while_the_market_is_closed_and_the_gap_then_cancels_the_buy(self):
        w = Scenario()
        w.setUp()
        try:
            did = self.legacy(w)
            w.cyc(TL.T_PRE + dt.timedelta(minutes=5))
            r = self.reason(w, did)
            self.assertAlmostEqual(r["ref_price"], 120.0, places=4)                  # the previous close
            self.assertTrue(r["decided_at"])
            w.mkt.price["NVDA"] = 109.0
            w.cyc(TL.T_OPEN)
            self.assertEqual(w.c.execute("SELECT status FROM decisions WHERE id=?", (did,)).fetchone()[0], "VETOED")
            self.assertEqual(w.positions(), 0)
        finally:
            w.tearDown()

    def test_a_normal_gap_still_executes_and_an_existing_reference_is_never_overwritten(self):
        w = Scenario()
        w.setUp()
        try:
            did = self.legacy(w)
            r = self.reason(w, did)
            r["ref_price"] = 119.0                                                    # a real reference stored earlier
            w.c.execute("UPDATE decisions SET reason=? WHERE id=?", (json.dumps(r), did))
            w.cyc(TL.T_PRE + dt.timedelta(minutes=5))
            self.assertEqual(self.reason(w, did)["ref_price"], 119.0)
            w.mkt.price["NVDA"] = 118.2
            w.cyc(TL.T_OPEN)
            self.assertEqual(w.c.execute("SELECT status FROM decisions WHERE id=?", (did,)).fetchone()[0], "EXECUTED")
        finally:
            w.tearDown()

    def test_it_never_backfills_during_the_session_when_the_last_bar_is_todays_live_price(self):
        w = Scenario()
        w.setUp()
        try:
            did = self.legacy(w)
            w.mkt.down = {"twelve", "finnhub", "alpaca", "yahoo"}                      # at the open no price can be verified, so the BUY stays queued
            w.cyc(TL.T_OPEN)
            self.assertEqual(w.c.execute("SELECT status FROM decisions WHERE id=?", (did,)).fetchone()[0], "AWAITING_QUOTE")
            self.assertIsNone(self.reason(w, did).get("ref_price"))
        finally:
            w.tearDown()

    def test_stale_or_missing_data_means_no_gap_test_rather_than_a_wrong_one(self):
        w = Scenario()
        w.setUp()
        try:
            did = self.legacy(w)
            w.c.execute("UPDATE market_series SET day=date(day,'-60 days') WHERE symbol='NVDA'")        # the whole history is two months old
            w.cyc(TL.T_PRE + dt.timedelta(minutes=5))
            self.assertIsNone(self.reason(w, did).get("ref_price"))
            w.c.execute("DELETE FROM market_series WHERE symbol='NVDA'")
            w.cyc(TL.T_PRE + dt.timedelta(minutes=10))                                         # no bar at all: must not crash
            self.assertIsNone(self.reason(w, did).get("ref_price"))
        finally:
            w.tearDown()


if __name__ == "__main__":
    unittest.main()
