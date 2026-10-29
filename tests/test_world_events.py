"""REGRESSION: 'China launches missile strikes on Taiwan as global markets plunge' was classified as a bullish PRODUCT LAUNCH (+0.48); almost every real crisis headline was invisible
(general_news, importance 0.15). And the detector must not cry wolf: ordinary company/market headlines are not shocks."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paper_agent import nlp  # noqa: E402

SHOCKS = [
    "China launches missile strikes on Taiwan as global markets plunge", "Russia invades Ukraine, stocks tumble", "Wall Street crashes as S&P 500 suffers worst day since 2020",
    "Dow plunges 1,200 points as selloff deepens", "Trading halted as circuit breakers trigger", "Fed announces emergency rate cut after market turmoil",
    "Major US bank collapses, regulators step in", "Cyberattack takes down major stock exchange", "Nuclear test by North Korea rattles Asian markets", "Missile strikes hit Gulf oil facilities, Brent jumps 8%",
    "US declares war after attack on naval base", "Global markets plunge as war fears grow", "Stocks crash worldwide as panic spreads", "Flash crash wipes out $1 trillion in minutes",
    "Black Monday: markets in freefall, Nasdaq tumbles 8%", "Iran launches drone attacks on Saudi oil facilities", "Terrorist attack in New York shakes markets", "Bank run spreads as lender halts withdrawals",
    "Emergency Fed meeting called as bond market seizes", "NYSE outage halts trading after hack",
]
BENIGN = [
    "Tesla shares plunge after earnings miss", "Netflix stock tumbles as subscriber growth slows", "Chip stocks rout continues as Nvidia slides", "Stocks rebound after Monday's crash",
    "Wall Street rallies after Fed decision", "Dow rises as markets calm after volatile week", "Markets recover from Friday's rout", "Lockheed Martin wins $2B missile defense contract",
    "Raytheon receives order for air defense missile systems", "Palantir to supply software to US troops", "Boeing workers strike at Seattle plant", "Lufthansa pilots announce air strike over pay",
    "Nuclear energy stocks rally as demand grows", "Constellation signs nuclear power deal with Microsoft", "Bank of America raises dividend", "One year after Silicon Valley Bank collapse, lessons for regulators",
    "Company calls emergency meeting of shareholders", "Emergency room visits drop in October", "Airline seeks government bailout", "Hedge fund fails to beat benchmark for third year",
    "Bank fails Fed stress test, shares slip", "JPMorgan beats estimates and raises guidance", "Apple launches new iPhone", "Nvidia launches new AI chip, shares jump", "Uber launches attack ad campaign against rival",
    "Marvel invades Netflix with new series", "Fed holds rates steady, markets calm", "Bitcoin plunges 8% as crypto selloff deepens", "Retail stocks tumble after Target warns on holiday sales",
    "Oil stocks crash after Exxon cuts dividend", "S&P 500 closes at record high", "Dow adds 300 points as earnings impress", "Futures edge higher ahead of jobs report", "Stocks slip as yields rise",
    "Amazon stock sinks after cloud miss", "Zoom outage frustrates users", "Microsoft Azure outage resolved after two hours", "Nasdaq to launch new trading platform for options",
    "NYSE extends trading hours to 22 hours", "Cyberattack hits hospital chain, shares fall",
]


class TestMarketShocks(unittest.TestCase):
    def test_real_crises_are_recognised_as_risk_off_macro_events(self):
        for h in SHOCKS:
            a = nlp.analyze(h, "")
            self.assertIn(a["event_type"], nlp.SHOCK_TYPES, h)
            self.assertEqual(a["scope"], "macro", h)
            self.assertLessEqual(a["direction"], -0.6, h)
            self.assertGreaterEqual(a["magnitude"], 0.6, h)

    def test_ordinary_headlines_are_not_shocks(self):
        wrong = [(h, nlp.analyze(h, "")["event_type"]) for h in BENIGN if nlp.analyze(h, "")["event_type"] in nlp.SHOCK_TYPES]
        self.assertEqual(wrong, [])

    def test_the_word_launches_no_longer_turns_a_war_into_a_product_launch(self):
        a = nlp.analyze("China launches missile strikes on Taiwan as global markets plunge", "")
        self.assertNotEqual(a["event_type"], "product_launch")
        self.assertLess(a["direction"], 0)

    def test_genuine_product_launches_and_ceasefires_are_untouched(self):
        self.assertEqual(nlp.analyze("Apple launches new iPhone with faster chip", "")["event_type"], "product_launch")
        a = nlp.analyze("Ceasefire agreed, oil falls and stocks rally", "")
        self.assertGreater(a["direction"], 0)

    def test_recovery_and_history_headlines_stand_down(self):
        for h in ("Stocks rebound after Monday's crash", "Markets recover from Friday's rout", "Ceasefire holds as markets calm after invasion fears",
                  "Five years after the flash crash, regulators look back"):
            self.assertNotIn(nlp.analyze(h, "")["event_type"], nlp.SHOCK_TYPES, h)


if __name__ == "__main__":
    unittest.main()
