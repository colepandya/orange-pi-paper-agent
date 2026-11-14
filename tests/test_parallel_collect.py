"""Collecting the feeds side by side. The promise: the DATABASE ends up exactly as if the feeds had been fetched one after another (same rows, same order, same health records), only sooner;
one slow or failing feed delays nothing but itself; nothing is hammered; the ETag handling and the GDELT limits are untouched."""
import datetime as dt
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import net, sources  # noqa: E402

NOW = dt.datetime(2026, 10, 27, 15, 0, tzinfo=dt.timezone.utc)


def rss(i, n=3):
    return H.rss([("Story %d-%d about Acme Corp earnings" % (i, k), "text %d" % k, "Tue, 27 Oct 2026 14:%02d:00 GMT" % (10 + k)) for k in range(n)])


def feeds(n=12):
    return [{"id": "f%02d" % i, "name": "Feed %02d" % i, "type": "rss", "url": "http://host%d.example.org/feed" % (i % 6), "interval": 300, "trust": 0.8} for i in range(n)]


def make_fetch(delay=0.0, fail=(), log=None):
    live, peak, lock = [0], [0], threading.Lock()

    def fetch(url, headers=None, timeout=15, max_bytes=0, data=None, method=None):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        try:
            if log is not None:
                log.append(url)
            time.sleep(delay)
            i = int(url.split("host")[1].split(".")[0])
            if any(("%02d" % f) in url or url.endswith(f) for f in ()):
                pass
            if url in fail:
                raise net.FetchError("HTTP 503", 503)
            return rss(int(hash(url) % 997)), 200, {"etag": '"v1-%s"' % url[-12:]}
        finally:
            with lock:
                live[0] -= 1
    fetch.peak = peak
    return fetch


def state(c):
    news = [tuple(r) for r in c.execute("SELECT source,title,summary FROM news ORDER BY id")]
    health = [tuple(r) for r in c.execute("SELECT source_id,last_status,failures,items_total FROM source_health ORDER BY source_id")]
    etags = [tuple(r) for r in c.execute("SELECT source_id,etag FROM source_state ORDER BY source_id")]
    return news, health, etags


class TestSameDatabaseEitherWay(unittest.TestCase):
    def run_with(self, workers, fail=()):
        c = H.fresh_db()
        out = sources.collect(c, feeds(), make_fetch(0.0, fail), clock=lambda: NOW.timestamp(), workers=workers)
        return c, out

    def test_the_database_is_identical_with_one_worker_or_six(self):
        a, oa = self.run_with(1)
        b, ob = self.run_with(6)
        self.assertEqual(state(a), state(b))
        for k in ("sources_ok", "fetch_failures", "skipped", "unchanged", "new", "errors"):
            self.assertEqual(oa[k], ob[k], k)
        self.assertGreater(len(state(a)[0]), 20)

    def test_identical_even_when_some_feeds_fail(self):
        bad = ("http://host2.example.org/feed", "http://host4.example.org/feed")
        a, oa = self.run_with(1, bad)
        b, ob = self.run_with(6, bad)
        self.assertEqual(state(a), state(b))
        self.assertEqual(sorted(oa["errors"]), sorted(ob["errors"]))
        self.assertEqual(ob["fetch_failures"], len([f for f in feeds() if f["url"] in bad]))

    def test_the_stored_order_follows_the_source_list_not_the_finishing_order(self):
        c = H.fresh_db()
        delays = {"http://host%d.example.org/feed" % i: 0.3 - 0.05 * i for i in range(6)}          # the first feed is the SLOWEST

        def fetch(url, headers=None, timeout=15, max_bytes=0, data=None, method=None):
            time.sleep(delays[url])
            return rss(1), 200, {}
        sources.collect(c, feeds(6), fetch, clock=lambda: NOW.timestamp(), workers=6)
        order = [r[0] for r in c.execute("SELECT DISTINCT source FROM news ORDER BY id")]
        self.assertEqual(order, ["Feed %02d" % i for i in range(6)])


class TestSpeedAndPoliteness(unittest.TestCase):
    def test_twelve_feeds_take_a_fraction_of_the_time(self):
        delay = 0.2
        t = time.monotonic()
        sources.collect(H.fresh_db(), feeds(), make_fetch(delay), clock=lambda: NOW.timestamp(), workers=1)
        seq = time.monotonic() - t
        t = time.monotonic()
        out = sources.collect(H.fresh_db(), feeds(), make_fetch(delay), clock=lambda: NOW.timestamp(), workers=6)
        par = time.monotonic() - t
        self.assertGreater(seq, 12 * delay * 0.9)
        self.assertLess(par, seq / 3)
        self.assertLess(out["fetch_seconds"], seq / 3)

    def test_never_more_than_two_requests_to_one_server_at_once(self):
        f = make_fetch(0.05)
        same = [{"id": "s%d" % i, "name": "S%d" % i, "type": "rss", "url": "http://one.example.org/feed%d" % i, "interval": 300, "trust": 0.8} for i in range(8)]

        def fetch(url, **k):
            return f("http://host0.example.org/feed", **k)
        sources.collect(H.fresh_db(), same, fetch, clock=lambda: NOW.timestamp(), workers=8)
        self.assertLessEqual(f.peak[0], 2)

    def test_gdelt_keeps_its_gap_and_its_three_per_cycle_cap_with_many_workers(self):
        starts = []
        slept = []
        g = [{"id": "g%d" % i, "name": "G%d" % i, "type": "gdelt", "query": "q%d" % i, "interval": 300, "trust": 0.6} for i in range(5)]

        def fetch(url, **k):
            starts.append(time.monotonic())
            return json.dumps({"articles": []}).encode(), 200, {}
        out = sources.collect(H.fresh_db(), g, fetch, clock=lambda: NOW.timestamp(), workers=6, sleep=lambda s: (slept.append(s), time.sleep(0.12))[1])
        self.assertEqual(len(starts), sources.GDELT_MAX_PER_CYCLE)                                  # the cap of three is unchanged
        self.assertEqual(out["skipped"], 2)
        self.assertGreaterEqual(len(slept), 2)                                                      # the gap was honoured (the injected sleep stands in for 5.5 s)


class TestFailuresStayLocal(unittest.TestCase):
    def test_a_hanging_feed_is_cut_off_and_the_others_are_stored(self):
        c = H.fresh_db()

        def fetch(url, **k):
            if "host3" in url:
                time.sleep(3.0)
            return rss(7), 200, {}
        t = time.monotonic()
        out = sources.collect(c, feeds(6), fetch, clock=lambda: NOW.timestamp(), workers=6, deadline_s=0.5)
        self.assertLess(time.monotonic() - t, 1.5)
        self.assertEqual(out["sources_ok"], 5)
        self.assertEqual(out["fetch_failures"], 1)
        self.assertTrue(any(e.startswith("f03:") and "Timeout" in e for e in out["errors"]), out["errors"])
        h = c.execute("SELECT failures,last_status FROM source_health WHERE source_id='f03'").fetchone()
        self.assertEqual(h["failures"], 1)

    def test_a_bad_payload_in_one_feed_does_not_stop_the_rest(self):
        c = H.fresh_db()
        fs = feeds(4) + [{"id": "fr", "name": "FR", "type": "federal_register", "url": "http://host1.example.org/fr", "interval": 300, "trust": 0.9}]

        def fetch(url, **k):
            if url.endswith("/fr"):
                return b"<<<not json>>>", 200, {}
            return rss(3), 200, {}
        out = sources.collect(c, fs, fetch, clock=lambda: NOW.timestamp(), workers=4)
        self.assertEqual((out["sources_ok"], out["fetch_failures"]), (4, 1))
        self.assertEqual(c.execute("SELECT failures FROM source_health WHERE source_id='fr'").fetchone()[0], 1)

    def test_a_missing_api_key_is_recorded_without_any_request(self):
        c = H.fresh_db()
        log = []
        os.environ.pop("FINNHUB_API_KEY", None)
        out = sources.collect(c, [{"id": "fg", "name": "Finnhub", "type": "finnhub_general", "url": "x", "interval": 300, "trust": 0.8}] + feeds(2), make_fetch(0.0, log=log), clock=lambda: NOW.timestamp(), workers=4)
        self.assertIn("fg:FetchError", out["errors"])                                               # the recorded code has always been the exception type; the reason is in the error text
        self.assertIn("DISABLED_NO_KEY", c.execute("SELECT last_error FROM source_health WHERE source_id='fg'").fetchone()[0])
        self.assertEqual(len(log), 2)                                                               # and no request was made for it


class TestEtagAndTiming(unittest.TestCase):
    def test_the_saved_etag_is_sent_back_and_a_304_is_healthy(self):
        c = H.fresh_db()
        seen = {}

        def fetch(url, headers=None, **k):
            seen[url] = dict(headers or {})
            if (headers or {}).get("If-None-Match"):
                return b"", 304, {}
            return rss(1), 200, {"etag": '"abc"'}
        fs = feeds(3)
        t0 = NOW.timestamp()
        sources.collect(c, fs, fetch, clock=lambda: t0, workers=3)
        out = sources.collect(c, fs, fetch, clock=lambda: t0 + 600, workers=3)
        self.assertEqual((out["unchanged"], out["fetch_failures"]), (3, 0))
        self.assertTrue(all(h.get("If-None-Match") == '"abc"' for h in seen.values()))

    def test_every_feed_records_how_long_it_took_and_the_report_names_the_slowest(self):
        c = H.fresh_db()

        def fetch(url, **k):
            time.sleep(0.25 if "host2" in url else 0.0)
            return rss(1), 200, {}
        out = sources.collect(c, feeds(6), fetch, clock=lambda: NOW.timestamp(), workers=6)
        ms = {r["source_id"]: r["last_ms"] for r in c.execute("SELECT source_id,last_ms FROM source_health")}
        self.assertTrue(all(v is not None for v in ms.values()))
        self.assertGreaterEqual(ms["f02"], 240)
        self.assertLess(ms["f00"], 200)
        self.assertTrue(out["slowest"][0].startswith("f02 "))

    def test_the_setting_exists_and_is_validated(self):
        from paper_agent import config, settings as S
        self.assertEqual(config.load_config("/nonexistent", with_overrides=False)["runtime"]["fetch_workers"], 6)
        f = next(x for x in S.FIELDS if x.path == "runtime.fetch_workers")
        self.assertEqual((S._parse(f, "1"), S._parse(f, "12")), (1, 12))
        for bad in ("0", "13", "x"):
            with self.assertRaises(ValueError):
                S._parse(f, bad)


if __name__ == "__main__":
    unittest.main()


class TestArticleDownloadsSideBySide(unittest.TestCase):
    """The reader downloads its pages together, then analyses and stores them one at a time in rank order: the same rows, sooner."""

    def setUp(self):
        import test_reader as TR
        self.TR = TR
        self.titles = ["NVIDIA quarterly earnings results beat estimates", "NVIDIA raises outlook after earnings", "NVIDIA acquisition of a chip startup agreed", "NVIDIA wins a large contract",
                       "NVIDIA announces a share buyback", "NVIDIA lifts its dividend"]

    def run_enrich(self, workers, delay=0.0, fail=()):
        TR = self.TR
        c, cfg, u = H.fresh_db(), H.cfg(), H.universe()
        cfg["reader"].update(mode="guard", max_per_cycle=6, time_budget_seconds=25)
        cfg["runtime"]["fetch_workers"] = workers
        live, peak, lock, order = [0], [0], threading.Lock(), []

        def fetcher(url, timeout=8):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
                order.append(url)
            try:
                time.sleep(delay)
                if url in fail:
                    raise net.FetchError("HTTP 403", 403)
                return TR.page(TR.LONG), url
            finally:
                with lock:
                    live[0] -= 1
        pre = [TR.pre_item(i, t, url="https://site%d.example.com/a" % i) for i, t in enumerate(self.titles)]
        st = TR.reader.enrich(c, u, cfg, pre, TR.NOW, fetcher)
        rows = [tuple(r) for r in c.execute("SELECT uid,status,via,domain,chars FROM article_reads ORDER BY uid")]
        dom = [tuple(r) for r in c.execute("SELECT domain,ok,fails,last_status FROM article_domains ORDER BY domain")]
        return st, rows, dom, peak[0]

    def test_same_stored_results_with_one_worker_or_six(self):
        a = self.run_enrich(1)
        b = self.run_enrich(6)
        self.assertEqual(a[1], b[1])
        self.assertEqual(a[2], b[2])
        self.assertEqual({k: a[0][k] for k in ("considered", "read", "page", "snippet")}, {k: b[0][k] for k in ("considered", "read", "page", "snippet")})
        self.assertEqual(a[0]["page"], 6)

    def test_six_pages_in_the_time_of_one(self):
        t = time.monotonic()
        self.run_enrich(1, delay=0.25)
        seq = time.monotonic() - t
        t = time.monotonic()
        st, rows, dom, peak = self.run_enrich(6, delay=0.25)
        par = time.monotonic() - t
        self.assertGreater(seq, 1.4)
        self.assertLess(par, seq / 2.5)
        self.assertGreaterEqual(peak, 3)                                                            # really side by side

    def test_a_refused_page_is_isolated_and_counted_against_its_own_site_only(self):
        st, rows, dom, peak = self.run_enrich(6, fail=("https://site2.example.com/a",))
        self.assertEqual(st["page"], 5)
        self.assertEqual(st["snippet"], 1)
        self.assertEqual([d for d in dom if d[2] > 0], [("site2.example.com", 0, 1, "FETCH_FAILED: HTTP 403")])   # the failure was recorded where it belongs, once


if __name__ == "__main__":
    unittest.main()
