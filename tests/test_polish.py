"""Fixes found from the first real run on the Pi: GDELT rate limits, misleading feed diagnostics, dead feeds, provider hints."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import dashboard, db, doctor, net, quotes, settings_ui, sources  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

NOW = H.NOW


def gdelt(i):
    return {"id": "gdelt_%d" % i, "name": "G%d" % i, "type": "gdelt", "query": "q%d" % i, "trust": 0.55, "interval": 0}


class TestGdeltPoliteness(unittest.TestCase):
    def test_at_most_three_per_cycle_spaced_and_rest_wait_for_next_cycle(self):
        c = H.fresh_db()
        calls, sleeps = [], []

        def fetch(url, **k):
            calls.append(url)
            return json.dumps({"articles": []}).encode(), 200, {}
        srcs = [gdelt(i) for i in range(7)]
        r1 = sources.collect(c, srcs, fetch, sleep=sleeps.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [5.5, 5.5])                  # 1 request per 5.5 s
        self.assertEqual(r1["sources_ok"], 3)
        r2 = sources.collect(c, srcs, fetch, sleep=sleeps.append)       # the ones not yet polled are still due
        self.assertEqual(len(calls), 6)
        self.assertEqual(c.execute("SELECT COUNT(DISTINCT source_id) FROM source_health").fetchone()[0], 6)
        sources.collect(c, srcs, fetch, sleep=sleeps.append)
        self.assertEqual(c.execute("SELECT COUNT(DISTINCT source_id) FROM source_health").fetchone()[0], 7)

    def test_rotation_is_fair_so_no_feed_is_starved_even_when_all_are_always_due(self):
        c = H.fresh_db()
        order = []

        def fetch(url, **k):
            order.append(url)
            return json.dumps({"articles": []}).encode(), 200, {}
        srcs = [gdelt(i) for i in range(7)]
        for _ in range(3):
            sources.collect(c, srcs, fetch, sleep=lambda s: None)       # interval 0: everything is due every time
        polled = [q.split("query=")[1].split("&")[0] for q in order]
        self.assertEqual(len(set(polled)), 7)                           # every feed got a turn within three cycles
        self.assertLessEqual(max(polled.count(p) for p in set(polled)), 2)

    def test_non_gdelt_feeds_are_not_throttled(self):
        c = H.fresh_db()
        sleeps = []
        srcs = [{"id": "r%d" % i, "name": "R%d" % i, "type": "rss", "url": "http://x/%d" % i, "trust": .5, "interval": 0} for i in range(8)]
        sources.collect(c, srcs, lambda url, **k: (H.rss([("Headline number %s here" % url, "", "Tue, 27 Oct 2026 14:00:00 +0000")]), 200, {}), sleep=sleeps.append)
        self.assertEqual((sleeps, c.execute("SELECT COUNT(*) FROM source_health").fetchone()[0]), ([], 8))


class TestDoctorDiagnostics(unittest.TestCase):
    def run_doctor(self, srcs, fetch, fetch_json=H.series_fetch):
        c, cfg, lines, sleeps = H.fresh_db(), H.cfg(), [], []
        cfg["runtime"]["bind_port"] = 1
        r = doctor.run(c, cfg, "/nonexistent", fetch=fetch, fetch_json=fetch_json, srcs=srcs, check_units=False, out=lines.append, sleep=sleeps.append)
        return r, "\n".join(lines), sleeps

    def test_failures_are_grouped_by_real_cause_with_every_feed_named(self):
        srcs = [{"id": i, "name": i, "type": "rss", "url": "http://x/" + i, "trust": .5, "interval": 0} for i in ("ok1", "ok2", "ok3", "ok4", "ok5", "ok6", "imf", "nrc", "treasury", "state", "glob")]

        def fetch(url, **k):
            code = {"imf": 404, "nrc": 404, "state": 403, "glob": 403}.get(url.rsplit("/", 1)[1])
            if url.endswith("treasury"):
                raise net.FetchError("timeout: The read operation timed out")
            if code:
                raise net.FetchError("HTTP %d" % code, code)
            return H.rss([("Hello world headline here", "", "Tue, 27 Oct 2026 14:00:00 +0000")]), 200, {}
        r, text, _ = self.run_doctor(srcs, fetch)
        self.assertRegex(text, r"HTTP 404\s+x2\s+imf, nrc")
        self.assertRegex(text, r"HTTP 403\s+x2\s+state, glob")
        self.assertRegex(text, r"timeout\s+x1\s+treasury")
        low = next(i["message"] for i in r["issues"] if "unreachable" in i["message"])
        self.assertIn("imf, nrc", low)                       # permanently-dead list is explicit
        self.assertNotIn("state", low.split("(404/410:")[1])  # 403s are not 'dead'

    def test_gdelt_is_sampled_two_at_a_time_not_hammered(self):
        srcs = [gdelt(i) for i in range(7)]
        seen = []

        def fetch(url, **k):
            seen.append(url)
            return json.dumps({"articles": []}).encode(), 200, {}
        _, text, sleeps = self.run_doctor(srcs, fetch)
        self.assertEqual(len(seen), 2)
        self.assertEqual(sleeps, [5.5])
        self.assertIn("GDELT: 2 of 7 sampled", text)

    def test_provider_hints_explain_401_and_403(self):
        os.environ["FINNHUB_API_KEY"], os.environ["MASSIVE_API_KEY"] = "bad", "free"
        try:
            def fj(url, headers=None, **k):
                if "finnhub" in url:
                    raise net.FetchError("HTTP 401", 401)
                if "massive" in url:
                    raise net.FetchError("HTTP 403", 403)
                return H.series_fetch(url, headers)
            r, text, _ = self.run_doctor([], lambda *a, **k: (b"", 200, {}), fj)
        finally:
            os.environ.pop("FINNHUB_API_KEY"), os.environ.pop("MASSIVE_API_KEY")
        msgs = " | ".join(i["message"] for i in r["issues"])
        self.assertIn("REJECTED the API key", msgs)
        self.assertIn("webhook secret", msgs)
        self.assertIn("free plan has no real-time", msgs)


class TestDeadFeedCleanup(unittest.TestCase):
    SRCS = [{"id": "imf", "name": "IMF", "type": "rss", "url": "u", "interval": 600}, {"id": "state", "name": "State", "type": "rss", "url": "u", "interval": 600},
            {"id": "new404", "name": "New", "type": "rss", "url": "u", "interval": 600}, {"id": "good", "name": "Good", "type": "rss", "url": "u", "interval": 600}]
    HEALTH = {"imf": {"last_status": "404", "failures": 5}, "state": {"last_status": "403", "failures": 9}, "new404": {"last_status": "404", "failures": 1}, "good": {"last_status": "200", "failures": 0, "last_success": "x"}}

    def test_only_permanently_dead_feeds_are_offered(self):
        cfg = H.cfg()
        self.assertEqual(settings_ui.dead_feed_ids(self.SRCS, cfg, self.HEALTH), ["imf"])      # 403 may be a UA block; 1 failure may be transient
        cfg["source_overrides"] = {"imf": {"enabled": False}}
        self.assertEqual(settings_ui.dead_feed_ids(self.SRCS, cfg, self.HEALTH), [])           # already disabled

    def test_settings_page_shows_feed_status_and_the_button(self):
        cfg = H.cfg()
        html = settings_ui.page_settings(cfg, cfg, {"csrf": "tok"}, self.SRCS, {}, {}, self.HEALTH)
        self.assertIn("failing x5 (404)", html)
        self.assertIn(">OK<", html)
        self.assertIn("not polled yet", settings_ui.page_settings(cfg, cfg, {"csrf": "tok"}, self.SRCS, {}, {}, {}))
        self.assertIn("Disable these 1 feeds", html)
        self.assertNotIn("Disable these", settings_ui.page_settings(cfg, cfg, {"csrf": "tok"}, self.SRCS, {}, {}, {}))


if __name__ == "__main__":
    unittest.main()
