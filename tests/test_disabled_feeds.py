"""A feed the owner switches off in Settings must never be contacted by ANYTHING (cycle, open run, doctor, preflight, report, dashboard health check), and must not be shown or researched as a live problem.
Reported by the owner: disabled feeds 'still get refreshed'. The collector already skipped them; the Sources table and the research-gap scan did not know, so they kept showing 'failing x12 HTTP 429'."""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import config, dashboard, db, doctor, gaps, pipeline, preflight, report, settings as S, sources  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 27, 15, 0, tzinfo=UTC)          # regular session
FEEDS = [{"id": "keep_news", "name": "Keep News", "type": "rss", "url": "http://keep.example.org/feed", "interval": 300},
         {"id": "off_news", "name": "Off News", "type": "rss", "url": "http://off.example.org/feed", "interval": 300},
         {"id": "gdelt_off", "name": "GDELT: Off", "type": "gdelt", "query": "off", "interval": 300},
         {"id": "gdelt_on", "name": "GDELT: On", "type": "gdelt", "query": "on", "interval": 300}]
OFF = ("off.example.org", "query=off")


class Recorder:
    def __init__(self):
        self.urls = []

    def __call__(self, url, headers=None, timeout=15, max_bytes=0, data=None, method=None):
        self.urls.append(url)
        if "gdeltproject" in url:
            return json.dumps({"articles": []}).encode(), 200, {}
        return H.rss([("Acme Corp reports results", "text", "Tue, 27 Oct 2026 14:50:00 GMT")]), 200, {}

    def fetch_json(self, url, *a, **k):
        self.urls.append(url)
        return {}

    def touched_off_feeds(self):
        return [u for u in self.urls if any(o in u for o in OFF)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, "sources.json"), "w") as f:
            json.dump({"sources": FEEDS}, f)
        self.patch = mock.patch.object(config, "CONFIG_DIR", self.tmp)
        self.patch2 = mock.patch.object(pipeline, "CONFIG_DIR", self.tmp)
        self.patch.start()
        self.patch2.start()
        self.c = H.fresh_db()
        self.cfg = H.cfg()
        self.cfg["source_overrides"] = {"off_news": {"enabled": False}, "gdelt_off": {"enabled": False}}      # exactly what the Settings page stores when you untick them
        self.rec = Recorder()

    def tearDown(self):
        self.patch.stop()
        self.patch2.stop()


class TestNothingContactsASwitchedOffFeed(Base):
    def test_the_cycle_never_fetches_it_over_many_cycles_and_long_gaps(self):
        for i in range(6):
            now = NOW + dt.timedelta(hours=7 * i)                                                          # far beyond every poll interval and back-off
            pipeline.run_cycle(self.c, self.cfg, H.universe(), None, pipeline.Deps(fetch=self.rec, fetch_json=self.rec.fetch_json, quote_fn=H.Quotes({}), sleep=lambda s: None), now)
        self.assertTrue(any("keep.example.org" in u for u in self.rec.urls), "sanity: the feed that is ON is polled")
        self.assertTrue(any("query=on" in u for u in self.rec.urls), "sanity: the GDELT query that is ON is polled")
        self.assertEqual(self.rec.touched_off_feeds(), [])

    def test_the_doctor_preflight_and_report_health_checks_never_contact_it(self):
        srcs = pipeline.load_sources(self.cfg)
        doctor.run(self.c, self.cfg, self.tmp, fetch=self.rec, fetch_json=self.rec.fetch_json, live=True, check_units=False, fix=False, srcs=srcs, out=lambda *a, **k: None)
        preflight.run(self.c, self.cfg, H.universe(), self.tmp, fetch=self.rec, fetch_json=self.rec.fetch_json, srcs=srcs, live=True, check_units=False, now=NOW, out=lambda *a, **k: None)
        report.build(self.c, self.cfg, H.universe(), srcs, NOW, live=True, fetch=self.rec, fetch_json=self.rec.fetch_json, check_units=False)
        self.assertTrue(any("keep.example.org" in u for u in self.rec.urls), "sanity: the enabled feed IS checked")
        self.assertEqual(self.rec.touched_off_feeds(), [])

    def test_the_settings_form_really_stores_what_the_collector_obeys(self):
        import copy
        base = config.load_config("/nonexistent", with_overrides=False)
        current = copy.deepcopy(base)
        current["source_overrides"] = {"off_news": {"enabled": False}}
        new, errors, _ = S.apply_form({"src.keep_news.enabled": "0", "src.off_news.enabled": "1"}, current, base, {"keep_news": 300, "off_news": 300})
        self.assertEqual(errors, [])
        self.assertEqual(new["source_overrides"], {"keep_news": {"enabled": False}})                       # unticking stores the exception; ticking removes it
        self.assertEqual(sources.disabled_ids(new), {"keep_news"})
        loaded = {x["id"]: x for x in pipeline.load_sources(new)}
        self.assertIs(loaded["keep_news"]["enabled"], False)                                                # what the collector obeys
        self.assertTrue(loaded["off_news"].get("enabled", True))

    def test_a_feed_shipped_switched_off_stays_off_unless_the_owner_turns_it_on(self):
        feeds = [dict(FEEDS[0], enabled=False), dict(FEEDS[1])]
        self.assertEqual(sources.disabled_ids({}, feeds), {"keep_news"})
        self.assertEqual(sources.disabled_ids({"source_overrides": {"keep_news": {"enabled": True}}}, feeds), set())
        self.assertEqual(sources.disabled_ids({"source_overrides": {"off_news": {"enabled": False}}}, feeds), {"keep_news", "off_news"})


class TestItIsNotShownOrResearchedAsALiveProblem(Base):
    def seed_health(self):
        for sid, name, fails, status in (("off_news", "Off News", 12, "429"), ("gdelt_off", "GDELT: Off", 9, "FetchError"), ("keep_news", "Keep News", 4, "503")):
            self.c.execute("INSERT INTO source_health(source_id,name,last_status,failures,items_total,last_error,last_attempt) VALUES(?,?,?,?,?,?,?)", (sid, name, status, fails, 0, "HTTP " + status, iso(NOW)))

    def test_the_sources_table_marks_them_off_and_lists_them_last(self):
        self.seed_health()
        html = dashboard._sources_table(self.c, self.cfg)
        self.assertIn("2 feed(s) are switched off in Settings and are <b>not being fetched</b>", html)
        self.assertEqual(html.count("OFF (Settings)"), 2)
        self.assertLess(html.index("Keep News"), html.index("Off News"))                                    # the live (and failing) feed comes first
        self.assertIn("503", html)                                                                           # a genuinely failing live feed is still shown as failing
        off_row = html[html.index("Off News"):html.index("Off News") + 400]
        self.assertNotIn("429", off_row)                                                                     # no stale failure for a feed that is off

    def test_with_nothing_switched_off_the_table_is_as_before(self):
        self.seed_health()
        self.cfg["source_overrides"] = {}
        html = dashboard._sources_table(self.c, self.cfg)
        self.assertNotIn("switched off", html)
        self.assertNotIn("OFF (Settings)", html)

    def test_the_research_gap_scan_ignores_them_and_closes_tasks_raised_earlier(self):
        self.seed_health()
        self.c.execute("INSERT INTO research_tasks(created,kind,subject,description,priority) VALUES(?,?,?,?,0.4)", (iso(NOW), "source_down", "off_news", "Source off_news failing (12 consecutive)"))
        gaps.scan(self.c, self.cfg, NOW)
        tasks = {r["subject"]: r["status"] for r in self.c.execute("SELECT subject,status FROM research_tasks WHERE kind='source_down'")}
        self.assertEqual(tasks["off_news"], "RESOLVED")                                                       # the old task is closed, with the reason
        self.assertNotIn("gdelt_off", tasks)                                                                   # and never raised for the other one
        self.assertEqual(tasks["keep_news"], "OPEN")                                                          # a live failing feed is still researched


if __name__ == "__main__":
    unittest.main()
