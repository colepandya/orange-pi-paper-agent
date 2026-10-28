"""Company recognition + event qualification on realistic wire headlines (SEC legal names in, brand names out).
Background: on a realistic set only 38% of headlines became candidates - 11/37 lost the company ('Uber' vs 'UBER TECHNOLOGIES INC') and many common
phrasings ('beats subscriber estimates', 'signs a contract', 'Tesla recalls') were never classified."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import entities, nlp, recall, universe as U  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402

SEC = [("AMAZON COM INC", "AMZN"), ("Alphabet Inc.", "GOOGL"), ("Meta Platforms, Inc.", "META"), ("NVIDIA CORP", "NVDA"), ("Advanced Micro Devices, Inc.", "AMD"), ("Walt Disney Co", "DIS"), ("Tesla, Inc.", "TSLA"),
       ("Apple Inc.", "AAPL"), ("Microsoft Corp", "MSFT"), ("JPMORGAN CHASE & CO", "JPM"), ("BANK OF AMERICA CORP /DE/", "BAC"), ("JOHNSON & JOHNSON", "JNJ"), ("Exxon Mobil Corp", "XOM"), ("Chevron Corp", "CVX"),
       ("PFIZER INC", "PFE"), ("BOEING CO", "BA"), ("LOCKHEED MARTIN CORP", "LMT"), ("NETFLIX INC", "NFLX"), ("INTEL CORP", "INTC"), ("Walmart Inc.", "WMT"), ("COSTCO WHOLESALE CORP /NEW", "COST"),
       ("COCA COLA CO", "KO"), ("ELI LILLY & Co", "LLY"), ("Home Depot, Inc.", "HD"), ("Salesforce, Inc.", "CRM"), ("ORACLE CORP", "ORCL"), ("Uber Technologies, Inc", "UBER"), ("Palantir Technologies Inc.", "PLTR"),
       ("CrowdStrike Holdings, Inc.", "CRWD"), ("Ford Motor Co", "F"), ("GENERAL MOTORS CO", "GM"), ("GOLDMAN SACHS GROUP INC", "GS"), ("STARBUCKS CORP", "SBUX"), ("NIKE, Inc.", "NKE"), ("Hess Corp", "HES"),
       ("Pioneer Natural Resources Co", "PXD"), ("Moderna, Inc.", "MRNA"), ("MICRON TECHNOLOGY INC", "MU"), ("International Business Machines Corp", "IBM"), ("Berkshire Hathaway Inc", "BRK-B"),
       ("Citigroup Inc", "C"), ("Visa Inc.", "V"), ("Target Corp", "TGT"), ("Block, Inc.", "XYZ")]


def uni():
    return Universe(U.build({"fields": ["cik", "name", "ticker", "exchange"], "data": [[1000 + i, n, t, "Nasdaq"] for i, (n, t) in enumerate(SEC)]}))


class TestRecognition(unittest.TestCase):
    def test_benchmark_recall_and_no_false_positives(self):
        r = recall.evaluate(uni())
        self.assertGreaterEqual(r["recall"], 0.97, recall.text(r))
        self.assertGreaterEqual(r["candidate_rate"], 0.95, recall.text(r))
        self.assertEqual(r["false_positives"], [], recall.text(r))
        self.assertGreaterEqual(r["direction_correct"], r["candidates"] - 1, recall.text(r))

    def test_headline_names_resolve_from_sec_legal_names(self):
        u = uni()
        for text, want in (("Amazon to invest more in AI", "AMZN"), ("Uber announces buyback", "UBER"), ("Ford recalls trucks", "F"), ("Meta raises outlook", "META"), ("JPMorgan beats estimates", "JPM"),
                           ("Coca-Cola beats estimates", "KO"), ("Palantir wins contract", "PLTR"), ("Google unveils new chip", "GOOGL"), ("Facebook outage hits users", "META"), ("Disney raises guidance", "DIS"),
                           ("Costco same-store sales rise", "COST"), ("Citi cuts forecast", "C")):
            self.assertIn(want, [t for t, c, _ in u.match(text) if c >= 0.6], text)

    def test_generic_and_lowercase_words_do_not_match(self):
        u = uni()
        for text in ("A meta-analysis of 20 studies", "he crossed the ford at dawn", "the oracle spoke", "an apple a day", "block party tonight", "visa free travel expands"):
            self.assertEqual([t for t, c, _ in u.match(text) if c >= 0.6], [], text)

    def test_junk_two_word_aliases_are_not_created(self):
        clean, multi, first = entities.derive_aliases("BANK OF AMERICA CORP /DE/")
        self.assertEqual((clean, multi), ("BANK OF AMERICA", []))                  # 'bank of' would match every "bank of ..." phrase
        self.assertEqual(entities.derive_aliases("International Business Machines Corp")[1], [])
        self.assertEqual(entities.derive_aliases("GOLDMAN SACHS GROUP INC")[0], "GOLDMAN SACHS")       # the cleaned legal name is itself the alias
        self.assertEqual(entities.derive_aliases("AMAZON COM INC")[0], "AMAZON")
        self.assertEqual(entities.derive_aliases("COSTCO WHOLESALE CORP /NEW")[0], "COSTCO WHOLESALE")

    def test_first_word_alias_only_when_unique_in_the_universe(self):
        u = Universe(U.build({"fields": ["cik", "name", "ticker", "exchange"], "data": [[1, "Zenith Robotics Inc", "ZNR", "Nasdaq"], [2, "Zenith Energy Corp", "ZNE", "Nasdaq"], [3, "Quasar Dynamics Inc", "QSR", "Nasdaq"]]}))
        self.assertEqual([t for t, c, _ in u.match("Quasar raises outlook") if c >= 0.6], ["QSR"])
        self.assertEqual([t for t, c, _ in u.match("Zenith raises outlook") if c >= 0.6], [])      # two Zeniths: ambiguous, so no guess
        self.assertEqual([t for t, c, _ in u.match("Zenith Robotics raises outlook") if c >= 0.6], ["ZNR"])

    def test_hyphens_are_word_breaks(self):
        self.assertEqual(nlp.norm("Coca-Cola full-year outlook"), "coca cola full year outlook")


class TestClassificationCoversCommonWording(unittest.TestCase):
    CASES = [("Nike warns of weaker holiday-quarter sales", "guidance_cut", -1), ("Netflix beats subscriber estimates, shares rise", "earnings_beat", 1),
             ("IBM raises full-year revenue forecast", "guidance_raise", 1), ("Oracle signs $30 billion cloud contract", "contract_win", 1), ("Tesla recalls 200,000 vehicles", "regulatory_action", -1),
             ("General Motors raises profit guidance", "guidance_raise", 1), ("Home Depot misses sales estimates", "earnings_miss", -1), ("Starbucks cuts forecast as US traffic falls", "guidance_cut", -1),
             ("Exxon Mobil to buy Pioneer Natural Resources in $59.5 billion deal", "merger_acquisition", 1), ("AMD forecasts revenue above estimates", "earnings_beat", 1),
             ("Ford recalls 150,000 trucks", "regulatory_action", -1), ("Boeing faces FAA investigation over door plug", "regulatory_action", -1)]

    def test_common_headline_forms_are_classified_with_the_right_sign(self):
        for h, etype, sign in self.CASES:
            a = nlp.analyze(h)
            self.assertEqual(a["event_type"], etype, h)
            self.assertEqual(a["direction"] > 0, sign > 0, h)

    def test_raising_a_spending_forecast_is_not_mistaken_for_good_news(self):
        for h in ("Meta raises 2026 capital spending forecast as AI costs climb", "Company raises cost forecast for the year", "Firm raises capex outlook"):
            self.assertNotEqual(nlp.analyze(h)["event_type"], "guidance_raise", h)

    def test_negation_still_flips_direction_with_the_wider_patterns(self):
        self.assertLess(nlp.analyze("Acme fails to beat estimates")["direction"], 0)
        self.assertLess(nlp.analyze("Fed holds rates, hawkish tone surprises markets")["direction"], 0)


class TestEndToEndOnRealisticHeadlines(unittest.TestCase):
    def test_most_realistic_headlines_reach_a_decision(self):
        import datetime as dt
        from paper_agent import pipeline
        rf = lambda d: d.strftime("%a, %d %b %Y %H:%M:%S +0000")  # noqa: E731
        items = [(h, "", rf(H.NOW - dt.timedelta(hours=1, minutes=i))) for i, (h, _, _) in enumerate(recall.POSITIVE)]
        c = H.fresh_db()
        pipeline.run_cycle(c, H.cfg(), uni(), [{"id": "t", "name": "W", "type": "rss", "url": "http://x", "trust": .85, "interval": 0}], H.deps(items, {t: 100.0 for _, t in SEC}), H.NOW)
        events = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        decided = c.execute("SELECT COUNT(DISTINCT event_id) FROM decisions WHERE event_id IS NOT NULL").fetchone()[0]
        self.assertEqual(events, len(recall.POSITIVE))
        self.assertGreaterEqual(decided / events, 0.9, "only %d/%d headlines produced a decision" % (decided, events))


class TestEachGuardIsExercisedOnItsOwn(unittest.TestCase):
    """The earlier tests used words ('stock', 'here', 'aware') that the built-in stoplist already blocks, so the OTHER guards were never really tested."""
    def uni(self, common=None):
        names = [("Beacon Therapeutics Inc", "BCON"), ("Lift Therapeutics Inc", "LIFT"), ("Palantir Technologies Inc.", "PLTR"), ("Zorvex Systems Inc", "ZRVX")]
        return Universe(U.build({"fields": ["cik", "name", "ticker", "exchange"], "data": [[1 + i, n, t, "Nasdaq"] for i, (n, t) in enumerate(names)]}), common)

    def found(self, u, text):
        return {t for t, c, h in u.match(text) if c >= 0.6}

    def test_words_the_news_uses_in_lowercase_are_learned_as_ordinary_english(self):
        text = "Beacon of hope: why stocks rally"                                                  # 'Beacon' is not on the built-in stoplist
        self.assertIn("BCON", self.found(self.uni(), text))                                       # without learning, a unique distinctive word looks like a company
        corpus = ["a beacon of hope for investors", "the beacon was lit", "shines like a beacon", "another beacon in the night", "beacon beacon"] * 2
        common = entities.build_common_words(corpus, min_count=4)
        self.assertIn("beacon", common)
        self.assertNotIn("BCON", self.found(self.uni(common), text))                              # ...once the corpus shows it is an ordinary word, it is not
        self.assertIn("ZRVX", self.found(self.uni(common), "Zorvex raises outlook"))              # while a genuine coined name still works

    def test_four_letter_ordinary_words_need_more_than_a_unique_first_word(self):
        self.assertNotIn("LIFT", self.found(self.uni(), "Lift for tech stocks as rates ease"))     # 4 letters: too risky to treat as a name
        self.assertIn("LIFT", self.found(self.uni(), "Lift Therapeutics raises outlook"))          # the full name still matches

    def test_a_name_glued_to_a_hyphen_is_part_of_a_compound(self):
        self.assertNotIn("PLTR", self.found(self.uni(), "Anti-Palantir protesters gather outside"))
        self.assertIn("PLTR", self.found(self.uni(), "Palantir wins a contract"))


if __name__ == "__main__":
    unittest.main()


class TestRealWorldFalsePositives(unittest.TestCase):
    """From the user's real funnel output: 'Why Garmin Stock Dropped' matched SYBT (Stock Yards Bancorp) because 'Stock' was treated as a company name, 'From Here' matched HERE,
    'Context-Aware' matched AWRE, 'Work Assistant' matched WOK. A synthetic negative set had missed all of it."""
    EXTRA = [("Stock Yards Bancorp, Inc.", "SYBT"), ("GARMIN LTD", "GRMN"), ("Here Group Inc", "HERE"), ("AWARE INC /MA/", "AWRE"), ("WORK Medical Technology Group Ltd", "WOK"),
             ("Main Street Capital Corp", "MAIN"), ("Core Molding Technologies Inc", "CMT"), ("Pure Cycle Corp", "PCYO"), ("Edge Therapeutics Inc", "EDGE"), ("Market Leader Inc", "LEAD"),
             ("Trade Desk, Inc.", "TTD"), ("Light & Wonder, Inc.", "LNW"), ("Alamo Group Inc", "ALG"), ("Wendy's Co", "WEN")]

    def universe(self, common=None):
        data = [[1000 + i, n, t, "Nasdaq"] for i, (n, t) in enumerate(SEC + self.EXTRA)]
        return Universe(U.build({"fields": ["cik", "name", "ticker", "exchange"], "data": data}), common)

    HEADLINES = {
        "Why Garmin Stock Dropped After Record Results": ({"GRMN"}, {"SYBT"}),
        "3 Reasons Micron Stock Could Keep Climbing From Here": ({"MU"}, {"SYBT", "HERE"}),
        "Is AMD Stock a Buy, Sell, or Hold Right Now? Here's What I'm Doing.": ({"AMD"}, {"SYBT", "HERE"}),
        "Why Micron Stock Is Up Today": ({"MU"}, {"SYBT"}),
        "Rocker Launches as the Context-Aware AI Work Assistant for the Busiest People": (set(), {"AWRE", "WOK"}),
        "How to Work Smarter: Core Ideas for the Edge in Pure Strategy": (set(), {"WOK", "CMT", "EDGE", "PCYO"}),
        "Trade Desk Stock Surges as Light Returns to the Main Street": ({"TTD"}, {"LEAD", "MAIN", "LNW"}),
    }

    def check(self, u):
        for h, (want, forbid) in self.HEADLINES.items():
            got = {t for t, c, how in u.match(h) if c >= 0.6}
            self.assertTrue(want <= got, "%r should find %s but found %s" % (h, want, got))
            self.assertFalse(got & forbid, "%r wrongly matched %s" % (h, got & forbid))

    def test_ordinary_words_do_not_identify_companies_even_with_no_learned_data(self):
        self.check(self.universe())

    def test_words_learned_from_the_news_corpus_are_excluded_too(self):
        corpus = ["the market could keep climbing", "analysts say the stock may fall", "see what happens here next", "good work by the team", "stay aware of rising risks", "downtown main road closed",
                  "the stock market and stock prices", "we work here", "be aware the stock is volatile", "this stock and that stock", "work work work", "aware aware aware here here here main main"] * 3
        common = entities.build_common_words(corpus, min_count=4)
        for w in ("stock", "here", "work", "aware", "main"):
            self.assertIn(w, common)
        self.assertNotIn("garmin", common)
        self.check(self.universe(common))

    def test_real_companies_with_distinctive_names_are_still_found(self):
        u = self.universe()
        for text, want in (("Garmin raises full-year outlook", "GRMN"), ("Wendy's shares rally on takeover talk", "WEN"), ("Alamo Group beats estimates", "ALG"), ("Main Street Capital raises dividend", "MAIN"),
                           ("Stock Yards Bancorp reports third-quarter earnings", "SYBT"), ("Trade Desk plunges after guidance", "TTD")):
            self.assertIn(want, [t for t, c, h in u.match(text) if c >= 0.6], text)

    def test_a_word_glued_to_a_hyphen_is_part_of_a_compound(self):
        u = self.universe()
        self.assertNotIn("AWRE", [t for t, c, h in u.match("A context-aware assistant and an Aware-style tool") if c >= 0.6])

    def test_benchmark_recall_is_not_hurt_by_the_stricter_matching(self):
        r = recall.evaluate(self.universe())
        self.assertGreaterEqual(r["recall"], 0.97, recall.text(r))
        self.assertEqual(r["false_positives"], [])
