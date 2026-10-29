"""BUY -> wait -> open -> position -> SELL, driven through the REAL quote-consensus code against faithful fake provider responses.
(Every earlier test, including the 20-day soak, used a stub that always answered PASS, so this path had never been exercised end to end.)"""
import collections
import datetime as dt
import os
import sys
import urllib.parse
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import dashboard, db, executor, net, pipeline, trader  # noqa: E402
from paper_agent.timeutil import add_trading_days, iso, market_state  # noqa: E402

UTC = dt.timezone.utc
RF = lambda d: d.strftime("%a, %d %b %Y %H:%M:%S +0000")  # noqa: E731
SRC = [{"id": "t", "name": "Wire", "type": "rss", "url": "http://x/rss", "trust": 0.9, "interval": 0}]
NVDA_NEWS = "NVIDIA beats estimates and raises full-year guidance as data center demand surges"
T_PRE = dt.datetime(2026, 10, 27, 12, 50, tzinfo=UTC)         # 08:50 New York: pre-market, BUYs must WAIT
T_OPEN = dt.datetime(2026, 10, 27, 13, 40, tzinfo=UTC)        # 09:40 New York: regular session
KEYS = ("TWELVE_DATA_API_KEY", "FINNHUB_API_KEY", "ALPACA_API_KEY", "ALPACA_API_SECRET")
OTHER = ("MASSIVE_API_KEY", "ALPHA_VANTAGE_API_KEY", "TIINGO_API_KEY", "EODHD_API_KEY")


class FakeMarket:
    """Twelve Data / Finnhub / Alpaca / Yahoo with the JSON shapes the real parsers read. Ages, outages and price skews are adjustable per provider."""

    def __init__(self):
        self.price = {}
        self.age = {"twelve": 5, "finnhub": 5, "alpaca": 8, "yahoo": 4}
        self.skew = {}
        self.down = set()
        self.now = T_OPEN
        self.urls = []

    def px(self, sym, prov):
        p = self.price.get(sym)
        if p is None:
            raise net.FetchError("HTTP 404", 404)
        return p * self.skew.get(prov, 1.0)

    def __call__(self, url, headers=None, **kw):
        self.urls.append(url)
        stamp = lambda prov: int(self.now.timestamp() - self.age[prov])  # noqa: E731
        u = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(u.query)
        if "twelvedata" in url:
            if "twelve" in self.down:
                raise net.FetchError("HTTP 429", 429)
            s = qs["symbol"][0]
            return {"symbol": s, "close": "%.4f" % self.px(s, "twelve"), "timestamp": stamp("twelve")}, 200
        if "finnhub.io" in url:
            if "finnhub" in self.down:
                raise net.FetchError("HTTP 401", 401)
            return {"c": self.px(qs["symbol"][0], "finnhub"), "t": stamp("finnhub")}, 200
        if "alpaca" in url:
            if "alpaca" in self.down:
                raise net.FetchError("HTTP 403", 403)
            s = u.path.split("/stocks/")[1].split("/")[0]
            p = self.px(s, "alpaca")
            return {"latestTrade": {"p": p, "t": dt.datetime.fromtimestamp(stamp("alpaca"), UTC).isoformat()}, "latestQuote": {"bp": p - 0.01, "ap": p + 0.01}}, 200
        if "finance.yahoo.com" in url:
            if "yahoo" in self.down:
                raise net.FetchError("HTTP 429", 429)
            s = u.path.split("/chart/")[1]
            body = H.chart_payload(s, last_price=self.px(s, "yahoo"))
            body["chart"]["result"][0]["meta"]["regularMarketTime"] = stamp("yahoo")
            return body, 200
        raise net.FetchError("unexpected URL in test: " + url[:60], 599)


def books_balance(t, c):
    """The accounting identities that must hold after ANY sequence of buys and sells."""
    start = db.start_capital(c)
    led = c.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE kind!='START'").fetchone()[0]
    t.assertAlmostEqual(db.cash(c), start + led, places=1)
    t.assertGreaterEqual(db.cash(c), 0)
    buys = -c.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE kind='VIRTUAL_BUY'").fetchone()[0]
    released = c.execute("SELECT COALESCE(SUM(cost_released),0) FROM virtual_exit_actions WHERE status='COMMITTED'").fetchone()[0]
    open_cost = c.execute("SELECT COALESCE(SUM(remaining_cost),0) FROM position_lifecycle l JOIN positions p ON p.id=l.position_id WHERE p.status='OPEN'").fetchone()[0]
    t.assertAlmostEqual(buys - released, open_cost, delta=1.0)
    t.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")


class Base(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in KEYS + OTHER}
        for k in KEYS:
            os.environ[k] = "k" * 20
        for k in OTHER:
            os.environ.pop(k, None)
        self.c, self.cfg, self.mkt = H.fresh_db(), H.cfg(), FakeMarket()
        self.cfg["risk"]["max_gap_down_pct"], self.cfg["risk"]["max_gap_up_pct"] = 0.03, 0.05          # the SHIPPED values: lifecycle tests run with the last-look guard on
        self.mkt.price["NVDA"] = 120.0
        self.mkt.price["SPY"] = 450.0
        self.prev_close = {"NVDA": 120.0}                                                              # yesterday's close = today's quote unless a test says otherwise

    def tearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def series(self, url, headers=None, **kw):
        """Price history whose LAST CLOSE is the (previous-close) price the market is quoting, so overnight gaps are measured against something real."""
        sym = url.split("/chart/")[1].split("?")[0].replace("%5E", "^").replace("%3D", "=")
        if sym in self.prev_close:
            nat = H.chart_payload(sym, start=100.0)["chart"]["result"][0]["indicators"]["quote"][0]["close"][-1]
            return H.chart_payload(sym, start=100.0 * self.prev_close[sym] / nat), 200      # the SAME history scaled to end at the quoted price: every return is unchanged
        return H.series_fetch(url, headers, **kw)

    def deps(self, items=()):
        return pipeline.Deps(fetch=H.make_fetch(list(items)), fetch_json=self.mkt, quote_fn=None, sleep=lambda s: None, series_fetch=self.series)

    def cycle(self, now, news=False):
        self.mkt.now = now
        items = [(NVDA_NEWS, "AI capex", RF(T_PRE - dt.timedelta(minutes=40)))] if news else []
        return pipeline.run_cycle(self.c, self.cfg, H.universe(), SRC, self.deps(items), now)

    def guard(self, now):
        self.mkt.now = now
        return pipeline.run_guard(self.c, self.cfg, self.deps(), now)

    def one(self, sql, *a):
        return self.c.execute(sql, a).fetchone()

    def open_position(self):
        """Overnight BUY that executes at the open. Returns the plan."""
        self.cycle(T_PRE, news=True)
        self.cycle(T_OPEN)
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions WHERE status='OPEN'")[0], 1, "setup: the BUY should have executed at the open")
        return dict(self.one("SELECT * FROM position_forecast_plans WHERE active=1"))


class TestBuy(Base):
    def test_a_buy_decided_before_the_open_waits_and_executes_at_the_open(self):
        r = self.cycle(T_PRE, news=True)
        self.assertEqual(r["errors"], [])
        d = self.one("SELECT status,action,reason FROM decisions WHERE ticker='NVDA'")
        self.assertEqual((d["status"], d["action"]), ("AWAITING_MARKET", "BUY"))
        self.assertIn("PRE_MARKET", d["reason"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)                        # nothing opens while the market is closed...
        self.cycle(T_PRE + dt.timedelta(minutes=20))
        self.assertEqual(self.one("SELECT status FROM decisions WHERE ticker='NVDA'")[0], "AWAITING_MARKET")
        r2 = self.cycle(T_OPEN)                                                                    # ...then the first cycle after the open buys
        self.assertEqual(r2["errors"], [])
        self.assertEqual(self.one("SELECT status FROM decisions WHERE ticker='NVDA'")[0], "EXECUTED")
        p = self.one("SELECT * FROM positions")
        self.assertEqual((p["ticker"], p["status"]), ("NVDA", "OPEN"))
        self.assertGreater(p["shares"], 0)
        self.assertAlmostEqual(p["entry_price"], 120.0, delta=0.5)                                # filled at the consensus price (+ small slippage)
        self.assertLess(db.cash(self.c), 1_000_000)
        plan = self.one("SELECT * FROM position_forecast_plans WHERE active=1")
        self.assertLess(plan["stop_price"], p["entry_price"])
        self.assertGreater(plan["target_price"], p["entry_price"])                                 # every position leaves with an exit plan
        books_balance(self, self.c)

    def test_waiting_overnight_costs_no_network_calls_for_the_waiting_ticker(self):
        """REGRESSION: every 10-minute cycle used to re-run the whole evaluation (and two market-data calls) per waiting BUY, all night."""
        self.cycle(T_PRE, news=True)
        seen = []
        orig = H.series_fetch
        H.series_fetch = lambda url, *a, **k: (seen.append(url), orig(url, *a, **k))[1]
        try:
            r = self.cycle(T_PRE + dt.timedelta(minutes=30))
        finally:
            H.series_fetch = orig
        self.assertEqual(r["entries"]["waiting_market"], 1)
        self.assertFalse([u for u in seen + self.mkt.urls if "NVDA" in u and u not in self.mkt.urls[:0] and "chart" in u and "range=1y" in u])       # no bars refetched for it
        self.assertEqual(self.one("SELECT COUNT(*) FROM forecasts")[0], 1)                         # and no new forecast row per retry

    def test_open_market_with_stale_quotes_waits_with_the_reason_then_buys_when_they_are_fresh(self):
        self.cycle(T_PRE, news=True)
        for p in self.mkt.age:
            self.mkt.age[p] = 900                                                                  # every provider's last trade is 15 minutes old
        self.cycle(T_OPEN)
        d = self.one("SELECT status,reason FROM decisions WHERE ticker='NVDA'")
        self.assertEqual(d["status"], "AWAITING_QUOTE")                                            # a DISTINCT status from "market closed"
        for needle in ("market is open but no verified price", "stale", "fresh agreeing provider(s), have 0"):
            self.assertIn(needle, d["reason"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)
        for p in self.mkt.age:
            self.mkt.age[p] = 5
        self.cycle(T_OPEN + dt.timedelta(minutes=10))
        self.assertEqual(self.one("SELECT status FROM decisions WHERE ticker='NVDA'")[0], "EXECUTED")
        books_balance(self, self.c)

    def test_one_fresh_provider_is_enough_by_default(self):
        """The owner's rule: a price from ONE source is sufficient to trade (as long as it is fresh and plausible)."""
        self.cycle(T_PRE, news=True)
        self.mkt.down = {"twelve", "finnhub", "yahoo"}                                             # only Alpaca answers
        self.cycle(T_OPEN)
        self.assertEqual(self.one("SELECT status FROM decisions")[0], "EXECUTED")

    def test_requiring_two_providers_is_still_available_from_settings(self):
        self.cfg["quotes"]["min_providers"], self.cfg["quotes"]["allow_single_source"] = 2, False
        self.cycle(T_PRE, news=True)
        self.mkt.down = {"twelve", "finnhub", "yahoo"}                                             # only Alpaca answers
        self.cycle(T_OPEN)
        self.assertEqual(self.one("SELECT status FROM decisions")[0], "AWAITING_QUOTE")
        self.mkt.down = {"finnhub", "yahoo"}                                                       # Twelve Data + Alpaca
        self.cycle(T_OPEN + dt.timedelta(minutes=10))
        self.assertEqual(self.one("SELECT status FROM decisions")[0], "AWAITING_QUOTE")           # Twelve Data was just rate-limited: it is paused for 15 minutes (this used to be skipped by a naming bug)
        self.cycle(T_OPEN + dt.timedelta(minutes=20))
        self.assertEqual(self.one("SELECT status FROM decisions")[0], "EXECUTED")                 # ...and is used again as soon as the pause is over

    def test_a_single_implausible_price_is_not_accepted_without_a_second_source(self):
        """One source has no cross-check, so it must at least be plausible against the last close (default +-20%)."""
        self.cycle(T_PRE, news=True)
        self.mkt.down = {"twelve", "finnhub", "yahoo"}
        self.mkt.price["NVDA"] = 120.0 * 1.3                                                       # +30% vs the 120 previous close, from a lone source
        self.cfg["risk"]["max_gap_up_pct"] = 1.0                                                   # (isolate this rule from the gap guard)
        self.cycle(T_OPEN)
        d = self.one("SELECT status,reason FROM decisions")
        self.assertEqual(d[0], "AWAITING_QUOTE")
        self.assertIn("needs a second source", d[1])

    def test_providers_that_disagree_do_not_trade(self):
        self.cycle(T_PRE, news=True)
        self.mkt.skew = {"twelve": 1.03}                                                           # one provider is 3% off
        self.mkt.down = {"finnhub", "yahoo"}
        self.cycle(T_OPEN)
        self.assertEqual(self.one("SELECT status FROM decisions")[0], "AWAITING_QUOTE")
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)

    def test_the_decisions_page_says_what_each_waiting_buy_is_waiting_for(self):
        self.cycle(T_PRE, news=True)
        for p in self.mkt.age:
            self.mkt.age[p] = 900
        self.cycle(T_OPEN)
        html = dashboard.render(self.c, self.cfg, "decisions", {})
        self.assertIn("AWAITING_QUOTE", html)
        self.assertIn("Ready to buy NVDA, but no fresh price is available yet", html)              # plain English, in the table itself, not hidden behind a click
        self.assertIn("Why? &rarr;", html)                                                          # the provider-by-provider detail is one click away (see test_explain)


class TestSell(Base):
    def setUp(self):
        super().setUp()
        self.plan = self.open_position()
        self.t = T_OPEN + dt.timedelta(minutes=30)

    def status(self):
        return self.one("SELECT status,review_code,pnl FROM positions")

    def test_stop_loss_sells_and_the_books_balance(self):
        self.mkt.price["NVDA"] = self.plan["stop_price"] - 0.5
        r = self.guard(self.t)
        self.assertEqual((r["committed"], r["errors"]), (1, 0))
        s = self.status()
        self.assertEqual((s["status"], s["review_code"]), ("CLOSED", "STOP_REACHED"))
        self.assertLess(s["pnl"], 0)
        self.assertAlmostEqual(db.cash(self.c) - 1_000_000, s["pnl"], places=1)                    # cash moved by exactly the realised loss
        self.assertEqual(self.one("SELECT COUNT(*) FROM ledger WHERE kind='VIRTUAL_SELL_V5'")[0], 1)
        books_balance(self, self.c)

    def test_target_hit_with_high_conviction_extends_the_hold_instead_of_selling(self):
        """Expected behaviour, made visible: at the target the system re-forecasts and usually HOLDS for more - it is not a missed sell."""
        self.mkt.price["NVDA"] = self.plan["target_price"] + 1
        r = self.guard(self.t)
        self.assertEqual(r["errors"], 0)
        if r["extended"]:
            self.assertEqual(self.status()["status"], "OPEN")
            self.assertTrue([d for d in r["details"] if d.get("result") == "HOLD_EXTENDED"])
        else:
            self.assertEqual(r["committed"], 1)                                                    # ...or it sold the planned partial
        books_balance(self, self.c)

    def test_target_partial_exit_then_runner_closes_at_its_stop(self):
        self.c.execute("UPDATE position_forecast_plans SET confidence=10")                         # low confidence: follow the exit plan, no hold-extension
        self.mkt.price["NVDA"] = self.plan["target_price"] + 1
        r = self.guard(self.t)
        self.assertEqual(r["committed"], 1)
        life = self.one("SELECT phase,remaining_shares,realized_pnl FROM position_lifecycle")
        self.assertEqual(life["phase"], "RUNNER_OPEN")
        self.assertGreater(life["remaining_shares"], 0)
        self.assertGreater(life["realized_pnl"], 0)                                                # the partial sale banked a profit
        self.assertEqual(self.one("SELECT status FROM positions")[0], "OPEN")
        books_balance(self, self.c)
        runner = dict(self.one("SELECT * FROM position_forecast_plans WHERE active=1"))
        self.mkt.price["NVDA"] = runner["stop_price"] - 0.3                                        # the runner now falls through ITS stop
        r2 = self.guard(self.t + dt.timedelta(minutes=30))
        self.assertEqual(r2["committed"], 1)
        s = self.status()
        self.assertEqual((s["status"], s["review_code"]), ("CLOSED", "RUNNER_STOP"))
        self.assertEqual(self.one("SELECT remaining_shares FROM position_lifecycle")[0], 0)
        self.assertAlmostEqual(db.cash(self.c) - 1_000_000, s["pnl"], places=1)
        books_balance(self, self.c)

    def test_time_stop_sells_a_position_that_goes_nowhere(self):
        opened = self.one("SELECT opened FROM positions")[0]
        from paper_agent.timeutil import parse_ts
        later = add_trading_days(parse_ts(opened), int(self.plan["max_hold_days"]) + 1).replace(hour=15, minute=0, second=0, microsecond=0)
        self.assertTrue(market_state(later)["execution"])
        self.mkt.price["NVDA"] = 120.0                                                             # flat: neither stop nor target
        self.guard(later)
        s = self.status()
        self.assertEqual((s["status"], s["review_code"]), ("CLOSED", "TIME_EXPIRED"))
        books_balance(self, self.c)

    def test_nothing_is_sold_after_hours_and_it_sells_at_the_next_open(self):
        self.mkt.price["NVDA"] = self.plan["stop_price"] - 2
        evening = dt.datetime(2026, 10, 27, 21, 0, tzinfo=UTC)                                     # 17:00 New York
        self.assertEqual(market_state(evening)["session"], "AFTER_HOURS")
        r = self.guard(evening)
        self.assertIn("AFTER_HOURS", r["skipped"])
        self.assertEqual(self.status()["status"], "OPEN")
        r = self.guard(dt.datetime(2026, 10, 28, 13, 45, tzinfo=UTC))                              # next morning, 09:45 New York
        self.assertEqual(r["committed"], 1)
        self.assertEqual(self.status()["status"], "CLOSED")
        books_balance(self, self.c)

    def test_the_247_cycle_never_sells_outside_regular_hours_even_though_it_runs_all_night(self):
        """The guard has its own market-hours check, but the full cycle runs around the clock and calls the executor directly: THIS is the path that must not sell at 3am."""
        self.mkt.price["NVDA"] = self.plan["stop_price"] - 2
        for when in (dt.datetime(2026, 10, 27, 21, 0, tzinfo=UTC), dt.datetime(2026, 10, 28, 2, 0, tzinfo=UTC), dt.datetime(2026, 10, 28, 12, 0, tzinfo=UTC), dt.datetime(2026, 10, 31, 15, 0, tzinfo=UTC)):
            self.assertFalse(market_state(when)["execution"])
            r = self.cycle(when)
            self.assertEqual(r["errors"], [])
            self.assertEqual(self.status()["status"], "OPEN", when)                                # after-hours, overnight, pre-market, Saturday: never sold
            self.assertEqual(self.one("SELECT COUNT(*) FROM ledger WHERE kind LIKE 'VIRTUAL%SELL%'")[0], 0)
        self.cycle(dt.datetime(2026, 10, 28, 13, 40, tzinfo=UTC))                                  # the next regular session
        self.assertEqual(self.status()["status"], "CLOSED")
        books_balance(self, self.c)

    def test_a_sale_is_blocked_by_stale_quotes_and_happens_once_they_are_fresh(self):
        self.mkt.price["NVDA"] = self.plan["stop_price"] - 1
        for p in self.mkt.age:
            self.mkt.age[p] = 2000
        r = self.guard(self.t)
        self.assertEqual((r["committed"], r["blocked"]), (0, 1))
        self.assertIn("BLOCKED_NO_FRESH_QUOTE", [d["result"] for d in r["details"]])
        self.assertEqual(self.status()["status"], "OPEN")                                          # it never sells on a price it cannot trust
        for p in self.mkt.age:
            self.mkt.age[p] = 5
        r = self.guard(self.t + dt.timedelta(minutes=2))
        self.assertEqual(r["committed"], 1)
        self.assertEqual(self.status()["status"], "CLOSED")

    def test_running_the_guard_repeatedly_never_sells_twice(self):
        self.mkt.price["NVDA"] = self.plan["stop_price"] - 1
        self.guard(self.t)
        before = (db.cash(self.c), self.one("SELECT COUNT(*) FROM ledger")[0])
        for k in range(1, 4):
            self.guard(self.t + dt.timedelta(minutes=2 * k))
        self.assertEqual((db.cash(self.c), self.one("SELECT COUNT(*) FROM ledger")[0]), before)
        books_balance(self, self.c)

    def test_a_broken_thesis_signal_sells_at_the_market(self):
        self.c.execute("INSERT INTO exit_signals(position_id,created,trigger,severity,reason,status) VALUES(1,?,?,?,?,'OPEN')", (iso(T_OPEN), "THESIS_BROKEN", 0.9, "test"))
        self.mkt.price["NVDA"] = 121.0
        r = self.guard(self.t)
        self.assertEqual(r["committed"], 1)
        self.assertEqual(self.status()["review_code"], "THESIS_INVALIDATED")

    def test_the_positions_page_shows_open_and_closed_positions(self):
        self.assertIn("NVDA", dashboard.render(self.c, self.cfg, "positions", {}))
        self.mkt.price["NVDA"] = self.plan["stop_price"] - 1
        self.guard(self.t)
        html = dashboard.render(self.c, self.cfg, "positions", {})
        self.assertIn("NVDA", html)
        self.assertIn("STOP_REACHED", html)

    def test_the_full_cycle_also_performs_exits_not_just_the_guard(self):
        self.mkt.price["NVDA"] = self.plan["stop_price"] - 1
        r = self.cycle(self.t)
        self.assertEqual(r["errors"], [])
        self.assertEqual(self.status()["status"], "CLOSED")
        books_balance(self, self.c)


if __name__ == "__main__":
    unittest.main()
