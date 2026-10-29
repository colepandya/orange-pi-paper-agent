"""An outside review (Copilot) of the code and data. Each claim was checked against the real code; these are the ones that were TRUE, reproduced with realistic SEC-style names and press-release text:
company attribution (body-only mentions, exchange names, first names, products, generic words), a headline that contradicts itself, a sizing rule that ignored the edge, and queue ageing."""
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_pretrade as TP  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import config, evidence, explain, nlp, settings as S, sizing, universe as U  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402

ROWS = [("SEAGATE TECHNOLOGY HOLDINGS PLC", "STX"), ("MICROSOFT CORP", "MSFT"), ("Broadcom Inc.", "AVGO"), ("NISOURCE INC.", "NI"), ("ServiceNow, Inc.", "NOW"), ("Alphabet Inc.", "GOOGL"),
        ("Gemini Space Station, Inc.", "GEMI"), ("HELEN OF TROY LTD", "HELE"), ("NBT BANCORP INC", "NBTB"), ("NASDAQ, INC.", "NDAQ"), ("Bancorp, Inc.", "TBBK"), ("GARMIN LTD", "GRMN"),
        ("MICRON TECHNOLOGY INC", "MU"), ("INTERCONTINENTAL EXCHANGE, INC.", "ICE"), ("Freedom Holding Corp.", "FRHC"), ("GEN DIGITAL INC.", "GEN"), ("EQUINIX INC", "EQIX"),
        ("POWELL INDUSTRIES INC", "POWL"), ("Jordan Industries Inc", "JORD"), ("NVIDIA CORP", "NVDA"), ("Apple Inc.", "AAPL"), ("Amazon.com, Inc.", "AMZN"), ("Main Street Capital Corp", "MAIN"), ("CASSIDY HOLDINGS CORP", "CSDY")]


def uni():
    return Universe(U.build({"fields": ["cik", "name", "ticker", "exchange"], "data": [[1000 + i, n, t, "Nasdaq"] for i, (n, t) in enumerate(ROWS)]}))


def found(u, title, summary=""):
    return sorted(t for t, c, h in u.match(title, summary) if c >= 0.6)


CASES = [  # (headline, summary, expected tickers)
    ("NBT Bancorp Inc. Announces Third Quarter 2026 Results", "NBT Bancorp Inc. (NASDAQ: NBTB) today reported net income of $60.1 million. The Company's common stock trades on the Nasdaq Global Select Market and is a member of the Bancorp community banks index.", ["NBTB"]),
    ("Seagate Reports Record HDD Shipments as AI Storage Demand Surges", "Seagate Technology (NASDAQ: STX) said demand from hyperscale customers including Microsoft and Broadcom-based systems drove growth.", ["STX"]),
    ("NiSource Signs Data Center Power Agreement With Microsoft", "NiSource Inc. (NYSE: NI) announced a long-term agreement to serve a new Microsoft data center campus.", ["NI"]),
    ("ServiceNow Unveils New AI Agents Built on Google Gemini", "ServiceNow (NYSE: NOW) today announced AI agents that integrate Alphabet's Google Cloud and Gemini models.", ["NOW"]),
    ("Microsoft Earnings Beat Estimates as Azure Growth Accelerates", "Microsoft (NASDAQ: MSFT) Azure grew 40%, outpacing Alphabet's Google Cloud and Amazon's AWS.", ["MSFT"]),
    ("Serbian Opposition Leaders Debate Election Plans, Helen Markovic Says", "Podcast episode: Helen Markovic discusses Belgrade politics and the Jordan Valley peace talks.", []),
    ("Gen Delivers Another Double-Digit Growth Quarter in Q2 FY27 and Raises FY27 Outlook", "Gen Digital Inc. (NASDAQ: GEN) today announced results. Gen's common stock is listed on the Nasdaq.", ["GEN"]),
    ("Fed Chair Powell Says Rates Will Stay Higher for Longer", "Powell spoke at the Economic Club in Washington.", []),
]


class TestAttribution(unittest.TestCase):
    def test_every_reviewed_example_now_attributes_only_the_company_the_story_is_about(self):
        u = uni()
        for title, summary, want in CASES:
            self.assertEqual(found(u, title, summary), want, title)

    def test_the_real_companies_are_still_found_in_every_normal_way(self):
        u = uni()
        for title, summary, want in (
                ("Garmin raises full-year outlook", "", ["GRMN"]), ("Helen of Troy Limited Reports Fiscal Results", "", ["HELE"]), ("Nasdaq, Inc. Reports Third Quarter Results", "", ["NDAQ"]),
                ("The Bancorp, Inc. Reports Record Quarter", "The Bancorp, Inc. (NASDAQ: TBBK) announced results.", ["TBBK"]), ("Gemini Space Station Announces Listing", "", ["GEMI"]),
                ("Powell Industries Wins $200 Million Order", "", ["POWL"]), ("Main Street Capital Raises Monthly Dividend", "", ["MAIN"]),
                ("Microsoft and Alphabet tapped to set the AI trade tone", "", ["GOOGL", "MSFT"]),                     # both are in the headline: both are subjects
                ("Q3 Results Beat Expectations", "Garmin Ltd (NASDAQ: GRMN) reported record revenue.", ["GRMN"]),         # the headline names nobody: the body is all we have
                ("Intercontinental Exchange Announces Dividend", "", ["ICE"])):
            self.assertEqual(found(u, title, summary), want, title)

    def test_each_cause_is_fixed_on_its_own(self):
        u = uni()
        self.assertEqual(found(u, "Seagate Reports Record Shipments", "Customers include Microsoft and Amazon."), ["STX"])                   # body-only mentions are context
        self.assertEqual(found(u, "Seagate Signs Supply Deal With Microsoft", ""), ["STX"])                                                    # the counterparty after 'with' is not the subject
        self.assertEqual(found(u, "Gen Raises Outlook (NASDAQ: GEN)", "Listed on the Nasdaq. The Nasdaq Composite rose."), ["GEN"])           # exchange names are not the company Nasdaq, Inc.
        self.assertEqual(found(u, "Sen. Warren Questions Jordan Hearing", ""), [])                                                             # first names / politicians
        self.assertEqual(found(u, "Google Gemini Gets New Features", ""), ["GOOGL"])                                                           # an AI product is not Gemini Space Station
        self.assertEqual(found(u, "President Powell Addresses the Fed", ""), [])                                                               # person-title guard
        self.assertEqual(found(u, "Sen. Cassidy Questions Fed Chair at Hearing", ""), [])                                                      # a person NOT in any name list: only the title guard can know
        self.assertEqual(found(u, "Cassidy Holdings Raises Full-Year Outlook", ""), ["CSDY"])                                                  # ...while the real company is still found

    def test_exchange_tags_stay_the_strongest_evidence(self):
        u = uni()
        m = {t: (c, h) for t, c, h in u.match("Company X announces results", "Seagate Technology (NASDAQ: STX) said ...")}
        self.assertEqual(m["STX"], (1.0, "exchange_tag"))


class TestHeadlinesThatContradictThemselves(unittest.TestCase):
    def a(self, h):
        return nlp.analyze(h, "")

    def test_good_news_while_the_stock_fell_is_neutral_and_says_why(self):
        for h in ("GOOGL Stock Slumps 11% After-Hours Despite Strong Cloud Growth and Earnings Beat", "Alphabet shares plunge 9% even as earnings beat and guidance raised", "Garmin shares fall 6% after earnings beat estimates"):
            a = self.a(h)
            self.assertEqual((a["direction"], a["contradictory"]), (0.0, True), h)
            self.assertEqual(evidence.candidate_gate(a, ["GOOGL"], {"GOOGL": 0.9}, 1.0, 1.0, H.cfg()["intelligence"]), "contradictory_signals")
        self.assertIn("contradicts itself", evidence.GATE_LABELS["contradictory_signals"])                      # and the funnel can explain it in words
        self.assertIn("already moved 10%", evidence.GATE_LABELS["already_moved"])

    def test_a_move_of_ten_percent_or_more_is_already_priced_in(self):
        for h in ("Tesla stock soars 12% despite earnings miss and guidance cut", "Micron stock climbs 15% on record earnings beat"):
            a = self.a(h)
            self.assertEqual((a["direction"], a["priced_in"]), (0.0, True), h)
            self.assertEqual(evidence.candidate_gate(a, ["TSLA"], {"TSLA": 0.9}, 1.0, 1.0, H.cfg()["intelligence"]), "already_moved")

    def test_a_smaller_aligned_move_is_discounted_not_ignored(self):
        a, b = self.a("Garmin stock jumps 8% after earnings beat and raised outlook"), self.a("Garmin raises full-year outlook and beats earnings estimates")
        self.assertGreater(a["direction"], 0)
        self.assertLess(a["direction"], b["direction"])

    def test_clean_news_and_ordinary_words_are_untouched(self):
        for h, kind in (("NVIDIA beats estimates and raises full-year guidance as data center demand surges", "guidance_raise"), ("Microsoft earnings beat estimates as Azure growth accelerates", "earnings_beat"),
                        ("Salesforce raises full-year revenue outlook", "guidance_raise"), ("Lockheed Martin wins $2B contract to supply missiles", "contract_win")):
            a = self.a(h)
            self.assertEqual((a["event_type"], a["contradictory"], a["priced_in"]), (kind, False, False), h)
            self.assertGreater(a["direction"], 0.6, h)
        self.assertIsNone(self.a("Boeing to supply 100 jets as price rise in aluminium slows")["reaction"])             # 'rise' without a stock/percent is not a reaction


class TestSizingUsesTheEdge(unittest.TestCase):
    def size(self, ev, sigma=0.035, **risk):
        cfg = H.cfg()
        cfg["risk"].update(risk)
        f = {"ev_net": ev, "sigma_h": sigma, "stop_return": -0.015, "confidence": 0.7, "risk_score": 30, "ticker": "NVDA"}
        st = {"equity": 1_000_000.0, "cash": 1_000_000.0, "holdings": [], "scenario_probs": {}}
        return sizing.size_position(f, {"sector": "Technology"}, st, cfg, {"risk_mult": 1.0}, {"state": "Strong", "score": 0.5, "primary": None})

    def test_a_stronger_edge_gets_a_bigger_position_and_a_weak_one_a_small_one(self):
        weak, mid, strong = self.size(0.007), self.size(0.012), self.size(0.027)
        self.assertLess(weak["pct"], mid["pct"])
        self.assertLess(mid["pct"], strong["pct"])
        self.assertGreater(weak["pct"] * 1.0, 0.0)

    def test_the_old_behaviour_gave_all_of_them_the_same_size(self):
        """Documented: 0.7% EV and 2.7% EV both saturated quarter-Kelly at 100%, so both got the maximum."""
        sat = lambda ev: min(0.25 * ev / 0.035 ** 2, 1.0)  # noqa: E731
        self.assertEqual((sat(0.007), sat(0.027)), (1.0, 1.0))

    def test_floor_ceiling_and_the_setting(self):
        mx = H.cfg()["risk"]["max_position_pct"]
        full = self.size(0.05, risk_per_trade_pct=1.0)
        self.assertLessEqual(full["pct"], mx + 1e-12)
        tiny = self.size(0.0005, risk_per_trade_pct=1.0)
        self.assertGreaterEqual(tiny["pct"], 0.25 * mx * 0.4)                       # never below the 25% conviction floor (after the ordinary multipliers)
        self.assertEqual(self.size(-0.01)["pct"], 0.0)                              # no edge, no position
        easy, hard = self.size(0.012, full_size_edge=0.2, risk_per_trade_pct=1.0), self.size(0.012, full_size_edge=1.5, risk_per_trade_pct=1.0)
        self.assertGreater(easy["pct"], hard["pct"])                                # the owner can demand a bigger edge for a full position
        self.assertTrue(any("conviction" in r for r in full["reasons"]))                  # the page can say how the size was reached

    def test_the_setting_is_editable_and_validated(self):
        base = config.load_config("/nonexistent", with_overrides=False)
        self.assertEqual(base["risk"]["full_size_edge"], 0.5)
        self.assertIn("risk.full_size_edge", {f.path for f in S.FIELDS})
        for bad in ("0", "9"):
            self.assertTrue(S.apply_form({"f.risk.full_size_edge": bad}, base, base, {})[1], bad)


class World(TP.Scenario):
    def status(self):
        return self.c.execute("SELECT status FROM decisions WHERE ticker='NVDA' AND action='BUY' ORDER BY id LIMIT 1").fetchone()[0]


class TestTheQueueAgesByTheNewsNotJustTheDecision(unittest.TestCase):
    def test_news_that_was_already_old_when_decided_expires_while_waiting_even_though_the_decision_is_young(self):
        w = World()
        w.setUp()
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            did = w.c.execute("SELECT id,reason FROM decisions WHERE ticker='NVDA' ORDER BY id LIMIT 1").fetchone()
            r = json.loads(did["reason"])
            r["candidate"]["age_hours"] = 20.0                                       # the news was already 20 h old when the BUY was queued
            w.c.execute("UPDATE decisions SET reason=? WHERE id=?", (json.dumps(r), did["id"]))
            w.cyc(TL.T_PRE + dt.timedelta(hours=10))                                 # 10 h later (market closed): the DECISION is 10 h old, the NEWS is 30 h old
            self.assertEqual(w.status(), "EXPIRED")
            ex = explain.explain(w.c, w.cfg, did["id"], TL.T_PRE + dt.timedelta(hours=10))
            self.assertTrue(ex["headline"].startswith("Expired: the news behind NVDA was too old"), ex["headline"])
        finally:
            w.tearDown()

    def test_fresh_news_keeps_waiting_and_a_closed_market_still_expires_stale_ones(self):
        w = World()
        w.setUp()
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            w.cyc(TL.T_PRE + dt.timedelta(hours=10))
            self.assertEqual(w.status(), "AWAITING_MARKET")                          # ~11 h old news: still good for the open
            w.cyc(TL.T_PRE + dt.timedelta(hours=26))
            self.assertEqual(w.status(), "EXPIRED")                                  # a weekend-length wait: dropped, not traded late
        finally:
            w.tearDown()


class TestTheThresholdMessageIsBelievable(unittest.TestCase):
    def test_the_message_shows_two_decimals_so_a_near_miss_never_reads_65_below_65(self):
        w = World()
        w.setUp()
        try:
            w.cfg["portfolio"]["entry_score"] = 95.0
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            d = w.c.execute("SELECT id,reason FROM decisions WHERE ticker='NVDA' ORDER BY id LIMIT 1").fetchone()
            gate = json.loads(d["reason"])["gates"][-1]
            self.assertRegex(gate, r"^eligibility \d+\.\d{2} < 95\.00$")
            self.assertIn("out of 100; 95.00 is needed", explain.translate_gate(gate)[1])
        finally:
            w.tearDown()


if __name__ == "__main__":
    unittest.main()
