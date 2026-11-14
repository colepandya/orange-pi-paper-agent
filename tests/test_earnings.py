"""A 1-3% swing stop cannot protect against an earnings gap, so: do not open a trade that would hold through a report, and sell before the close when one would land overnight."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_explain as TE  # noqa: E402
import test_fake_internet as TF  # noqa: E402
import test_pretrade as TP  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import earnings, explain, net, preflight  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

UTC = dt.timezone.utc
TUE_NOON = dt.datetime(2026, 10, 27, 16, 0, tzinfo=UTC)       # Tuesday 12:00 New York
SECOND = "NVIDIA wins $3 billion data center contract with major cloud provider"      # a contract win: NOT an earnings release


def cal(c, ticker, date, hour):
    c.execute("INSERT OR REPLACE INTO earnings_calendar(ticker,date,hour,fetched) VALUES(?,?,?,?)", (ticker, date, hour, iso(TUE_NOON)))


class TestCalendar(unittest.TestCase):
    def test_parsing_survives_junk(self):
        good = {"earningsCalendar": [{"date": "2026-10-29", "hour": "AMC", "symbol": "aapl"}, {"date": "bad", "symbol": "X"}, {"symbol": "Y"}, "junk", None, {"date": "2026-10-30", "hour": None, "symbol": "MSFT"}]}
        self.assertEqual(earnings.parse(good), [{"ticker": "AAPL", "date": "2026-10-29", "hour": "amc"}, {"ticker": "MSFT", "date": "2026-10-30", "hour": ""}])
        for junk in (None, [], {}, "x", {"earningsCalendar": "x"}, {"earningsCalendar": None}):
            self.assertEqual(earnings.parse(junk), [])

    def test_refresh_stores_replaces_and_is_throttled(self):
        c = H.fresh_db()
        os.environ["FINNHUB_API_KEY"] = "k" * 20
        calls = []

        def f(url, **kw):
            calls.append(url)
            return {"earningsCalendar": [{"date": "2026-10-29", "hour": "amc", "symbol": "AAPL"}]}, 200
        self.assertEqual(earnings.refresh(c, {}, TUE_NOON, f)["releases"], 1)
        self.assertEqual(earnings.refresh(c, {}, TUE_NOON + dt.timedelta(hours=1), f), {"skipped": "not due"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(earnings.refresh(c, {}, TUE_NOON + dt.timedelta(hours=12), lambda url, **kw: ({"earningsCalendar": []}, 200))["releases"], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM earnings_calendar").fetchone()[0], 0)              # a report that disappeared from the calendar is removed

    def test_an_outage_never_raises_and_retries_in_an_hour(self):
        c = H.fresh_db()
        os.environ["FINNHUB_API_KEY"] = "k" * 20

        def boom(url, **kw):
            raise net.FetchError("HTTP 403", 403)
        self.assertIn("fetch failed", earnings.refresh(c, {}, TUE_NOON, boom)["skipped"])
        self.assertEqual(earnings.refresh(c, {}, TUE_NOON + dt.timedelta(minutes=5), boom), {"skipped": "retrying later"})
        self.assertIn("fetch failed", earnings.refresh(c, {}, TUE_NOON + dt.timedelta(hours=1, minutes=1), boom)["skipped"])

    def test_without_a_key_it_quietly_does_nothing(self):
        os.environ.pop("FINNHUB_API_KEY", None)
        self.assertEqual(earnings.refresh(H.fresh_db(), {}, TUE_NOON), {"skipped": "no FINNHUB_API_KEY"})

    def test_which_reports_are_still_ahead(self):
        c = H.fresh_db()
        cal(c, "A", "2026-10-27", "bmo")        # today before the open: already out
        cal(c, "B", "2026-10-27", "amc")        # today after the close: ahead
        cal(c, "C", "2026-10-28", "bmo")        # tomorrow before the open: ahead (overnight)
        cal(c, "D", "2026-11-05", "amc")        # next week
        self.assertIsNone(earnings.upcoming(c, "A", TUE_NOON))
        self.assertEqual(earnings.upcoming(c, "B", TUE_NOON)["label"], "Tue 27 Oct (after the close)")
        self.assertEqual(earnings.upcoming(c, "C", TUE_NOON)["days"], 1)
        self.assertEqual(earnings.upcoming(c, "D", TUE_NOON)["days"], 9)

    def test_inside_the_holding_window_and_landing_overnight(self):
        c = H.fresh_db()
        cal(c, "B", "2026-10-27", "amc")
        cal(c, "C", "2026-10-28", "bmo")
        cal(c, "E", "2026-10-28", "amc")
        cal(c, "D", "2026-11-05", "amc")
        self.assertTrue(earnings.inside_hold(c, "E", TUE_NOON, 3))
        self.assertIsNone(earnings.inside_hold(c, "D", TUE_NOON, 3))
        self.assertTrue(earnings.lands_overnight(c, "B", TUE_NOON))             # today after the close
        self.assertTrue(earnings.lands_overnight(c, "C", TUE_NOON))             # tomorrow before the open
        self.assertIsNone(earnings.lands_overnight(c, "E", TUE_NOON))           # tomorrow AFTER the close: not overnight yet
        self.assertIsNone(earnings.lands_overnight(c, "D", TUE_NOON))


class World(TE.World):
    pass


def world():
    w = TE.world()
    return w


class TestEntryGate(unittest.TestCase):
    def queue(self, headline, calendar=None, **strategy):
        w = world()
        try:
            w.cfg["strategy"].update(min_reward_risk=0.5)                            # isolate the EARNINGS rule: a plain contract win is otherwise declined by the reward:risk gate
            w.cfg["strategy"].update(strategy)
            if calendar:
                cal(w.c, "NVDA", *calendar)
            w.cyc(TL.T_PRE, [headline])
            d = w.c.execute("SELECT status,reason FROM decisions WHERE ticker='NVDA' ORDER BY id LIMIT 1").fetchone()
            return d["status"], d["reason"], explain.explain(w.c, w.cfg, w.c.execute("SELECT id FROM decisions WHERE ticker='NVDA' ORDER BY id LIMIT 1").fetchone()[0], TL.T_PRE)
        finally:
            w.tearDown()

    def test_a_trade_that_would_hold_through_a_report_is_skipped_and_the_page_says_why(self):
        st, reason, ex = self.queue(SECOND, ("2026-10-29", "amc"))
        self.assertEqual(st, "REJECTED")
        self.assertIn("earnings report due Thu 29 Oct (after the close)", reason)
        self.assertTrue(ex["headline"].startswith("Passed on NVDA: A quarterly earnings report is due Thu 29 Oct"), ex["headline"])
        self.assertIn("gap a stock", " ".join(t for _o, t in ex["checks"]))

    def test_without_a_nearby_report_the_same_news_is_a_buy(self):
        self.assertEqual(self.queue(SECOND)[0], "AWAITING_MARKET")
        self.assertEqual(self.queue(SECOND, ("2026-11-12", "amc"))[0], "AWAITING_MARKET")      # a report weeks away is not inside the hold

    def test_news_that_IS_the_earnings_release_is_exempt(self):
        self.assertEqual(self.queue(TL.NVDA_NEWS, ("2026-10-29", "amc"))[0], "AWAITING_MARKET")     # "beats estimates and raises guidance"

    def test_it_can_be_switched_off(self):
        self.assertEqual(self.queue(SECOND, ("2026-10-29", "amc"), avoid_earnings=False)[0], "AWAITING_MARKET")

    def test_classic_mode_is_unaffected(self):
        self.assertEqual(self.queue(SECOND, ("2026-10-29", "amc"), swing_enabled=False)[0], "AWAITING_MARKET")


class TestExitBeforeTheReport(unittest.TestCase):
    def held(self, calendar, **strategy):
        w = world()
        w.cfg["strategy"].update(strategy)
        w.day()
        cal(w.c, "NVDA", *calendar)
        return w

    def guard_at(self, w, hh, mm):
        r = w.guard(dt.datetime(2026, 10, 27, hh, mm, tzinfo=UTC))
        return r["committed"], w.one("SELECT status,review_code FROM positions")

    def test_a_report_tonight_sells_shortly_before_the_close_not_in_the_morning(self):
        w = self.held(("2026-10-27", "amc"))
        try:
            self.assertEqual(self.guard_at(w, 15, 0)[0], 0)                               # 11:00 New York: hours to go: keep
            committed, row = self.guard_at(w, 19, 25)                                    # 15:25 New York: 35 minutes to the close
            self.assertEqual((committed, row["status"], row["review_code"]), (1, "CLOSED", "EARNINGS_AHEAD"))
        finally:
            w.tearDown()

    def test_a_report_tomorrow_before_the_open_also_sells_tonight(self):
        w = self.held(("2026-10-28", "bmo"))
        try:
            self.assertEqual(self.guard_at(w, 19, 25)[1]["review_code"], "EARNINGS_AHEAD")
        finally:
            w.tearDown()

    def test_no_sale_when_it_is_not_overnight_or_the_feature_is_off(self):
        for cal_, strat in ((("2026-10-27", "bmo"), {}), (("2026-10-28", "amc"), {}), (("2026-11-05", "amc"), {}), (("2026-10-27", "amc"), {"avoid_earnings": False})):
            w = self.held(cal_, **strat)
            try:
                self.assertEqual(self.guard_at(w, 19, 25)[0], 0, (cal_, strat))
            finally:
                w.tearDown()


class TestWholeSystem(unittest.TestCase):
    def test_the_calendar_is_fetched_through_the_real_network_layer_and_preflight_reports_it(self):
        tf = TF.TestWholeSystemAgainstTheSimulatedInternet("test_a_clean_morning_runs_without_a_single_stage_error_and_trades")
        tf.setUp()
        try:
            c, errors, net_ = tf.morning(seed=1, cycles=4, swing=True)
            self.assertEqual(errors, [])
            self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM earnings_calendar").fetchone()[0], 2)
            self.assertTrue([u for u in net_.requests if "calendar/earnings" in u])
            cfg = H.cfg()
            cfg["strategy"]["swing_enabled"] = True
            res = preflight.run(c, cfg, tf.uni, now=TF.T0 + dt.timedelta(minutes=15), live=False, check_units=False, mem_mb=lambda: 600, out=lambda s: None)
            row = next(x for x in res["checks"] if x["name"] == "Earnings calendar loaded")
            self.assertEqual(row["status"], "PASS")
            c.execute("DELETE FROM earnings_calendar")
            res = preflight.run(c, cfg, tf.uni, now=TF.T0 + dt.timedelta(minutes=15), live=False, check_units=False, mem_mb=lambda: 600, out=lambda s: None)
            row = next(x for x in res["checks"] if x["name"] == "Earnings calendar loaded")
            self.assertEqual(row["status"], "WARN")
            self.assertIn("NOT protected", row["detail"])                                   # an empty calendar is loud, never silent
        finally:
            tf.tearDown()


if __name__ == "__main__":
    unittest.main()
