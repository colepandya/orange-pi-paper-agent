"""Build 37 (strategy S2). Found by reviewing the owner's real Friday 30 Oct open, where 8 queued BUYs (edge +2.3% to +5.2%) were held until they expired.
SPY really was down 1.37% that day, so the market gate worked. What was wrong was WHAT counted as the shock and HOW it was corroborated:
  * a crypto hack whose feed summary said 'halts withdrawals' was classified as a bank run (financial_stress_event, -0.85);
  * an opinion column on Brexit and a West Bank raids story were classified as war escalation (the summary was read, not just the headline);
  * 'two outlets' meant two feeds, not two outlets (CNBC Top News and CNBC World), and not the same story (a Ukraine blackout from CNBC plus a crypto heist from Al Jazeera 'corroborated' each other).
This replays the real timeline: real headlines, real outlets, real times, SPY's real move."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from test_open_blockers import at, event, spy  # noqa: E402
from paper_agent import nlp, pretrade, stamp, whatif  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

FRI_OPEN = at(2026, 10, 30, 13, 45)                          # a few minutes after the open
# the shock-type stories the owner's database held, with the outlet that published each (UTC, 29-30 Oct). Headlines the log cut off are completed in the obvious way.
STORIES = [
    ("BBC World News", at(2026, 10, 29, 13, 40), "Russian drone attack on Ukrainian train station kills at least 13, officials say"),
    ("BBC World News", at(2026, 10, 29, 14, 19), "Russian drone attack on Ukrainian train station kills at least 14, officials say"),
    ("Al Jazeera", at(2026, 10, 29, 18, 25), "At least 14 people killed in attack on Ukraine train station"),
    ("Al Jazeera", at(2026, 10, 29, 20, 57), "Palestinians injured, arrested in Israeli raids across West Bank"),
    ("The Guardian Business", at(2026, 10, 29, 22, 29), "The Guardian view on a changing Brexit debate: Kemi Badenoch is trapped in denial"),
    ("Al Jazeera", at(2026, 10, 30, 0, 40), "Houthi forces claim six attacks on Saudi airports, oil facilities"),
    ("Al Jazeera", at(2026, 10, 30, 9, 43), "Russian oil refinery struck as Ukraine renews drone attacks"),
    ("CNBC World", at(2026, 10, 30, 11, 3), "Tech scare trade hits Nasdaq futures while crypto reels from its biggest-ever sell-off"),
    ("CNBC Top News", at(2026, 10, 30, 11, 33), "Russian strikes plunge Ukrainian city into blackout as Zelenskyy warns of more"),
    ("CNBC World", at(2026, 10, 30, 11, 46), "Russian strikes plunge Ukrainian city into blackout as Zelenskyy warns of more"),
    ("Al Jazeera", at(2026, 10, 30, 12, 54), "Binance says $6.3bn stolen in biggest crypto heist in history: What we know"),
]
# how the database had CLASSIFIED them (headline + feed summary): all macro, all strongly negative
STORED_TYPE = ["war_escalation", "war_escalation", "war_escalation", "war_escalation", "war_escalation", "war_escalation", "war_escalation", "market_crash", "war_escalation", "war_escalation", "financial_stress_event"]


def friday(spy_move=-0.0137, drop_stories=()):
    c = H.fresh_db()
    for i, ((src, when, head), kind) in enumerate(zip(STORIES, STORED_TYPE)):
        if i not in drop_stories:
            event(c, kind, "macro", -0.85, 0.85, src, iso(when), cred=0.75, headline=head)
    c.execute("INSERT OR REPLACE INTO market_series(symbol,day,open,high,low,close,volume) VALUES('SPY','2026-10-29',769.36,769.36,769.36,769.36,1)")
    c.execute("INSERT INTO quote_consensus(ticker,created,session,status,consensus_price,provider_count,max_deviation) VALUES('SPY',?,'REGULAR','PASS',?,2,0.0)", (iso(FRI_OPEN - dt.timedelta(minutes=4)), 769.36 * (1 + spy_move)))
    return c


def shock(c, decided, **risk):
    cfg = H.cfg()
    cfg["risk"].update(shock_hold_hours=6, shock_direction=-0.6, **risk)
    return pretrade.market_shock(c, iso(decided), FRI_OPEN, cfg)


class TestTheClassifierNoLongerReadsAHeistAsABankRun(unittest.TestCase):
    def test_halting_withdrawals_is_a_bank_run_only_for_a_bank(self):
        for t in ("Brokerage halts withdrawals for UK customers as regulators step in", "Bank halts withdrawals after deposit run", "Money market fund suspends withdrawals as redemptions surge",
                  "Regional bank collapses after deposit run, regulators seize it"):
            self.assertEqual(nlp.analyze(t, "")["event_type"], "financial_stress_event", t)
        for t, summ in (("Binance says $6.3bn stolen in biggest crypto heist in history", "Binance halts withdrawals as it investigates the theft of $6.3bn from its hot wallets."),
                        ("Coinbase halts withdrawals for ether after network outage", ""), ("Bitcoin exchange suspends withdrawals after hack", "")):
            self.assertNotEqual(nlp.analyze(t, summ)["event_type"], "financial_stress_event", t)                 # a crypto exchange is not a bank


class TestWhatCountsAsCorroboration(unittest.TestCase):
    def test_two_feeds_of_one_publisher_are_one_outlet(self):
        for a, b in (("CNBC Top News", "CNBC World"), ("BBC World News", "BBC Business"), ("The Guardian World", "The Guardian Business"), ("NPR News", "NPR Economy")):
            self.assertEqual(pretrade.publisher(a), pretrade.publisher(b), (a, b))
        self.assertNotEqual(pretrade.publisher("Al Jazeera"), pretrade.publisher("CNBC World"))
        self.assertEqual(pretrade.publisher("Wire2"), "wire2")                                                  # a feed with no category suffix is left alone

    def test_it_is_the_headline_that_has_to_name_the_shock(self):
        c = friday()
        decided = at(2026, 10, 30, 11, 33)                                                                      # CBOE: decided at 11:33, held for a 'shock' that was a blackout story plus a heist
        self.assertIsNone(shock(c, decided))                                                                    # after the fix: nothing named a shock after 11:33
        stored = [r["event_type"] for r in c.execute("SELECT event_type FROM events WHERE created>?", (iso(decided),))]
        self.assertIn("financial_stress_event", stored)                                                          # ...although the database had labelled the Binance story a shock (summary-driven)

    def test_the_stories_that_were_never_market_shocks_are_not(self):
        for i in (2, 3, 4, 8, 9, 10):                                                                           # Al Jazeera train station, West Bank, Brexit column, blackout x2, Binance
            self.assertNotIn(nlp.analyze(STORIES[i][2], "")["event_type"], nlp.SHOCK_TYPES, STORIES[i][2])
        for i in (0, 5, 6, 7):                                                                                  # drone attack, Houthi oil attacks, refinery strike, Nasdaq tech scare: real risk-off headlines
            self.assertIn(nlp.analyze(STORIES[i][2], "")["event_type"], nlp.SHOCK_TYPES, STORIES[i][2])


class TestFridayReplayedAgainstTheRealTimeline(unittest.TestCase):
    """The decisions the owner's database holds, at their real decision times, against the real stories and SPY's real -1.37%."""

    def test_the_two_stories_that_decided_the_owners_holds_no_longer_do(self):
        c = friday()
        decided = {"RIVN (Thu 20:08)": at(2026, 10, 29, 20, 8), "FHI (Thu 22:36)": at(2026, 10, 29, 22, 36), "RIVN (Fri 01:04)": at(2026, 10, 30, 1, 4), "CBOE (Fri 11:33)": at(2026, 10, 30, 11, 33),
                   "SPG (Fri 12:04)": at(2026, 10, 30, 12, 4), "NBHC (Fri 12:35)": at(2026, 10, 30, 12, 35)}
        out = {k: shock(c, d) for k, d in decided.items()}
        for k in ("CBOE (Fri 11:33)", "SPG (Fri 12:04)", "NBHC (Fri 12:35)"):
            self.assertIsNone(out[k], k)                                                                         # decided after the last story that names a shock: nothing to react to
        for k in ("RIVN (Thu 20:08)", "FHI (Thu 22:36)", "RIVN (Fri 01:04)"):
            s = out[k]
            self.assertIsNotNone(s, k)                                                                           # a genuine risk-off morning: Houthi oil attacks + refinery strike (Al Jazeera) + tech scare (CNBC)...
            self.assertEqual(s["outlets"], ["al jazeera", "cnbc"], k)                                            # ...from two distinct outlets, with the market confirming
            self.assertAlmostEqual(s["spy_move"], -0.0137, places=4)
            self.assertNotIn("Binance", s["headline"])
            self.assertNotIn("blackout", s["headline"])

    def test_the_market_has_to_agree_exactly_as_before(self):
        for move, held in ((-0.0137, True), (-0.0099, False), (-0.0038, False), (0.004, False)):
            self.assertEqual(shock(friday(spy_move=move), at(2026, 10, 29, 20, 8)) is not None, held, move)

    def test_one_outlet_is_still_not_corroboration_even_with_several_stories(self):
        c = friday(drop_stories=(5, 6))                                                                          # remove the Houthi and refinery stories: only CNBC's tech scare remains
        self.assertIsNone(shock(c, at(2026, 10, 29, 20, 8)))
        c2 = H.fresh_db()
        for src, when in (("CNBC Top News", at(2026, 10, 30, 11, 3)), ("CNBC World", at(2026, 10, 30, 11, 5))):      # two feeds, one publisher
            event(c2, "market_crash", "macro", -0.85, 0.85, src, iso(when), headline="Dow plunges 1,500 points as global stock market crash deepens")
        c2.execute("INSERT OR REPLACE INTO market_series(symbol,day,open,high,low,close,volume) VALUES('SPY','2026-10-29',100,100,100,100,1)")
        c2.execute("INSERT INTO quote_consensus(ticker,created,session,status,consensus_price,provider_count,max_deviation) VALUES('SPY',?,'REGULAR','PASS',98.0,2,0.0)", (iso(FRI_OPEN - dt.timedelta(minutes=4)),))
        self.assertIsNone(shock(c2, at(2026, 10, 30, 8, 0)))
        event(c2, "market_crash", "macro", -0.85, 0.85, "Reuters", iso(at(2026, 10, 30, 11, 20)), headline="Dow plunges 1,500 points as global stock market crash deepens")
        self.assertEqual(shock(c2, at(2026, 10, 30, 8, 0))["outlets"], ["cnbc", "reuters"])

    def test_the_hold_text_says_what_it_saw(self):
        c = friday()
        cfg = H.cfg()
        cfg["risk"].update(shock_hold_hours=6, shock_direction=-0.6)
        r = pretrade.review(c, cfg, {"ticker": "RIVN", "event_id": None, "headline": "x"}, iso(at(2026, 10, 29, 20, 8)), FRI_OPEN)
        self.assertEqual(r["action"], "hold")
        self.assertIn("reported by al jazeera, cnbc", r["reason"])
        self.assertIn("SPY is -1.37% versus its previous close", r["reason"])
        self.assertTrue(r["reason"].startswith("HELD: market-wide shock since this BUY was decided ("))        # the explain page keys on this prefix


class TestWhichBuysAShockHolds(unittest.TestCase):
    """The design question the review exposed: SPG, NBHC and CBOE were decided AFTER the risk-off stories and traded into the same falling market (SPG and NBHC lost money); RIVN and FHI, decided
    before, were held. Default unchanged (after_decision); all_buys holds every queued BUY while a confirmed shock is live."""

    def verdict(self, mode, decided):
        c = friday()
        cfg = H.cfg()
        cfg["risk"].update(shock_hold_hours=6, shock_direction=-0.6, shock_applies_to=mode)
        return pretrade.review(c, cfg, {"ticker": "SPG", "event_id": None, "headline": "x"}, iso(decided), FRI_OPEN)

    def test_the_default_is_the_old_behaviour(self):
        self.assertEqual(H.cfg()["risk"]["shock_applies_to"], "after_decision")
        self.assertEqual(self.verdict("after_decision", at(2026, 10, 30, 12, 4))["action"], "go")
        self.assertEqual(self.verdict("after_decision", at(2026, 10, 29, 20, 8))["action"], "hold")

    def test_all_buys_treats_equally_good_ideas_equally(self):
        for when in (at(2026, 10, 29, 20, 8), at(2026, 10, 30, 11, 33), at(2026, 10, 30, 12, 4), at(2026, 10, 30, 12, 35)):
            r = self.verdict("all_buys", when)
            self.assertEqual(r["action"], "hold", when)
            self.assertIn("while this BUY waits", r["reason"])
        c = friday(spy_move=-0.004)                                                                          # the market gate still applies: a calm market holds nothing
        cfg = H.cfg()
        cfg["risk"].update(shock_hold_hours=6, shock_direction=-0.6, shock_applies_to="all_buys")
        self.assertEqual(pretrade.review(c, cfg, {"ticker": "SPG", "event_id": None, "headline": "x"}, iso(at(2026, 10, 30, 12, 4)), FRI_OPEN)["action"], "go")

    def test_the_setting_is_a_validated_choice(self):
        from paper_agent import settings as S
        f = next(x for x in S.FIELDS if x.path == "risk.shock_applies_to")
        self.assertEqual((S._parse(f, "all_buys"), S._parse(f, "after_decision")), ("all_buys", "after_decision"))
        for bad in ("", "everyone", "ALL_BUYS"):
            with self.assertRaises(ValueError):
                S._parse(f, bad)


class TestTheMeasurementAndTheVersion(unittest.TestCase):
    def test_what_if_groups_shock_holds_as_their_own_family_so_their_cost_can_be_read(self):
        g = ('HELD: market-wide shock since this BUY was decided (financial_stress_event, direction -0.85): "Binance says $6.3bn stolen" (reported by al jazeera, cnbc). Re-checked every cycle')
        self.assertEqual(whatif.gate_family("EXPIRED", g), "held for a market-wide shock")                          # was filed under 'expired or cancelled', hiding the rule under review
        self.assertEqual(whatif.gate_family("EXPIRED", "market session AFTER_HOURS; will retry while the event is fresh"), "expired or cancelled")

    def test_the_shock_rule_arrived_with_s2_and_a_later_version_keeps_it(self):
        self.assertIn(stamp.STRATEGY_VERSION, ("S2", "S3"))                                                       # S2 introduced the rule; S3 (build 38) changed the article reader, not this
        c = H.fresh_db()
        stamp.record(c, H.cfg(), at(2026, 10, 30, 11, 22))
        self.assertEqual(stamp.current(c)["version"], stamp.STRATEGY_VERSION)


if __name__ == "__main__":
    unittest.main()
