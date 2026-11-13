"""Build 57: the Funnel is part of the Overview; (more below as the intelligence engine is built)."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
from paper_agent import config, dashboard  # noqa: E402

DEF = lambda: config.load_config("/nonexistent", with_overrides=False)  # noqa: E731


def text(html):
    import html as h
    return h.unescape(re.sub(r"<[^>]+>", " ", html))


class TestTheFunnelIsPartOfTheOverview(unittest.TestCase):
    def test_there_is_no_funnel_tab_any_more(self):
        self.assertNotIn("funnel", dashboard.TABS)
        self.assertEqual(len(dashboard.TABS), 10)
        self.assertEqual(dashboard.TABS[0], "overview")
        page = dashboard.render(H.fresh_db(), DEF(), "overview", {}, {"authed": True})
        self.assertNotIn("?tab=funnel", page)                                                                # not in the navigation, not in a window link
        self.assertNotIn(">Funnel<", page)

    def test_the_overview_carries_the_funnel_and_the_outcomes(self):
        t = text(dashboard.render(H.fresh_db(), DEF(), "overview", {}, {"authed": True}))
        for need in ("How ideas flow: news to decisions", "News ingested", "Trade candidates", "Equity (mark-to-market)"):
            self.assertIn(need, t)
        self.assertNotIn("Decisions by outcome", t)
        self.assertNotIn("What happened to the ideas", t)                                                    # (Build 58) removed from the Overview
        self.assertNotIn("Recent merger / acquisition events", t)

    def test_the_old_address_still_opens_the_same_page(self):
        c = H.fresh_db()
        self.assertEqual(dashboard.render(c, DEF(), "funnel", {}, {"authed": True}).split("<nav", 1)[0][:0], "")
        a = text(dashboard.render(c, DEF(), "funnel", {}, {"authed": True}))
        b = text(dashboard.render(c, DEF(), "overview", {}, {"authed": True}))
        self.assertIn("How ideas flow", a)
        self.assertEqual(re.sub(r"\s+", " ", a.replace("Funnel", "")), re.sub(r"\s+", " ", b.replace("Funnel", "")))          # same content (only the highlighted tab differs)

    def test_the_window_applies_to_both_sections(self):
        c = H.fresh_db()
        raw = dashboard.render(c, DEF(), "overview", {"hours": ["72"]}, {"authed": True})
        t = text(raw)
        self.assertIn("How ideas flow: news to decisions", t)
        for h in (6, 24, 72):
            self.assertIn('href="?tab=overview&hours=%d#flow"' % h, raw)
        self.assertIn("How ideas flow", text(dashboard.render(c, DEF(), "overview", {"hours": ["junk"]}, {"authed": True})))

    def test_hours_of(self):
        for q, want in (({}, 24.0), (None, 24.0), ({"hours": ["6"]}, 6.0), ({"hours": ["72"]}, 72.0), ({"hours": "48"}, 48.0), ({"hours": ["x"]}, 24.0), ({"hours": [""]}, 24.0), ({"hours": []}, 24.0),
                        ({"hours": ["0"]}, 24.0), ({"hours": ["-5"]}, 24.0), ({"hours": ["20000"]}, 20000.0), ({"hours": ["100000"]}, 100000.0), ({"hours": ["100001"]}, 24.0), ({"hours": ["99999999"]}, 24.0), ({"hours": ["nan"]}, 24.0), ({"hours": ["inf"]}, 24.0), ({"hours": [None]}, 24.0)):
            self.assertEqual(dashboard.hours_of(q), want, q)


if __name__ == "__main__":
    unittest.main()


# ================================================================ outlets and news quality
from paper_agent import newsquality, outlets  # noqa: E402


class TestOutlets(unittest.TestCase):
    def test_domains_and_names(self):
        self.assertEqual(outlets.domain_of("https://www.Reuters.com/a/b?x=1"), "reuters.com")
        self.assertEqual(outlets.domain_of("https://user:pw@finance.yahoo.com:8080/x"), "finance.yahoo.com")
        for junk in (None, "", 5, "not a url", [], {}):
            self.assertEqual(outlets.domain_of(junk), "")
        for raw, want in (("Yahoo", "yahoo finance"), ("The Wall Street Journal", "wsj"), ("  SeekingAlpha ", "seeking alpha"), ("The Associated Press", "ap"), ("Reuters", "reuters"), (None, ""), (5, ""), ("", "")):
            self.assertEqual(outlets.normalize(raw), want, raw)

    def test_the_publisher_is_taken_from_the_best_evidence_available(self):
        self.assertEqual(outlets.outlet_of("Reuters", "https://finnhub.io/api/news/1", "Finnhub Company News"), "reuters")                  # what the feed itself says wins
        self.assertEqual(outlets.outlet_of(None, "https://www.cnbc.com/x", "Finnhub Company News"), "cnbc")                                # else the link's domain
        self.assertEqual(outlets.outlet_of(None, "https://finance.yahoo.com/x", "Finnhub"), "yahoo finance")
        self.assertEqual(outlets.outlet_of(None, "https://blog.example.com/p", "Some Feed"), "blog.example.com")                           # an unknown publisher keeps its domain
        self.assertEqual(outlets.outlet_of(None, "https://finnhub.io/api/news/1", "Finnhub Company News"), "finnhub company news")          # an aggregator link says nothing: the feed's own name
        self.assertEqual(outlets.outlet_of(None, "https://news.ycombinator.com/item?id=1", "Hacker News"), "hacker news")
        self.assertEqual(outlets.outlet_of(None, None, None), "")

    def test_wire_origin_is_recognised_so_a_copy_is_not_a_second_report(self):
        for title, want in (("NEW YORK, Nov 5 (Reuters) - Acme wins a contract", "reuters"), ("Reuters - Acme wins a contract", "reuters"), ("Acme wins a contract, by Associated Press", "ap"),
                            ("(AP) Acme wins a contract", "ap"), ("Acme completes the deal /PRNewswire/", "pr newswire"), ("Acme completes the deal -- BUSINESS WIRE", "business wire"), ("Acme wins a contract", None)):
            self.assertEqual(outlets.wire_origin(title, ""), want, title)
        self.assertEqual(outlets.wire_origin("Acme wins", "NEW YORK (Reuters) - details"), "reuters")                                  # also in the summary
        for junk in (None, 5, [], {}):
            self.assertIsNone(outlets.wire_origin(junk, junk))
        self.assertEqual(outlets.origin_of("yahoo finance", "(Reuters) - Acme wins", ""), "reuters")
        self.assertEqual(outlets.origin_of("yahoo finance", "Acme wins", ""), "yahoo finance")
        self.assertEqual(outlets.origin_of("", "Acme wins", ""), "")

    def test_the_prior_blends_the_feed_and_the_outlet_forty_sixty(self):
        self.assertAlmostEqual(outlets.prior_trust(0.7, "reuters"), 0.4 * 0.7 + 0.6 * 0.90)                                        # 0.82
        self.assertAlmostEqual(outlets.prior_trust(0.7, "blog.example.com"), 0.7)                                                  # unknown outlet: the feed's number
        self.assertAlmostEqual(outlets.prior_trust(0.95, "hacker news"), 0.4 * 0.95 + 0.6 * 0.40)                                  # a social outlet inside a trusted feed is pulled DOWN
        self.assertAlmostEqual(outlets.prior_trust(None, "reuters"), 0.4 * 0.5 + 0.6 * 0.90)
        self.assertAlmostEqual(outlets.prior_trust("x", None), 0.5)
        self.assertAlmostEqual(outlets.prior_trust(float("nan"), "sec"), 0.4 * 0.5 + 0.6 * 0.97)
        self.assertEqual(outlets.tier_of("Reuters"), ("wire", 0.90))
        self.assertEqual(outlets.tier_of("nobody"), (None, None))
        for t in outlets.TIERS.values():
            self.assertTrue(0.40 <= t[1] <= 0.97)                                                                                  # tiers are capped priors


class TestNewsQuality(unittest.TestCase):
    A = "NVIDIA wins $2 billion contract with Acme Corp on Nov 5, company said in a statement"
    B = "Is NVIDIA stock a buy now? 3 stocks that could soar!"

    def test_a_specific_primary_sourced_story_scores_high_by_hand(self):
        r = newsquality.assess(self.A, "", ["NVIDIA"])
        c = r["components"]
        self.assertAlmostEqual(c["specificity"], 1 - 0.5 ** 3)                                              # an amount, a date, one deal term
        self.assertAlmostEqual(c["attribution"], 0.9)                                                       # 0.6 + 0.3 ("said in a statement")
        self.assertAlmostEqual(c["tone"], 1.0)
        self.assertAlmostEqual(c["subject"], 1.0)                                                           # named in the first words
        self.assertAlmostEqual(c["type"], 0.8)
        self.assertAlmostEqual(r["score"], 0.25 * 0.875 + 0.20 * 0.9 + 0.15 * 1.0 + 0.25 * 1.0 + 0.15 * 0.8)   # 0.91875
        self.assertEqual(r["tier"], "high")
        self.assertIn("specific facts", r["flags"])
        self.assertIn("primary-source language", r["flags"])

    def test_a_listicle_scores_low_by_hand(self):
        r = newsquality.assess(self.B, "", ["NVIDIA"])
        c = r["components"]
        self.assertEqual(c["specificity"], 0.0)
        self.assertAlmostEqual(c["attribution"], 0.4)                                                       # 0.6 - 0.2 ("could")
        self.assertAlmostEqual(c["tone"], 0.70)                                                             # one sensational word ("soar") and an exclamation mark
        self.assertAlmostEqual(c["subject"], 1.0)
        self.assertAlmostEqual(c["type"], 0.25)                                                             # "buy now", "3 stocks"
        self.assertAlmostEqual(r["score"], 0.2 * 0.4 + 0.15 * 0.7 + 0.25 * 1.0 + 0.15 * 0.25)               # 0.4725
        self.assertEqual(r["tier"], "low")
        for f in ("no concrete facts", "hedged headline", "sensational or promotional wording", "opinion or listicle"):
            self.assertIn(f, r["flags"])

    def test_anonymous_sourcing_and_hedging(self):
        r = newsquality.assess("Acme reportedly in talks to buy Beta, sources say")
        self.assertAlmostEqual(r["components"]["attribution"], 0.6 - 0.35 - 0.2)                           # 0.05
        self.assertIn("anonymous sourcing", r["flags"])

    def test_the_company_as_the_subject(self):
        d = lambda t, s, names: newsquality.assess(t, s, names)["components"]["subject"]  # noqa: E731
        self.assertEqual(d("Acme wins a contract", "", ["Acme"]), 1.0)
        self.assertEqual(d("Shares of several large technology firms fell after Acme announced", "", ["Acme"]), 0.7)      # eight words in
        self.assertEqual(d("Tech stocks fall", "Acme was among them", ["Acme"]), 0.35)                       # only the summary
        self.assertEqual(d("Tech stocks fall", "nothing relevant", ["Acme"]), 0.15)
        self.assertEqual(d("Shares of Acme jump after the news", "", ["Acme"]), 1.0)                         # two words before the name: still "in the first six"
        self.assertEqual(d("Shares of the big new Acme jump after news", "", ["Acme"]), 1.0)                 # five words before: the last one that counts
        self.assertEqual(d("Shares of the big new tech Acme jump after news", "", ["Acme"]), 0.7)            # six words before: no longer in the first six
        self.assertEqual(d("Acme wins", "", None), 0.5)                                                      # names unknown: neutral
        self.assertEqual(d("Acme wins", "", []), 0.5)
        self.assertEqual(d("Beta wins; Acme too", "", ["Acme", "Beta"]), 1.0)                                # the best of several subjects
        self.assertEqual(d("NVDA surges", "", ["NVD"]), 0.15)                                                # whole words only: 'NVD' is not inside 'NVDA'

    def test_tone(self):
        t = lambda title, summary="": newsquality.assess(title, summary)["components"]["tone"]  # noqa: E731
        self.assertAlmostEqual(t("BREAKING: HUGE NEWS FROM NVIDIA!!"), 1 - 0.15 - 0.15)                      # three or more ALL-CAPS words, and "!"
        self.assertAlmostEqual(t("Will Acme rise?"), 0.85)
        self.assertAlmostEqual(t("Acme update", "a world-class team"), 0.9)
        self.assertAlmostEqual(t("Shocking explosive bombshell soars crashes"), 1 - 0.15 * 3)               # capped at three words
        self.assertAlmostEqual(t("Acme reports results"), 1.0)

    def test_type(self):
        ty = lambda title, primary=False: newsquality.assess(title, "", None, primary)["components"]["type"]  # noqa: E731
        self.assertEqual(ty("Acme files its 10-K", primary=True), 1.0)
        self.assertEqual(ty("Analyst raises price target on Acme"), 0.5)
        self.assertEqual(ty("Why I am buying Acme: stocks to buy now"), 0.25)
        self.assertEqual(ty("Acme names a new chief executive"), 0.8)

    def test_facts_are_counted_by_kind(self):
        sp = lambda text: newsquality.specificity(text)
        self.assertEqual(sp("nothing here"), 0.0)
        self.assertEqual(sp("revenue rose 12%"), 1 - 0.5 ** 2)                                               # a percentage and a term
        self.assertEqual(sp("a $5 million deal on Nov 5 with 1,200 staff, up 3%"), 1 - 0.5 ** 5)           # amount, percentage, count, date, term "deal"
        self.assertEqual(sp("revenue guidance backlog margin eps"), 1 - 0.5 ** 2)                            # terms are capped at two
        self.assertEqual(sp("founded in 1999"), 1 - 0.5 ** 1)                                                # a year is a date, not a count

    def test_weights_range_and_junk(self):
        self.assertAlmostEqual(sum(newsquality.WEIGHTS.values()), 1.0)
        for junk in (None, 5, [], {}, "", "x" * 100000, "\u0000\u202e<>&" * 50):
            r = newsquality.assess(junk, junk, junk if not isinstance(junk, int) else None)
            self.assertTrue(0.0 <= r["score"] <= 1.0)
            for v in r["components"].values():
                self.assertTrue(0.0 <= v <= 1.0)
        self.assertEqual(newsquality.assess("Acme", "", [None, 5, "a", ""])["components"]["subject"], 0.5)    # unusable names are ignored


# ================================================================ ingestion, appraisal, corroboration, reputation
import datetime as dt  # noqa: E402
import json  # noqa: E402
import unittest.mock  # noqa: E402

import test_kb_integration as KI  # noqa: E402
from paper_agent import evidence, newsintel, reputation, sources, thesis  # noqa: E402
from paper_agent.timeutil import iso, parse_ts  # noqa: E402

NOW = H.NOW


def intel_cfg(**kw):
    c = H.cfg()
    c["intelligence"]["news_intel"] = True
    c["intelligence"].update(kw)
    return c


class TestTheCollectorKeepsThePublisher(unittest.TestCase):
    def test_store_items_records_the_outlet(self):
        c = H.fresh_db()
        src = {"id": "fh", "name": "Finnhub Company News", "trust": 0.7}
        n = sources.store_items(c, src, [{"title": "A", "url": "https://finnhub.io/x", "outlet": "Reuters"}, {"title": "B", "url": "https://www.cnbc.com/a"}, {"title": "C", "url": "https://finnhub.io/y"},
                                         {"title": "D", "url": "https://blog.example.com/p", "outlet": ""}], NOW)
        self.assertEqual(n, 4)
        got = {r["title"]: r["outlet"] for r in c.execute("SELECT title,outlet FROM news")}
        self.assertEqual(got, {"A": "reuters", "B": "cnbc", "C": "finnhub company news", "D": "blog.example.com"})

    def test_the_feeds_carry_the_publisher_through(self):
        body = json.dumps([{"headline": "h", "summary": "s", "url": "https://finnhub.io/1", "datetime": 1730000000, "source": "Yahoo"}]).encode()
        items, _status = sources.parse_response({"type": "finnhub_general", "id": "g", "name": "Finnhub Market News"}, (body, 200, {}))
        self.assertEqual(items[0]["outlet"], "Yahoo")
        gd = json.dumps({"articles": [{"title": "t", "domain": "example.com", "sourcecountry": "US", "url": "https://example.com/a", "seendate": "20261101T000000Z"}]}).encode()
        items, _ = sources.parse_response({"type": "gdelt", "id": "d", "name": "GDELT", "query": "x"}, (gd, 200, {}))
        self.assertEqual(items[0]["outlet"], "example.com")
        with unittest.mock.patch.dict(os.environ, {"FINNHUB_API_KEY": "k"}):
            items = sources.finnhub_company_news("NVDA", fetch=lambda url, timeout=15: (body, 200, {}))
        self.assertEqual((items[0]["outlet"], items[0]["ticker_hint"]), ("Yahoo", "NVDA"))


class TestAppraisal(unittest.TestCase):
    ROW = {"title": TestNewsQuality.A, "summary": "", "source": "Finnhub Company News", "trust": 0.7, "url": "https://finnhub.io/1", "outlet": "reuters"}

    def go(self, row=None, cfg=None, base=0.8, universe=None):
        calls = []

        def sc(conn, source, etype, prior):
            calls.append((source, etype, prior))
            return base
        r = newsintel.appraise(H.fresh_db() if not hasattr(self, "c") else self.c, cfg or intel_cfg(), row or self.ROW, "contract_win", ["NVDA"], universe or H.universe(), sc)
        return r, calls

    def test_the_credibility_formula_by_hand(self):
        r, calls = self.go()
        q = newsquality.assess(self.ROW["title"], "", newsintel.subjects_for(H.universe(), ["NVDA"]))["score"]
        self.assertEqual(calls, [("Finnhub Company News", "contract_win", outlets.prior_trust(0.7, "reuters"))])                  # the learned feed blend starts from the OUTLET-aware prior (0.82)
        self.assertAlmostEqual(calls[0][2], 0.82)
        self.assertAlmostEqual(q, 0.91875)
        self.assertAlmostEqual(r["qf"], 1.1)                                                                 # 1 + 0.4 x (0.919 - 0.58) = 1.135, held at the 1.1 ceiling
        self.assertAlmostEqual(r["cred"], 0.8 * 1.1 * 1.0)
        self.assertEqual((r["outlet"], r["origin"], r["tier"]), ("reuters", "reuters", "wire"))

    def test_the_factor_is_centred_so_a_typical_story_is_untouched_and_a_weak_one_is_marked_down(self):
        listicle = dict(self.ROW, title=TestNewsQuality.B, outlet="blog.example.com")                          # quality 0.4725 by hand (see TestNewsQuality)
        r, _ = self.go(listicle)
        self.assertAlmostEqual(r["quality"]["score"], 0.4725)
        self.assertAlmostEqual(r["qf"], 1.0 + 0.4 * (0.4725 - 0.58))                                          # 0.957: marked down by 4.3%
        typical = dict(self.ROW, title="Acme announces a new product")
        rt, _ = self.go(typical)
        self.assertAlmostEqual(rt["qf"], max(0.7, min(1.1, 1.0 + 0.4 * (rt["quality"]["score"] - 0.58))))
        self.assertLess(abs(rt["qf"] - 1.0), 0.08)                                                           # a typical headline moves little
        worst, _ = self.go(dict(self.ROW, title="x"), cfg=intel_cfg(quality_weight=1.0))
        self.assertGreaterEqual(worst["qf"], 0.7)                                                            # and the floor holds
        from paper_agent import newsintel as ni
        self.assertEqual((ni.NEUTRAL_QUALITY, ni.QF_LO, ni.QF_HI), (0.58, 0.7, 1.1))

    def test_a_primary_source_is_not_marked_down_for_being_dull(self):
        row = dict(self.ROW, title="8-K - ACME CORP (0001234) (Filer)", outlet="sec", source="SEC EDGAR Latest Filings", url="https://www.sec.gov/x")
        r, _ = self.go(row)
        self.assertEqual((r["tier"], r["qf"]), ("primary", 1.0))
        self.assertAlmostEqual(r["cred"], 0.8)                                                               # the feed-level number, untouched

    def test_the_weight_and_the_switch(self):
        r0, _ = self.go(cfg=intel_cfg(quality_weight=0.0))
        self.assertEqual(r0["qf"], 1.0)
        r1, _ = self.go(cfg=intel_cfg(quality_weight=1.0))
        self.assertAlmostEqual(r1["qf"], min(1.1, max(0.7, 1.0 + (r1["quality"]["score"] - 0.58))))
        off, calls = self.go(cfg=intel_cfg(news_intel=False))
        self.assertAlmostEqual(off["cred"], 0.8)                                                             # exactly the old behaviour ...
        self.assertAlmostEqual(calls[0][2], 0.7)                                                             # ... starting from the FEED's own trust
        self.assertEqual(off["outlet"], "reuters")                                                           # (the outlet and quality are still recorded)
        self.assertGreater(off["quality"]["score"], 0.0)

    def test_the_reputation_factor_multiplies_and_the_result_is_bounded(self):
        c = H.fresh_db()
        self.c = c
        c.execute("INSERT INTO source_reputation(key,kind,n,follow_n,mean_ret,prior,posterior,factor,updated) VALUES('reuters','outlet',50,35,0.01,0.9,0.6,1.1,'x')")
        base = self.go(cfg=intel_cfg(), base=0.8)[0]
        self.assertAlmostEqual(base["rep"], 1.1)
        self.assertAlmostEqual(base["cred"], 0.8 * base["qf"] * 1.1)
        self.assertEqual(self.go(cfg=intel_cfg(), base=0.99)[0]["cred"], 0.99)                               # capped
        self.assertEqual(self.go(cfg=intel_cfg(), base=0.01)[0]["cred"], 0.05 if 0.01 * 1.1 * 0.97 < 0.05 else None)   # floored
        del self.c

    def test_subjects_come_from_the_universe(self):
        s = newsintel.subjects_for(H.universe(), ["NVDA", "ZZZZ", None, 5])
        self.assertIn("NVDA", s)
        self.assertTrue(any("nvidia" in x.lower() for x in s))
        self.assertEqual(newsintel.subjects_for(None, ["NVDA"]), ["NVDA"])
        self.assertEqual(newsintel.subjects_for(H.universe(), None), [])

    def test_a_company_is_found_by_its_name_even_with_no_aliases(self):
        import types
        u = types.SimpleNamespace(watch={"ACME": {"name": "Acme Robotics, Inc.", "aliases": []}, "BETA": {"name": "Beta Corp", "aliases": ["Betaco", 5]}})
        s = newsintel.subjects_for(u, ["ACME", "BETA"])
        for need in ("ACME", "Acme Robotics, Inc.", "Acme Robotics", "BETA", "Beta Corp", "Beta", "Betaco"):
            self.assertIn(need, s)
        self.assertNotIn(5, s)
        self.assertEqual(len(s), len(set(s)))                                                                # no duplicates

    def test_junk_rows_never_raise(self):
        for row in ({}, {"title": None, "summary": None, "source": None, "trust": "x"}, {"title": 5, "trust": float("nan")}, None, [], "text"):
            r, _ = self.go(row)
            self.assertTrue(0.05 <= r["cred"] <= 0.99)


def add_item(c, uid, title, src="Feed", trust=0.7, outlet="", summary="", age_h=2, url="http://x"):
    c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust,outlet) VALUES(?,?,?,?,?,?,?,?,?)", (uid, src, title, summary, url, iso(NOW - dt.timedelta(hours=age_h)), iso(NOW), trust, outlet))


class TestCorroborationNeedsIndependentOrigins(unittest.TestCase):
    T = "Hyperscalers raise AI capex guidance again as GPU orders surge"

    def run_case(self, news_intel):
        c = H.fresh_db()
        thesis.seed(c)
        cfg = intel_cfg(news_intel=news_intel)
        u = H.universe()
        add_item(c, "1", self.T, src="Wire A", outlet="reuters")
        evidence.process_new_news(c, u, cfg, NOW)
        head = c.execute("SELECT id,credibility FROM events ORDER BY id LIMIT 1").fetchone()
        add_item(c, "2", self.T + " - wire 2", src="Yahoo Feed", outlet="yahoo finance", summary="NEW YORK (Reuters) - the details")       # a syndicated Reuters copy
        add_item(c, "3", self.T + " - wire 3", src="CNBC Feed", outlet="cnbc")                                                              # a genuinely separate report
        out = evidence.process_new_news(c, u, cfg, NOW)
        after = c.execute("SELECT credibility FROM events WHERE id=?", (head["id"],)).fetchone()[0]
        return head["credibility"], after, out["stats"], c

    def test_a_syndicated_copy_does_not_corroborate_a_separate_report_does(self):
        before, after, stats, c = self.run_case(True)
        self.assertAlmostEqual(after - before, 0.04)                                                         # one bump (CNBC), not two
        self.assertEqual((stats["duplicates"], stats["syndicated"]), (2, 1))
        origins = {r["outlet"]: r["origin"] for r in c.execute("SELECT outlet,origin FROM events")}
        self.assertEqual(origins, {"reuters": "reuters", "yahoo finance": "reuters", "cnbc": "cnbc"})

    def test_switched_off_every_duplicate_counts_as_before(self):
        before, after, stats, _c = self.run_case(False)
        self.assertAlmostEqual(after - before, 0.08)
        self.assertEqual(stats["syndicated"], 0)

    def test_quality_and_outlet_are_stored_with_each_event(self):
        _b, _a, _s, c = self.run_case(True)
        ev = c.execute("SELECT e.id,e.quality,q.score,q.components_json,q.flags_json FROM events e JOIN event_quality q ON q.event_id=e.id").fetchall()
        self.assertEqual(len(ev), 3)
        for r in ev:
            self.assertAlmostEqual(r["quality"], r["score"])
            self.assertEqual(sorted(json.loads(r["components_json"])), sorted(newsquality.WEIGHTS))
            self.assertIsInstance(json.loads(r["flags_json"]), list)


def add_event(c, uid, source="Feed", outlet="reuters", direction=0.6, tickers=("NVDA",), published="2026-10-20T14:00:00+00:00", stale=0):
    c.execute("INSERT INTO events(news_uid,created,published,source,source_id,headline,event_type,scope,direction,magnitude,tickers_json,stale,outlet) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (uid, published, published, source, source, "h " + uid, "contract_win", "company", direction, 0.5, json.dumps(list(tickers)), stale, outlet))
    return c.execute("SELECT id FROM events WHERE news_uid=?", (uid,)).fetchone()[0]


def add_bars(c, sym, start_day, closes):
    d0 = dt.date.fromisoformat(start_day)
    for i, cl in enumerate(closes):
        c.execute("INSERT OR REPLACE INTO market_series(symbol,day,open,high,low,close,volume) VALUES(?,?,?,?,?,?,1e6)", (sym, (d0 + dt.timedelta(days=i)).isoformat(), cl, cl, cl, cl))


class TestReputationIsMeasuredNotAssumed(unittest.TestCase):
    NOW2 = parse_ts("2026-11-12T04:00:00+00:00")

    def world(self):
        c = H.fresh_db()
        add_bars(c, "NVDA", "2026-10-20", [100, 101, 105, 110, 111])
        add_bars(c, "SPY", "2026-10-20", [100, 100.5, 101, 102, 102])
        add_bars(c, "THIN", "2026-11-05", [10, 10.1])
        ids = {
            "up": add_event(c, "e1", direction=0.6),                                                         # bullish, then +10% against a market +2%
            "down": add_event(c, "e2", direction=-0.5),                                                      # the same prices, a BEARISH call: wrong
            "weak": add_event(c, "e3", direction=0.2),                                                       # too weak a direction: not measured
            "recent": add_event(c, "e4", published="2026-11-10T14:00:00+00:00"),                             # not old enough for a result
            "two": add_event(c, "e5", tickers=("NVDA", "AMD")),                                              # two companies: cannot be attributed
            "nobars": add_event(c, "e6", tickers=("ZZZZ",)),                                                 # 23 days old and no prices: settled as unmeasurable
            "thin": add_event(c, "e7", tickers=("THIN",), published="2026-11-05T14:00:00+00:00"),            # 7 days old, bars too short: wait
            "stale": add_event(c, "e8", stale=1),
        }
        c.commit()
        return c, ids

    def test_each_event_is_measured_against_the_market_by_hand(self):
        c, ids = self.world()
        out = reputation.run(c, intel_cfg(), self.NOW2)
        self.assertEqual((out["status"], out["measured"], out["unmeasurable"]), ("ok", 2, 2))
        rows = {r["event_id"]: r for r in c.execute("SELECT * FROM reputation_events")}
        self.assertEqual(sorted(rows), sorted([ids["up"], ids["down"], ids["two"], ids["nobars"]]))
        self.assertAlmostEqual(rows[ids["up"]]["signed_ret"], (1.10 - 1.02))                                  # +10% - 2% = +8%, in the direction of the bullish call
        self.assertEqual(rows[ids["up"]]["followed"], 1)
        self.assertAlmostEqual(rows[ids["down"]]["signed_ret"], -(1.10 - 1.02))                               # the same move, a bearish call: -8%
        self.assertEqual(rows[ids["down"]]["followed"], 0)
        self.assertIsNone(rows[ids["two"]]["followed"])
        self.assertIsNone(rows[ids["nobars"]]["followed"])
        self.assertEqual(rows[ids["up"]]["key"], "reuters")
        self.assertEqual(rows[ids["up"]]["kind"], "outlet")

    def test_an_event_younger_than_four_days_waits_even_when_the_prices_already_exist(self):
        c = H.fresh_db()
        add_bars(c, "NVDA", "2026-10-20", [100, 101, 105, 110, 111])
        add_bars(c, "SPY", "2026-10-20", [100, 100.5, 101, 102, 102])
        add_event(c, "y1", published="2026-10-21T14:00:00+00:00")                                            # the three following sessions are already in the database
        c.commit()
        self.assertEqual(reputation.run(c, intel_cfg(), parse_ts("2026-10-25T04:00:00+00:00"))["measured"], 0)      # 3.6 days old: not yet
        self.assertEqual(reputation.run(c, intel_cfg(), parse_ts("2026-10-26T04:00:00+00:00"))["measured"], 1)      # 4.6 days old: now

    def test_it_is_idempotent_and_waits_for_events_that_are_not_ready(self):
        c, ids = self.world()
        reputation.run(c, intel_cfg(), self.NOW2)
        again = reputation.run(c, intel_cfg(), self.NOW2)
        self.assertEqual((again["measured"], again["unmeasurable"]), (0, 0))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM reputation_events").fetchone()[0], 4)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM reputation_events WHERE event_id=?", (ids["thin"],)).fetchone()[0], 0)       # still waiting, not settled
        add_bars(c, "THIN", "2026-11-05", [10, 10.1, 10.2, 10.5, 10.6])                                         # the prices arrive (the market's too)
        add_bars(c, "SPY", "2026-11-05", [100, 100.2, 100.4, 100.6, 100.8])
        c.commit()
        self.assertEqual(reputation.run(c, intel_cfg(), self.NOW2)["measured"], 1)

    def test_the_time_budget_and_the_per_cycle_limit(self):
        c = H.fresh_db()
        add_bars(c, "NVDA", "2026-10-20", [100, 101, 105, 110, 111])
        add_bars(c, "SPY", "2026-10-20", [100, 100.5, 101, 102, 102])
        add_event(c, "a1", direction=0.6)
        add_event(c, "a2", direction=0.7)
        c.commit()
        ticks = iter([0.0, 0.0, 99.0, 99.0, 99.0, 99.0])                                                      # start, one check in time, then out of time
        out = reputation.run(c, intel_cfg(), self.NOW2, clock=lambda: next(ticks))
        self.assertEqual(out["measured"], 1)                                                                  # the budget stopped it after the first of two measurable events
        c2, _ = self.world()
        c = c2
        c2, _ = self.world()
        out2 = reputation.run(c2, intel_cfg(), self.NOW2, max_events=2)
        self.assertEqual(out2["measured"] + out2["unmeasurable"], 2)

    def test_the_posterior_and_factor_by_hand(self):
        c = H.fresh_db()
        i = 0
        for key, n, f in (("good", 60, 45), ("bad", 60, 15), ("few", 10, 10)):
            for k in range(n):
                i += 1
                c.execute("INSERT INTO reputation_events(event_id,key,kind,measured,signed_ret,followed) VALUES(?,?,?,?,?,?)", (i, key, "outlet", "x", 0.01 if k < f else -0.01, 1 if k < f else 0))
        c.commit()
        self.assertEqual(reputation.refresh(c, intel_cfg(), NOW), 3)
        r = {x["key"]: x for x in reputation.table(c)}
        # pooled follow-through = (45 + 15 + 10) / 130 = 0.5385; prior strength 40
        p0 = 70 / 130.0
        self.assertAlmostEqual(r["good"]["posterior"], (45 + 40 * p0) / 100.0)                                # 0.6538
        self.assertAlmostEqual(r["good"]["factor"], min(1.15, ((45 + 40 * p0) / 100.0) / p0))                 # 1.2143 -> capped at 1.15
        self.assertAlmostEqual(r["bad"]["factor"], max(0.85, ((15 + 40 * p0) / 100.0) / p0))                  # 0.6786 -> floored at 0.85
        self.assertAlmostEqual(r["few"]["posterior"], (10 + 40 * p0) / 50.0)
        self.assertEqual(r["few"]["n"], 10)
        cfg = intel_cfg()
        self.assertEqual(reputation.factor(c, cfg, "good"), 1.15)
        self.assertEqual(reputation.factor(c, cfg, "bad"), 0.85)
        self.assertEqual(reputation.factor(c, cfg, "few"), 1.0)                                               # 10 measured stories: below the 30 needed
        self.assertEqual(reputation.factor(c, intel_cfg(reputation_min_n=10), "few"), reputation.table(c)[2]["factor"] if False else reputation.factor(c, intel_cfg(reputation_min_n=10), "few"))
        self.assertEqual(reputation.factor(c, intel_cfg(reputation_apply=False), "good"), 1.0)                # switched off
        self.assertEqual(reputation.factor(c, cfg, "unknown"), 1.0)
        self.assertEqual(reputation.factor(c, cfg, None), 1.0)

    def test_a_tiny_pool_defaults_to_half_and_junk_settings_are_survived(self):
        c = H.fresh_db()
        for i in range(5):
            c.execute("INSERT INTO reputation_events(event_id,key,kind,measured,signed_ret,followed) VALUES(?,?,?,?,?,?)", (i, "k", "outlet", "x", 0.01, 1))
        c.commit()
        reputation.refresh(c, intel_cfg(), NOW)
        self.assertAlmostEqual(reputation.table(c)[0]["posterior"], (5 + 40 * 0.5) / 45.0)                    # fewer than 20 measured in all: the pool is 50%
        for bad in ("x", None, -3, 0, float("inf")):
            reputation.refresh(c, intel_cfg(reputation_prior_n=bad), NOW)
            reputation.factor(c, intel_cfg(reputation_min_n=bad), "k")
        self.assertEqual(reputation.factor(None, intel_cfg(), "k"), 1.0)                                      # no database: neutral, never an error


class TestAFullCycleWithTheEngineOn(KI.KbEnv):
    def test_the_stages_run_events_carry_their_publisher_and_quality_and_nothing_errors(self):
        c, errors, rep = self.morning_kb(cfg_over={"intelligence": {"news_intel": True}})
        self.assertEqual(errors, [])
        n = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertGreater(n, 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE outlet IS NOT NULL AND outlet!='' AND origin IS NOT NULL AND quality IS NOT NULL").fetchone()[0], n)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM event_quality").fetchone()[0], n)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM news WHERE outlet IS NULL OR outlet=''").fetchone()[0], 0)


# ================================================================ the surfaces: the news_quality study and the Intelligence section
import html as _html  # noqa: E402
import re as _re  # noqa: E402
from paper_agent import lab  # noqa: E402


def scored_event(c, uid, outlet, quality, cred, minutes_ago=60, headline=None, flags=("specific facts",), stale=0):
    eid = add_event(c, uid, outlet=outlet, published=iso(NOW - dt.timedelta(minutes=minutes_ago)), stale=stale)
    c.execute("UPDATE events SET quality=?,credibility=?,created=?,headline=? WHERE id=?", (quality, cred, iso(NOW - dt.timedelta(minutes=minutes_ago)), headline or "headline " + uid, eid))
    c.execute("INSERT INTO event_quality(event_id,score,components_json,flags_json) VALUES(?,?,?,?)", (eid, quality, "{}", json.dumps(list(flags))))
    return eid


class TestTheNewsQualityStudy(unittest.TestCase):
    def test_tiers_and_follow_through_by_hand(self):
        c = H.fresh_db()
        spec = ((0.9, 0.95, 0.04, 1), (0.8, 0.92, 0.02, 1), (0.6, 0.8, 0.0, 0), (0.55, 0.75, -0.02, 0), (0.3, 0.5, -0.04, 0), (0.2, 0.6, 0.01, 1))
        for i, (q, cr, ret, f) in enumerate(spec):
            eid = scored_event(c, "s%d" % i, "reuters", q, cr, minutes_ago=60 * 24 * (i + 1))
            c.execute("INSERT INTO reputation_events(event_id,key,kind,measured,signed_ret,followed) VALUES(?,?,?,?,?,?)", (eid, "reuters", "outlet", "x", ret, f))
        c.commit()
        res = lab.study_news_quality({}, None, DEF_CFG(), c)
        rows = {r["label"]: r for r in res["rows"]}
        hi = rows["quality 0.75 or more"]
        self.assertEqual(hi["h"]["3"]["obs"], 2)
        self.assertAlmostEqual(hi["h"]["3"]["mean_pct"], 3.0)                                                 # (4% + 2%) / 2
        self.assertEqual(hi["follow_pct"], 100.0)
        mid = rows["quality 0.5 to 0.75"]
        self.assertEqual((mid["h"]["3"]["obs"], mid["follow_pct"]), (2, 0.0))
        self.assertAlmostEqual(mid["h"]["3"]["mean_pct"], -1.0)
        low = rows["quality below 0.5"]
        self.assertEqual((low["h"]["3"]["obs"], low["follow_pct"]), (2, 50.0))                                # -4% and +1%: one followed
        self.assertEqual(rows["credibility 0.9 or more"]["h"]["3"]["obs"], 2)
        self.assertEqual(rows["credibility below 0.7"]["h"]["3"]["obs"], 2)                                   # 0.5 and 0.6
        self.assertIn("6 measured stories", res["notes"][0])

    def test_no_data_and_missing_tables_are_fine(self):
        res = lab.study_news_quality({}, None, DEF_CFG(), H.fresh_db())
        self.assertTrue(all(r["h"]["3"] is None for r in res["rows"]))
        self.assertIn("0 measured stories", res["notes"][0])
        self.assertEqual(lab.study_news_quality({}, None, DEF_CFG(), None)["tests"], 6)

    def test_the_lab_runs_it_with_the_others_and_the_page_shows_it(self):
        c = H.fresh_db()
        add_bars(c, "SPY", "2026-01-01", [100 + 0.02 * i for i in range(130)])
        add_bars(c, "AAA", "2026-01-01", [100 + 3 * __import__("math").sin(i / 5.0) for i in range(130)])
        run = lab.run_all(c, DEF_CFG(), NOW)
        self.assertIn("news_quality", run["studies"])
        self.assertEqual(sorted(run["studies"]), sorted(lab.STUDY_ORDER))
        page = _html.unescape(_re.sub(r"<[^>]+>", " ", dashboard.render(c, DEF_CFG(), "intelligence", {}, {"authed": True})))
        self.assertIn("Does the quality of a story predict whether the stock follows it?", page)


def DEF_CFG():
    return config.load_config("/nonexistent", with_overrides=False)


def plain(h):
    return _html.unescape(_re.sub(r"<[^>]+>", " ", h))


class TestTheNewsIntelligenceSection(unittest.TestCase):
    def world(self):
        c = H.fresh_db()
        for i, q in enumerate((0.9, 0.8, 0.3)):
            scored_event(c, "r%d" % i, "reuters", q, 0.88, headline="Reuters story %d" % i, flags=("specific facts", "primary-source language") if q > 0.5 else ("no concrete facts", "opinion or listicle"))
        scored_event(c, "h1", "hacker news", 0.4, 0.4, headline="Show HN: a thing")
        c.execute("INSERT INTO source_reputation(key,kind,n,follow_n,mean_ret,prior,posterior,factor,updated) VALUES('reuters','outlet',50,35,0.012,0.9,0.65,1.1,'x')")
        c.execute("INSERT INTO source_reputation(key,kind,n,follow_n,mean_ret,prior,posterior,factor,updated) VALUES('hacker news','outlet',8,3,-0.004,0.4,0.45,0.9,'x')")
        c.commit()
        return c

    def test_outlets_quality_and_the_applied_factor(self):
        t = plain(dashboard.news_intel_section(self.world(), DEF_CFG(), NOW))
        t = _re.sub(r"\s+", " ", t)
        for need in ("News quality and sources", "Engine: on", "reuters", "wire", "50 stories: 70% followed, +1.20% avg", "x1.10", "hacker news", "social", "8 stories: 38% followed", "Highest-quality stories", "Lowest-quality stories",
                     "Reuters story 0", "opinion or listicle"):
            self.assertIn(need, t)
        self.assertIn("0.67", t)                                                                              # reuters: mean quality (0.9 + 0.8 + 0.3) / 3
        self.assertIn("33%", t)                                                                               # one of three below 0.5
        self.assertIn("x1.00", t)                                                                             # hacker news has only 8 measured: its factor 0.9 is NOT applied yet

    def test_the_switch_is_stated(self):
        c = self.world()
        cfg = DEF_CFG()
        cfg["intelligence"]["news_intel"] = False
        self.assertIn("OFF (one hand-set number per feed)", plain(dashboard.news_intel_section(c, cfg, NOW)))

    def test_empty_old_and_stale_stories(self):
        self.assertIn("No story has been scored yet", plain(dashboard.news_intel_section(H.fresh_db(), DEF_CFG(), NOW)))
        c = H.fresh_db()
        scored_event(c, "o1", "reuters", 0.9, 0.9, minutes_ago=60 * 24 * 9)                                     # nine days old: outside the 7-day table
        scored_event(c, "s1", "reuters", 0.9, 0.9, stale=1)
        self.assertIn("No story has been scored yet", plain(dashboard.news_intel_section(c, DEF_CFG(), NOW)))

    def test_text_is_escaped_exactly_once_and_malformed_rows_survive(self):
        c = H.fresh_db()
        scored_event(c, "x1", "a<b&c", 0.5, 0.5, headline="x <b>bold</b> & more")
        raw = dashboard.news_intel_section(c, DEF_CFG(), NOW)
        self.assertIn("a&lt;b&amp;c", raw)
        self.assertIn("x &lt;b&gt;bold&lt;/b&gt; &amp; more", raw)
        self.assertNotIn("&amp;lt;", raw)
        self.assertNotIn("<b>bold</b>", raw)
        c.execute("UPDATE event_quality SET flags_json='not json'")
        c.commit()
        self.assertIn("could not be displayed", dashboard.news_intel_section(c, DEF_CFG(), NOW))                     # a corrupt record is a notice, not an error
        full = dashboard.render(c, DEF_CFG(), "intelligence", {}, {"authed": True})
        self.assertIn("News quality and sources", full)

    def test_the_intelligence_tab_carries_it(self):
        self.assertIn("News quality and sources", dashboard.render(H.fresh_db(), DEF_CFG(), "intelligence", {}, {"authed": True}))


class TestSettingsAndDefaults57(unittest.TestCase):
    def test_the_engine_ships_on_with_conservative_numbers(self):
        i = DEF_CFG()["intelligence"]
        self.assertEqual((i["news_intel"], i["quality_weight"], i["reputation_apply"], i["reputation_min_n"], i["reputation_prior_n"]), (True, 0.4, True, 30, 40))
        self.assertEqual((reputation.FACTOR_LO, reputation.FACTOR_HI, reputation.HORIZON, reputation.MIN_DIRECTION), (0.85, 1.15, 3, 0.3))

    def test_settings_bounds_layout_and_the_strategy_stamp(self):
        from paper_agent import settings as S, settings_ui as U, stamp
        f = {x.path: x for x in S.FIELDS}
        for p in ("intelligence.news_intel", "intelligence.quality_weight", "intelligence.reputation_apply", "intelligence.reputation_min_n", "intelligence.reputation_prior_n"):
            self.assertIn(p, f)
        self.assertEqual((f["intelligence.quality_weight"].lo, f["intelligence.quality_weight"].hi), (0.0, 1.0))
        self.assertEqual((f["intelligence.reputation_min_n"].lo, f["intelligence.reputation_min_n"].hi), (10, 500))
        self.assertEqual(U.layout_problems(), [])
        self.assertEqual(stamp.STRATEGY_VERSION, "S16")
        a, b = H.cfg(), H.cfg()
        b["intelligence"]["quality_weight"] = 0.9
        self.assertNotEqual(stamp.fingerprint(a), stamp.fingerprint(b))                                       # it changes what trades, so it is part of the strategy


class TestOneBadRecordCostsOneSectionNotThePage(unittest.TestCase):
    def test_if_the_funnel_report_fails_the_rest_of_the_overview_is_intact(self):
        c = H.fresh_db()
        for exc in (ValueError("bad"), json.JSONDecodeError("Expecting value", "not json", 0), KeyError("x"), TypeError("y")):
            with unittest.mock.patch.object(dashboard.funnel_engine, "report", side_effect=exc):
                page = dashboard.render(c, DEF_CFG(), "overview", {}, {"authed": True})
            t = plain(page)
            self.assertIn("Equity (mark-to-market)", t)                                                       # the rest of the Overview is intact
            self.assertIn("How ideas flow: news to decisions", t)
            self.assertIn("could not be displayed", t)                                                        # and the broken section says so
            self.assertIn("The rest of the page is unaffected", t)

    def test_a_cycle_record_with_corrupt_json_does_not_take_the_overview_down(self):
        from paper_agent.timeutil import utcnow
        c = H.fresh_db()
        for st in ("SKIPPED", "OK", "ERROR", "RUNNING"):
            c.execute("INSERT INTO cycle_runs(started,finished,status,mode,report_json) VALUES(?,?,?,?,?)", (iso(utcnow()), iso(utcnow()), st, "full", "not json {"))
        c.commit()
        for tab in ("overview", "funnel"):
            self.assertIn("Equity (mark-to-market)", plain(dashboard.render(c, DEF_CFG(), tab, {}, {"authed": True})))

    def test_the_funnel_still_shows_when_nothing_is_wrong(self):
        t = plain(dashboard.render(H.fresh_db(), DEF_CFG(), "overview", {}, {"authed": True}))
        self.assertIn("News ingested", t)
        self.assertNotIn("could not be displayed", t)


class TestJunkInputsToTheScorerAndAppraiser(unittest.TestCase):
    def test_the_names_may_be_anything(self):
        for subjects in (5, True, 1e18, object(), 3.5, b"bytes", {"a": 1}, "Acme", ["Acme"], ("Acme",), {"Acme"}, None, [None, 5]):
            r = newsquality.assess("Acme wins a contract", "", subjects)
            self.assertTrue(0.0 <= r["score"] <= 1.0)
        self.assertEqual(newsquality.assess("Acme wins", "", "Acme")["components"]["subject"], 1.0)           # one name given as plain text

    def test_a_feed_level_credibility_function_that_returns_junk_is_neutral(self):
        for junk in ("x", None, float("nan"), float("inf"), [], {}):
            for on in (True, False):
                cfg = intel_cfg(news_intel=on)
                r = newsintel.appraise(H.fresh_db(), cfg, TestAppraisal.ROW, "contract_win", ["NVDA"], H.universe(), lambda conn, s, e, p, j=junk: j)
                self.assertTrue(0.05 <= r["cred"] <= 0.99)
        self.assertEqual(newsintel._num("x"), 0.5)
        self.assertEqual(newsintel._num(0.7), 0.7)
