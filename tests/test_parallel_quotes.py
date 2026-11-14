"""Quotes for several stocks fetched side by side. The consensus logic is untouched: it reads the providers' answers from a pre-filled cache instead of asking the network stock by stock.
So the promise is that every price, status and rate-limit decision is IDENTICAL to the one-at-a-time lookup, only available sooner."""
import datetime as dt
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import pipeline, quotes  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

NOW = TL.T_OPEN + dt.timedelta(minutes=20)                                   # regular session
PRICE = {"AAA": 100.0, "BBB": 50.0, "CCC": 200.0, "DDD": 10.0, "EEE": 75.0, "FFF": 33.0}


def make_providers(delay=0.0, calls=None, bad=()):
    """Three fake providers that behave like the real ones: answer with an observation, sometimes disagree or fail."""
    live, peak, lock = [0], [0], threading.Lock()

    def mk(name, skew):
        def fn(t, fetch):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            try:
                if calls is not None:
                    calls.append((t, name))
                time.sleep(delay)
                if (t, name) in bad:
                    return quotes.obs(name, t, error="HTTP 503", http=503)
                return quotes.obs(name, t, PRICE[t] * (1 + skew), stamp=iso(NOW - dt.timedelta(seconds=4)))
            finally:
                with lock:
                    live[0] -= 1
        fn.__name__ = name.lower()
        return fn
    provs = [mk("ALPHA1", 0.0), mk("BETA2", 0.0002), mk("GAMMA3", -0.0001)]
    provs[0].peak = peak
    return provs


class Patched(unittest.TestCase):
    def setUp(self):
        self.saved = (quotes.PROVIDERS, dict(quotes._NAMES))
        self.cfg = H.cfg()
        self.cfg["quotes"]["min_providers"] = 2
        self.cfg["quotes"]["allow_single_source"] = False
        self.cfg["quotes"]["single_source_max_move_pct"] = 0

    def tearDown(self):
        quotes.PROVIDERS, quotes._NAMES = self.saved[0], self.saved[1]

    def install(self, provs):
        quotes.PROVIDERS = provs
        quotes._NAMES = {p.__name__: p.__name__.upper() for p in provs}


def comparable(c):
    return {k: c[k] for k in ("ticker", "status", "price", "providers_used", "provider_count", "execution_eligible")}


class TestIdenticalAnswers(Patched):
    def sequential(self, tickers, **kw):
        self.install(make_providers(**kw))
        c = H.fresh_db()
        return c, [comparable(quotes.get_quote(c, t, self.cfg, "exit", now=NOW, fetch=lambda *a, **k: None)) for t in tickers]

    def prefetched(self, tickers, **kw):
        self.install(make_providers(**kw))
        c = H.fresh_db()
        warm = quotes.prefetch(c, tickers, self.cfg, NOW, lambda *a, **k: None, workers=6)
        return c, [comparable(quotes.get_quote(c, t, self.cfg, "exit", now=NOW, fetch=lambda *a, **k: (_ for _ in ()).throw(AssertionError("the network was asked again")), prefetched=warm)) for t in tickers], warm

    def test_same_prices_statuses_and_providers_as_one_at_a_time(self):
        tk = sorted(PRICE)
        c1, a = self.sequential(tk)
        c2, b, warm = self.prefetched(tk)
        self.assertEqual(a, b)
        self.assertEqual(len(warm), len(tk) * 3)
        self.assertTrue(all(x["status"] == "PASS" for x in a))
        rows = lambda c: sorted(tuple(r) for r in c.execute("SELECT provider FROM provider_calls"))        # noqa: E731
        self.assertEqual(rows(c1), rows(c2))                                                                    # the same calls were charged against the rate budgets

    def test_a_failing_provider_gives_the_same_degraded_answer_either_way(self):
        bad = (("CCC", "BETA2"), ("CCC", "GAMMA3"), ("AAA", "ALPHA1"))
        tk = ["AAA", "CCC", "BBB"]
        _, a = self.sequential(tk, bad=bad)
        _, b, _ = self.prefetched(tk, bad=bad)
        self.assertEqual(a, b)
        self.assertEqual({x["ticker"]: x["status"] for x in a}["CCC"], "VALUATION_ONLY")                          # one source cannot execute when two are required

    def test_a_free_tier_budget_is_spent_on_the_first_stocks_exactly_as_before(self):
        self.cfg["quotes"]["limits"] = dict(self.cfg["quotes"].get("limits") or {}, BETA2=(3, 60))
        tk = sorted(PRICE)
        c1, a = self.sequential(tk)
        c2, b, warm = self.prefetched(tk)
        self.assertEqual(a, b)
        self.assertEqual(sum(1 for k in warm if k[1] == "BETA2"), 3)                                            # planned within the budget, not 6 calls into a limit of 3
        self.assertEqual(c2.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='BETA2'").fetchone()[0], 3)
        self.assertEqual([k[0] for k in warm if k[1] == "BETA2"].sort() or 0, 0)

    def test_stocks_with_a_fresh_cached_quote_are_not_fetched_again(self):
        self.install(make_providers())
        c = H.fresh_db()
        quotes.get_quote(c, "AAA", self.cfg, "exit", now=NOW, fetch=lambda *a, **k: None)
        warm = quotes.prefetch(c, ["AAA", "BBB"], self.cfg, NOW, lambda *a, **k: None, workers=4)
        self.assertEqual({k[0] for k in warm}, {"BBB"})


class TestSpeedAndSafety(Patched):
    def test_six_stocks_in_the_time_of_one_instead_of_six(self):
        delay, tk = 0.25, sorted(PRICE)
        self.install(make_providers(delay=delay))
        c = H.fresh_db()
        t = time.monotonic()
        for s in tk:
            quotes.get_quote(c, s, self.cfg, "exit", now=NOW, fetch=lambda *a, **k: None)
        seq = time.monotonic() - t
        self.install(make_providers(delay=delay))
        c = H.fresh_db()
        t = time.monotonic()
        warm = quotes.prefetch(c, tk, self.cfg, NOW, lambda *a, **k: None, workers=6)
        for s in tk:
            quotes.get_quote(c, s, self.cfg, "exit", now=NOW, fetch=lambda *a, **k: None, prefetched=warm)
        par = time.monotonic() - t
        self.assertLess(par, seq / 2.5)

    def test_a_provider_that_timed_out_during_the_prefetch_is_simply_absent_never_a_crash_or_a_late_request(self):
        provs = make_providers()
        slow = provs[2]
        orig = slow

        def slow_fn(t, fetch):
            time.sleep(1.0)
            return orig(t, fetch)
        slow_fn.__name__ = orig.__name__
        self.install([provs[0], provs[1], slow_fn])
        c = H.fresh_db()
        warm = quotes.prefetch(c, ["AAA", "BBB"], self.cfg, NOW, lambda *a, **k: None, workers=6, deadline_s=0.3)
        self.assertEqual({k[1] for k in warm}, {"ALPHA1", "BETA2"})                                                 # the slow one timed out
        q = quotes.get_quote(c, "AAA", self.cfg, "exit", now=NOW, fetch=lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked the network after the deadline")), prefetched=warm)
        self.assertEqual((q["status"], q["provider_count"]), ("PASS", 2))                                           # decided on what arrived, exactly like a provider that answered with an error

    def test_a_stock_nobody_prefetched_still_gets_its_normal_lookup(self):
        self.install(make_providers())
        c = H.fresh_db()
        warm = quotes.prefetch(c, ["AAA"], self.cfg, NOW, lambda *a, **k: None, workers=3)
        q = quotes.get_quote(c, "BBB", self.cfg, "exit", now=NOW, fetch=lambda *a, **k: None, prefetched=warm)
        self.assertEqual((q["status"], q["price"] > 0), ("PASS", True))

    def test_no_more_than_three_requests_to_one_provider_at_once(self):
        provs = make_providers(delay=0.08)
        self.install(provs)
        quotes.prefetch(H.fresh_db(), sorted(PRICE), self.cfg, NOW, lambda *a, **k: None, workers=12)
        self.assertLessEqual(provs[0].peak[0], 3 * 3)                                                           # per provider <= 3, three providers share the counter


class TestThePipelineUsesItOnlyWhereItIsSafe(Patched):
    def test_never_for_a_tests_own_quote_function_nor_one_worker_nor_a_closed_market(self):
        self.install(make_providers())
        c = H.fresh_db()
        self.cfg["runtime"]["fetch_workers"] = 6                                                                  # parallel IS enabled, so only the custom quote function can be what stops it
        deps = pipeline.Deps(quote_fn=lambda *a: None)
        self.assertEqual(pipeline._warm_quotes(c, self.cfg, deps, NOW, ["AAA"]), {})                              # a custom quote function is left alone
        self.assertNotEqual(pipeline._warm_quotes(c, self.cfg, pipeline.Deps(fetch_json=lambda *a, **k: None), NOW, ["AAA"]), {})   # the same call without one does fetch
        self.cfg["runtime"]["fetch_workers"] = 1
        self.assertEqual(pipeline._warm_quotes(c, self.cfg, pipeline.Deps(), NOW, ["AAA"]), {})                   # one worker = the old behaviour
        self.cfg["runtime"]["fetch_workers"] = 6
        self.assertEqual(pipeline._warm_quotes(c, self.cfg, pipeline.Deps(), TL.T_PRE, ["AAA"]), {})              # orders cannot execute: nothing to hurry
        self.assertEqual(pipeline._warm_quotes(c, self.cfg, pipeline.Deps(), NOW, []), {})

    def test_the_wishlist_is_in_priority_order_what_we_hold_first(self):
        c = H.fresh_db()
        c.execute("INSERT INTO positions(ticker,opened,entry_price,shares,cost,status) VALUES('HELD1',?,10,1,10,'OPEN')", (iso(NOW),))
        for tk, ev in (("LOW", 0.01), ("HIGH", 0.06), ("MID", 0.03)):
            c.execute("INSERT INTO decisions(created,ticker,action,status,reason,ev_net) VALUES(?,?,?,?,?,?)", (iso(NOW), tk, "BUY", "AWAITING_MARKET", "{}", ev))
        self.assertEqual(pipeline._quote_wishlist(c, [{"ticker": "NEW1"}, {"ticker": "HIGH"}]), ["HELD1", "HIGH", "MID", "LOW", "NEW1"])


if __name__ == "__main__":
    unittest.main()


class TestPricesRefreshSideBySide(unittest.TestCase):
    """The dashboard price refresh: downloads side by side (at most 2 at a time, 5 a second), stores one symbol at a time in order."""

    def run_prices(self, workers, delay=0.0, bad=(), rate_limit_at=None):
        from paper_agent import net, prices
        c, cfg = H.fresh_db(), H.cfg()
        cfg["runtime"]["fetch_workers"] = workers
        cfg["prices"] = dict(cfg.get("prices") or {}, max_symbols=10, refresh_open_minutes=5, refresh_closed_minutes=5, extended_hours=True)
        syms = ["S%d" % i for i in range(8)]
        live, peak, lock = [0], [0], threading.Lock()
        bar_t = int(NOW.timestamp()) - 30

        def fetch(url, headers=None, **k):
            sym = url.split("/chart/")[1].split("?")[0]
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            try:
                time.sleep(delay)
                if sym in bad:
                    raise net.FetchError("HTTP 404", 404)
                if sym == rate_limit_at:
                    raise net.FetchError("HTTP 429", 429)
                return {"chart": {"result": [{"meta": {"symbol": sym, "regularMarketPrice": 10.0, "chartPreviousClose": 9.5, "regularMarketTime": bar_t},
                                              "timestamp": [bar_t], "indicators": {"quote": [{"close": [10.0 + int(sym[1:]) / 10.0], "open": [10], "high": [11], "low": [9], "volume": [100]}]}}], "error": None}}, 200
            finally:
                with lock:
                    live[0] -= 1
        orig = prices.symbols_of_interest
        prices.symbols_of_interest = lambda conn, limit: syms
        try:
            out = prices.run(c, cfg, NOW, fetch, 25.0)
        finally:
            prices.symbols_of_interest = orig
        cache = [tuple(r) for r in c.execute("SELECT ticker,round(price,2) FROM quote_cache ORDER BY ticker")]
        return out, cache, peak[0]

    def test_identical_stored_prices_with_one_worker_or_six(self):
        a = self.run_prices(1)
        b = self.run_prices(6)
        self.assertEqual(a[1], b[1])
        self.assertEqual({k: a[0][k] for k in ("symbols", "updated", "failed")}, {k: b[0][k] for k in ("symbols", "updated", "failed")})
        self.assertGreater(a[0]["updated"], 0)

    def test_faster_and_never_more_than_two_at_once(self):
        t = time.monotonic()
        self.run_prices(1, delay=0.4)                                                                 # about a real Yahoo request
        seq = time.monotonic() - t
        t = time.monotonic()
        out, cache, peak = self.run_prices(6, delay=0.4)
        par = time.monotonic() - t
        self.assertLess(par, seq * 0.75)
        self.assertLessEqual(peak, 2)

    def test_one_bad_symbol_and_a_rate_limit_behave_as_before(self):
        out, cache, _ = self.run_prices(6, bad=("S3",))
        self.assertEqual((out["updated"], out["failed"]), (7, 1))
        out, cache, _ = self.run_prices(6, rate_limit_at="S4")
        self.assertEqual(out["stopped"], "rate limited (HTTP 429)")
        self.assertEqual([c[0] for c in cache], ["S0", "S1", "S2", "S3"])                           # stored in order up to the limit, nothing after it


if __name__ == "__main__":
    unittest.main()
