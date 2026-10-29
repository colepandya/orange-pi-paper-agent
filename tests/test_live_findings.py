"""Found by reading the owner's live funnel output after build 26: (1) commentary scored as news ("Is Earnings Beat Altering The Investment Case For Equinix?" +0.78),
(2) BUYs queued by older builds rest on a looser company match and would still execute at the open, (3) a -18% pre-market gap was shown but not called out."""
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_pretrade as TP  # noqa: E402
import test_review_fixes as RF  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import evidence, explain, funnel, nlp, pretrade, report  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

COMMENTARY = ["2 Reasons I'd Buy Marvell Stock More Than 20% Below Its Record, and 1 Reason I'd Wait", "Is Earnings Beat Altering The Investment Case For Equinix (EQIX)?",
              "Not Nvidia. Not AMD. This Is the Best Artificial Intelligence (AI) Stock to Buy in November", "Microsoft: 3 Key Takeaways for Long-Term Investors", "Terex Q3 Earnings Call Highlights",
              "American Century Equity Income Fund Q3 2026 Portfolio Activity", "How Broadcom's stock can surge and overcome 'the biggest dislocation in the market'", "Should You Buy Nvidia Stock Before November?",
              "What Happened to Tesla Stock This Week?", "Is Intel Stock a Buy, Sell, or Hold Right Now?", "5 Top Dividend Stocks to Buy Now", "Why I'd Buy Apple Stock and Hold It Forever", "Will Palantir Stock Hit $200?",
              "7 Things Every Investor Should Know Before Nvidia Reports", "Three Ways Apple Could Surprise Investors This Quarter"]       # pure listicles: only the list pattern can catch these
REAL = ["Equinix Reports Third-Quarter Results and Raises 2026 Guidance", "NVIDIA beats estimates and raises full-year guidance as data center demand surges", "3M Raises Full-Year Outlook After Strong Quarter",
        "Five Below Reports Record Holiday Sales", "Two Harbors Investment Corp. Announces Dividend", "Best Buy Raises Full-Year Outlook", "Microsoft Signs $10 Billion Cloud Deal With OpenAI",
        "Intel Announces New Foundry Customer", "Garmin Q3 Results: Record Revenue, Raises Guidance", "Lockheed Martin Wins $2B Contract", "Alphabet Announces $70 Billion Share Buyback",
        "Amazon Could Buy Viasat As AST SpaceMobile Tries Rival Bid"]


class TestCommentaryIsNotNews(unittest.TestCase):
    def test_commentary_is_neutral_and_gets_its_own_reason(self):
        for h in COMMENTARY:
            a = nlp.analyze(h, "")
            self.assertIn(a["event_type"], nlp.RECAP_TYPES, h)
            self.assertEqual(a["direction"], 0.0, h)
        a = nlp.analyze("Is Earnings Beat Altering The Investment Case For Equinix (EQIX)?", "")
        self.assertEqual(a["event_type"], "opinion_piece")
        self.assertEqual(evidence.candidate_gate(a, ["EQIX"], {"EQIX": 0.9}, 1.0, 1.0, H.cfg()["intelligence"]), "commentary_not_news")
        self.assertIn("not new information", evidence.GATE_LABELS["commentary_not_news"])

    def test_real_news_is_never_mistaken_for_commentary(self):
        self.assertEqual([h for h in REAL if nlp.analyze(h, "")["event_type"] in nlp.RECAP_TYPES], [])
        self.assertGreater(nlp.analyze("Equinix Reports Third-Quarter Results and Raises 2026 Guidance", "")["direction"], 0.6)

    def test_only_a_question_mark_at_the_END_makes_a_question(self):
        self.assertEqual(nlp.analyze("Equinix beats estimates and raises guidance?", "")["event_type"], "opinion_piece")
        self.assertNotEqual(nlp.analyze("Equinix (NASDAQ: EQIX) beats estimates and raises guidance", "")["event_type"], "opinion_piece")


def candidate(ticker, headline, **kw):
    return dict({"ticker": ticker, "headline": headline, "origin": "event"}, **kw)


class TestAQueuedBuyMustStillBeAboutThatCompany(unittest.TestCase):
    def test_a_buy_from_a_looser_older_match_is_cancelled(self):
        u = RF.uni()
        why = pretrade.still_about(candidate("AVGO", "Seagate Reports Record HDD Shipments as AI Storage Demand Surges"), u)
        self.assertIn("the headline is about STX, not AVGO", why)
        self.assertIn("MSFT", pretrade.still_about(candidate("MSFT", "NiSource Signs Data Center Power Agreement With Microsoft"), u) or "")        # the counterparty is not the subject
        self.assertIsNone(pretrade.still_about(candidate("STX", "Seagate Reports Record HDD Shipments as AI Storage Demand Surges"), u))

    def test_things_that_must_not_be_cancelled(self):
        u = RF.uni()
        for c in (candidate("AVGO", "Seagate Reports Record HDD Shipments", mechanism="supplier of Seagate controllers", chain_order=1),      # a reasoned supply-chain candidate
                  candidate("AVGO", "Seagate Reports Record HDD Shipments", origin="thesis"),                                                    # thesis-driven
                  candidate("GRMN", "Q3 Results Beat Expectations"),                                                                            # headline names nobody: cannot tell
                  candidate("AVGO", "")):
            self.assertIsNone(pretrade.still_about(c, u), c)
        self.assertIsNone(pretrade.still_about(candidate("AVGO", "Seagate Reports Record HDD Shipments"), None))

    def test_at_the_open_a_wrong_company_buy_is_vetoed_and_a_right_one_executes(self):
        w = TP.Scenario()
        w.setUp()
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            d = w.c.execute("SELECT id,reason FROM decisions WHERE ticker='NVDA' ORDER BY id LIMIT 1").fetchone()
            r = json.loads(d["reason"])
            r["candidate"]["headline"] = "Microsoft earnings beat estimates as Azure growth accelerates"          # what an older build stored: the story is about MSFT, the BUY is on NVDA
            w.c.execute("UPDATE decisions SET reason=? WHERE id=?", (json.dumps(r), d["id"]))
            w.cyc(TL.T_OPEN)
            st = w.c.execute("SELECT status FROM decisions WHERE id=?", (d["id"],)).fetchone()[0]
            self.assertEqual((st, w.positions()), ("VETOED", 0))
            self.assertIn("the headline is about MSFT, not NVDA", json.loads(w.c.execute("SELECT reason FROM decisions WHERE id=?", (d["id"],)).fetchone()[0])["gates"][-1])
            ex = explain.explain(w.c, w.cfg, d["id"], TL.T_OPEN)
            self.assertIn("older, looser company match", ex["headline"])
        finally:
            w.tearDown()
        w = TP.Scenario()
        w.setUp()
        try:
            w.day()
            self.assertEqual(w.positions(), 1)                                                                  # the same flow with an honest headline still trades
        finally:
            w.tearDown()


class TestPreMarketGapsAreCalledOut(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 27, 9, 0, tzinfo=dt.timezone.utc)

    def setup(self, price, **risk):
        c = H.fresh_db()
        cfg = H.cfg()
        cfg["risk"].update(max_gap_down_pct=0.03, max_gap_up_pct=0.05)                                           # the SHIPPED thresholds (the shared test config switches the guard off)
        cfg["risk"].update(risk)
        c.execute("INSERT INTO decisions(created,ticker,action,status,ev_net,reason) VALUES(?,?,?,?,?,?)", (iso(self.NOW - dt.timedelta(hours=3)), "NOW", "BUY", "AWAITING_MARKET", .012,
                  json.dumps({"gates": ["market closed"], "ref_price": 100.0})))
        c.execute("INSERT INTO quote_cache(ticker,price,source,quoted_at,updated_epoch) VALUES('NOW',?,'YAHOO_EXT:PRE_MARKET',?,0)", (price, iso(self.NOW - dt.timedelta(minutes=8))))
        return c, cfg

    def test_a_big_gap_is_flagged_with_what_will_happen(self):
        c, cfg = self.setup(81.6)                                                                               # -18.4%, as in the live funnel
        text = funnel.text(funnel.report(c, 6, self.NOW, cfg=cfg))
        self.assertIn("trading -18.4% vs previous close, pre-market, 8 min ago -> WILL BE CANCELLED at the open if it holds (gap down)", text)
        c, cfg = self.setup(108.0)
        self.assertIn("(the move already happened)", funnel.text(funnel.report(c, 6, self.NOW, cfg=cfg)))

    def test_a_normal_gap_is_not_flagged_and_it_follows_the_owners_thresholds(self):
        c, cfg = self.setup(99.0)
        self.assertNotIn("CANCELLED", funnel.text(funnel.report(c, 6, self.NOW, cfg=cfg)))
        c, cfg = self.setup(90.0, max_gap_down_pct=0.15)                                                        # the owner tolerates a 15% gap down
        self.assertNotIn("CANCELLED", funnel.text(funnel.report(c, 6, self.NOW, cfg=cfg)))
        c, cfg = self.setup(90.0)
        self.assertIn("CANCELLED", funnel.text(funnel.report(c, 6, self.NOW, cfg=cfg)))

    def test_the_morning_report_says_it_too(self):
        c, cfg = self.setup(81.6)
        c.execute("INSERT INTO cycle_runs(started,finished,status,mode) VALUES(?,?,?,?)", (iso(self.NOW - dt.timedelta(minutes=3)), iso(self.NOW), "OK", "full"))
        import test_preflight as TPF
        cfg2 = TPF.cfg_real()
        cfg2["runtime"]["bind_port"] = 1
        cfg2["risk"].update(max_gap_down_pct=0.03, max_gap_up_pct=0.05)
        rep = report.build(c, cfg2, TPF.big_uni(), [], self.NOW, live=False, check_units=False)
        self.assertIn("WILL BE CANCELLED at the open", rep["text"])


if __name__ == "__main__":
    unittest.main()
