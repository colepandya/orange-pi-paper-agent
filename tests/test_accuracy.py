"""The reasoning engine's ACCURACY on two labelled sets: the one its rules were developed against, and a held-out one written afterwards with different wording. A change that makes the engine worse fails here.
When a held-out case fails, fix the general rule: never add the headline."""
import collections
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import reasoning_corpus as DEV  # noqa: E402
import reasoning_heldout as HOLD  # noqa: E402
from paper_agent import graph, nlp, reasoning as R  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402

W = {"NVDA": {"name": "NVIDIA Corp", "aliases": ["NVIDIA", "Nvidia"]}, "TSM": {"name": "Taiwan Semiconductor", "aliases": ["TSMC"]}, "AMD": {"name": "Advanced Micro Devices", "aliases": ["AMD"]}, "ACME": {"name": "Acme Corp", "aliases": ["Acme"]},
     "BETA": {"name": "Beta Industries", "aliases": ["Beta"]}, "GAMMA": {"name": "Gamma Inc", "aliases": ["Gamma"]}, "DELTA": {"name": "Delta Co", "aliases": ["Delta Co"]}, "EPS": {"name": "Epsilon Ltd", "aliases": ["Epsilon"]},
     "XOM": {"name": "Exxon Mobil", "aliases": ["Exxon"]}, "DAL": {"name": "Delta Air Lines", "aliases": ["Delta Air"]}, "DHI": {"name": "D R Horton", "aliases": ["Horton"]}, "NEM": {"name": "Newmont", "aliases": ["Newmont"]},
     "FCX": {"name": "Freeport-McMoRan", "aliases": ["Freeport"]}, "LMT": {"name": "Lockheed Martin", "aliases": ["Lockheed"]}, "MLM": {"name": "Martin Marietta Materials", "aliases": ["Martin Marietta"]}, "MSFT": {"name": "Microsoft Corp", "aliases": ["Microsoft"]}, "BTI": {"name": "British American Tobacco", "aliases": ["British American Tobacco"]}, "VOD": {"name": "Vodafone Group", "aliases": ["Vodafone"]}, "LSE": {"name": "London Stock Exchange", "aliases": ["London Stock Exchange"]}, "AMZN": {"name": "Amazon.com Inc", "aliases": ["Amazon"]}, "VSAT": {"name": "Viasat Inc", "aliases": ["Viasat"]}}
U = Universe(W)
NOW = dt.datetime(2026, 10, 27, 13, 0, tzinfo=dt.timezone.utc)
SEED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "relations.json")


def check(c, cfg, case):
    cat, title, summ, tk, exp = case
    item = {"uid": "x", "title": title, "summary": summ, "analysis": dict(nlp.analyze(title, summ)), "tickers": tk}
    r = R.analyze_item(c, cfg, U, item, NOW)
    why = []
    first = {e["ticker"]: ("+" if e in r["beneficiaries"] else "-") for e in (r["beneficiaries"] + r["losers"]) if e["order"] == 1} if r else {}
    anyside = {e["ticker"]: ("+" if e in r["beneficiaries"] else "-") for e in (r["beneficiaries"] + r["losers"])} if r else {}
    if exp.get("abstain"):
        if r is not None:
            why.append("should abstain but analysed (%s)" % r["event_type"])
    elif r is None:
        if exp.get("side") or exp.get("macro") or exp.get("mixed"):
            why.append("abstained")
        if exp.get("state") and item.get("_state") != exp["state"]:
            why.append("abstained, and its state %s != %s" % (item.get("_state"), exp["state"]))
    if r is not None:
        for t, s in exp.get("side", {}).items():
            if first.get(t) != s:
                why.append("%s expected %s got %s" % (t, s, first.get(t)))
        for t, s in exp.get("not_side", {}).items():
            if first.get(t) == s:
                why.append("%s must not be %s" % (t, s))
        for t, s in exp.get("macro", {}).items():
            if anyside.get(t) != s:
                why.append("%s expected %s got %s" % (t, s, anyside.get(t)))
        if exp.get("state") and r["state"] != exp["state"]:
            why.append("state %s != %s" % (r["state"], exp["state"]))
        if exp.get("mixed") and not r["contradictions"]:
            why.append("no contradiction flagged")
        if "max_cert" in exp and r["certainty"] > exp["max_cert"]:
            why.append("certainty %.2f > %.2f" % (r["certainty"], exp["max_cert"]))
    return why


class TestAccuracy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = H.fresh_db()
        cls.cfg = H.cfg()
        cls.cfg["intelligence"]["influence"] = "active"
        cls.cfg["graph"]["enabled"] = True
        graph.seed(cls.c, NOW, SEED)

    def run_set(self, cases):
        by, fails = collections.defaultdict(lambda: [0, 0]), []
        for case in cases:
            why = check(self.c, self.cfg, case)
            by[case[0]][1] += 1
            if why:
                fails.append("[%s] %s -> %s" % (case[0], case[1][:70], "; ".join(why)))
            else:
                by[case[0]][0] += 1
        return sum(v[0] for v in by.values()), sum(v[1] for v in by.values()), by, fails

    def test_the_development_set(self):
        ok, n, by, fails = self.run_set(DEV.CASES)
        self.assertGreaterEqual(ok / float(n), 0.97, "\n".join(fails))

    def test_the_held_out_set_which_measures_generalisation(self):
        ok, n, by, fails = self.run_set(HOLD.CASES)
        self.assertGreaterEqual(ok / float(n), 0.92, "\n".join(fails))

    def test_no_category_collapses(self):
        for cases in (DEV.CASES, HOLD.CASES):
            _, _, by, fails = self.run_set(cases)
            for cat, (k, n) in by.items():
                if n >= 3:
                    self.assertGreaterEqual(k / float(n), 0.66, "%s %d/%d\n%s" % (cat, k, n, "\n".join(fails)))

    def test_the_things_that_must_never_happen(self):
        """Hard errors, not accuracy: an opinion piece must never become a trade idea, and a downgrade must never be read as good news."""
        for title in ("3 Reasons to Buy Acme Stock Right Now", "Is Acme a Buy After Its Recent Rally?", "Acme stock: should you buy the dip?", "Why Acme Stock Soared Today"):
            r = R.analyze_item(self.c, self.cfg, U, {"uid": "x", "title": title, "summary": "", "analysis": dict(nlp.analyze(title, "")), "tickers": ["ACME"]}, NOW)
            self.assertIsNone(r, title)
        for title in ("Bank of America downgrades Acme citing slowing growth", "Morgan Stanley downgrades Acme to underweight"):
            r = R.analyze_item(self.c, self.cfg, U, {"uid": "x", "title": title, "summary": "", "analysis": dict(nlp.analyze(title, "")), "tickers": ["ACME"]}, NOW)
            self.assertIn("ACME", [e["ticker"] for e in r["losers"]], title)


if __name__ == "__main__":
    unittest.main()
