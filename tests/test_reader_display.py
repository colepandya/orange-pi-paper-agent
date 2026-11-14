"""What you can SEE of the article reader: the Why page, the Funnel line, the clean-up of its log, and the plain-English fact descriptions."""
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_reader as TR  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import dashboard, explain, funnel, lens, maintain, net  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

UTC = dt.timezone.utc


def decided(mode="guard", pages=None, head=TR.HEAD_BULL, body=TR.BULL_BODY):
    w = TR.World()
    w.setUp()
    w.cfg["reader"]["mode"] = mode
    w.pages = {"http://x/0": TR.page(body)} if pages is None else pages
    w.cyc(TL.T_PRE, [head])
    did = w.c.execute("SELECT id FROM decisions WHERE action='BUY' ORDER BY id LIMIT 1").fetchone()
    return w, (did[0] if did else None)


class TestTheWhyPageShowsWhatTheArticleSaid(unittest.TestCase):
    def test_the_numbers_and_the_source_sentence_are_on_the_page(self):
        w, did = decided()
        try:
            ex = explain.explain(w.c, w.cfg, did, TL.T_PRE)
            a = ex["article"]
            self.assertEqual((a["via"], a["type"]), ("page", "report"))
            self.assertGreater(a["direction"], 0.5)
            lines = [t for _, t, _ in a["facts"]]
            self.assertIn("Earnings per share $2.31 versus $1.95 expected (+18%)", lines)
            self.assertIn("Raised its outlook", lines)
            html = explain.render_html(ex)
            self.assertIn("What the full article says", html)
            self.assertIn("Read the full article page", html)
            self.assertIn("Earnings per share $2.31 versus $1.95 expected (+18%)", html)
            self.assertIn("above the $1.95 per share analysts had expected", html)                       # the sentence it came from, so a person can check it
            self.assertIn("good news", html)
        finally:
            w.tearDown()

    def test_when_only_the_snippet_was_available_it_says_so(self):
        w, did = decided(pages={})                                                                         # every page is a 404
        try:
            html = explain.render_html(explain.explain(w.c, w.cfg, did, TL.T_PRE))
            self.assertIn("Only the feed&#x27;s short snippet was available", html.replace("&#39;", "&#x27;"))
            self.assertIn("fetch failed", html)
        finally:
            w.tearDown()

    def test_what_the_article_changed_is_stated(self):
        w, did = decided(mode="full", head=TR.HEAD_VAGUE)
        try:
            ex = explain.explain(w.c, w.cfg, did, TL.T_PRE)
            self.assertIn("the headline was vague; the article's numbers read good", ex["article"]["changed"])
            self.assertIn("What this changed:", explain.render_html(ex))
        finally:
            w.tearDown()

    def test_no_section_when_the_reader_was_off_or_the_decision_has_no_story(self):
        w, did = decided(mode="off")
        try:
            self.assertIsNone(explain.explain(w.c, w.cfg, did, TL.T_PRE)["article"])
            self.assertNotIn("What the full article says", explain.render_html(explain.explain(w.c, w.cfg, did, TL.T_PRE)))
        finally:
            w.tearDown()
        self.assertIsNone(explain.article_section(H.fresh_db(), None))

    def test_every_kind_of_fact_has_a_plain_english_line(self):
        kinds = ["eps_surprise", "revenue_surprise", "growth", "contract", "buyback", "deal", "guidance_raise", "guidance_cut", "guidance_keep", "beat_generic", "miss_generic", "dividend_raise", "dividend_cut", "upgrade", "downgrade",
                 "target_raise", "target_cut", "bankruptcy", "delisting", "restatement", "investigation", "lawsuit", "recall", "fda_reject", "fda_approve", "short_report", "cyber", "exec_exit", "layoffs", "strike_outage", "record",
                 "partnership", "insider_buy", "insider_sell", "product_launch"]
        base = {"pol": 0.5, "actual": 2.31, "estimate": 1.95, "surprise": 0.18, "metric": "revenue", "pct": 12.0, "amount": 2e9, "pct_of_cap": 6.7, "definitive": True, "premium": 28.0}
        for k in kinds:
            line = explain.describe_fact(dict(base, kind=k))
            self.assertTrue(line and "_" not in line, (k, line))                                           # no raw internal names leak into the page
        self.assertEqual(explain.describe_fact(dict(base, kind="contract", pol=-0.4)), "A contract was lost or cancelled")
        self.assertIn("Rumoured / in talks", explain.describe_fact(dict(base, kind="deal", definitive=False)))


class TestTheFunnelSaysWhatWasRead(unittest.TestCase):
    def test_counts_text_and_dashboard(self):
        w, did = decided()
        try:
            w.c.execute("INSERT INTO article_domains(domain,ok,fails,blocked_until,last_status) VALUES('wall.example.com',0,3,?,'HTTP 403')", (iso(TL.T_PRE + dt.timedelta(hours=20)),))
            rep = funnel.report(w.c, 24, TL.T_PRE + dt.timedelta(hours=1))
            self.assertEqual({k: rep["articles"][k] for k in ("read", "page", "snippet", "changed")}, {"read": 1, "page": 1, "snippet": 0, "changed": 0})
            self.assertEqual(rep["articles"]["left_alone"], ["wall.example.com"])
            txt = funnel.text(rep)
            self.assertIn("articles read: 1 (full page 1, feed snippet only 0)", txt)
            self.assertIn("sites being left alone after refusing us: wall.example.com", txt)
            html = dashboard.render(w.c, w.cfg, "funnel", {"hours": ["20000"]})                              # the page uses the real clock: look far enough back to include the fixture
            self.assertIn("Articles read:", html)
        finally:
            w.tearDown()

    def test_the_new_reasons_are_explained_in_the_funnel(self):
        for g in ("article_disagrees", "stale_in_article"):
            self.assertIn(g, funnel.GATE_LABELS)
        self.assertIn("full article says something different from the headline", funnel.GATE_LABELS["article_disagrees"])


class TestTheLogDoesNotGrowForever(unittest.TestCase):
    def test_old_reads_are_expired_with_the_news(self):
        c = H.fresh_db()
        now = dt.datetime(2026, 10, 29, 9, 30, tzinfo=UTC)
        for uid, ago in (("old", 60), ("new", 2)):
            c.execute("INSERT INTO article_reads(uid,url,fetched_at,status,via,chars,domain,analysis_json) VALUES(?,?,?,?,?,?,?,?)", (uid, "u", iso(now - dt.timedelta(days=ago)), "OK", "page", 1000, "d", "{}"))
        cfg = H.cfg()
        self.assertEqual(maintain.plan(c, cfg, now)["retention"]["article_reads"], 1)
        _count, delete, params = maintain._retention(c, 30, 30, now)["article_reads"]
        c.execute(delete, params)
        self.assertEqual([r[0] for r in c.execute("SELECT uid FROM article_reads")], ["new"])


if __name__ == "__main__":
    unittest.main()


class TestPreflightWatchesTheReader(unittest.TestCase):
    def check(self, mode, rows):
        from paper_agent import preflight
        c, cfg = H.fresh_db(), H.cfg()
        cfg["reader"]["mode"] = mode
        now = dt.datetime(2026, 10, 29, 9, 30, tzinfo=UTC)
        for i, via in enumerate(rows):
            c.execute("INSERT INTO article_reads(uid,url,fetched_at,status,via,chars,domain,analysis_json) VALUES(?,?,?,?,?,?,?,?)", ("u%d" % i, "u", iso(now - dt.timedelta(hours=1)), "OK", via, 1000, "d", "{}"))
        res = preflight.run(c, cfg, H.universe(), now=now, live=False, out=lambda *a, **k: None)
        return [x for x in res["checks"] if x["name"].startswith("Article reader")]

    def test_it_reports_when_healthy_and_warns_when_no_page_ever_loads(self):
        ok = self.check("guard", ["page"] * 5 + ["snippet"] * 10)
        self.assertEqual((ok[0]["status"], "5 as full pages" in ok[0]["detail"]), ("PASS", True))
        bad = self.check("guard", ["snippet"] * 15)
        self.assertEqual(bad[0]["status"], "WARN")
        self.assertIn("no page ever loaded", bad[0]["detail"])
        self.assertEqual(self.check("guard", ["snippet"] * 3)[0]["status"], "PASS")                       # too few reads to judge
        self.assertEqual(self.check("off", ["snippet"] * 15), [])                                          # off: nothing to report
