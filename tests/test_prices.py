"""Prices keep refreshing while the market is closed (pre-market / after-hours), on a schedule the owner edits in Settings. They are for valuation and early warning - never for execution."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import fake_internet as FI  # noqa: E402
import test_fake_internet as TF  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import dashboard, funnel, marketdata as md, net, pipeline, prices, quotes, settings as S  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

UTC = dt.timezone.utc
CLOSED = dt.datetime(2026, 10, 27, 22, 0, tzinfo=UTC)        # 18:00 New York: after-hours
OPEN = dt.datetime(2026, 10, 27, 15, 0, tzinfo=UTC)          # 11:00 New York: regular session


def payload(sym, closes, t_end, prev=100.0):
    ts = [int(t_end.timestamp()) - 60 * (len(closes) - 1 - i) for i in range(len(closes))]
    return {"chart": {"result": [{"meta": {"symbol": sym, "chartPreviousClose": prev}, "timestamp": ts, "indicators": {"quote": [{"close": closes}]}}], "error": None}}


class Fetch:
    def __init__(self, now, fail=(), rate_limit_after=None, price=101.5):
        self.now, self.calls, self.fail, self.rl, self.price = now, [], set(fail), rate_limit_after, price

    def __call__(self, url, headers=None, **kw):
        sym = url.split("/chart/")[1].split("?")[0]
        self.calls.append(sym)
        if self.rl is not None and len(self.calls) > self.rl:
            raise net.FetchError("HTTP 429", 429)
        if sym in self.fail:
            raise RuntimeError("boom")
        return payload(sym, [self.price - 1, self.price, None], self.now - dt.timedelta(minutes=2)), 200


class TestParsing(unittest.TestCase):
    def test_the_latest_bar_with_a_price_wins_and_trailing_nulls_are_skipped(self):
        b = md.parse_last_bar(payload("X", [10, 11, None, None], CLOSED))
        self.assertEqual(b["price"], 11.0)
        self.assertEqual(b["symbol"], "X")

    def test_garbage_is_ignored(self):
        for junk in (None, {}, {"chart": {}}, {"chart": {"result": None}}, {"chart": {"result": [None]}}, payload("X", [None, None], CLOSED), payload("X", [0, -1], CLOSED),
                     payload("X", ["N/A", "x"], CLOSED), {"chart": {"result": [{"timestamp": [1], "indicators": {"quote": []}}]}}):
            self.assertIsNone(md.parse_last_bar(junk), junk)


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self.c, self.cfg = H.fresh_db(), H.cfg()

    def test_the_price_is_stored_with_its_session_and_time(self):
        r = prices.refresh_symbol(self.c, "NVDA", Fetch(CLOSED), CLOSED)
        self.assertEqual(r["session"], "AFTER_HOURS")
        row = self.c.execute("SELECT price,source,quoted_at FROM quote_cache WHERE ticker='NVDA'").fetchone()
        self.assertEqual((row["price"], row["source"]), (101.5, "YAHOO_EXT:AFTER_HOURS"))
        v = quotes.valuation_price(self.c, "NVDA", CLOSED)                       # everything that values positions now sees it
        self.assertEqual((v["price"], v["source"]), (101.5, "YAHOO_EXT:AFTER_HOURS"))

    def test_another_symbols_price_a_future_timestamp_and_an_older_bar_are_never_stored(self):
        def wrong(url, headers=None, **kw):
            return payload("AMD", [50, 51], CLOSED), 200
        self.assertIsNone(prices.refresh_symbol(self.c, "NVDA", wrong, CLOSED))
        future = lambda url, headers=None, **kw: (payload("NVDA", [1, 2], CLOSED + dt.timedelta(hours=2)), 200)  # noqa: E731
        self.assertIsNone(prices.refresh_symbol(self.c, "NVDA", future, CLOSED))
        self.assertIsNone(self.c.execute("SELECT 1 FROM quote_cache").fetchone())
        prices.refresh_symbol(self.c, "NVDA", Fetch(CLOSED, price=110), CLOSED)
        prices.refresh_symbol(self.c, "NVDA", Fetch(CLOSED - dt.timedelta(hours=1), price=90), CLOSED)           # an OLDER bar must not overwrite a newer one
        self.assertEqual(self.c.execute("SELECT price FROM quote_cache WHERE ticker='NVDA'").fetchone()[0], 110)

    def test_the_stocks_that_matter_come_first(self):
        self.c.execute("INSERT INTO positions(ticker,opened,entry_price,shares,cost,status) VALUES('HELD1',?,10,1,10,'OPEN')", (iso(CLOSED),))
        for i, (t, st) in enumerate((("WAIT1", "AWAITING_MARKET"), ("OLD1", "REJECTED"), ("WAIT2", "HELD"), ("", "REJECTED"))):
            self.c.execute("INSERT INTO decisions(created,ticker,action,status) VALUES(?,?,?,?)", (iso(CLOSED), t, "BUY", st))
        syms = prices.symbols_of_interest(self.c, 25)
        self.assertEqual(syms[:4], ["SPY", "HELD1", "WAIT1", "WAIT2"])
        self.assertIn("OLD1", syms)
        self.assertNotIn("", syms)
        self.assertEqual(prices.symbols_of_interest(self.c, 3), ["SPY", "HELD1", "WAIT1"])

    def test_the_schedule_is_driven_by_the_settings(self):
        self.cfg["prices"].update(refresh_closed_minutes=10, refresh_open_minutes=5)
        f = Fetch(CLOSED)
        self.assertEqual(prices.run(self.c, self.cfg, CLOSED, f)["session"], "CLOSED")
        n = len(f.calls)
        self.assertEqual(prices.run(self.c, self.cfg, CLOSED + dt.timedelta(minutes=4), f), {"skipped": "not due"})
        self.assertEqual(len(f.calls), n)
        self.assertEqual(prices.run(self.c, self.cfg, CLOSED + dt.timedelta(minutes=9.5), f)["session"], "CLOSED")       # 10 min setting, a cycle 30s early still runs
        self.cfg["prices"]["refresh_closed_minutes"] = 0
        self.assertEqual(prices.run(self.c, self.cfg, CLOSED + dt.timedelta(hours=3), f), {"skipped": "off"})
        c2 = H.fresh_db()                                                                                                 # the OPEN-market interval is separate (fresh state: the clock must not run backwards)
        self.assertEqual(prices.run(c2, self.cfg, OPEN, Fetch(OPEN))["session"], "OPEN")
        self.assertEqual(prices.run(c2, self.cfg, OPEN + dt.timedelta(minutes=3), Fetch(OPEN)), {"skipped": "not due"})
        self.assertEqual(prices.run(c2, self.cfg, OPEN + dt.timedelta(minutes=5), Fetch(OPEN))["session"], "OPEN")

    def test_max_symbols_and_extended_hours_settings_are_honoured(self):
        for i in range(30):
            self.c.execute("INSERT INTO decisions(created,ticker,action,status) VALUES(?,?,?,?)", (iso(CLOSED), "T%d" % i, "BUY", "AWAITING_MARKET"))
        self.cfg["prices"]["max_symbols"] = 6
        urls = []
        f = Fetch(CLOSED)
        wrapped = lambda url, headers=None, **kw: (urls.append(url), f(url, headers, **kw))[1]  # noqa: E731
        self.assertEqual(prices.run(self.c, self.cfg, CLOSED, wrapped)["symbols"], 6)
        self.assertTrue(all("includePrePost=true" in u and "interval=1m" in u for u in urls))
        self.cfg["prices"]["extended_hours"] = False
        urls.clear()
        prices.run(self.c, self.cfg, CLOSED + dt.timedelta(hours=1), wrapped)
        self.assertTrue(all("includePrePost=false" in u for u in urls))

    def test_rate_limiting_stops_the_round_and_one_bad_symbol_does_not(self):
        for t in ("A", "B", "C", "D"):
            self.c.execute("INSERT INTO decisions(created,ticker,action,status) VALUES(?,?,?,?)", (iso(CLOSED), t, "BUY", "AWAITING_MARKET"))
        r = prices.run(self.c, self.cfg, CLOSED, Fetch(CLOSED, rate_limit_after=2))
        self.assertEqual((r["updated"], r["stopped"]), (2, "rate limited (HTTP 429)"))
        r = prices.run(self.c, self.cfg, CLOSED + dt.timedelta(hours=1), Fetch(CLOSED + dt.timedelta(hours=1), fail=["A"]))
        self.assertEqual((r["failed"], r["updated"]), (1, 4))                                       # SPY + B + C + D still updated
        self.assertEqual(r["stopped"], "")


class TestSettings(unittest.TestCase):
    def test_the_refresh_settings_are_editable_validated_and_have_sane_defaults(self):
        from paper_agent import config
        base = config.load_config("/nonexistent", with_overrides=False)
        self.assertEqual(base["prices"], {"refresh_closed_minutes": 10, "refresh_open_minutes": 5, "max_symbols": 25, "extended_hours": True})
        names = {f.path for f in S.FIELDS}
        for k in ("prices.refresh_closed_minutes", "prices.refresh_open_minutes", "prices.max_symbols", "prices.extended_hours"):
            self.assertIn(k, names)
        for field, good, bad in (("prices.refresh_closed_minutes", "30", "9999"), ("prices.refresh_open_minutes", "0", "-1"), ("prices.max_symbols", "40", "1000")):
            _, err_ok, _ = S.apply_form({"f." + field: good}, base, base, {})
            _, err_bad, _ = S.apply_form({"f." + field: bad}, base, base, {})
            self.assertFalse(err_ok, field)
            self.assertTrue(err_bad, field)

    def test_a_single_quote_source_is_the_default_and_editable(self):
        from paper_agent import config
        base = config.load_config("/nonexistent", with_overrides=False)
        self.assertEqual((base["quotes"]["min_providers"], base["quotes"]["allow_single_source"]), (1, True))
        self.assertIn("quotes.single_source_max_move_pct", {f.path for f in S.FIELDS})


class TestWholeSystem(unittest.TestCase):
    def test_a_closed_market_cycle_refreshes_held_and_queued_prices_but_never_enables_trading_on_them(self):
        tf = TF.TestWholeSystemAgainstTheSimulatedInternet("test_a_clean_morning_runs_without_a_single_stage_error_and_trades")
        tf.setUp()
        try:
            c, errors, net_ = tf.morning(seed=1, cycles=12)
            held = [r[0] for r in c.execute("SELECT DISTINCT ticker FROM positions WHERE status='OPEN'")]
            self.assertTrue(held)
            c.execute("DELETE FROM quote_cache")                                                  # forget everything: only the after-hours refresh can repopulate it
            cfg = H.cfg()
            cfg["runtime"]["min_available_memory_mb"] = 0
            with net_:
                net_.now = CLOSED
                r = pipeline.run_cycle(c, cfg, tf.uni, TF.SRC, pipeline.Deps(sleep=lambda s: None), CLOSED)
            self.assertEqual(r["errors"], [])
            self.assertGreaterEqual(r["prices"]["updated"], len(held))
            for t in held:
                src = c.execute("SELECT source FROM quote_cache WHERE ticker=?", (t,)).fetchone()
                self.assertTrue(src and src[0] == "YAHOO_EXT:AFTER_HOURS", (t, src and src[0]))
            self.assertTrue([u for u in net_.requests if "includePrePost=true" in u])
        finally:
            tf.tearDown()

    def test_an_extended_hours_price_never_lets_a_buy_execute(self):
        class LB(TL.Base):
            def runTest(self):
                pass
        s = LB()
        s.setUp()
        try:
            s.cycle(TL.T_PRE, news=True)
            s.c.execute("INSERT INTO quote_cache(ticker,price,source,quoted_at,updated_epoch) VALUES('NVDA',120,'YAHOO_EXT:PRE',?,0)", (iso(TL.T_OPEN - dt.timedelta(minutes=1)),))
            s.mkt.down = {"twelve", "finnhub", "alpaca", "yahoo"}                              # every verified provider is down at the open
            s.cycle(TL.T_OPEN)
            self.assertEqual(s.one("SELECT status FROM decisions")[0], "AWAITING_QUOTE")       # the unverified extended-hours price did NOT make it executable
            self.assertEqual(s.one("SELECT COUNT(*) FROM positions")[0], 0)
        finally:
            s.tearDown()


class TestDisplay(unittest.TestCase):
    def test_prices_are_described_in_plain_language(self):
        f = dashboard.price_label
        self.assertEqual(f("YAHOO_EXT:AFTER_HOURS", 240), "after-hours, 4 min ago")
        self.assertEqual(f("YAHOO_EXT:PRE_MARKET", 30), "pre-market, just now")
        self.assertEqual(f("CONSENSUS:TWELVE_DATA+FINNHUB", 7200), "verified TWELVE_DATA/FINNHUB, 2h ago")
        self.assertEqual(f("DAILY_CLOSE", None), "last close")
        self.assertEqual(f("COST", None), "cost (no price yet)")

    def test_a_waiting_buy_shows_where_it_is_trading_versus_the_price_it_was_decided_against(self):
        c = H.fresh_db()
        import json
        c.execute("INSERT INTO decisions(created,ticker,action,status,ev_net,reason) VALUES(?,?,?,?,?,?)", (iso(CLOSED), "NVDA", "BUY", "AWAITING_MARKET", .04,
                  json.dumps({"gates": ["market closed"], "ref_price": 120.0})))
        c.execute("INSERT INTO quote_cache(ticker,price,source,quoted_at,updated_epoch) VALUES('NVDA',113.4,'YAHOO_EXT:AFTER_HOURS',?,0)", (iso(CLOSED - dt.timedelta(minutes=7)),))
        rep = funnel.report(c, 6, CLOSED)
        self.assertAlmostEqual(rep["pending"][0]["indication"]["gap"], -0.055, places=3)
        self.assertIn("trading -5.5% vs previous close, after-hours, 7 min ago", funnel.text(rep))


if __name__ == "__main__":
    unittest.main()
