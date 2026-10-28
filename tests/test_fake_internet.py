"""The WHOLE system against a simulated internet (urlopen replaced): real network layer, real parsers, real company refresh, real quote consensus.
Found: numbers arriving as strings ("28.4", "N/A") crashed the company stage and the BUY path (a likely cause of whole cycles ending in ERROR)."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import fake_internet as FI  # noqa: E402
import test_recall as TR  # noqa: E402
from test_trade_lifecycle import books_balance  # noqa: E402
from paper_agent import company, db, pipeline, recall  # noqa: E402

UTC = dt.timezone.utc
KEYS = ("TWELVE_DATA_API_KEY", "FINNHUB_API_KEY", "ALPACA_API_KEY", "ALPACA_API_SECRET", "ALPHA_VANTAGE_API_KEY")
OTHER = ("MASSIVE_API_KEY", "TIINGO_API_KEY", "EODHD_API_KEY")
T0 = dt.datetime(2026, 10, 27, 12, 50, tzinfo=UTC)               # 08:50 New York; the market opens at 13:30 UTC
SRC = [{"id": "r%d" % i, "name": "R%d" % i, "type": "rss", "url": "https://feeds.example%d.com/rss" % i, "trust": .8, "interval": 0} for i in range(4)] + [
    {"id": "sec", "name": "SEC", "type": "sec_atom", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom", "trust": .9, "interval": 0},
    {"id": "g1", "name": "G1", "type": "gdelt", "query": "markets", "trust": .5, "interval": 0}, {"id": "fh", "name": "FH", "type": "finnhub_general", "trust": .7, "interval": 0},
    {"id": "fda", "name": "FDA", "type": "openfda", "url": "https://api.fda.gov/drug/enforcement.json", "trust": .9, "interval": 0},
    {"id": "usa", "name": "USA", "type": "usaspending", "url": "https://api.usaspending.gov/api/v2/search/spending_by_award/", "trust": .9, "interval": 0},
    {"id": "fr", "name": "FR", "type": "federal_register", "url": "https://www.federalregister.gov/api/v1/documents.json", "trust": .9, "interval": 0}]


class Env(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in KEYS + OTHER}
        for k in KEYS:
            os.environ[k] = "k" * 20
        for k in OTHER:
            os.environ.pop(k, None)
        self.uni = TR.uni()
        self.co = {t: {"name": m["name"], "cik": m["cik"], "price": 100.0 + i * 7} for i, (t, m) in enumerate(self.uni.watch.items())}
        self.heads = [(h, "") for h, _, _ in recall.POSITIVE]

    def tearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def morning(self, seed=1, degrade=0.0, degrade_quotes=False, weird=False, cycles=10, c=None):
        c = c or H.fresh_db()
        cfg = H.cfg()
        cfg["runtime"]["min_available_memory_mb"] = 0
        errors, bad = [], 0
        net = FI.Internet(T0, self.co, self.heads, seed, degrade, degrade_quotes, weird)
        with net:
            for k in range(cycles):
                now = T0 + dt.timedelta(minutes=5 * k)
                net.now = now
                r = pipeline.run_cycle(c, cfg, self.uni, SRC, pipeline.Deps(sleep=lambda s: None), now)
                errors += r["errors"]
                bad += len(r.get("company_errors") or [])
        return c, errors, net


class TestNumbersFromProviders(unittest.TestCase):
    def test_num_accepts_only_finite_numbers(self):
        n = company.num
        for good, want in ((28.4, 28.4), ("28.4", 28.4), ("1,234.5", 1234.5), (" 7 ", 7.0), ("12%", 12.0), (0, 0.0), ("-3", -3.0)):
            self.assertEqual(n(good), want, good)
        for bad in (None, "", "N/A", "n/a", "NaN", "inf", "--", "abc", True, False, [], {}, float("nan"), float("inf"), "1e999"):
            self.assertIsNone(n(bad), repr(bad))

    def test_a_metrics_payload_full_of_strings_and_nulls_cannot_crash_scoring(self):
        """REGRESSION: `lin()` did arithmetic on a string: TypeError -> the whole company stage / BUY path failed."""
        j = {"metric": {"beta": "1.21", "roeTTM": "N/A", "netProfitMarginTTM": "24.2", "operatingMarginTTM": None, "grossMarginTTM": "", "revenueGrowthTTMYoy": "18.2",
                        "totalDebt/totalEquityAnnual": "0.4", "totalDebt/totalEquityQuarterly": "N/A"}}
        m = company.parse_finnhub_metrics(j)
        self.assertEqual((m["beta"], m["net_margin"], m["revenue_growth"], m["roe"]), (1.21, 0.242, 0.182, None))
        s = company.score_company({k: v for k, v in m.items()})
        self.assertTrue(0 <= s["financial_strength"] <= 100)
        company.score_company({"beta": "1.2", "roe": "N/A", "net_margin": "x", "adv_usd": "123", "vol_daily": None, "revenue_growth": [1]})   # even raw junk is tolerated
        for junk in ({}, [], None, "x", 5, {"metric": "x"}, {"metric": []}):
            company.parse_finnhub_metrics(junk)
            company.parse_finnhub_profile(junk)
            company.parse_sec_facts(junk)
            company.parse_finnhub_earnings(junk)
        self.assertEqual(company.parse_finnhub_earnings([{"actual": "1.2", "estimate": "1.1"}, {"actual": "N/A", "estimate": 1}, "junk"]), 1.0)
        self.assertEqual(company.parse_finnhub_profile({"marketCapitalization": "25000.5"})["market_cap"], 25000.5e6)

    def test_text_stored_in_a_numeric_column_by_an_older_build_is_neutralised_on_read(self):
        c = H.fresh_db()
        c.execute("INSERT INTO companies(ticker,name,sector,market_cap,adv_usd,beta,vol_daily,financial_strength) VALUES('ZZZ','Z','Technology','N/A','n/a','abc','',?)", ("oops",))
        d = company.get(c, "ZZZ")
        for k in ("market_cap", "adv_usd", "beta", "vol_daily", "financial_strength"):
            self.assertIsNone(d[k], k)
        self.assertEqual(d["name"], "Z")


class TestWholeSystemAgainstTheSimulatedInternet(Env):
    def test_a_clean_morning_runs_without_a_single_stage_error_and_trades(self):
        c, errors, net = self.morning(seed=1, cycles=12)
        self.assertEqual(errors, [])
        self.assertGreater(c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 5)
        self.assertGreater(c.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0], 0)       # BUYs executed through the real net / parsers / consensus
        self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE gate LIKE 'error%'").fetchone()[0], 0)
        self.assertTrue([u for u in net.requests if "finnhub.io/api/v1/stock/metric" in u], "the company refresh (Finnhub) must actually have been exercised")
        self.assertTrue([u for u in net.requests if "companyfacts" in u], "...and SEC company facts")
        books_balance(self, c)

    def test_with_no_per_cycle_cap_the_money_limits_are_what_stop_it(self):
        """The user's rule: open as many as we want as long as the funds are there. Cash reserve, max invested, per-company and per-theme limits must still hold."""
        from paper_agent import portfolio as pf
        c, errors, net = self.morning(seed=1, cycles=12)
        self.assertEqual(errors, [])
        cfg = H.cfg()
        per_cycle = [r[0] for r in c.execute("SELECT COUNT(*) FROM positions GROUP BY opened")]
        self.assertGreater(max(per_cycle), 3, "with no cap a single cycle should be able to open more than 3 positions: %s" % per_cycle)
        now = T0 + dt.timedelta(minutes=5 * 11)
        eq = pf.equity_of(c, pf.holdings(c, now), now)
        self.assertLessEqual(eq["gross"], cfg["risk"]["max_gross_pct"] + 0.02)                        # never more than 80% invested (small drift allowed)
        self.assertGreaterEqual(eq["cash"] / eq["equity"], cfg["risk"]["min_cash_pct"] - 0.02)        # the 20% cash reserve holds
        for r in c.execute("SELECT ticker,SUM(remaining_cost) v FROM position_lifecycle l JOIN positions p ON p.id=l.position_id WHERE p.status='OPEN' GROUP BY ticker"):
            self.assertLessEqual(r["v"] / eq["equity"], cfg["risk"]["max_issuer_pct"] + 0.03, r["ticker"])    # per-company limit
        books_balance(self, c)

    def test_and_the_positions_it_opened_are_sold_when_prices_fall(self):
        c, errors, net = self.morning(seed=1, cycles=12)
        original = [r[0] for r in c.execute("SELECT id FROM positions WHERE status='OPEN'")]
        held = [r[0] for r in c.execute("SELECT DISTINCT ticker FROM positions WHERE status='OPEN'")]
        self.assertTrue(original)
        for t in held:
            self.co[t]["price"] *= 0.6                                                       # a crash through every stop
        cfg = H.cfg()
        cfg["runtime"]["min_available_memory_mb"] = 0
        with net:
            for k in range(12, 16):
                now = T0 + dt.timedelta(minutes=5 * k)
                net.now = now
                r = pipeline.run_cycle(c, cfg, self.uni, SRC, pipeline.Deps(sleep=lambda s: None), now)
                errors += r["errors"]
        self.assertEqual(errors, [])
        still = [r[0] for r in c.execute("SELECT id FROM positions WHERE status='OPEN' AND id IN (%s)" % ",".join("?" * len(original)), original)]
        self.assertEqual(still, [])                                                          # every ORIGINAL position was sold (the bot may legitimately re-buy later at the new low)
        codes = [r[0] for r in c.execute("SELECT review_code FROM positions WHERE id IN (%s)" % ",".join("?" * len(original)), original)]
        self.assertTrue(set(codes) <= {"STOP_REACHED", "SUPERIOR_OPPORTUNITY", "RISK_ESCALATION", "THESIS_INVALIDATED"}, codes)   # every exit has a known, recorded reason
        self.assertGreaterEqual(codes.count("STOP_REACHED"), 0.8 * len(codes))                                                    # (the portfolio may also rotate its weakest holding out)
        books_balance(self, c)

    def test_broken_provider_responses_never_cause_a_stage_error_or_an_error_decision(self):
        """Empty bodies, nulls, numbers-as-strings, HTML error pages, truncated JSON, HTTP 403/429/500, malformed news items - across many random seeds."""
        for seed, deg in ((2, 0.3), (3, 0.6), (4, 0.8), (5, 0.5), (6, 0.7), (7, 0.4)):
            c, errors, net = self.morning(seed=seed, degrade=deg, degrade_quotes=(seed % 2 == 0), weird=(seed % 3 == 0), cycles=8)
            self.assertEqual(errors, [], "seed %d: %s" % (seed, errors))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM decisions WHERE status='ERROR'").fetchone()[0], 0, "seed %d" % seed)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE gate LIKE 'error%'").fetchone()[0], 0, "seed %d" % seed)
            books_balance(self, c)

    def test_one_failing_company_refresh_does_not_abort_the_others(self):
        """The company stage refreshes up to 6 companies; before, an exception for one aborted the stage and left the rest unenriched."""
        import json
        from unittest import mock
        real, calls = company.refresh_company, []

        def flaky(conn, ticker, *a, **k):
            calls.append(ticker)
            if len(calls) % 2:                                                               # every other refresh blows up
                raise RuntimeError("boom for " + ticker)
            return real(conn, ticker, *a, **k)
        with mock.patch.object(company, "refresh_company", flaky):
            c, errors, net = self.morning(seed=1, cycles=8)
        self.assertGreater(len(calls), 6)
        self.assertEqual([e for e in errors if e == "companies"], [])                        # the STAGE never fails...
        rep = [json.loads(r[0]) for r in c.execute("SELECT report_json FROM cycle_runs")]
        recorded = [e for r in rep for e in r.get("company_errors", [])]
        self.assertTrue(recorded and all("RuntimeError" in e for e in recorded))              # ...each failure is recorded, not hidden...
        self.assertGreater(c.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 2)      # ...and the healthy companies were still enriched

    def test_malformed_news_items_are_handled(self):
        c, errors, net = self.morning(seed=9, weird=True, cycles=6)
        self.assertEqual(errors, [])
        self.assertGreater(c.execute("SELECT COUNT(*) FROM events").fetchone()[0], 20)


if __name__ == "__main__":
    unittest.main()
