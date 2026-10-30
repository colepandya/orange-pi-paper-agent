"""Thesis-driven ideas (no news): proposed by a strong thesis, then forecast, risk-checked, bought and sold like any other. Found on the owner's data: every one of them was ALAB / AMD / AMT / AMZN / ANET,
because ties were broken by table order and the limit was applied BEFORE the filters, so the same four stocks were proposed and skipped forever and nothing else was ever considered."""
import datetime as dt
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_pretrade as TP  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import config, db, explain, pipeline, regime as regime_engine, settings as S, thesis, trader  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 29, 14, 0, tzinfo=UTC)
RISK_ON = {"regime": "risk_on", "risk_mult": 1.0}


def world(beneficiaries, universe_tickers=None, strong=True):
    """A database with one strong thesis and the given beneficiaries (ticker, weight)."""
    c = H.fresh_db()
    thesis.seed(c)
    c.execute("DELETE FROM thesis_companies WHERE thesis_key='ai_infrastructure'")
    for t, w in beneficiaries:
        c.execute("INSERT INTO thesis_companies(thesis_key,ticker,role,weight) VALUES('ai_infrastructure',?,'beneficiary',?)", (t, w))
    if strong:
        c.execute("UPDATE theses SET probability=0.95,state='Strong',net_strength=0.6,active=1 WHERE key='ai_infrastructure'")
    cfg = H.cfg()
    cfg["intelligence"]["allow_thesis_entries"] = True
    uni = Universe({t: {"name": t, "sector": "Technology"} for t in (universe_tickers or [b[0] for b in beneficiaries])})
    return c, cfg, uni


def decide(c, ticker, status, ago_h, now=NOW):
    c.execute("INSERT INTO decisions(created,ticker,action,status,reason,origin,thesis_key) VALUES(?,?,?,?,?,'thesis','ai_infrastructure')", (iso(now - dt.timedelta(hours=ago_h)), ticker, "NO_TRADE", status, "{}"))


def tickers(c, cfg, uni, now=NOW):
    return [x["ticker"] for x in trader.thesis_candidates(c, cfg, RISK_ON, uni, now)]


class TestTheGeneratorConsidersMoreThanTheSameFourStocks(unittest.TestCase):
    def test_stocks_outside_the_universe_do_not_use_up_the_limit(self):
        c, cfg, uni = world([("ALAB", 1), ("AMD", 1), ("AMT", 1), ("AMZN", 1), ("NVDA", 1), ("MSFT", 1)], universe_tickers=["NVDA", "MSFT"])
        self.assertEqual(sorted(tickers(c, cfg, uni)), ["MSFT", "NVDA"])                                  # used to be [] : the first four were not in the universe

    def test_a_stock_decided_yesterday_makes_way_for_the_next_one_instead_of_blocking_the_list(self):
        c, cfg, uni = world([(t, 1) for t in ("ALAB", "AMD", "AMT", "AMZN", "ANET", "ARM")])
        for t in ("ALAB", "AMD", "AMT", "AMZN"):
            decide(c, t, "EXECUTED", ago_h=2)
        self.assertEqual(tickers(c, cfg, uni), ["ANET", "ARM"])                                            # used to be [] for 24 hours: the same four were proposed and skipped

    def test_importance_to_the_thesis_ranks_them_not_table_order(self):
        c, cfg, uni = world([("ALAB", 0.2), ("AMD", 0.3), ("NVDA", 1.0), ("MSFT", 0.9), ("ANET", 0.4), ("ARM", 0.1)])
        self.assertEqual(tickers(c, cfg, uni), ["NVDA", "MSFT", "ANET", "AMD"])

    def test_equal_weights_are_ordered_by_company_size_not_the_alphabet(self):
        c, cfg, uni = world([("ALAB", 1), ("AMD", 1), ("AMT", 1), ("NVDA", 1), ("MSFT", 1), ("ANET", 1)])
        for t, cap in (("NVDA", 4.5e12), ("MSFT", 3.8e12), ("AMD", 2.5e11), ("ANET", 1.6e11), ("AMT", 9e10), ("ALAB", 2e10)):
            c.execute("INSERT OR REPLACE INTO companies(ticker,name,market_cap) VALUES(?,?,?)", (t, t, cap))
        self.assertEqual(tickers(c, cfg, uni), ["NVDA", "MSFT", "AMD", "ANET"])                            # in the owner's real data every weight is equal: this used to give ALAB, AMD, AMT, ANET

    def test_a_stock_turned_down_is_left_alone_for_three_days_but_a_bought_one_is_free_after_one(self):
        c, cfg, uni = world([("AMD", 1), ("NVDA", 1)])
        decide(c, "AMD", "REJECTED", ago_h=30)
        decide(c, "NVDA", "EXECUTED", ago_h=30)
        self.assertEqual(tickers(c, cfg, uni), ["NVDA"])                                                   # AMD was turned down 30h ago: still resting; NVDA's 24h is over
        self.assertEqual(sorted(tickers(c, cfg, uni, NOW + dt.timedelta(hours=48))), ["AMD", "NVDA"])     # 78h after AMD was turned down

    def test_held_stocks_are_not_proposed_again(self):
        c, cfg, uni = world([("NVDA", 1), ("MSFT", 1)])
        c.execute("INSERT INTO positions(ticker,opened,entry_price,shares,cost,status) VALUES('NVDA',?,100,10,1000,'OPEN')", (iso(NOW),))
        self.assertEqual(tickers(c, cfg, uni), ["MSFT"])

    def test_it_uses_the_cycles_clock_not_the_real_one(self):
        c, cfg, uni = world([("NVDA", 1)])
        decide(c, "NVDA", "EXECUTED", ago_h=2)
        self.assertEqual(tickers(c, cfg, uni, NOW), [])
        self.assertEqual(tickers(c, cfg, uni, NOW + dt.timedelta(hours=30)), ["NVDA"])

    def test_a_daily_cap_stops_it_flooding_the_decisions_page(self):
        c, cfg, uni = world([(t, 1) for t in ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8")])
        cfg["intelligence"]["thesis_entries_per_day"] = 3
        self.assertEqual(len(tickers(c, cfg, uni)), 3)                                                     # per-cycle limit is 4; only 3 left in the day
        decide(c, "A1", "REJECTED", 1)
        decide(c, "A2", "REJECTED", 1)
        self.assertEqual(len(tickers(c, cfg, uni)), 1)
        decide(c, "A3", "REJECTED", 1)
        self.assertEqual(tickers(c, cfg, uni), [])
        decide(c, "X", "REJECTED", 30)
        cfg["intelligence"]["thesis_entries_per_day"] = 0
        self.assertEqual(tickers(c, cfg, uni, NOW + dt.timedelta(hours=40)), [])                           # 0 = off

    def test_off_means_off_and_a_weak_thesis_proposes_nothing(self):
        c, cfg, uni = world([("NVDA", 1)])
        cfg["intelligence"]["allow_thesis_entries"] = False
        self.assertEqual(tickers(c, cfg, uni), [])
        c, cfg, uni = world([("NVDA", 1)], strong=False)
        self.assertEqual(tickers(c, cfg, uni), [])

    def test_the_setting_exists_with_a_sane_range(self):
        self.assertEqual(config.load_config("/nonexistent", with_overrides=False)["intelligence"]["thesis_entries_per_day"], 12)
        f = next(x for x in S.FIELDS if x.path == "intelligence.thesis_entries_per_day")
        self.assertEqual((S._parse(f, "0"), S._parse(f, "100")), (0, 100))
        with self.assertRaises(ValueError):
            S._parse(f, "101")


def thesis_world():
    """A full cycle machine whose thesis is held strong (as if supporting evidence kept arriving) and whose regime is risk-on; everything downstream is the real code."""
    w = TP.Scenario()
    w.setUp()
    w.cfg["intelligence"]["allow_thesis_entries"] = True
    thesis.seed(w.c)
    w.c.execute("INSERT OR REPLACE INTO thesis_companies(thesis_key,ticker,role,weight) VALUES('ai_infrastructure','NVDA','beneficiary',1.0)")
    orig = regime_engine.current

    def strong(conn, now=None, record_history=True):
        conn.execute("UPDATE theses SET probability=0.95,state='Strong',net_strength=0.6,active=1 WHERE key='ai_infrastructure'")
        return []

    def risk_on(conn):
        r = dict(orig(conn))
        r.update(regime="risk_on", risk_mult=1.0)
        return r
    patches = [mock.patch.object(thesis, "recompute", strong), mock.patch.object(regime_engine, "current", risk_on), mock.patch.object(regime_engine, "refresh", lambda *a, **k: None)]
    for p in patches:
        p.start()
    return w, patches


class TestAThesisIdeaIsBoughtAndSoldWithNoNewsAtAll(unittest.TestCase):
    def setUp(self):
        self.w, self.patches = thesis_world()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.w.tearDown()

    def test_candidate_forecast_buy_at_the_open_then_a_stop_loss_sells_it_and_the_books_balance(self):
        w = self.w
        rep = w.cyc(TL.T_OPEN)
        self.assertEqual(rep["errors"], [])
        d = w.c.execute("SELECT * FROM decisions WHERE ticker='NVDA'").fetchone()
        self.assertEqual((d["origin"], d["status"]), ("thesis", "EXECUTED"))
        self.assertIsNone(d["news_uid"])
        self.assertGreater(d["ev_net"], 0.006)
        self.assertEqual(w.c.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0], 1)
        plan = w.c.execute("SELECT stop_price FROM position_forecast_plans WHERE active=1").fetchone()
        t = TL.T_OPEN + dt.timedelta(minutes=30)
        w.mkt.price["NVDA"] = plan["stop_price"] - 0.5
        w.mkt.now = t
        pipeline.run_guard(w.c, w.cfg, w.deps(), t)
        p = w.c.execute("SELECT status,review_code,pnl FROM positions").fetchone()
        self.assertEqual((p["status"], p["review_code"]), ("CLOSED", "STOP_REACHED"))
        self.assertLess(p["pnl"], 0)
        self.assertAlmostEqual(db.cash(w.c) - 1_000_000, p["pnl"], places=1)
        TL.books_balance(w, w.c)

    def test_it_is_bought_once_not_every_cycle_and_not_again_while_held(self):
        w = self.w
        w.cyc(TL.T_OPEN)
        for i in range(1, 6):
            w.cyc(TL.T_OPEN + dt.timedelta(minutes=5 * i))
        self.assertEqual(w.c.execute("SELECT COUNT(*) FROM decisions WHERE ticker='NVDA' AND origin='thesis'").fetchone()[0], 1)
        self.assertEqual(w.c.execute("SELECT COUNT(*) FROM positions WHERE ticker='NVDA'").fetchone()[0], 1)

    def test_the_why_page_says_plainly_there_was_no_news(self):
        w = self.w
        w.cyc(TL.T_OPEN)
        did = w.c.execute("SELECT id FROM decisions WHERE ticker='NVDA'").fetchone()[0]
        ex = explain.explain(w.c, w.cfg, did, TL.T_OPEN)
        html = explain.render_html(ex)
        self.assertIn("An idea from our", html)
        self.assertIn("not from a news story", html)
        self.assertIn("no article to read", html)
        self.assertNotIn("Thesis-driven:", html)                                                           # the old pseudo-headline that looked like a broken news item
        self.assertNotIn("What the full article says", html)


class TestWhyAStrongThesisCanStillBeTurnedDown(unittest.TestCase):
    def test_a_stock_that_already_ran_says_so(self):
        c = H.fresh_db()
        now = TL.T_OPEN
        cols = {r[1]: r for r in c.execute("PRAGMA table_info(forecasts)")}
        vals = {k: (0 if v[2].upper() in ("INTEGER", "REAL") else "x") for k, v in cols.items() if v[3] and v[4] is None and not v[5]}
        vals.update({"created": iso(now), "ticker": "ANET", "verdict": "REJECT_NEGATIVE_EV", "ev_net": -0.0804, "inputs_json": json.dumps({"priced_in": 0.16, "event_type": "thesis_signal"}), "scenarios_json": "{}", "horizon_days": 3})
        use = {k: v for k, v in vals.items() if k in cols}
        c.execute("INSERT INTO forecasts(%s) VALUES(%s)" % (",".join(use), ",".join("?" * len(use))), tuple(use.values()))
        fid = c.execute("SELECT MAX(id) FROM forecasts").fetchone()[0]
        reason = {"gates": ["EV net -8.04% <= 0"], "forecast": {"verdict": "REJECT_NEGATIVE_EV", "why": ["EV net -8.04% <= 0"]}}
        c.execute("INSERT INTO decisions(created,ticker,action,status,reason,origin,thesis_key,forecast_id,ev_net,score) VALUES(?,?,?,?,?,'thesis','ai_infrastructure',?,?,76)", (iso(now), "ANET", "NO_TRADE", "REJECTED", json.dumps(reason), fid, -0.0804))
        did = c.execute("SELECT MAX(id) FROM decisions").fetchone()[0]
        html = explain.render_html(explain.explain(c, H.cfg(), did, now))
        self.assertIn("Expected profit after costs is -8.04%", html)
        self.assertIn("already risen about 20% over the last 5 trading days", html)                          # 0.16 / 0.8 = 20%


if __name__ == "__main__":
    unittest.main()


class TestTheDecisionsPageSaysWhereEachIdeaCameFrom(unittest.TestCase):
    def page(self, q=None):
        from paper_agent import dashboard
        c = H.fresh_db()
        c.execute("INSERT INTO decisions(created,ticker,action,status,reason,origin,thesis_key) VALUES(?,?,?,?,?,?,?)", (iso(NOW), "ANET", "NO_TRADE", "REJECTED", "{}", "thesis", "ai_infrastructure"))
        c.execute("INSERT INTO decisions(created,ticker,action,status,reason,origin,thesis_key) VALUES(?,?,?,?,?,?,?)", (iso(NOW), "MSFT", "BUY", "EXECUTED", "{}", "event", "cloud_expansion"))
        return dashboard.page_decisions(c, H.cfg(), {"now": NOW}, q or {})

    def test_every_row_is_tagged_and_the_filters_work(self):
        html = self.page()
        self.assertEqual(html.count(">thesis idea<"), 1)                                                     # exactly the ANET row's cell (the filter link says "thesis ideas (no news)")
        self.assertEqual(html.count(">news<"), 1)                                                          # exactly the MSFT row's cell
        self.assertIn("ANET", html)
        self.assertIn("MSFT", html)
        th = self.page({"origin": ["thesis"]})
        self.assertIn("ANET", th)
        self.assertNotIn("MSFT", th)
        nw = self.page({"origin": ["news"]})
        self.assertIn("MSFT", nw)
        self.assertNotIn("ANET", nw)
        self.assertIn("<b>thesis ideas (no news)</b>", th)                                                  # the active filter is marked
