"""Insights on every trade. The maths is checked against a hand-computed answer and against the cash ledger of a real simulated trade (partial sell + runner)."""
import datetime as dt
import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_pretrade as TP  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import dashboard, db, performance as P  # noqa: E402
from paper_agent.timeutil import iso, utcnow  # noqa: E402

NOW = H.NOW


def add_trade(c, ticker, pnl, amount=10000.0, reason="STOP_REACHED", closed_ago_h=1.0, held_h=5.0, event="guidance_raise", decision=True, base=None):
    base = base or NOW
    did = None
    if decision:
        c.execute("INSERT INTO decisions(created,ticker,action,status,amount,shares,entry_price,reason) VALUES(?,?,?,?,?,?,?,?)",
                  (iso(base - dt.timedelta(hours=closed_ago_h + held_h)), ticker, "BUY", "EXECUTED", amount, amount / 100.0, 100.0, json.dumps({"candidate": {"event_type": event}})))
        did = c.execute("SELECT MAX(id) FROM decisions").fetchone()[0]
    c.execute("INSERT INTO positions(ticker,opened,entry_price,shares,cost,status,closed,exit_price,pnl,review_code,decision_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (ticker, iso(base - dt.timedelta(hours=closed_ago_h + held_h)), 100.0, 0, 0, "CLOSED", iso(base - dt.timedelta(hours=closed_ago_h)), 100.0 * (1 + pnl / amount), pnl, reason, did))


def known(c):
    for i, (t, pnl) in enumerate((("AAA", 100), ("BBB", 200), ("CCC", -50), ("DDD", -150), ("EEE", 50))):
        add_trade(c, t, pnl, closed_ago_h=10 - i)


class TestMaths(unittest.TestCase):
    def test_a_hand_computed_answer(self):
        c = H.fresh_db()
        known(c)
        s = P.summarize(P.closed_trades(c))
        self.assertEqual((s["n"], s["wins"], s["losses"]), (5, 3, 2))
        self.assertAlmostEqual(s["win_rate"], 0.6)
        self.assertAlmostEqual(s["total_pnl"], 150.0)
        self.assertAlmostEqual(s["profit_factor"], 350 / 200)
        self.assertAlmostEqual(s["avg_win_pct"], (0.01 + 0.02 + 0.005) / 3)
        self.assertAlmostEqual(s["avg_loss_pct"], -0.01)
        self.assertAlmostEqual(s["payoff"], ((0.01 + 0.02 + 0.005) / 3) / 0.01)
        self.assertAlmostEqual(s["expectancy_pct"], 0.003)
        self.assertAlmostEqual(s["expectancy_usd"], 30.0)
        self.assertEqual(s["max_losing_streak"], 2)
        self.assertEqual((s["best"]["ticker"], s["worst"]["ticker"]), ("BBB", "DDD"))
        self.assertAlmostEqual(s["avg_hold_hours"], 5.0, places=3)

    def test_no_trades_and_no_losses_do_not_divide_by_zero(self):
        s = P.summarize([])
        self.assertEqual((s["n"], s["win_rate"], s["profit_factor"], s["best"]), (0, None, None, None))
        c = H.fresh_db()
        add_trade(c, "AAA", 100)
        s = P.summarize(P.closed_trades(c))
        self.assertEqual(s["profit_factor"], math.inf)
        self.assertIsNone(s["avg_loss_pct"])

    def test_grouping_days_and_the_running_total(self):
        c = H.fresh_db()
        known(c)
        add_trade(c, "AAA", -30, reason="TIME_EXPIRED", event="earnings_beat", held_h=30)
        tr = P.closed_trades(c)
        g = {r["key"]: r for r in P.group(tr, lambda t: t["exit_reason"])}
        self.assertEqual((g["STOP_REACHED"]["n"], g["TIME_EXPIRED"]["n"]), (5, 1))
        self.assertAlmostEqual(g["TIME_EXPIRED"]["total_pnl"], -30)
        self.assertEqual({r["key"] for r in P.group(tr, lambda t: t["event_type"])}, {"guidance_raise", "earnings_beat"})
        self.assertEqual(sum(r["pnl"] for r in P.by_day(tr)), sum(t["pnl"] for t in tr))
        self.assertEqual(P.cumulative(tr)[-1], sum(t["pnl"] for t in tr))
        self.assertIn(P.closed_trades(c)[-1]["hold_bucket"], P.HOLD_BUCKETS)

    def test_a_trade_whose_decision_is_missing_still_counts_with_a_pnl_but_no_percentage(self):
        c = H.fresh_db()
        add_trade(c, "ORPH", 75, decision=False)
        t = P.closed_trades(c)[0]
        self.assertEqual((t["pnl"], t["ret"], t["event_type"]), (75.0, None, "unknown"))
        P.summarize([t])


class TestEdgeCases(unittest.TestCase):
    def test_a_break_even_trade_is_neither_a_win_nor_a_loss(self):
        c = H.fresh_db()
        add_trade(c, "AAA", 100, closed_ago_h=3)
        add_trade(c, "BBB", 0.0, closed_ago_h=2)
        add_trade(c, "CCC", -50, closed_ago_h=1)
        s = P.summarize(P.closed_trades(c))
        self.assertEqual((s["n"], s["wins"], s["losses"], s["flat"]), (3, 1, 1, 1))
        self.assertAlmostEqual(s["win_rate"], 1 / 3)

    def test_a_losing_streak_is_broken_by_a_win(self):
        c = H.fresh_db()
        for i, pnl in enumerate((-1, 1, -1, 1, -1)):
            add_trade(c, "T%d" % i, pnl, closed_ago_h=10 - i)
        self.assertEqual(P.summarize(P.closed_trades(c))["max_losing_streak"], 1)
        c = H.fresh_db()
        for i, pnl in enumerate((-1, -1, -1, 1, -1)):
            add_trade(c, "T%d" % i, pnl, closed_ago_h=10 - i)
        self.assertEqual(P.summarize(P.closed_trades(c))["max_losing_streak"], 3)


class TestVerdict(unittest.TestCase):
    def trades(self, wins, losses, win=200.0, loss=-100.0, reason="TARGET_REACHED"):
        c = H.fresh_db()
        for i in range(wins):
            add_trade(c, "W%d" % i, win, closed_ago_h=100 - i, reason=reason)
        for i in range(losses):
            add_trade(c, "L%d" % i, loss, closed_ago_h=50 - i, reason="STOP_REACHED")
        return P.closed_trades(c)

    def v(self, tr):
        return P.verdict(P.summarize(tr), P.group(tr, lambda t: t["exit_reason"]))

    def test_small_samples_are_never_judged(self):
        v = self.v(self.trades(5, 1))
        self.assertEqual(v["level"], "early")
        self.assertIn("Too early to judge", v["headline"])
        self.assertIn("do not change settings", v["points"][0])
        self.assertEqual(self.v([])["level"], "early")

    def test_good_marginal_and_bad_with_enough_trades(self):
        self.assertEqual(self.v(self.trades(14, 8))["level"], "good")                    # PF 2800/800
        self.assertEqual(self.v(self.trades(10, 12, win=130.0))["level"], "marginal")    # PF 1300/1200
        bad = self.v(self.trades(6, 16))
        self.assertEqual(bad["level"], "bad")
        self.assertIn("Losing", bad["headline"])

    def test_it_points_at_the_specific_problem(self):
        pts = " ".join(self.v(self.trades(5, 25, win=120.0))["points"])
        self.assertIn("ended at a stop", pts)                                            # 83% of trades hit the stop
        self.assertIn("Settings -> Swing strategy", pts)
        c = H.fresh_db()
        for i in range(8):
            add_trade(c, "W%d" % i, 100.0, closed_ago_h=60 - i, reason="TARGET_REACHED")
        for i in range(12):
            add_trade(c, "T%d" % i, -20.0, closed_ago_h=30 - i, reason="TIME_EXPIRED")
        tr = P.closed_trades(c)
        pts = " ".join(self.v(tr)["points"])
        self.assertIn("hit the time limit without reaching target or stop", pts)        # 60% of trades went nowhere
        self.assertNotIn("ended at a stop", pts)


class TestAgainstTheRealLedger(unittest.TestCase):
    def test_a_partial_sell_then_a_runner_stop_reports_the_pnl_the_cash_ledger_shows(self):
        w = TP.Scenario()
        w.setUp()
        try:
            w.cfg["strategy"]["swing_enabled"] = True
            plan = w.open_position()
            w.c.execute("UPDATE position_forecast_plans SET confidence=10")
            w.mkt.price["NVDA"] = plan["target_price"] + 0.1
            w.guard(TL.T_OPEN + dt.timedelta(minutes=30))
            stop = float(w.one("SELECT stop_price FROM position_forecast_plans WHERE active=1")[0])
            w.mkt.price["NVDA"] = stop - 0.3
            w.guard(TL.T_OPEN + dt.timedelta(minutes=60))
            tr = P.closed_trades(w.c)
            self.assertEqual(len(tr), 1)
            t = tr[0]
            self.assertTrue(t["partial"])                                               # it really was a two-step exit
            self.assertEqual(t["exit_reason"], "RUNNER_STOP")
            self.assertAlmostEqual(t["pnl"], db.cash(w.c) - 1_000_000, places=1)       # the report agrees with the cash, to the cent
            amount = w.one("SELECT amount FROM decisions WHERE ticker='NVDA'")[0]
            self.assertAlmostEqual(t["ret"], t["pnl"] / amount, places=8)               # percentage uses the ORIGINAL size, not the zeroed cost
            self.assertGreater(t["pnl"], -0.02 * amount)
        finally:
            w.tearDown()


class TestCharts(unittest.TestCase):
    def test_charts_never_crash(self):
        for vals in ([], [1], [1, 1], [0, 0, 0], [-5, -2, -9], [1e9, -1e9], list(range(500))):
            self.assertIn("<", P.svg_line([float(v) for v in vals]))
            self.assertIn("<", P.svg_bars([float(v) for v in vals]))
        self.assertIn("polyline", P.svg_line([0.0, 5.0, 3.0]))


class TestDashboard(unittest.TestCase):
    def page(self, c, **q):
        return dashboard.render(c, H.cfg(), "performance", {k: [v] for k, v in q.items()})

    def test_empty_state_is_friendly(self):
        html = self.page(H.fresh_db())
        self.assertIn("No closed trades yet", html)
        self.assertIn("What happened to every BUY idea", html)

    def test_full_page_with_trades(self):
        c = H.fresh_db()
        known(c)
        add_trade(c, "<b>X</b>", 40, reason="TIME_EXPIRED")
        html = self.page(c, days="all")
        for needle in ("Realized P&amp;L", "Win rate", "Profit factor", "How trades ended", "Stop loss hit", "Time limit (went nowhere)", "By kind of news", "By how long it was held",
                       "By time of day opened (New York)", "By stock (best and worst)", "Biggest wins", "Biggest losses", "Latest closed trades", "Why? &rarr;", "Too early to judge", "polyline"):
            self.assertIn(needle, html, needle)
        self.assertNotIn("<b>X</b>", html)                                            # tickers are escaped

    def test_the_window_selector_filters_trades(self):
        c = H.fresh_db()
        now = utcnow()                                                                   # the page measures its window from the live clock
        add_trade(c, "NEW", 100, closed_ago_h=2, base=now)
        add_trade(c, "OLD", -999, closed_ago_h=24 * 40, base=now)
        self.assertNotIn("OLD", self.page(c, days="30"))
        self.assertIn("OLD", self.page(c, days="all"))
        self.assertIn("NEW", self.page(c, days="7"))

    def test_the_overview_shows_realized_today_and_the_tab_is_in_the_menu(self):
        c = H.fresh_db()
        real = utcnow()                                                                  # the dashboard decides "today" from THIS clock (not the fixed fake one)
        add_trade(c, "TOD", 123, closed_ago_h=0.05, base=real)
        add_trade(c, "OLD", -500, closed_ago_h=24 * 3, base=real)                       # an older loss must NOT be in "today"
        html = dashboard.render(c, H.cfg(), "overview", {})
        self.assertRegex(html, r"(?s)Realized today.{0,80}?\$123\b")
        self.assertNotIn("377", html)
        self.assertIn("performance", dashboard.TABS)


if __name__ == "__main__":
    unittest.main()
