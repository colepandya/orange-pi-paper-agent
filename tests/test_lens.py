"""The in-house article understanding engine (no LLM). Realistic wire / press-release / opinion text in, auditable facts out. Each bug found while building it is pinned here."""
import datetime as dt
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paper_agent import lens  # noqa: E402

UTC = dt.timezone.utc
THU = dt.datetime(2026, 10, 29, 14, 0, tzinfo=UTC)

BEAT_RAISE = ("Garmin lifts full-year outlook after third-quarter profit tops estimates", [
    "Garmin Ltd on Wednesday raised its full-year revenue forecast and reported third-quarter earnings that beat Wall Street expectations, helped by strong demand for its fitness wearables.",
    "The Swiss-based company reported adjusted earnings of $2.31 per share, above the $1.95 per share analysts had expected, according to LSEG data.",
    "Revenue rose 18% to $1.77 billion, compared with analyst estimates of $1.68 billion.",
    "Garmin now expects 2026 revenue of about $7.1 billion, up from its prior forecast of $6.8 billion.",
    "Shares of the company fell 3% in premarket trading as investors worried about margins."])
MISS_CUT = ("Acme Corp shares tumble as profit misses, outlook cut", [
    "Acme Corp cut its annual profit forecast on Tuesday and posted quarterly earnings that fell short of analysts' estimates, citing weaker demand in Europe.",
    "The company earned 84 cents per share, below the consensus estimate of 97 cents.",
    "Revenue was $3.2 billion versus the $3.4 billion expected by analysts.",
    "Acme lowered its full-year guidance to $3.10-$3.30 per share from $3.60-$3.80."])
MIXED = ("Beta Inc beats on earnings but warns on outlook", [
    "Beta Inc reported quarterly earnings of $1.40 per share, topping the $1.25 estimate, but warned that sales would slow next quarter.",
    "The company cut its full-year revenue forecast to $5.0 billion from $5.4 billion, saying customers were delaying orders.",
    "Analysts said the guidance reduction overshadowed the better-than-expected quarter."])


def kinds(r, excl=("deal",)):
    return [f["kind"] for f in r["facts"] if f["kind"] not in excl]


def fact(r, kind):
    return next(f for f in r["facts"] if f["kind"] == kind)


class TestReadsRealisticArticles(unittest.TestCase):
    def test_a_clean_beat_and_raise_is_strongly_positive_with_the_actual_numbers(self):
        r = lens.analyze(*BEAT_RAISE, THU)
        self.assertGreater(r["direction"], 0.7)
        self.assertGreaterEqual(r["confidence"], 0.6)
        e = fact(r, "eps_surprise")
        self.assertEqual((e["actual"], e["estimate"]), (2.31, 1.95))
        self.assertAlmostEqual(e["surprise"], 0.1846, places=3)
        self.assertIn("guidance_raise", kinds(r))
        self.assertAlmostEqual(fact(r, "revenue_surprise")["surprise"], (1.77 - 1.68) / 1.68, places=2)

    def test_a_miss_and_a_cut_is_strongly_negative(self):
        r = lens.analyze(*MISS_CUT, THU)
        self.assertLess(r["direction"], -0.7)
        e = fact(r, "eps_surprise")
        self.assertEqual((e["actual"], e["estimate"]), (0.84, 0.97))                               # "84 cents" vs "97 cents"
        self.assertIn("guidance_cut", kinds(r))

    def test_a_beat_with_a_guidance_cut_is_flagged_mixed_and_not_acted_on_with_conviction(self):
        r = lens.analyze(*MIXED, THU)
        self.assertTrue(r["mixed"])
        self.assertLess(abs(r["direction"]), 0.35)
        self.assertLess(r["confidence"], 0.6)

    def test_deal_talks_are_hedged_and_a_signed_deal_is_not(self):
        talks = lens.analyze("DoorDash in advanced talks to acquire Instacart", [
            "DoorDash is in advanced talks to acquire Instacart, according to people familiar with the matter, and a deal could be announced as soon as this week."], THU)
        signed = lens.analyze("Gamma to acquire Delta for $4.2 billion in cash", [
            "Gamma Corp has agreed to acquire Delta Systems for $4.2 billion in cash, a 28% premium to Delta's Tuesday closing price, the companies said on Wednesday."], THU)
        t, s = fact(talks, "deal"), fact(signed, "deal")
        self.assertFalse(t["definitive"])
        self.assertLessEqual(t["w"], 0.4)
        self.assertTrue(s["definitive"])
        self.assertEqual((s["amount"], s["premium"]), (4.2e9, 28.0))
        self.assertGreaterEqual(s["w"], 0.8)

    def test_contract_importance_depends_on_the_size_of_the_company(self):
        txt = ("Lockheed wins $2 billion Navy contract", ["Lockheed Martin was awarded a $2 billion contract by the U.S. Navy to supply missile systems, the Pentagon said on Thursday."])
        small = fact(lens.analyze(*txt, THU, market_cap=30e9), "contract")
        huge = fact(lens.analyze(*txt, THU, market_cap=2e12), "contract")
        self.assertAlmostEqual(small["pct_of_cap"], 6.667, places=2)
        self.assertGreater(small["mag"], huge["mag"] * 3)
        self.assertGreater(small["pol"], huge["pol"])

    def test_regulatory_outcomes(self):
        no = lens.analyze("Epsilon shares plunge", ["Epsilon Bio said the U.S. Food and Drug Administration issued a complete response letter for its lead drug, delaying its launch by at least a year."], THU)
        yes = lens.analyze("Zeta wins approval", ["The FDA approved Zeta Pharma's once-weekly diabetes treatment on Friday. The company said the trial met its primary endpoint."], THU)
        self.assertLess(no["direction"], -0.5)
        self.assertGreater(yes["direction"], 0.5)

    def test_analyst_actions_are_modest_and_a_price_target_is_not_company_guidance(self):
        r = lens.analyze("Analyst downgrades Omega", ["Morgan Stanley downgraded Omega Corp to underweight from equal-weight on Monday and cut its price target to $40 from $55, citing slowing growth."], THU)
        self.assertEqual(sorted(set(kinds(r))), ["downgrade", "target_cut"])                         # REGRESSION: used to add a phantom 'guidance_cut'
        self.assertGreater(r["direction"], -0.6)

    def test_buybacks_and_dividends(self):
        r = lens.analyze("Phi announces buyback", ["Phi Corp on Thursday announced a $3 billion share repurchase program and raised its quarterly dividend by 12%."], THU)
        self.assertIn("buyback", kinds(r))
        self.assertIn("dividend_raise", kinds(r))
        c = lens.analyze("Chi cuts dividend", ["Chi Corp slashed its quarterly dividend by half on Tuesday to preserve cash."], THU)
        self.assertIn("dividend_cut", kinds(c))
        self.assertLess(c["direction"], -0.3)


class TestNumbersAreReadCorrectly(unittest.TestCase):
    def eps(self, text):
        r = lens.analyze("t", [text], None)
        return next((f for f in r["facts"] if f["kind"] == "eps_surprise"), None), r

    def test_losses_are_negative_numbers(self):
        f, r = self.eps("The company posted a loss of $0.30 per share, narrower than the loss of $0.45 per share analysts had expected.")
        self.assertEqual((f["actual"], f["estimate"]), (-0.30, -0.45))
        self.assertGreater(r["direction"], 0.4)                                                     # REGRESSION: a narrower loss used to score as a MISS
        f, r = self.eps("The company posted a loss of $0.60 per share, wider than the loss of $0.45 per share analysts expected.")
        self.assertLess(r["direction"], -0.4)
        f, r = self.eps("It earned $0.10 per share, versus a loss of $0.20 per share expected by analysts.")
        self.assertGreater(r["direction"], 0.4)
        f, r = self.eps("It reported a loss of $0.15 per share, compared with expectations for a profit of $0.05 per share.")
        self.assertLess(r["direction"], -0.4)

    def test_money_formats(self):
        self.assertEqual([round(v) for v, a, b in lens.money_values("$1.2 billion, $950M, US$3.5bn, €2 million, $2.31 a share, $1,250,000")], [1_200_000_000, 950_000_000, 3_500_000_000, 2_000_000, 2, 1_250_000])

    def test_sentences_do_not_split_on_abbreviations_or_decimals(self):
        s = lens.sentences("Acme Inc. earned $2.31 a share in the U.S. market. J. Smith, the CEO, said it was strong. Mr. Jones agreed.")
        self.assertEqual(len(s), 3)
        self.assertTrue(s[0].endswith("market."))

    def test_the_word_forecast_in_guidance_is_not_an_analyst_estimate(self):
        r = lens.analyze("t", ["The company cut its full-year revenue forecast to $5.0 billion from $5.4 billion, saying customers were delaying orders."], None)
        self.assertNotIn("revenue_surprise", kinds(r))                                              # REGRESSION: read as an 8% revenue BEAT
        self.assertIn("guidance_cut", kinds(r))

    def test_cents_a_share(self):
        f, r = self.eps("Ford earned 52 cents a share, beating analysts' forecast of 41 cents, as truck sales jumped.")
        self.assertAlmostEqual(f["surprise"], (0.52 - 0.41) / 0.41, places=2)


class TestContextChangesMeaning(unittest.TestCase):
    def test_it_does_not_invent_facts_from_future_plans_or_price_moves(self):
        for text in ("The company said it expects to report third-quarter earnings on Thursday after the market closes. Analysts expect earnings of $1.20 per share on revenue of $1.6 billion.",
                     "Shares of Apple rose 2% on Tuesday, outpacing the broader market, as investors cheered the launch of a new iPhone."):
            r = lens.analyze("t", [text], THU)
            self.assertEqual([f for f in r["facts"] if f["kind"] not in ("deal", "product_launch")], [], text)

    def test_hedged_statements_count_for_much_less(self):
        firm = lens.analyze("t", ["The company raised its full-year outlook on Tuesday."], None)
        hedged = lens.analyze("t", ["The company may raise its full-year outlook later this year if demand holds up, executives said."], None)
        self.assertGreater(fact(firm, "guidance_raise")["w"], fact(hedged, "guidance_raise")["w"] * 2)

    def test_a_hedge_elsewhere_in_the_sentence_does_not_discount_an_unrelated_fact(self):
        r = lens.analyze("t", ["Beta Inc reported quarterly earnings of $1.40 per share, topping the $1.25 estimate, but warned that sales would slow next quarter."], None)
        self.assertGreaterEqual(fact(r, "eps_surprise")["w"], 0.9)                                  # REGRESSION: 'would' halved it

    def test_a_real_hedge_far_from_the_fact_does_not_discount_it_but_one_next_to_it_does(self):
        far = lens.analyze("t", ["Beta Inc reported quarterly earnings of $1.40 per share, topping the $1.25 estimate, and the company said sales could slow materially in the coming quarter if customers delay orders."], None)
        near = lens.analyze("t", ["Beta Inc could report quarterly earnings of $1.40 per share, topping the $1.25 estimate, if sales hold up."], None)
        self.assertGreaterEqual(fact(far, "eps_surprise")["w"], 0.9)                                 # the 'could ... if' is about next quarter, not about the reported number
        self.assertLessEqual(fact(near, "eps_surprise")["w"], 0.45)                                  # here the hedge is about the fact itself

    def test_would_in_reported_speech_is_not_a_hedge(self):
        r = lens.analyze("t", ["Acme said on Tuesday it would raise its quarterly dividend by 10%."], None)
        self.assertGreaterEqual(fact(r, "dividend_raise")["w"], 0.7)

    def test_negation_flips_and_denial_does_not_turn_a_probe_into_good_news(self):
        beat = lens.analyze("t", ["The company did not miss estimates; it reported earnings of $2.00 per share versus the $1.90 consensus."], None)
        self.assertGreater(beat["direction"], 0.2)
        probe = lens.analyze("Sigma denies wrongdoing as regulators open probe", ["Sigma Corp said it has done nothing wrong after the Justice Department opened an investigation into its sales practices.",
                                                                                   "The company denies the allegations and said there was no evidence of fraud."], THU)
        inv = [f for f in probe["facts"] if f["kind"] == "investigation"]
        self.assertTrue(inv)
        self.assertTrue(all(f["pol"] < 0 for f in inv))                                              # REGRESSION: 'denies' made the probe POSITIVE
        self.assertLess(probe["direction"], -0.25)

    def test_a_resolved_lawsuit_is_mildly_good_news(self):
        r = lens.analyze("t", ["A federal judge dismissed the class-action lawsuit against Zeta Corp on Friday, ending a two-year legal fight."], None)
        self.assertGreater(r["direction"], 0.0)

    def test_historical_mentions_are_discounted(self):
        now = lens.analyze("t", ["The company cut its dividend on Tuesday."], None)
        past = lens.analyze("t", ["The company said last year it would cut its dividend; this quarter it kept the payout steady."], None)
        self.assertGreater(abs(fact(now, "dividend_cut")["pol"] * fact(now, "dividend_cut")["w"]), 2 * abs(fact(past, "dividend_cut")["pol"] * fact(past, "dividend_cut")["w"]))

    def test_revenue_up_but_below_expectations_is_net_negative(self):
        r = lens.analyze("t", ["Revenue rose 12% in the quarter to $4.1 billion, below the $4.3 billion analysts expected, as demand for its premium products softened."], None)
        self.assertLess(r["direction"], 0.0)

    def test_the_same_claim_repeated_is_corroboration_not_new_evidence(self):
        once = lens.analyze("t", ["The FDA approved Zeta's drug on Friday."], None)
        five = lens.analyze("t", ["The FDA approved Zeta's drug on Friday."] * 1 + ["Zeta's drug was approved by the FDA, the agency said."] * 1 + ["The FDA approved the treatment, a win for Zeta."] * 3, None)
        self.assertLess(five["direction"], once["direction"] * 1.35)                                 # REGRESSION: headline + body echoes inflated certainty


class TestFalseAlarmsFoundInRealNews(unittest.TestCase):
    """Found by running the engine over ~2,400 real headlines and snippets from the owner's database."""

    def facts(self, text, title="t"):
        return [f["kind"] for f in lens.analyze(title, [text], None)["facts"] if f["kind"] != "deal"]

    def test_a_military_strike_is_not_a_labour_stoppage(self):
        for text in ("US military says strike on alleged drug vessel in eastern Pacific kills five people.", "An air strike hit the port overnight, officials said.", "A drone strike damaged the refinery, the ministry said.",
                     "The option has a strike price of $50 and expires in March.", "Israel carried out a strike on a convoy near the border."):
            self.assertNotIn("strike_outage", self.facts(text), text)
        for text in ("Workers at the Boeing plant voted to go on strike on Monday.", "The union said its members would walk out at midnight.", "A fire at the factory halted production for two weeks.", "A power outage shut the data centre."):
            self.assertIn("strike_outage", self.facts(text), text)

    def test_a_software_or_energy_upgrade_is_not_an_analyst_rating(self):
        for text in ("Apple charges $14,549 for a fully specced touchscreen MacBook Pro.", "Customers can upgrade their plan to unlock the new features.", "The grid upgrade will cost $2 billion over five years."):
            self.assertNotIn("upgrade", self.facts(text), text)
        self.assertIn("upgrade", self.facts("Morgan Stanley upgraded Apple to overweight from equal-weight on Monday."))
        self.assertIn("upgrade", self.facts("The analyst upgraded the stock to buy, citing strong demand."))
        self.assertIn("downgrade", self.facts("Goldman downgraded the shares to sell."))

    def test_settling_a_lawsuit_is_not_a_new_lawsuit(self):
        r = lens.analyze("Zillow agrees to pay $212.5M to settle investor lawsuit over failed home-flipping business", [], None)
        self.assertTrue(all(f["pol"] >= 0 for f in r["facts"] if f["kind"] == "lawsuit"))
        self.assertGreaterEqual(r["direction"], -0.05)
        new = lens.analyze("Acme hit with class action lawsuit over its data practices", [], None)
        self.assertLess(new["direction"], -0.1)


class TestOpinionNeedsStrongEvidence(unittest.TestCase):
    """Found on the owner's live data: 2 of the 7 'opinion / list article' cancellations were real ideas (an AWS earnings story, a Kimco FFO beat). A single stray word was enough: 'zacks rank',
    'motley fool', 'reasons to', 'opinion', 'column'. Now opinion needs a label / listicle / advice-style title, an opinion URL section, or two DIFFERENT first-person cues."""

    KIM = ("Kimco Realty (KIM) Surpasses Q3 FFO Estimates", [
        "Kimco Realty (KIM) came out with quarterly funds from operations (FFO) of $0.42 per share, beating the Zacks Consensus Estimate of $0.41 per share. This compares to FFO of $0.40 per share a year ago.",
        "These figures are adjusted for non-recurring items. This quarterly report represents an earnings surprise of +2.44%. A quarter ago, it was expected that this real estate investment trust would post FFO of $0.41 per share when it actually produced FFO of $0.43.",
        "Over the last four quarters, the company has surpassed consensus FFO estimates four times. Kimco Realty shares have added about 8% since the beginning of the year versus the S&P 500's gain of 15%.",
        "Kimco currently has a Zacks Rank #3 (Hold), which means it is expected to perform in line with the market in the near future. Here's why investors should keep an eye on the stock."])
    AWS = ("AWS will be a $1T business, Jassy says \u2014 but first, it will spend big", [
        "Amazon Web Services revenue increased 39 percent from the year-ago quarter, a trend of steady growth for the sixth straight quarter, Amazon reported on Thursday.",
        "In his opinion, Amazon CEO Andy Jassy said the cloud unit could become a $1 trillion business over time, though capital spending will rise sharply to get there.",
        "The spending column on the balance sheet will grow, and for now investors should expect margins to stay under pressure, analysts said after the report."])

    def test_a_zacks_earnings_recap_is_a_report_and_its_ffo_beat_is_read(self):
        r = lens.analyze(*self.KIM, THU)
        self.assertEqual(r["type"], "report")                                                           # 'Zacks Rank' and "Here's why" in the body no longer make it opinion
        e = fact(r, "eps_surprise")
        self.assertEqual((e["actual"], e["estimate"]), (0.42, 0.41))                                    # FFO is the REIT earnings measure
        self.assertGreater(r["direction"], 0.15)

    def test_hard_news_that_happens_to_contain_the_word_opinion_or_column_is_a_report(self):
        r = lens.analyze(*self.AWS, THU)
        self.assertEqual(r["type"], "report")
        self.assertIn("growth", kinds(r))
        self.assertGreater(r["direction"], 0.0)

    def test_a_publisher_name_or_a_stray_phrase_is_not_evidence(self):
        for text in ("The company reported results. Shares were featured by the Motley Fool on Tuesday.", "There are three reasons to expect margins to recover, executives told analysts.",
                     "The analysis: revenue rose 8% as the firm reported on Monday.", "\"I think we can beat that next quarter,\" the chief executive said on a call."):
            self.assertEqual(lens.classify_type("Acme reports quarterly results", text), "report", text)

    def test_the_same_cue_twice_is_one_cue_not_two(self):
        self.assertEqual(lens.classify_type("Acme results", "\"I think we can do it,\" said the CEO. Later he added: \"I think margins will recover.\""), "report")
        self.assertEqual(lens.classify_type("Acme results", "\"I think we can do it,\" said the CEO. \"We believe margins will recover,\" he added."), "opinion")

    def test_ffo_is_read_as_the_reits_earnings_even_without_the_words_per_share(self):
        r = lens.analyze("Kimco reports", ["Kimco reported FFO of $0.42 versus the consensus estimate of $0.41 for the third quarter."], THU)
        e = fact(r, "eps_surprise")
        self.assertEqual((e["actual"], e["estimate"]), (0.42, 0.41))
        r2 = lens.analyze("Prologis reports", ["Prologis posted core FFO of $1.40, below the consensus estimate of $1.50, and lowered its outlook."], THU)
        self.assertLess(r2["direction"], -0.3)

    def test_real_opinion_is_still_opinion(self):
        self.assertEqual(lens.classify_type("Opinion: why the Fed is wrong", "Rates should fall."), "opinion")                            # an explicit label
        self.assertEqual(lens.classify_type("Here's why I'd buy Nvidia before earnings", "Nvidia reported strong demand."), "opinion")      # an advice-style title
        self.assertEqual(lens.classify_type("5 reasons to buy this stock now", "Text."), "opinion")                                         # a listicle
        self.assertEqual(lens.classify_type("Is Apple stock a buy?", "Is it a buy at today's price? Should you buy it now?"), "opinion")
        self.assertEqual(lens.classify_type("Rates will fall", "Text about rates.", "https://www.theguardian.com/commentisfree/2026/oct/30/rates"), "opinion")   # an opinion section in the URL
        self.assertEqual(lens.classify_type("Acme outlook", "I think the stock is cheap. In my view the market is wrong about the outlook."), "opinion")         # two different first-person cues
        self.assertEqual(lens.classify_type("Rates will fall", "Text about rates.", "https://www.reuters.com/markets/rates-2026-10-30/"), "report")

    def test_a_press_release_with_opinion_words_stays_a_release(self):
        self.assertEqual(lens.classify_type("Acme announces results", "SAN JOSE /PRNewswire/ -- Acme today announced record results, in our view a strong quarter. I think so too."), "release")

    def test_the_reader_passes_the_url_so_an_opinion_section_is_recognised(self):
        from paper_agent import reader
        self.assertEqual(reader.lens.analyze("Rates will fall", ["Text about rates and the economy."], None, None, None, url="https://x.com/opinion/a")["type"], "opinion")


class TestWhatKindOfTextIsIt(unittest.TestCase):
    def test_types(self):
        self.assertEqual(lens.analyze("Here's why I'd buy Nvidia stock before earnings", ["In my view, Nvidia is one of the best stocks to buy right now. I think the company will beat estimates again."])["type"], "opinion")
        self.assertEqual(lens.analyze("Sponsored: 5 stocks to buy now", ["Sponsored content. This article contains affiliate links. Not investment advice."])["type"], "promo")
        self.assertEqual(lens.analyze("Upsilon Reports Record Third Quarter Revenue", ["SAN JOSE, Calif., Oct. 29, 2026 /PRNewswire/ -- Upsilon Inc. today announced record revenue of $812 million."])["type"], "release")
        self.assertEqual(lens.analyze("Q3 call", ["Operator: Good morning, everyone, and welcome to the earnings call. Prepared remarks follow."])["type"], "transcript")
        self.assertEqual(lens.analyze("Garmin raises outlook", ["Garmin on Wednesday raised its forecast."])["type"], "report")

    def test_opinion_and_promo_carry_little_weight(self):
        op = lens.analyze("I think Acme will beat", ["In my view the company will beat. Acme reported earnings of $2.00 per share versus the $1.90 consensus last quarter."])
        rep = lens.analyze("Acme beats", ["Acme reported earnings of $2.00 per share versus the $1.90 consensus."])
        self.assertLess(abs(op["direction"]), abs(rep["direction"]) * 0.6)
        self.assertLess(op["confidence"], rep["confidence"])


class TestOldNewsAndWhoItIsAbout(unittest.TestCase):
    def test_a_lead_that_says_it_happened_days_ago_is_stale(self):
        r = lens.analyze("Tau shares slide", ["Tau Corp reported on Monday that quarterly profit rose 12%, and the shares have been under pressure since."], THU)
        self.assertTrue(r["stale"])
        self.assertIn("Monday", r["stale_why"])
        fresh = lens.analyze("Tau", ["Tau Corp reported on Wednesday that quarterly profit rose 12%."], THU)
        self.assertFalse(fresh["stale"])
        self.assertFalse(lens.analyze("Tau", ["Tau Corp will report on Monday."], THU)["stale"])           # a future date is not staleness

    def test_subject_scores_say_what_the_article_is_about(self):
        r = lens.analyze("Microsoft earnings beat as Azure accelerates", [
            "Microsoft reported earnings of $3.30 per share, above the $3.10 analysts expected.", "Microsoft said Azure grew 40%. Microsoft raised its outlook.", "Alphabet's Google Cloud and Amazon's AWS grew more slowly, analysts said."],
            THU, names_by_ticker={"MSFT": ["microsoft"], "GOOGL": ["alphabet", "google"], "AMZN": ["amazon"]})
        s = r["subject"]
        self.assertGreater(s["MSFT"], 6 * max(s["GOOGL"], s["AMZN"]) / 2)
        self.assertLess(s["GOOGL"], 1.5)


class TestItIsSafeAndSmall(unittest.TestCase):
    def test_it_stores_facts_not_articles(self):
        r = lens.analyze(*BEAT_RAISE, THU)
        blob = json.dumps(r)
        self.assertLess(len(blob), 6000)
        self.assertTrue(all(len(f["text"]) <= 220 for f in r["facts"]))
        self.assertLessEqual(len(r["facts"]), 12)
        self.assertNotIn(" ".join(BEAT_RAISE[1])[:300], blob)                                       # the article text is never kept: only short source sentences

    def test_nonsense_in_never_crashes_and_is_fast(self):
        for paras in ([], [""], [None], ["$" * 5000], ["the " * 12000], ["1.2.3.4. " * 3000], ["\u0000\u200b weird \ud7ff" * 500], ["a" * 200_000]):
            t = time.time()
            r = lens.analyze("title", paras, THU)
            self.assertLess(time.time() - t, 2.0)
            self.assertIn("direction", r)
        self.assertEqual(lens.analyze("", None)["facts"], [])

    def test_a_long_article_is_analysed_quickly(self):
        paras = list(BEAT_RAISE[1]) * 6
        t = time.time()
        lens.analyze(BEAT_RAISE[0], paras, THU)
        self.assertLess(time.time() - t, 0.5)


if __name__ == "__main__":
    unittest.main()
