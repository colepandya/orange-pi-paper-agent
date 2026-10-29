"""The 'Quote providers' table was a frozen leftover: it only changed when a trade needed a price, counted slow answers as failures, stored errors under the wrong name (so a rate-limited
Twelve Data was never paused), and showed old-app rows as alarming failures. Now: honest states, a scheduled probe, canonical names, and a probe that can never block a provider."""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import fake_internet as FI  # noqa: E402
import test_fake_internet as TF  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import dashboard, net, pipeline, quotes, settings as S  # noqa: E402
from paper_agent.timeutil import iso, utcnow  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 27, 21, 0, tzinfo=UTC)               # 17:00 New York: the market is closed


def provider(name_fn, canonical, age=5.0, price=100.0, error=None, http=None, returned=None, at=NOW):
    def f(t, fetch):
        if error:
            raise net.FetchError(error, http)
        return quotes.obs(canonical, t, price, stamp=at.timestamp() - age, returned=returned or t)
    f.__name__ = name_fn
    return f


def row(c, name):
    r = c.execute("SELECT * FROM provider_health WHERE provider=?", (name,)).fetchone()
    return dict(r) if r else None


class TestHonestStates(unittest.TestCase):
    def check(self, **kw):
        x = quotes.validate(quotes.obs("P", "SPY", kw.pop("price", 100.0), stamp=NOW.timestamp() - kw.pop("age", 5.0), returned=kw.pop("returned", "SPY"), http=kw.pop("http", None), error=kw.pop("error", None)), NOW)
        return quotes.health_of(x)

    def test_each_kind_of_answer_is_classified_correctly(self):
        self.assertEqual(self.check(), ("HEALTHY", True, True))
        self.assertEqual(self.check(age=5000), ("STALE", True, False))                      # answered with an old price: normal when the market is closed, NOT a failure
        self.assertEqual(self.check(price=0), ("DEGRADED", False, False))
        self.assertEqual(self.check(returned="AAPL"), ("DEGRADED", False, False))           # wrong company back
        self.assertEqual(self.check(error="HTTP 429", http=429), ("RATE_LIMITED", False, False))
        self.assertEqual(self.check(error="HTTP 403", http=403), ("AUTH_FAILED", False, False))
        self.assertEqual(self.check(error="HTTP 401", http=401), ("AUTH_FAILED", False, False))
        self.assertEqual(self.check(error="timeout"), ("DOWN", False, False))

    def test_a_working_provider_after_hours_is_stale_not_failed(self):
        c = H.fresh_db()
        q = quotes.consensus(c, "SPY", H.cfg(), NOW, [provider("twelve", "TWELVE_DATA", age=40000)], None)
        quotes.store(c, q, NOW)
        r = row(c, "TWELVE_DATA")
        self.assertEqual((r["state"], r["successes"], r["failures"]), ("STALE", 1, 0))
        self.assertEqual((r["last_checked"], r["last_success"], r["last_fresh"]), (iso(NOW), iso(NOW), None))     # reachable, but never "fresh"
        later = NOW + dt.timedelta(hours=16)
        q = quotes.consensus(c, "SPY", H.cfg(), later, [provider("twelve", "TWELVE_DATA", age=3, at=later)], None)
        quotes.store(c, q, later)
        r = row(c, "TWELVE_DATA")
        self.assertEqual((r["state"], r["successes"], r["last_fresh"]), ("HEALTHY", 2, iso(later)))

    def test_real_failures_are_counted_as_failures(self):
        c = H.fresh_db()
        for fn, canon, err, http in (("finnhub", "FINNHUB", "HTTP 403", 403), ("twelve", "TWELVE_DATA", "HTTP 429", 429), ("yahoo", "YAHOO", "boom", None)):
            quotes.store(c, quotes.consensus(c, "SPY", H.cfg(), NOW, [provider(fn, canon, error=err, http=http)], None), NOW)
        self.assertEqual([(row(c, n)["state"], row(c, n)["failures"], row(c, n)["successes"]) for n in ("FINNHUB", "TWELVE_DATA", "YAHOO")], [("AUTH_FAILED", 1, 0), ("RATE_LIMITED", 1, 0), ("DOWN", 1, 0)])

    def test_errors_are_recorded_under_the_canonical_name_not_the_function_name(self):
        """REGRESSION: 'TWELVE' / 'ALPHA' rows were created, so the pause for a rate-limited Twelve Data was never found and it was never paused."""
        c = H.fresh_db()
        for fn in ("twelve", "alpha"):
            quotes.store(c, quotes.consensus(c, "SPY", H.cfg(), NOW, [provider(fn, "X", error="HTTP 429", http=429)], None), NOW)
        names = {r[0] for r in c.execute("SELECT provider FROM provider_health")}
        self.assertEqual(names, {"TWELVE_DATA", "ALPHA_VANTAGE"})
        self.assertGreater(row(c, "TWELVE_DATA")["backoff_until"], NOW.timestamp())              # ...and the pause is really found by the budget check
        self.assertFalse(quotes._budget_ok(c, "TWELVE_DATA", H.cfg(), NOW.timestamp() + 60))
        self.assertTrue(quotes._budget_ok(c, "TWELVE_DATA", H.cfg(), NOW.timestamp() + 901))

    def test_numbers_left_by_the_old_app_are_discarded_once_and_only_once(self):
        c = H.fresh_db()
        c.execute("INSERT INTO provider_health(provider,requests,successes,failures,state) VALUES('FINNHUB',6,0,6,'DEGRADED')")
        c.execute("INSERT INTO provider_health(provider,requests,successes,failures,state) VALUES('TIINGO',6,0,6,'DEGRADED')")
        quotes.store(c, quotes.consensus(c, "SPY", H.cfg(), NOW, [provider("finnhub", "FINNHUB")], None), NOW)
        quotes.store(c, quotes.consensus(c, "SPY", H.cfg(), NOW + dt.timedelta(minutes=10), [provider("finnhub", "FINNHUB")], None), NOW + dt.timedelta(minutes=10))
        r = row(c, "FINNHUB")
        self.assertEqual((r["requests"], r["successes"], r["failures"]), (2, 2, 0))          # 6 old failures gone; the 2 new checks kept
        self.assertEqual((row(c, "TIINGO")["failures"], row(c, "TIINGO")["last_checked"]), (6, None))     # an untouched legacy row is left alone (and hidden by the dashboard)


class TestProbeNeverHarmsTrading(unittest.TestCase):
    def test_a_rate_limited_answer_to_the_probe_does_not_pause_the_provider(self):
        c = H.fresh_db()
        cfg = H.cfg()
        with mock.patch.object(quotes, "PROVIDERS", [provider("twelve", "TWELVE_DATA", error="HTTP 429", http=429)]):
            self.assertIn("checked", quotes.probe(c, cfg, NOW))
        r = row(c, "TWELVE_DATA")
        self.assertEqual(r["state"], "RATE_LIMITED")                                         # the table tells the truth...
        self.assertFalse(r["backoff_until"])                                                 # ...but a health check must never block a provider the trade path may need
        self.assertTrue(quotes._budget_ok(c, "TWELVE_DATA", cfg, NOW.timestamp()))

    def test_an_existing_pause_from_a_real_trade_lookup_is_left_alone(self):
        c = H.fresh_db()
        quotes.store(c, quotes.consensus(c, "NVDA", H.cfg(), NOW, [provider("twelve", "TWELVE_DATA", error="HTTP 429", http=429)], None), NOW)
        paused = row(c, "TWELVE_DATA")["backoff_until"]
        with mock.patch.object(quotes, "PROVIDERS", [provider("twelve", "TWELVE_DATA")]):
            quotes.probe(c, H.cfg(), NOW + dt.timedelta(minutes=11))
        self.assertEqual(row(c, "TWELVE_DATA")["backoff_until"], paused)

    def test_the_probe_never_overwrites_a_newer_after_hours_price(self):
        c = H.fresh_db()
        c.execute("INSERT INTO quote_cache(ticker,price,source,quoted_at,updated_epoch) VALUES('SPY',777.0,'YAHOO_EXT:AFTER_HOURS',?,0)", (iso(NOW - dt.timedelta(seconds=10)),))
        with mock.patch.object(quotes, "PROVIDERS", [provider("twelve", "TWELVE_DATA", age=120, price=700.0)]):          # FRESH enough to pass, but 110 s OLDER than the cached after-hours price
            self.assertEqual(quotes.probe(c, H.cfg(), NOW)["status"], "PASS")
        self.assertEqual(c.execute("SELECT price,source FROM quote_cache WHERE ticker='SPY'").fetchone()[:], (777.0, "YAHOO_EXT:AFTER_HOURS"))

    def test_scarce_daily_quotas_are_protected(self):
        """Alpha Vantage allows ~20 calls a DAY: a 10-minute probe would burn them all in 3 hours and starve the trade path."""
        c = H.fresh_db()
        cfg = H.cfg()
        calls = []

        def alpha(t, fetch):
            calls.append("alpha")
            return quotes.obs("ALPHA_VANTAGE", t, 100.0, stamp=NOW.timestamp() - 5)

        def twelve(t, fetch):
            calls.append("twelve")
            return quotes.obs("TWELVE_DATA", t, 100.0, stamp=NOW.timestamp() - 5)
        with mock.patch.object(quotes, "PROVIDERS", [twelve, alpha]):
            for i in range(0, 60, 10):                                                     # six probes in the first hour
                quotes.probe(c, cfg, NOW + dt.timedelta(minutes=i))
            self.assertEqual((calls.count("twelve"), calls.count("alpha")), (6, 1))
            quotes.probe(c, cfg, NOW + dt.timedelta(hours=3, minutes=5))
            self.assertEqual(calls.count("alpha"), 2)                                       # checked again once 3 hours have passed

    def test_a_probe_that_explodes_never_raises(self):
        c = H.fresh_db()
        with mock.patch.object(quotes, "consensus", side_effect=RuntimeError("boom")):
            self.assertIn("probe failed", quotes.probe(c, H.cfg(), NOW)["skipped"])
        self.assertEqual(quotes.probe(c, H.cfg(), NOW + dt.timedelta(minutes=1)), {"skipped": "not due"})          # and it does not hammer: the run is recorded

    def test_the_schedule_is_editable_and_zero_turns_it_off(self):
        c = H.fresh_db()
        cfg = H.cfg()
        cfg["quotes"]["probe_minutes"] = 0
        self.assertEqual(quotes.probe(c, cfg, NOW), {"skipped": "off"})
        cfg["quotes"]["probe_minutes"] = 30
        with mock.patch.object(quotes, "PROVIDERS", [provider("twelve", "TWELVE_DATA")]):
            self.assertIn("checked", quotes.probe(c, cfg, NOW))
            self.assertEqual(quotes.probe(c, cfg, NOW + dt.timedelta(minutes=20)), {"skipped": "not due"})
            self.assertIn("checked", quotes.probe(c, cfg, NOW + dt.timedelta(minutes=28)))             # 30-minute setting, a cycle 2 minutes early still runs
        base = __import__("paper_agent.config", fromlist=["x"]).load_config("/nonexistent", with_overrides=False)
        self.assertEqual(base["quotes"]["probe_minutes"], 10)
        self.assertIn("quotes.probe_minutes", {f.path for f in S.FIELDS})
        for bad in ("-1", "999"):
            self.assertTrue(S.apply_form({"f.quotes.probe_minutes": bad}, base, base, {})[1])


class TestWholeSystem(unittest.TestCase):
    def test_a_closed_market_cycle_refreshes_every_configured_provider_without_a_single_failure(self):
        tf = TF.TestWholeSystemAgainstTheSimulatedInternet("test_a_clean_morning_runs_without_a_single_stage_error_and_trades")
        tf.setUp()
        try:
            c, errors, net_ = tf.morning(seed=1, cycles=3)
            c.execute("DELETE FROM provider_health")
            c.execute("DELETE FROM task_schedule WHERE task='quote_probe'")
            cfg = H.cfg()
            cfg["runtime"]["min_available_memory_mb"] = 0
            closed = dt.datetime(2026, 10, 27, 22, 0, tzinfo=UTC)
            with net_:
                net_.now = closed
                r = pipeline.run_cycle(c, cfg, tf.uni, TF.SRC, pipeline.Deps(sleep=lambda s: None), closed)
            self.assertEqual(r["errors"], [])
            self.assertTrue(r["quote_health"]["checked"])
            rows = {x["provider"]: dict(x) for x in c.execute("SELECT * FROM provider_health")}
            for name in ("TWELVE_DATA", "FINNHUB", "ALPACA", "ALPHA_VANTAGE", "YAHOO"):
                self.assertEqual(rows[name]["last_checked"], iso(closed), name)
                self.assertEqual(rows[name]["failures"], 0, name)
            self.assertNotIn("UNKNOWN", rows)
        finally:
            tf.tearDown()


class TestDashboard(unittest.TestCase):
    def test_the_table_speaks_plainly_and_hides_the_old_apps_leftovers(self):
        c = H.fresh_db()
        c.execute("INSERT INTO provider_health(provider,requests,successes,failures,state) VALUES('UNKNOWN',6,0,6,'DEGRADED')")
        c.execute("INSERT INTO provider_health(provider,requests,successes,failures,state) VALUES('TIINGO',6,0,6,'DEGRADED')")
        real = utcnow()                                                                  # the page measures 'minutes ago' on the live clock
        with mock.patch.object(quotes, "PROVIDERS", [provider("twelve", "TWELVE_DATA", age=40000, at=real), provider("alpaca", "ALPACA", at=real), provider("finnhub", "FINNHUB", error="HTTP 403", http=403)]):
            quotes.probe(c, H.cfg(), real)
        html = dashboard.render(c, H.cfg(), "system", {})
        for needle in ("Last checked", "Last fresh price", "answering; prices are old (normal when the market is closed)", "working: fresh prices", "key rejected (paused 1 hour)", "just now"):
            self.assertIn(needle, html, needle)
        self.assertIn("Not in use (no key, or only seen by the old app): TIINGO, UNKNOWN.", html)
        self.assertNotIn(">UNKNOWN<", html)                                               # the old rows are not shown as failures
        self.assertIn("every 10 minutes, market open or closed", html)
        cfg = H.cfg()
        cfg["quotes"]["probe_minutes"] = 0
        self.assertIn("the scheduled check is switched off", dashboard.render(c, cfg, "system", {}))


if __name__ == "__main__":
    unittest.main()
