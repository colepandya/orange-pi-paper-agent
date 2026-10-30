"""Reading the full article (no LLM): safe fetching, text extraction, per-site back-off, the four modes, and what it does to real candidates."""
import datetime as dt
import json
import os
import socket
import sys
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_pretrade as TP  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import config, db, evidence, net, nlp, reader, settings as S  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 29, 14, 0, tzinfo=UTC)


def resolver(*ips):
    return lambda host, port, type=None: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]


def page(paras, extra_head="", before="", after="", article=True):
    body = "".join("<p>%s</p>" % p for p in paras)
    inner = ("<article><h1>Headline</h1>%s</article>" % body) if article else ("<div class='story-body'>%s</div>" % body)
    return ("<html><head><title>Site | Headline</title>%s</head><body><nav><ul><li><a href='/'>Home</a></li><li><a href='/markets'>Markets and more links to click here today</a></li></ul></nav>%s%s%s"
            "<footer><p>Copyright 2026 The Site. All rights reserved. Terms and conditions apply to everything.</p></footer></body></html>" % (extra_head, before, inner, after))


LONG = ["Garmin Ltd on Wednesday raised its full-year revenue forecast and reported third-quarter earnings that beat Wall Street expectations, helped by strong demand for its fitness wearables.",
        "The company reported adjusted earnings of $2.31 per share, above the $1.95 per share analysts had expected, according to LSEG data and company filings reviewed by the news agency.",
        "Revenue rose 18% to $1.77 billion, compared with analyst estimates of $1.68 billion, the company said in a statement released after the close of trading on Wednesday.",
        "Garmin now expects 2026 revenue of about $7.1 billion, up from its prior forecast of $6.8 billion, citing strength in its fitness and aviation segments across all regions."]


class TestExtraction(unittest.TestCase):
    def test_it_takes_the_article_and_leaves_the_furniture(self):
        sidebar = "<aside class='related'><p>Related: ten other stories you might like to click on right now and read</p></aside>"
        comments = "<div id='comments'><p>Great article, I totally agree with everything the author wrote here today!!</p></div>"
        ads = "<div class='advert-slot'><p>Advertisement: buy our amazing product today with this limited time discount!</p></div>"
        ex = reader.extract(page(LONG, before=sidebar + ads, after=comments))
        self.assertEqual(ex["paragraphs"], LONG)
        self.assertEqual(ex["via"], "article")
        self.assertEqual(ex["chars"], sum(len(p) for p in LONG))

    def test_a_page_with_no_article_tag_uses_the_biggest_block_of_text(self):
        side = "<div class='sidebar'><p>%s</p></div>" % ("Trending: a bunch of other headlines and links " * 4)
        ex = reader.extract(page(LONG, before=side, article=False))
        self.assertEqual(ex["paragraphs"], LONG)

    def test_link_heavy_blocks_are_not_article_text(self):
        links = "<p>" + " ".join("<a href='/s%d'>Another story headline number %d from the site</a>" % (i, i) for i in range(5)) + "</p>"
        ex = reader.extract(page(LONG + [links]))
        self.assertEqual(ex["paragraphs"], LONG)

    def test_structured_data_wins_when_the_visible_page_is_only_a_teaser(self):
        extra = ["Analysts at several brokerages said the results pointed to a durable recovery in consumer demand for connected devices, and some raised their price targets after the report was released.",
                 "The company also said it had completed the integration of two acquisitions made last year and expects further cost savings from supply chain changes over the next several quarters of operations.",
                 "Chief executive officer said on a call with investors that demand in the aviation segment remained robust and that order backlogs were at the highest level the company has seen in a decade."]
        body = "\n\n".join(LONG + extra)
        ld = '<script type="application/ld+json">%s</script>' % json.dumps({"@context": "https://schema.org", "@type": "NewsArticle", "articleBody": body, "datePublished": "2026-10-29T12:00:00Z"})
        ex = reader.extract(page([LONG[0]], extra_head=ld))
        self.assertEqual(ex["via"], "ld+json")
        self.assertGreater(ex["chars"], 1000)
        self.assertEqual(ex["published"], "2026-10-29T12:00:00Z")

    def test_broken_html_still_yields_the_text(self):
        html = "<html><body><article><p>%s<p>%s<div><p>%s</article></body>" % (LONG[0], LONG[1], LONG[2])
        self.assertEqual(reader.extract(html)["paragraphs"], LONG[:3])
        self.assertEqual(reader.extract("")["paragraphs"], [])
        self.assertEqual(reader.extract("<<<>>> not html at all \x00")["paragraphs"], [])

    def test_duplicate_paragraphs_and_boilerplate_lines_are_dropped(self):
        ex = reader.extract(page([LONG[0], LONG[0], "Subscribe to our newsletter for the latest news delivered to your inbox every morning", LONG[1]]))
        self.assertEqual(ex["paragraphs"], [LONG[0], LONG[1]])


class TestFetchingIsSafe(unittest.TestCase):
    def blocked(self, url, res=resolver("93.184.216.34")):
        with self.assertRaises(reader.Blocked, msg=url):
            reader.check_url(url, res)

    def test_only_plain_public_web_addresses_are_allowed(self):
        self.assertEqual(reader.check_url("https://www.reuters.com/markets/x", resolver("93.184.216.34")), "www.reuters.com")
        for bad in ("file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "gopher://example.com", "//example.com/x", "", "http://user:pw@example.com/x", "http://localhost/admin",
                    "http://printer.local/", "http://nas.lan/", "http://127.0.0.1/", "http://[::1]/", "http://192.168.1.91:8088/", "http://169.254.169.254/latest/meta-data/", "http://example.com:8088/x", "http://10.0.0.5/"):
            self.blocked(bad)

    def test_a_public_looking_name_that_resolves_to_a_private_address_is_refused(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.91", "172.16.0.9", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "fd00::1", "fe80::1"):
            self.blocked("https://news.example.com/story", resolver(ip))
        self.blocked("https://news.example.com/story", resolver("93.184.216.34", "10.0.0.5"))               # ANY private answer is enough to refuse
        self.blocked("https://nope.example.com/", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("no")))

    def test_every_redirect_hop_is_rechecked(self):
        h = reader._SafeRedirect()
        req = urllib.request.Request("https://news.example.com/a")
        with mock.patch.object(reader, "check_url", side_effect=lambda u, *a: (_ for _ in ()).throw(reader.Blocked("private")) if "127.0.0.1" in u else "x"):
            with self.assertRaises(reader.Blocked):
                h.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/admin")
            self.assertIsNotNone(h.redirect_request(req, None, 302, "Found", {}, "https://news.example.com/b"))

    def test_size_type_and_decoding_limits(self):
        class Resp:
            def __init__(self, body, ctype="text/html; charset=utf-8", url="https://n.example.com/a"):
                self._b, self.headers, self._u = body, {"Content-Type": ctype}, url
                self.headers = type("H", (), {"get": lambda s, k, d=None: {"Content-Type": ctype}.get(k, d)})()

            def read(self, n=-1):
                return self._b[:n] if n >= 0 else self._b

            def geturl(self):
                return self._u

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Op:
            def __init__(self, r):
                self.r = r

            def open(self, req, timeout=None):
                return self.r
        with mock.patch.object(reader, "check_url", return_value="n.example.com"):
            html, final = reader.fetch_html("https://n.example.com/a", opener=Op(Resp("<p>caf\u00e9</p>".encode("utf-8"))))
            self.assertIn("caf\u00e9", html)
            html, _ = reader.fetch_html("https://n.example.com/a", opener=Op(Resp("<p>caf\u00e9</p>".encode("latin-1"), "text/html; charset=iso-8859-1")))
            self.assertIn("caf\u00e9", html)
            with self.assertRaises(net.FetchError):
                reader.fetch_html("https://n.example.com/a", opener=Op(Resp(b"%PDF-1.4", "application/pdf")))
            with self.assertRaises(net.FetchError):
                reader.fetch_html("https://n.example.com/a", max_bytes=100, opener=Op(Resp(b"x" * 500)))

    def test_it_identifies_itself_honestly(self):
        seen = {}

        class Op:
            def open(self, req, timeout=None):
                seen["ua"] = req.get_header("User-agent")
                raise urllib_error()
        import urllib.error as ue
        urllib_error = lambda: ue.HTTPError("https://n.example.com/a", 403, "Forbidden", {}, None)  # noqa: E731
        with mock.patch.object(reader, "check_url", return_value="n.example.com"):
            with self.assertRaises(net.FetchError) as cm:
                reader.fetch_html("https://n.example.com/a", opener=Op())
        self.assertEqual(cm.exception.code, 403)
        self.assertIn("PaperAgent", seen["ua"])
        self.assertIn("contact", seen["ua"])


def ok_fetcher(paras=LONG):
    return lambda url, timeout=8: (page(paras), url)


class TestRobotsTxt(unittest.TestCase):
    def setUp(self):
        reader._ROBOTS.clear()

    def fetch(self, body=None, error=None, log=None):
        def f(url, timeout=5.0):
            (log if log is not None else []).append(url)
            if error:
                raise error
            return body, url
        return f

    def test_a_site_can_forbid_bots_from_part_of_itself(self):
        f = self.fetch("User-agent: *\nDisallow: /private/\n\nUser-agent: PaperAgent\nDisallow: /markets/premium/\n")
        self.assertFalse(reader.robots_allows("https://news.example.com/markets/premium/x", f))
        self.assertTrue(reader.robots_allows("https://news.example.com/markets/story-1", f))
        self.assertTrue(reader.robots_allows("https://news.example.com/private/x", f))                    # the PaperAgent section replaces the generic one for us

    def test_a_site_can_forbid_everything(self):
        self.assertFalse(reader.robots_allows("https://closed.example.com/a", self.fetch("User-agent: *\nDisallow: /\n")))

    def test_no_robots_file_means_allowed_but_an_unreadable_one_means_stay_away(self):
        self.assertTrue(reader.robots_allows("https://a.example.com/x", self.fetch(error=net.FetchError("HTTP 404", 404))))
        self.assertFalse(reader.robots_allows("https://b.example.com/x", self.fetch(error=net.FetchError("HTTP 503", 503))))
        self.assertFalse(reader.robots_allows("https://c.example.com/x", self.fetch(error=net.FetchError("timeout"))))
        self.assertFalse(reader.robots_allows("https://d.example.com/x", self.fetch(error=reader.Blocked("private"))))

    def test_it_asks_once_a_day_not_once_per_article(self):
        log, now = [], [1000.0]
        f = self.fetch("User-agent: *\nAllow: /\n", log=log)
        for i in range(5):
            reader.robots_allows("https://e.example.com/s%d" % i, f, clock=lambda: now[0])
        self.assertEqual(len(log), 1)
        now[0] += 25 * 3600
        reader.robots_allows("https://e.example.com/s9", f, clock=lambda: now[0])
        self.assertEqual(len(log), 2)
        now[0] = 0
        reader.robots_allows("https://f.example.com/s", self.fetch(error=net.FetchError("HTTP 503", 503), log=log), clock=lambda: now[0])
        now[0] += 3601                                                                                    # an unreadable robots.txt is retried after an hour, not a day
        n = len(log)
        reader.robots_allows("https://f.example.com/s", self.fetch("User-agent: *\nAllow: /\n", log=log), clock=lambda: now[0])
        self.assertEqual(len(log), n + 1)

    def test_a_page_the_site_forbids_is_never_downloaded(self):
        c, cfg, calls = H.fresh_db(), H.cfg(), []
        cfg["reader"]["mode"] = "guard"
        r = reader.read_one(c, "u1", "t", "Acme earned $2.00 per share versus the $1.90 consensus.", "https://www.news.example.com/a", NOW, NOW, lambda u, t=8: calls.append(u) or (page(LONG), u), cfg, robots=lambda u: False)
        self.assertEqual(calls, [])
        self.assertEqual(r["via"], "snippet")
        self.assertIn("robots.txt", r["status"])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM article_domains").fetchone()[0], 0)                  # a polite refusal is not a failure: the site is not marked as hostile

    def test_a_test_fetcher_does_not_trigger_real_robots_lookups(self):
        c, cfg = H.fresh_db(), H.cfg()
        with mock.patch.object(reader, "robots_allows", side_effect=AssertionError("network")):
            r = reader.read_one(c, "u2", "t", "x", "https://www.news.example.com/a", NOW, NOW, lambda u, t=8: (page(LONG), u), cfg)
        self.assertEqual(r["via"], "page")


class TestReadingWithMemory(unittest.TestCase):
    def setUp(self):
        self.c = H.fresh_db()
        self.cfg = H.cfg()
        self.cfg["reader"]["mode"] = "guard"

    def read(self, fetcher, url="https://www.news.example.com/a1", uid="u1", summary="short snippet about earnings"):
        return reader.read_one(self.c, uid, "Garmin lifts outlook", summary, url, NOW, NOW, fetcher, self.cfg)

    def test_a_good_page_is_read_analysed_and_remembered_without_keeping_the_article(self):
        r = self.read(ok_fetcher())
        self.assertEqual((r["via"], r["status"]), ("page", "OK"))
        self.assertGreater(r["direction"], 0.5)
        row = self.c.execute("SELECT * FROM article_reads WHERE uid='u1'").fetchone()
        self.assertEqual((row["via"], row["status"], row["domain"]), ("page", "OK", "news.example.com"))
        blob = row["analysis_json"]
        self.assertNotIn(" ".join(LONG)[:250], blob)                                                  # facts and short sentences only
        self.assertLess(len(blob), 6000)

    def test_a_failed_download_falls_back_to_the_feeds_own_snippet_and_never_raises(self):
        for exc in (net.FetchError("HTTP 403", 403), net.FetchError("timeout"), reader.Blocked("private"), RuntimeError("boom"), ValueError("x")):
            def bad(url, timeout=8, e=exc):
                raise e
            r = reader.read_one(self.c, "u-%s" % type(exc).__name__, "t", "Acme earned $2.00 per share versus the $1.90 consensus.", "https://a%d.example.com/x" % id(exc), NOW, NOW, bad, self.cfg)
            self.assertEqual(r["via"], "snippet")
            self.assertGreater(r["direction"], 0.2)                                                  # the snippet alone still carries the numbers

    def test_a_paywall_stub_is_not_mistaken_for_an_article(self):
        stub = lambda url, timeout=8: (page(["Subscribe to continue reading this article. Sign in to read the full story today for free."]), url)  # noqa: E731
        r = self.read(stub)
        self.assertEqual(r["via"], "snippet")
        self.assertIn("PAYWALL", r["status"])
        wall = ["Sign in to read the full story: this content is available to subscribers only, and you can create a free account to continue reading the rest of this article today.",
                "To read the full article please subscribe to continue; already a subscriber? Sign in to read more of the latest news from our markets team around the world every day.",
                "Subscribe to continue reading: our journalism needs your support, so please create a free account to continue and enjoy unlimited access to all of our articles and videos."]
        r = self.read(lambda url, timeout=8: (page(wall), url), url="https://www.wall.example.com/a2", uid="wall2")                    # long enough to pass the size test: only the WORDING gives it away
        self.assertEqual(r["via"], "snippet")
        self.assertIn("PAYWALL", r["status"])

    def test_a_teaser_keeps_its_real_lead_and_loses_only_the_wall(self):
        teaser = LONG[:3] + ["The rest of this story is for paying readers only: subscribe to continue reading and get unlimited access to all of our reporting."]
        r = self.read(lambda url, timeout=8: (page(teaser), url), url="https://www.teaser.example.com/a3", uid="teaser")
        self.assertEqual((r["via"], r["status"]), ("page", "OK"))
        self.assertGreater(r["direction"], 0.5)

    def test_a_site_that_keeps_refusing_us_is_left_alone_for_a_day_and_forgiven_after_success(self):
        def deny(url, timeout=8):
            raise net.FetchError("HTTP 403", 403)
        for i in range(3):
            self.read(deny, url="https://paywalled.example.com/s%d" % i, uid="d%d" % i)
        why = reader.skip_reason(self.c, "https://paywalled.example.com/s9", NOW)
        self.assertIn("left alone", why)
        self.assertIsNone(reader.skip_reason(self.c, "https://paywalled.example.com/s9", NOW + dt.timedelta(hours=25)))
        calls = []
        self.read(lambda u, t=8: calls.append(u) or (page(LONG), u), url="https://paywalled.example.com/s10", uid="d10")
        self.assertEqual(calls, [])                                                                  # blocked: not even attempted
        self.c.execute("UPDATE article_domains SET blocked_until=NULL")
        self.read(ok_fetcher(), url="https://paywalled.example.com/s11", uid="d11")
        self.assertEqual(self.c.execute("SELECT fails FROM article_domains WHERE domain='paywalled.example.com'").fetchone()[0], 0)

    def test_sites_that_are_never_worth_fetching(self):
        for u in ("https://www.youtube.com/watch?v=1", "https://x.com/user/status/1", "https://github.com/a/b", "https://www.sec.gov/Archives/x", "https://m.reddit.com/r/x"):
            self.assertIn("not worth", reader.skip_reason(self.c, u, NOW) or "", u)
        self.assertIn("no URL", reader.skip_reason(self.c, "", NOW))
        self.assertIsNone(reader.skip_reason(self.c, "https://www.reuters.com/x", NOW))


def res(direction=0.0, conf=0.7, **kw):
    r = {"via": "page", "paragraphs": 5, "type": "report", "direction": direction, "confidence": conf, "magnitude": abs(direction), "mixed": False, "stale": False, "stale_why": "", "facts": []}
    r.update(kw)
    return r


def an(direction, event_type="earnings_beat", magnitude=0.55):
    return {"direction": direction, "magnitude": magnitude, "event_type": event_type, "scope": "company"}


class TestWhatTheArticleIsAllowedToChange(unittest.TestCase):
    def test_off_and_shadow_change_nothing(self):
        for mode in ("off", "shadow"):
            a = an(0.8)
            self.assertEqual(reader.merge(a, res(-0.9), mode, ["NVDA"]), "")
            self.assertEqual(a, an(0.8))

    def test_an_article_that_contradicts_the_headline_cancels_the_candidate(self):
        a = an(0.8)
        note = reader.merge(a, res(-0.7), "guard", ["NVDA"])
        self.assertEqual(a["direction"], 0.0)
        self.assertIn("article_contradicts", a)
        self.assertIn("headline reads good but the article's facts read bad", note)

    def test_it_only_acts_when_it_is_sure_enough(self):
        for r in (res(-0.7, conf=0.3), res(-0.2), res(-0.7, mixed=True)):
            a = an(0.8)
            reader.merge(a, r, "guard", ["NVDA"])
            self.assertEqual(a["direction"], 0.8 * (0.6 if r.get("mixed") else 1.0))

    def test_guard_can_weaken_but_never_strengthen_or_create(self):
        a = an(0.5)
        reader.merge(a, res(0.95, magnitude=0.95), "guard", ["NVDA"])
        self.assertEqual((a["direction"], a["magnitude"]), (0.5, 0.55))                              # agrees: unchanged, not boosted
        vague = an(0.05, "general_news", 0.15)
        reader.merge(vague, res(0.9), "guard", ["NVDA"])
        self.assertEqual(vague["direction"], 0.05)                                                   # a vague headline is NOT rescued in guard mode

    def test_opinion_sponsored_and_transcripts_are_not_news(self):
        for t in ("opinion", "promo", "transcript"):
            a = an(0.8)
            reader.merge(a, res(0.5, type=t), "guard", ["NVDA"])
            self.assertEqual((a["event_type"], a["direction"]), ("opinion_piece", 0.0))
        a = an(0.8)
        reader.merge(a, res(0.5, type="opinion", via="snippet", paragraphs=1), "guard", ["NVDA"])
        self.assertEqual(a["direction"], 0.8)                                                        # a short snippet is not enough to call an article opinion

    def test_old_news_is_flagged_and_mixed_results_are_shrunk(self):
        a = an(0.8)
        reader.merge(a, res(0.5, stale=True, stale_why="it happened Monday"), "guard", ["NVDA"])
        self.assertEqual(a["article_stale"], "it happened Monday")
        b = an(0.8)
        reader.merge(b, res(0.1, mixed=True), "guard", ["NVDA"])
        self.assertAlmostEqual(b["direction"], 0.48)

    def test_a_company_only_mentioned_in_passing_is_dropped_as_a_candidate(self):
        a = an(0.8)
        reader.merge(a, res(0.6, subject={"MSFT": 12.0, "GOOGL": 0.5}), "guard", ["MSFT", "GOOGL"])
        self.assertEqual(a["article_not_about"], ["GOOGL"])
        c = an(0.8)
        reader.merge(c, res(0.6, subject={"MSFT": 2.0, "GOOGL": 1.5}), "guard", ["MSFT", "GOOGL"])
        self.assertNotIn("article_not_about", c)                                                     # two co-stars: nobody is dropped

    def test_full_confirms_strengthens_and_rescues_but_only_from_a_real_article(self):
        a = an(0.5)
        reader.merge(a, res(0.9, magnitude=0.9), "full", ["NVDA"])
        self.assertAlmostEqual(a["direction"], 0.45 * 0.5 + 0.55 * 0.9)
        self.assertEqual(a["magnitude"], 0.9)
        vague = an(0.05, "general_news", 0.15)
        reader.merge(vague, res(0.9, magnitude=0.9, facts=[{"kind": "eps_surprise", "pol": 0.7, "w": 0.95}]), "full", ["NVDA"])
        self.assertAlmostEqual(vague["direction"], 0.72)
        self.assertEqual(vague["event_type"], "earnings_beat")
        for r in (res(0.9, via="snippet"), res(0.9, conf=0.4), res(0.9, type="opinion"), res(0.9, type="promo"), res(0.3)):
            v = an(0.05, "general_news", 0.15)
            reader.merge(v, r, "full", ["NVDA"])
            self.assertLess(abs(v["direction"]), 0.25, r)                                            # none of these may rescue a vague headline

    def test_odd_input_never_raises(self):
        for r in ({}, {"direction": None}, res(0.5, subject=None), None):
            try:
                reader.merge(an(0.5), r, "full", [])
            except Exception as e:  # noqa: BLE001
                self.fail("merge raised %r for %r" % (e, r))


def pre_item(i, title, summary="", url=None, trust=0.7, stale=False, scope="company", ticker="NVDA"):
    n = {"uid": "uid-%d" % i, "title": title, "summary": summary, "url": url if url is not None else "https://site%d.example.com/a" % i, "trust": trust}
    a = nlp.analyze(title, summary)
    a["scope"] = scope
    return (n, a, NOW - dt.timedelta(minutes=20), 0.3, stale, [(ticker, 0.9, "name")])


class TestTheBudgetedStep(unittest.TestCase):
    def setUp(self):
        self.c, self.cfg, self.u = H.fresh_db(), H.cfg(), H.universe()
        self.cfg["reader"].update(mode="guard", max_per_cycle=3, time_budget_seconds=25)
        self.calls = []

    def fetcher(self, url, timeout=8):
        self.calls.append(url)
        return page(LONG), url

    def test_it_downloads_only_the_best_ranked_items_within_the_cap_and_snippets_the_rest(self):
        titles = ["NVIDIA quarterly earnings results beat estimates", "NVIDIA unveils a new logo for its stores", "NVIDIA raises outlook after earnings", "NVIDIA sponsors a local race",
                  "NVIDIA acquisition of a chip startup agreed", "NVIDIA opens an office"]
        pre = [pre_item(i, t) for i, t in enumerate(titles)]
        st = reader.enrich(self.c, self.u, self.cfg, pre, NOW, self.fetcher)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual((st["read"], st["page"], st["snippet"]), (6, 3, 3))
        first = {self.c.execute("SELECT title FROM (SELECT ? AS title)", (t,)).fetchone()[0] for t in titles}
        got = {r["uid"] for r in self.c.execute("SELECT uid FROM article_reads WHERE via='page'")}
        self.assertTrue(got <= {"uid-0", "uid-2", "uid-4"}, got)                                      # the earnings / outlook / acquisition headlines were chosen
        self.assertTrue(first)

    def test_a_second_pass_does_not_download_the_same_article_again(self):
        pre = [pre_item(0, "NVIDIA quarterly earnings beat")]
        reader.enrich(self.c, self.u, self.cfg, pre, NOW, self.fetcher)
        reader.enrich(self.c, self.u, self.cfg, pre, NOW, self.fetcher)
        self.assertEqual(len(self.calls), 1)

    def test_the_time_budget_stops_downloads_but_not_the_cheap_snippet_reading(self):
        ticks = iter([0, 0, 100, 100, 100, 100, 100, 100, 100, 100])             # 1st call sets the deadline (25 s), 2nd is item 1 (in budget), then the budget is gone
        pre = [pre_item(i, "NVIDIA earnings beat number %d" % i) for i in range(4)]
        st = reader.enrich(self.c, self.u, self.cfg, pre, NOW, self.fetcher, clock=lambda: next(ticks))
        self.assertEqual(len(self.calls), 1)                                                          # the 25-second budget was used up after the first
        self.assertEqual(st["read"], 4)

    def test_what_is_never_read(self):
        pre = [pre_item(0, "NVIDIA earnings beat", stale=True), pre_item(1, "NVIDIA earnings beat again", scope="macro"), pre_item(2, "8-K - NVIDIA CORP (0001045810) (Filer)"), pre_item(3, "NVIDIA earnings beat v2", url=""),
               pre_item(4, "Why NVIDIA stock soared today"), pre_item(5, "Is NVIDIA stock a buy?")]
        pre[0][1]["event_type"] = "earnings_beat"
        st = reader.enrich(self.c, self.u, self.cfg, pre, NOW, self.fetcher)
        self.assertEqual(self.calls, [])
        self.assertEqual(st["read"] if "read" in st else 0, st["read"])
        self.assertEqual(st["considered"], 1)                                                         # only the one with no URL is considered (and then not downloaded)

    def test_one_article_that_blows_up_does_not_cost_the_others_their_reading(self):
        real, calls = reader.read_one, []

        def flaky(conn, uid, *a, **k):
            calls.append(uid)
            if uid == "uid-0":
                raise RuntimeError("boom")
            return real(conn, uid, *a, **k)
        pre = [pre_item(0, "NVIDIA earnings beat estimates"), pre_item(1, "NVIDIA raises outlook after earnings")]
        with mock.patch.object(reader, "read_one", side_effect=flaky):
            st = reader.enrich(self.c, self.u, self.cfg, pre, NOW, self.fetcher)
        self.assertEqual(calls, ["uid-0", "uid-1"])
        self.assertEqual((st["errors"], st["read"]), (1, 1))

    def test_off_does_nothing_at_all(self):
        self.cfg["reader"]["mode"] = "off"
        st = reader.enrich(self.c, self.u, self.cfg, [pre_item(0, "NVIDIA earnings beat")], NOW, self.fetcher)
        self.assertEqual((self.calls, st["read"] if "read" in st else 0, self.c.execute("SELECT COUNT(*) FROM article_reads").fetchone()[0]), ([], 0, 0))


class World(TP.Scenario):
    """A full cycle where each news item's page is served by a fake fetcher keyed on the item's position (URL http://x/<n>)."""
    pages = {}

    def deps(self, items=()):
        d = super().deps(items)
        pages, calls = self.pages, self.calls
        d.article_fetch = lambda url, timeout=8: (calls.append(url), (pages[url], url))[1] if url in pages else (_ for _ in ()).throw(net.FetchError("HTTP 404", 404))
        return d

    def setUp(self):
        super().setUp()
        self.calls = []
        self.pages = {}

    def event(self):
        return self.c.execute("SELECT gate,direction,event_type FROM events ORDER BY id LIMIT 1").fetchone()

    def buys(self):
        return self.c.execute("SELECT COUNT(*) FROM decisions WHERE action='BUY'").fetchone()[0]


HEAD_BULL = "NVIDIA beats estimates and raises full-year guidance as data center demand surges"
BEAR_BODY = ["NVIDIA Corp on Tuesday reported quarterly earnings of $0.84 per share, below the $0.97 per share consensus estimate, and lowered its full-year guidance as customers delayed orders.",
             "Revenue was $3.2 billion versus the $3.4 billion expected by analysts, the company said in a statement released after the close of trading.",
             "NVIDIA cut its full-year outlook to $3.10-$3.30 per share from $3.60-$3.80, citing weaker demand in Europe and a slowdown in enterprise spending.",
             "Shares of the company fell sharply in extended trading as investors reassessed the growth outlook for the coming year and beyond."]
HEAD_VAGUE = "NVIDIA Reports Third Quarter Results"
BULL_BODY = ["NVIDIA Corp on Tuesday reported third-quarter earnings that beat Wall Street expectations and raised its full-year revenue forecast, helped by strong demand for its data center chips.",
             "The company reported adjusted earnings of $2.31 per share, above the $1.95 per share analysts had expected, according to LSEG data and company filings reviewed by the news agency.",
             "Revenue rose 18% to $1.77 billion, compared with analyst estimates of $1.68 billion, the company said in a statement released after the close of trading on Tuesday.",
             "NVIDIA now expects 2026 revenue of about $7.1 billion, up from its prior forecast of $6.8 billion, citing strength in its data center and automotive segments across all regions."]


class TestRealCandidatesThroughTheWholePipeline(unittest.TestCase):
    def run_one(self, mode, head, body):
        w = World()
        w.setUp()
        try:
            w.cfg["reader"]["mode"] = mode
            w.pages = {"http://x/0": page(body)}
            w.cyc(TL.T_PRE, [head])
            return w.event(), w.buys(), list(w.calls), w.c.execute("SELECT status,merge_note FROM article_reads").fetchall()
        finally:
            w.tearDown()

    def test_a_bullish_headline_whose_article_says_the_opposite_is_cancelled_in_guard_and_full_but_not_in_off_or_shadow(self):
        for mode, trades in (("off", 1), ("shadow", 1), ("guard", 0), ("full", 0)):
            ev, buys, calls, reads = self.run_one(mode, HEAD_BULL, BEAR_BODY)
            self.assertEqual(buys, trades, mode)
            if mode in ("guard", "full"):
                self.assertEqual(ev["gate"], "article_disagrees", mode)
                self.assertIn("headline reads good but the article's facts read bad", reads[0]["merge_note"])
            elif mode == "shadow":
                self.assertEqual(ev["gate"], "candidate")
                self.assertEqual(len(reads), 1)                                                      # recorded for later study, nothing changed
            else:
                self.assertEqual(reads, [])

    def test_a_vague_headline_is_rescued_only_in_full_mode(self):
        for mode, trades in (("off", 0), ("shadow", 0), ("guard", 0), ("full", 1)):
            ev, buys, calls, reads = self.run_one(mode, HEAD_VAGUE, BULL_BODY)
            self.assertEqual(buys, trades, mode)
            self.assertEqual(ev["gate"], "candidate" if mode == "full" else "direction_too_weak", mode)
            if mode == "full":
                self.assertEqual(ev["event_type"], "earnings_beat")

    def test_old_news_in_the_article_is_not_traded(self):
        stale = ["NVIDIA Corp said on Friday that quarterly profit rose 25%, and the shares have been moving since as investors digest the results.",
                 "The company reported earnings of $2.31 per share, above the $1.95 per share analysts had expected, according to data compiled by LSEG for the news agency.",
                 "Revenue rose 18% to $1.77 billion, compared with analyst estimates of $1.68 billion, the company said in a statement released earlier in the week.",
                 "NVIDIA raised its full-year revenue forecast to about $7.1 billion from its prior forecast of $6.8 billion, citing strength in its data center segment."]
        ev, buys, calls, reads = self.run_one("guard", "NVIDIA shares jump after earnings beat and raised outlook", stale)
        self.assertEqual((ev["gate"], buys), ("stale_in_article", 0))

    def test_a_fetch_failure_or_a_crashing_fetcher_never_stops_news_from_being_processed(self):
        w = World()
        w.setUp()
        try:
            w.cfg["reader"]["mode"] = "full"
            w.pages = {}                                                                              # every page is a 404
            w.cyc(TL.T_PRE, [HEAD_BULL])
            self.assertEqual((w.event()["gate"], w.buys()), ("candidate", 1))                          # decided on the headline alone, as before
            self.assertEqual(w.c.execute("SELECT via FROM article_reads").fetchone()[0], "snippet")
        finally:
            w.tearDown()
        w = World()
        w.setUp()
        try:
            w.cfg["reader"]["mode"] = "full"
            with mock.patch.object(reader, "read_one", side_effect=RuntimeError("boom")):
                rep = w.cyc(TL.T_PRE, [HEAD_BULL])
            self.assertEqual((w.event()["gate"], w.buys()), ("candidate", 1))
            self.assertEqual(rep["errors"], [])
            self.assertEqual(rep["articles"]["read"], 0)                                              # every item failed, none was lost to it, the cycle carried on
        finally:
            w.tearDown()

    def test_the_cycle_report_says_what_was_read(self):
        w = World()
        w.setUp()
        try:
            w.cfg["reader"]["mode"] = "guard"
            w.pages = {"http://x/0": page(BULL_BODY)}
            rep = w.cyc(TL.T_PRE, [HEAD_BULL])
            a = rep["articles"]
            self.assertEqual((a["mode"], a["read"], a["page"]), ("guard", 1, 1))
            self.assertNotIn("notes", a)
        finally:
            w.tearDown()


class TestSettingsAndSchema(unittest.TestCase):
    def test_defaults_and_the_choice_setting(self):
        base = config.load_config("/nonexistent", with_overrides=False)
        self.assertEqual(base["reader"], {"mode": "guard", "max_per_cycle": 8, "time_budget_seconds": 25, "timeout_seconds": 8})
        f = {x.path: x for x in S.FIELDS}
        self.assertEqual(f["reader.mode"].choices, ["off", "shadow", "guard", "full"])
        for ok in ("off", "shadow", "guard", "full"):
            self.assertEqual(S._parse(f["reader.mode"], ok), ok)
        for bad in ("", "FULL", "guard; drop", "llm"):
            with self.assertRaises(ValueError):
                S._parse(f["reader.mode"], bad)
        for bad in ("-1", "41", "x"):
            with self.assertRaises(ValueError):
                S._parse(f["reader.max_per_cycle"], bad)

    def test_the_tables_exist_and_nothing_else_is_stored(self):
        c = db.open_db(":memory:")
        self.assertEqual([r[1] for r in c.execute("PRAGMA table_info(article_reads)")], ["uid", "url", "fetched_at", "status", "http", "via", "chars", "domain", "analysis_json", "merge_note"])
        self.assertEqual(db.schema_drift(c), {})


if __name__ == "__main__":
    unittest.main()
