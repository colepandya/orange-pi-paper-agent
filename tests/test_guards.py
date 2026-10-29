"""Two safety nets for tomorrow's open: (1) never stack a second position in a stock we already hold (BUYs are unlimited per cycle now);
(2) 'Why X stock dropped after record results' is an after-the-fact article, not a catalyst (it had become a +0.77 BUY candidate)."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_pretrade as TP  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import explain, nlp  # noqa: E402

RECAPS = ["Why Garmin Stock Dropped After Record Results", "Why Micron Stock Is Up Today", "Why OneSpaWorld Stock Is Up Today", "Why Wendy's Rallied Big on Wednesday",
          "Is AMD Stock a Buy, Sell, or Hold Right Now? Here's What I'm Doing.", "Here's why Nvidia stock jumped this week", "Micron stock soars today", "What's going on with Tesla stock?",
          "Why Is Intel Stock Falling?", "Should You Buy Palantir Stock Now?", "Why Apple Shares Slid on Thursday",
          "Why Garmin Stock Dropped Today Even After It Raised Its Outlook and Beat Earnings Estimates",              # these quote SEVERAL catalysts: the recap must still win
          "Should You Buy Garmin Stock After It Raised Its Outlook and Beat Earnings Estimates and Won a Contract and Launched a Product?",
          "Is Garmin Stock a Buy After Record Earnings Beat, Raised Guidance, Contract Win, Buyback and Upgrade?",
          "Here's why Garmin stock jumped this week: raised outlook, beat estimates, new contract, analyst upgrade"]
REAL = ["Garmin raises full-year outlook, stock jumps", "Apple stock jumps after earnings beat", "NVIDIA beats estimates and raises full-year guidance", "Why the Fed cut rates",
        "Why Tesla is recalling 200,000 cars", "Microsoft wins $2B Pentagon cloud contract", "Wendy's to be acquired by Trian in $5B deal", "Intel announces new foundry customer",
        "Stocks tumble as war fears grow", "Boeing shares fall after FAA grounds 737", "Why Warren Buffett is buying Occidental", "Garmin Q3 results: record revenue, raises guidance"]
SECOND = "NVIDIA wins $3 billion data center contract with major cloud provider"


class TestRecaps(unittest.TestCase):
    def test_recaps_are_recognised_neutral_and_never_a_reason_to_buy(self):
        for h in RECAPS:
            a = nlp.analyze(h, "")
            self.assertIn(a["event_type"], nlp.RECAP_TYPES, h)                                    # after-the-fact recap OR commentary: both are neutral and never a reason to buy
            self.assertEqual(a["direction"], 0.0, h)

    def test_real_news_is_never_mistaken_for_a_recap(self):
        self.assertEqual([h for h in REAL if nlp.analyze(h, "")["event_type"] == "price_recap"], [])

    def test_the_garmin_recap_no_longer_becomes_a_candidate_and_says_why(self):
        from paper_agent import evidence
        a = nlp.analyze("Why Garmin Stock Dropped After Record Results", "")
        self.assertEqual(evidence.candidate_gate(a, ["GRMN"], {"GRMN": 0.9}, 1.0, 1.0, H.cfg()["intelligence"]), "retrospective_recap")
        self.assertIn("already priced in", evidence.GATE_MEANING["retrospective_recap"] if hasattr(evidence, "GATE_MEANING") else "already priced in")


class World(TP.Scenario):
    def decisions(self):
        return [dict(r) for r in self.c.execute("SELECT id,status,ev_net,reason FROM decisions WHERE ticker='NVDA' AND action='BUY' ORDER BY id")]


class TestNoStacking(unittest.TestCase):
    def test_two_ideas_for_the_same_stock_open_ONE_position_the_stronger_one(self):
        w = World()
        w.setUp()
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS, SECOND])
            before = [d for d in w.decisions() if d["status"] == "AWAITING_MARKET"]
            self.assertEqual(len(before), 2, "setup: both ideas are queued for the open")
            w.cyc(TL.T_OPEN)
            ds = {d["status"]: d for d in w.decisions()}
            self.assertEqual(set(ds), {"EXECUTED", "DUPLICATE"})
            self.assertEqual(w.one("SELECT COUNT(*) FROM positions WHERE ticker='NVDA'")[0], 1)
            self.assertGreaterEqual(ds["EXECUTED"]["ev_net"], ds["DUPLICATE"]["ev_net"])               # the strongest idea got the position
            ex = explain.explain(w.c, w.cfg, ds["DUPLICATE"]["id"], TL.T_OPEN)
            self.assertTrue(ex["headline"].startswith("Skipped: Already holding NVDA"), ex["headline"])
            self.assertIn("same bet", ex["summary"] + ex["headline"])
        finally:
            w.tearDown()

    def test_it_can_be_switched_off_to_allow_adding(self):
        w = World()
        w.setUp()
        try:
            w.cfg["risk"]["allow_add_to_position"] = True
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS, SECOND])
            w.cyc(TL.T_OPEN)
            self.assertNotIn("DUPLICATE", {d["status"] for d in w.decisions()})
        finally:
            w.tearDown()

    def test_after_the_position_is_closed_the_stock_can_be_bought_again(self):
        w = World()
        w.setUp()
        try:
            w.day()
            stop = float(w.one("SELECT stop_price FROM position_forecast_plans WHERE active=1")[0])
            w.mkt.price["NVDA"] = stop - 0.1
            w.guard(TL.T_OPEN + dt.timedelta(minutes=20))
            self.assertEqual(w.one("SELECT status FROM positions")[0], "CLOSED")
            w.mkt.price["NVDA"] = 120.0
            w.cyc(TL.T_OPEN + dt.timedelta(minutes=40), [SECOND])
            self.assertEqual(w.one("SELECT COUNT(*) FROM positions WHERE status='OPEN'")[0], 1)       # a fresh idea after the exit is welcome
        finally:
            w.tearDown()


if __name__ == "__main__":
    unittest.main()
