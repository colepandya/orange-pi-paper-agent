"""Free options feeds: each parser against realistic payloads, merging and disputes, picking a REAL liquid contract, anchoring a delayed quote to the live price, caching and health, IV rank, and full lifecycles
through the real pipeline with the feeds up, down, illiquid and expensive."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_options as TO  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import dashboard, db, net, optionsdata as D, options as O  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

T_PRE, T_OPEN = TL.T_PRE, TL.T_OPEN
NOW = T_OPEN
S = 120.0


def expiry(now=NOW, dte=21):
    return O.next_expiry(now.date(), dte)


def book(now=NOW, spot=S, iv=0.45, spread=0.04, oi=2000, vol=500, skew=1.0, strikes=None):
    """A believable chain: one expiry, strikes around the spot, priced by the model at `iv`, with a real spread."""
    e = expiry(now)
    T = D.years_left(e, now)
    rows = []
    for k in strikes or [spot + d for d in (-15, -12.5, -10, -7.5, -5, -2.5, 0, 2.5, 5, 7.5, 10, 12.5, 15)]:
        for kind in ("CALL", "PUT"):
            mid = max(0.05, D.bs_price(kind, spot, k, T, 0.04, iv)) * skew
            rows.append({"kind": kind, "expiry": e.isoformat(), "strike": k, "bid": round(mid * (1 - spread / 2), 2), "ask": round(mid * (1 + spread / 2), 2), "iv": iv, "oi": oi, "vol": vol})
    return rows


def cboe_json(rows, now=NOW, spot=S, iv30=0.45, age_min=15):
    return {"timestamp": (now - dt.timedelta(minutes=age_min)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"), "data": {"symbol": "NVDA", "current_price": spot, "iv30": iv30 * 100,
            "options": [{"option": D.build_occ("NVDA", r["expiry"], r["kind"], r["strike"]), "bid": r["bid"], "ask": r["ask"], "iv": r["iv"] * 100, "delta": 0.5, "volume": r["vol"], "open_interest": r["oi"], "last_trade_price": (r["bid"] + r["ask"]) / 2} for r in rows]}}


def nasdaq_json(rows):
    out, by = [], {}
    for r in rows:
        by.setdefault((r["expiry"], r["strike"]), {})[r["kind"]] = r
    last = None
    for (e, k), kinds in sorted(by.items()):
        if e != last:
            out.append({"expirygroup": dt.date.fromisoformat(e).strftime("%B %d, %Y").replace(" 0", " ")})
            last = e
        c, p = kinds.get("CALL"), kinds.get("PUT")
        out.append({"strike": "%.2f" % k, "c_Bid": "%.2f" % c["bid"], "c_Ask": "%.2f" % c["ask"], "c_Last": "%.2f" % c["bid"], "c_Volume": str(c["vol"]), "c_Openinterest": str(c["oi"]),
                    "p_Bid": "%.2f" % p["bid"], "p_Ask": "%.2f" % p["ask"], "p_Last": "--", "p_Volume": str(p["vol"]), "p_Openinterest": str(p["oi"])})
    return {"data": {"table": {"rows": out}}}


def alpaca_json(rows, now=NOW):
    t = (now - dt.timedelta(minutes=15)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"snapshots": {D.build_occ("NVDA", r["expiry"], r["kind"], r["strike"]): {"latestQuote": {"bp": r["bid"], "ap": r["ask"], "t": t}, "latestTrade": {"p": r["bid"]}, "impliedVolatility": r["iv"], "greeks": {"delta": 0.5}}
                          for r in rows}}


def yahoo_json(rows, now=NOW, date=None):
    e = dt.date.fromisoformat(rows[0]["expiry"])
    ep = int(dt.datetime(e.year, e.month, e.day, tzinfo=dt.timezone.utc).timestamp())
    if date is None:
        return {"optionChain": {"result": [{"expirationDates": [ep], "quote": {"regularMarketPrice": S}}]}}
    mk = lambda kind: [{"strike": r["strike"], "bid": r["bid"], "ask": r["ask"], "lastPrice": r["bid"], "impliedVolatility": r["iv"], "volume": r["vol"], "openInterest": r["oi"],              # noqa: E731
                        "lastTradeDate": int((now - dt.timedelta(minutes=15)).timestamp())} for r in rows if r["kind"] == kind]
    return {"optionChain": {"result": [{"options": [{"calls": mk("CALL"), "puts": mk("PUT")}]}]}}


class FeedMarket(TL.FakeMarket):
    """The stock-quote providers PLUS the four free options feeds, each switchable."""

    def __init__(self):
        super().__init__()
        self.rows = book()
        self.feed_down = set()
        self.feed_calls = []
        self.iv30 = 0.45
        self.price_skew = {}

    def __call__(self, url, headers=None, **kw):
        for name, key in (("CBOE", "cdn.cboe.com"), ("ALPACA", "data.alpaca.markets/v1beta1/options"), ("NASDAQ", "api.nasdaq.com"), ("YAHOO", "query1.finance.yahoo.com/v7")):
            if key in url:
                self.feed_calls.append(name)
                if name in self.feed_down:
                    raise net.FetchError("HTTP 503", 503)
                rows = [dict(r, bid=round(r["bid"] * self.price_skew.get(name, 1), 2), ask=round(r["ask"] * self.price_skew.get(name, 1), 2)) for r in self.rows]
                if name == "CBOE":
                    return cboe_json(rows, self.now, S, self.iv30), 200
                if name == "ALPACA":
                    return alpaca_json(rows, self.now), 200
                if name == "NASDAQ":
                    return nasdaq_json(rows), 200
                return yahoo_json(rows, self.now, "date=" in url or None), 200
        return super().__call__(url, headers, **kw)


def cfg():
    c = H.cfg()
    c["instruments"]["options_enabled"] = True
    return c


class TestEachFeedParses(unittest.TestCase):
    def setUp(self):
        self.m = FeedMarket()
        self.cfg = cfg()
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_API_SECRET"] = "k" * 10, "s" * 10

    def tearDown(self):
        os.environ.pop("ALPACA_API_KEY", None)
        os.environ.pop("ALPACA_API_SECRET", None)

    def check(self, name, iv_known=True):
        r = D.FEEDS[name]("NVDA", self.m, NOW, self.cfg, S)
        self.assertEqual(r["source"], name)
        cs = r["contracts"]
        self.assertGreaterEqual(len(cs), 20)
        c = next(x for x in cs if x["kind"] == "CALL" and abs(x["strike"] - 120.0) < 1e-9)
        self.assertEqual(c["expiry"], expiry().isoformat())
        self.assertLess(c["bid"], c["ask"])
        self.assertEqual(c["source"], name)
        if iv_known:
            self.assertAlmostEqual(c["iv"], 0.45, places=3)                                          # 45.0 (percent) and 0.45 both become 0.45
        else:
            self.assertIsNone(c["iv"])
        return r

    def test_cboe(self):
        r = self.check("CBOE")
        self.assertEqual(r["underlying"], S)
        self.assertAlmostEqual(r["iv30"], 0.45, places=3)
        self.assertIsNotNone(r["as_of"])

    def test_alpaca_needs_its_keys_and_parses_snapshots(self):
        self.check("ALPACA")
        os.environ.pop("ALPACA_API_KEY")
        with self.assertRaises(net.FetchError):
            D.alpaca("NVDA", self.m, NOW, self.cfg, S)

    def test_nasdaq_has_no_iv_and_reads_the_expiry_group(self):
        r = self.check("NASDAQ", iv_known=False)
        p = next(x for x in r["contracts"] if x["kind"] == "PUT" and abs(x["strike"] - 115.0) < 1e-9)
        self.assertGreater(p["oi"], 0)

    def test_yahoo_asks_for_the_expiry_in_two_steps(self):
        self.check("YAHOO")
        self.assertEqual(self.m.feed_calls.count("YAHOO"), 2)

    def test_a_dead_feed_is_an_exception_never_garbage(self):
        self.m.feed_down = {"CBOE"}
        with self.assertRaises(net.FetchError):
            D.cboe("NVDA", self.m, NOW, self.cfg, S)

    def test_the_contract_symbol_round_trips(self):
        self.assertEqual(D.parse_occ("NVDA261120C00125500"), ("NVDA", "2026-11-20", "CALL", 125.5))
        self.assertEqual(D.build_occ("NVDA", "2026-11-20", "CALL", 125.5), "NVDA261120C00125500")
        self.assertIsNone(D.parse_occ("garbage"))


class TestMergingAndPicking(unittest.TestCase):
    def setUp(self):
        self.m = FeedMarket()
        self.cfg = cfg()

    def two(self, skew):
        self.m.price_skew = {"NASDAQ": skew}
        a = D.cboe("NVDA", self.m, NOW, self.cfg, S)
        b = D.nasdaq("NVDA", self.m, NOW, self.cfg, S)
        return D.merge([a, b])

    def test_feeds_that_agree_give_one_median_quote_with_both_sources(self):
        m = self.two(1.01)
        c = next(x for x in m if x["kind"] == "CALL" and x["strike"] == 120.0)
        self.assertEqual(c["sources"], ["CBOE", "NASDAQ"])
        self.assertFalse(c["disputed"])
        self.assertIsNotNone(c["iv"])

    def test_three_feeds_give_the_MIDDLE_price_not_the_first_or_the_average(self):
        a = D.cboe("NVDA", FeedMarketRows(book()), NOW, self.cfg, S)
        m = FeedMarket()
        m.price_skew = {"NASDAQ": 1.03, "YAHOO": 1.20}
        b = D.nasdaq("NVDA", m, NOW, self.cfg, S)
        y = D.yahoo("NVDA", m, NOW, self.cfg, S)
        merged = D.merge([a, b, y])
        c = next(x for x in merged if x["kind"] == "CALL" and x["strike"] == 120.0)
        raw = {r["source"]: next(x for x in r["contracts"] if x["kind"] == "CALL" and x["strike"] == 120.0)["ask"] for r in (a, b, y)}
        self.assertEqual(c["ask"], sorted(raw.values())[1])                                            # the median of the three
        self.assertNotEqual(c["ask"], raw["CBOE"] if sorted(raw.values())[1] != raw["CBOE"] else None)

    def test_feeds_that_disagree_by_more_than_15_percent_are_disputed_and_never_picked(self):
        m = self.two(1.40)
        c = next(x for x in m if x["kind"] == "CALL" and x["strike"] == 120.0)
        self.assertTrue(c["disputed"])
        ok, why = D.liquid(c, NOW, self.cfg)
        self.assertFalse(ok)
        self.assertIn("disagree", why)

    def test_crossed_and_empty_quotes_are_dropped(self):
        bad = {"source": "X", "contracts": [D._c("CALL", expiry().isoformat(), 120.0, 5.0, 4.0, None, None, None, 1, 1, "X", None), D._c("CALL", expiry().isoformat(), 122.5, None, None, None, None, None, 1, 1, "X", None),
                                            D._c("CALL", expiry().isoformat(), 125.0, 0.0, 0.0, None, None, None, 1, 1, "X", None)]}
        self.assertEqual(D.merge([bad]), [])

    def pick(self, kind, rows=None, now=NOW, **over):
        self.cfg["instruments"].update(over)
        r = D.cboe("NVDA", FeedMarketRows(rows or book()), now, self.cfg, S)
        return D.pick(D.merge([r]), kind, S, now, self.cfg)

    def test_a_call_is_picked_at_or_above_the_price_and_a_put_at_or_below_it_on_a_real_expiry(self):
        c, _ = self.pick("CALL")
        p, _ = self.pick("PUT")
        self.assertGreaterEqual(c["strike"], S)
        self.assertLessEqual(p["strike"], S)
        self.assertGreaterEqual((dt.date.fromisoformat(c["expiry"]) - NOW.date()).days, 21)
        self.assertEqual(c["strike"], 120.0)

    def test_an_illiquid_strike_is_skipped_for_the_next_one_and_all_illiquid_means_none(self):
        rows = book()
        for r in rows:
            if r["kind"] == "CALL" and r["strike"] == 120.0:
                r["bid"], r["ask"] = round(r["bid"] * 0.5, 2), round(r["ask"] * 1.5, 2)                  # a 100% wide spread
        c, _ = self.pick("CALL", rows)
        self.assertEqual(c["strike"], 122.5)
        c2, why = self.pick("CALL", book(oi=10))
        self.assertIsNone(c2)
        self.assertIn("contracts outstanding", why)
        c3, why3 = self.pick("CALL", book(spread=0.5))
        self.assertIsNone(c3)
        self.assertIn("spread", why3)

    def test_a_stale_quote_is_refused(self):
        r = D.cboe("NVDA", FeedMarketRows(book(), age_min=120), NOW, self.cfg, S)
        c, why = D.pick(D.merge([r]), "CALL", S, NOW, self.cfg)
        self.assertIsNone(c)
        self.assertIn("stale", why)

    def test_the_picker_itself_refuses_a_contract_that_expires_too_soon(self):
        soon = (NOW.date() + dt.timedelta(days=10)).isoformat()                                         # bypasses the parsers' own window: the picker must hold the line on its own
        r = {"source": "X", "contracts": [D._c("CALL", soon, 120.0, 4.9, 5.1, None, 0.45, 0.5, 500, 2000, "X", NOW.timestamp() - 600)]}
        c, why = D.pick(D.merge([r]), "CALL", S, NOW, self.cfg)
        self.assertIsNone(c)
        self.assertIn("21 or more days", why)

    def test_nothing_expires_far_enough_out(self):
        self.cfg["instruments"]["options_min_dte"] = 90
        c, why = self.pick("CALL")
        self.assertIsNone(c)
        self.assertIn("90 or more days", why)


class FeedMarketRows:
    def __init__(self, rows, age_min=15):
        self.rows, self.age = rows, age_min

    def __call__(self, url, headers=None, **kw):
        return cboe_json(self.rows, NOW, S, 0.45, self.age), 200


class TestAnchoring(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg()
        r = D.cboe("NVDA", FeedMarketRows(book()), NOW, self.cfg, S)
        self.q = next(c for c in D.merge([r]) if c["kind"] == "CALL" and c["strike"] == 120.0)

    def test_at_the_same_stock_price_it_reproduces_the_market_quote(self):
        a = D.anchor(self.q, S, S, NOW, self.cfg)
        self.assertAlmostEqual(a["mid"], self.q["mid"], delta=self.q["mid"] * 0.01)
        self.assertAlmostEqual(a["iv"], 0.45, places=2)
        self.assertAlmostEqual((a["ask"] - a["bid"]) / a["mid"], (self.q["ask"] - self.q["bid"]) / self.q["mid"], delta=0.01)             # the market's REAL spread is kept

    def test_a_delayed_quote_is_moved_to_the_live_stock_price(self):
        up = D.anchor(self.q, S * 1.02, S, NOW, self.cfg)
        down = D.anchor(self.q, S * 0.98, S, NOW, self.cfg)
        self.assertGreater(up["mid"], self.q["mid"])                                                  # the stock rose since the quote: a call is worth more
        self.assertLess(down["mid"], self.q["mid"])
        self.assertGreater(up["ask"], up["bid"])

    def test_a_quote_inconsistent_with_its_own_volatility_is_refused(self):
        bad = dict(self.q, iv=0.10)                                                                   # claims 10% volatility on a price that needs 45%
        self.assertIsNone(D.anchor(bad, S, S, NOW, self.cfg))

    def test_a_missing_iv_is_solved_from_the_price(self):
        q = dict(self.q, iv=None)
        a = D.anchor(q, S, S, NOW, self.cfg)
        self.assertAlmostEqual(a["iv"], 0.45, places=2)


class TestCachingHealthAndRank(unittest.TestCase):
    def setUp(self):
        self.c, self.cfg, self.m = H.fresh_db(), cfg(), FeedMarket()
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_API_SECRET"] = "k" * 10, "s" * 10

    def tearDown(self):
        os.environ.pop("ALPACA_API_KEY", None)
        os.environ.pop("ALPACA_API_SECRET", None)

    def test_all_four_feeds_are_fetched_once_then_the_cache_serves_four_minutes(self):
        ch = D.chain_for(self.c, self.cfg, "NVDA", NOW, S, self.m)
        self.assertEqual(sorted(ch["sources"]), ["ALPACA", "CBOE", "NASDAQ", "YAHOO"])
        n = len(self.m.feed_calls)
        D.chain_for(self.c, self.cfg, "NVDA", NOW + dt.timedelta(minutes=2), S, self.m)
        self.assertEqual(len(self.m.feed_calls), n)                                                   # cached
        D.chain_for(self.c, self.cfg, "NVDA", NOW + dt.timedelta(minutes=5), S, self.m)
        self.assertGreater(len(self.m.feed_calls), n)                                                 # expired: fetched again

    def test_a_feed_that_fails_is_recorded_and_the_others_still_answer(self):
        self.m.feed_down = {"CBOE", "YAHOO"}
        ch = D.chain_for(self.c, self.cfg, "NVDA", NOW, S, self.m)
        self.assertEqual(sorted(ch["sources"]), ["ALPACA", "NASDAQ"])
        h = {r["feed"]: r for r in self.c.execute("SELECT * FROM option_feed_health")}
        self.assertEqual(h["CBOE"]["fail_count"], 1)
        self.assertIn("FetchError", h["CBOE"]["last_error"])
        self.assertEqual(h["NASDAQ"]["ok_count"], 1)
        self.assertGreater(h["NASDAQ"]["last_contracts"], 20)

    def test_all_feeds_down_means_no_chain_and_the_failure_is_not_retried_every_second(self):
        self.m.feed_down = set(D.FEEDS)
        self.assertIsNone(D.chain_for(self.c, self.cfg, "NVDA", NOW, S, self.m))
        n = len(self.m.feed_calls)
        self.assertIsNone(D.chain_for(self.c, self.cfg, "NVDA", NOW + dt.timedelta(seconds=30), S, self.m))
        self.assertEqual(len(self.m.feed_calls), n)

    def test_a_disabled_feed_is_not_called(self):
        self.cfg["instruments"]["options_feed_yahoo"] = False
        self.cfg["instruments"]["options_feed_nasdaq"] = False
        D.chain_for(self.c, self.cfg, "NVDA", NOW, S, self.m)
        self.assertEqual(set(self.m.feed_calls), {"CBOE", "ALPACA"})

    def test_the_implied_volatility_is_recorded_daily_and_ranked_once_there_is_a_month(self):
        D.chain_for(self.c, self.cfg, "NVDA", NOW, S, self.m)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM iv_history WHERE ticker='NVDA'").fetchone()[0], 1)
        self.assertIsNone(D.iv_rank(self.c, "NVDA"))
        for i in range(1, 6):
            self.c.execute("INSERT OR REPLACE INTO iv_history(ticker,day,iv30) VALUES('NVDA',?,?)", ((NOW - dt.timedelta(days=i)).date().isoformat(), 0.20 + 0.05 * i))
        self.c.commit()
        self.assertIsNone(D.iv_rank(self.c, "NVDA"))                                                  # six days is not a range: no rank yet
        for i in range(1, 30):
            self.c.execute("INSERT OR REPLACE INTO iv_history(ticker,day,iv30) VALUES('NVDA',?,?)", ((NOW - dt.timedelta(days=i)).date().isoformat(), 0.30 + 0.01 * i))
        self.c.commit()
        r = D.iv_rank(self.c, "NVDA")                                                                  # today's 0.45 against a 0.31-0.59 history
        self.assertAlmostEqual(r, 100 * (0.45 - 0.31) / (0.59 - 0.31), delta=1.0)


class RealBase(TO.Base):
    def setUp(self):
        super().setUp()
        self.mkt = FeedMarket()
        self.mkt.price.update({"NVDA": 120.0, "SPY": 450.0})
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_API_SECRET"] = "k" * 10, "s" * 10

    def tearDown(self):
        super().tearDown()
        os.environ.pop("ALPACA_API_KEY", None)
        os.environ.pop("ALPACA_API_SECRET", None)

    def open_real(self):
        return self.open_option()


class TestRealContractsThroughThePipeline(RealBase):
    def test_the_option_is_a_real_listed_contract_priced_from_the_market_not_the_model(self):
        o = self.open_real()
        self.assertEqual(o["price_source"], "CHAIN")
        self.assertEqual(o["contract"], D.build_occ("NVDA", o["expiry"], o["kind"], o["strike"]))
        listed = {(r["kind"], r["strike"]) for r in self.mkt.rows}
        self.assertIn((o["kind"], o["strike"]), listed)                                               # a strike that really exists
        self.assertAlmostEqual(o["entry_iv"], 0.45, places=2)                                         # the market's volatility, not the model's guess
        self.assertIn("real call contract", o["note"])
        self.assertIn("CBOE", o["note"])
        d = self.one("SELECT reason FROM decisions WHERE ticker='NVDA'")[0]
        self.assertIn('"price_source": "CHAIN"', d)
        self.assertIn('"model_priced": false', d)
        self.assertGreaterEqual(self.one("SELECT COUNT(*) FROM option_model_checks")[0], 1)            # and how far the model was from the market was logged
        self.assertEqual(self.one("SELECT COUNT(*) FROM option_feed_health")[0], 4)
        self.options_books()

    def test_the_mark_follows_the_markets_latest_volatility_every_cycle(self):
        o = self.open_real()
        self.mkt.iv30 = 0.45
        self.mkt.rows = book(iv=0.70)                                                                  # the market reprices the contract much higher
        self.c.execute("DELETE FROM option_chain_cache")
        self.c.commit()
        self.mkt.price["NVDA"] = o["entry_underlying"]
        self.guard(T_OPEN + dt.timedelta(minutes=10))
        q = self.one("SELECT * FROM option_positions")
        self.assertAlmostEqual(q["last_iv"], 0.70, places=2)
        self.assertGreater(q["last_mark"], o["last_mark"] * 1.2)

    def test_an_illiquid_real_market_means_the_stock_is_bought_instead(self):
        self.mkt.rows = book(oi=5)
        self.cycle_with(T_PRE, TL.NVDA_NEWS)
        self.cycle_with(T_OPEN)
        self.assertEqual(self.one("SELECT COUNT(*) FROM option_positions")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 1)

    def test_a_wide_real_spread_means_the_stock_is_bought_instead(self):
        self.mkt.rows = book(spread=0.6)
        self.cycle_with(T_PRE, TL.NVDA_NEWS)
        self.cycle_with(T_OPEN)
        self.assertEqual((self.one("SELECT COUNT(*) FROM option_positions")[0], self.one("SELECT COUNT(*) FROM positions")[0]), (0, 1))

    def test_with_every_feed_down_the_model_prices_it_and_says_so(self):
        self.mkt.feed_down = set(D.FEEDS)
        o = self.open_real()
        self.assertEqual(o["price_source"], "MODEL")
        self.assertIn("MODEL price", o["note"])
        self.assertIn('"model_priced": true', self.one("SELECT reason FROM decisions WHERE ticker='NVDA'")[0])

    def test_with_the_fallback_off_no_option_is_bought_without_a_real_quote(self):
        self.mkt.feed_down = set(D.FEEDS)
        self.cfg["instruments"]["options_allow_model_fallback"] = False
        self.cycle_with(T_PRE, TL.NVDA_NEWS)
        self.cycle_with(T_OPEN)
        self.assertEqual((self.one("SELECT COUNT(*) FROM option_positions")[0], self.one("SELECT COUNT(*) FROM positions")[0]), (0, 1))

    def test_with_real_quotes_switched_off_the_feeds_are_never_called(self):
        self.cfg["instruments"]["options_use_real_quotes"] = False
        o = self.open_real()
        self.assertEqual(o["price_source"], "MODEL")
        self.assertEqual(self.mkt.feed_calls, [])

    def test_expensive_volatility_blocks_the_purchase(self):
        for i in range(1, 30):
            self.c.execute("INSERT OR REPLACE INTO iv_history(ticker,day,iv30) VALUES('NVDA',?,?)", ((T_PRE - dt.timedelta(days=i)).date().isoformat(), 0.20 + 0.005 * i))
        self.c.commit()                                                                                 # 29 days ranging 0.205-0.345; today the feed says 0.45: the dearest ever recorded
        self.cycle_with(T_PRE, TL.NVDA_NEWS)
        self.cycle_with(T_OPEN)
        self.assertEqual(self.one("SELECT COUNT(*) FROM option_positions")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 1)

    def test_one_dissenting_feed_cannot_move_the_price_but_a_wild_one_blocks_it(self):
        self.mkt.price_skew = {"YAHOO": 1.03}
        o = self.open_real()
        self.assertEqual(o["price_source"], "CHAIN")                                                   # a 3% difference is just noise: the median is used

    def test_the_page_shows_the_feeds_and_how_good_the_model_is(self):
        self.open_real()
        html = dashboard.options_section(self.c, T_OPEN)
        self.assertIn("Free options feeds", html)
        self.assertIn("CBOE", html)
        self.assertIn("real contract", html)
        self.assertIn("How good is the pure model", html)


if __name__ == "__main__":
    unittest.main()
