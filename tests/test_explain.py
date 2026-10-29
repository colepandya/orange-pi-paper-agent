"""Decisions > Why used to be a raw JSON dump. It must read like a person explaining the decision."""
import datetime as dt
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_pretrade as TP  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import dashboard, explain  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

JARGON = re.compile(r"\bgates?\b|\bEV net\b|<=|\bverdict\b|AWAITING_|REJECT_|\{|\}|\[\'|None\b|eligibility \d")


class World(TP.Scenario):
    def decision(self, ticker="NVDA"):
        return dict(self.c.execute("SELECT * FROM decisions WHERE ticker=? ORDER BY id LIMIT 1", (ticker,)).fetchone())

    def ex(self, ticker="NVDA", now=TL.T_OPEN):
        return explain.explain(self.c, self.cfg, self.decision(ticker)["id"], now)

    def text(self, ex):
        return " ".join([ex["headline"], ex["summary"], (ex["trigger"] or {}).get("line", "")] + [t for _o, t in ex["checks"]] + ex["next"] + [b for _a, b in ex["plan"]])


def world(**risk):
    w = World()
    w.setUp()
    w.cfg["risk"].update(risk)
    w.cfg["strategy"]["swing_enabled"] = True
    return w


class TestOutcomes(unittest.TestCase):
    def test_a_bought_position_explains_what_was_bought_why_and_the_plan(self):
        w = world()
        try:
            w.day()
            ex = w.ex()
            self.assertRegex(ex["headline"], r"^Bought \d+ shares of NVDA at \$120\.\d+ \(\$[\d,]+\)$")
            self.assertEqual(ex["tone"], "good")
            self.assertIn("raised its outlook", ex["trigger"]["line"])
            self.assertTrue(all(ok for ok, _ in ex["checks"] if ok is not None), ex["checks"])
            plan = dict(ex["plan"])
            self.assertIn("sell 60% there", plan["Profit target"])
            self.assertIn("break-even", plan["Profit protection"])
            self.assertIn("3 session", plan["Time limit"])
            self.assertTrue([n for n in ex["numbers"] if n[0] == "Expected profit after costs"])
            self.assertAlmostEqual(sum(p for _k, p, _r in ex["scenarios"]), 1.0, places=2)
        finally:
            w.tearDown()

    def test_a_sold_position_tells_the_whole_story_including_the_protected_stop(self):
        w = world()
        try:
            w.day()
            entry = float(w.one("SELECT entry_price FROM positions")[0])
            w.mkt.price["NVDA"] = entry * 1.012
            w.guard(TL.T_OPEN + dt.timedelta(minutes=30))
            stop = float(w.one("SELECT stop_price FROM position_forecast_plans WHERE active=1")[0])
            w.mkt.price["NVDA"] = stop - 0.05
            w.guard(TL.T_OPEN + dt.timedelta(minutes=60))
            ex = w.ex(now=TL.T_OPEN + dt.timedelta(hours=2))
            self.assertRegex(ex["headline"], r"then sold at \$[\d.]+ for a (profit|loss) of \$[\d,]+ \(stop reached\)")
            story = " ".join(t for _a, t in ex["timeline"])
            self.assertIn("Initial stop set", story)
            self.assertIn("Swing protection", story)                       # the stop was raised, and the page says why
            self.assertIn("Sold at", story)
        finally:
            w.tearDown()

    def test_a_queued_buy_says_what_it_is_waiting_for_and_what_would_cancel_it(self):
        w = world()
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            w.c.execute("INSERT INTO quote_cache(ticker,price,source,quoted_at,updated_epoch) VALUES('NVDA',117.6,'YAHOO_EXT:PRE_MARKET',?,0)", (iso(TL.T_PRE - dt.timedelta(minutes=4)),))
            ex = w.ex(now=TL.T_PRE)
            self.assertEqual((ex["tone"], ex["headline"]), ("wait", "Queued to buy NVDA: waiting for the US market to open"))
            nxt = " ".join(ex["next"])
            for needle in ("trading -2.0% versus the price it was decided against", "Next market open", "CANCELLED if later news", "HELD for 6", "3.0% below", "5.0% above", "EXPIRES"):
                self.assertIn(needle, nxt)
            self.assertIn("market is closed", self.w_text(w, ex))
        finally:
            w.tearDown()

    def w_text(self, w, ex):
        return w.text(ex).lower().replace("the us market is closed", "market is closed")

    def test_a_buy_cancelled_by_later_news_quotes_the_news(self):
        w = world()
        try:
            w.day(later=[TP.BAD])
            ex = w.ex()
            self.assertEqual(ex["tone"], "bad")
            self.assertIn("arrived after this BUY was decided", ex["headline"])
            self.assertIn("DOJ opens probe", ex["headline"])
            self.assertIn("final", ex["summary"].lower())
        finally:
            w.tearDown()

    def test_a_buy_cancelled_by_a_gap_and_a_buy_held_by_a_shock_are_explained(self):
        w = world()
        try:
            w.day(open_price=109.0)
            self.assertIn("opened 9.2% below its previous close", w.ex()["headline"])
        finally:
            w.tearDown()
        w = world()
        try:
            w.day(later=[TP.WAR])
            ex = w.ex()
            self.assertTrue(ex["headline"].startswith("On hold: a market-wide shock happened"), ex["headline"])
            self.assertIn("released", " ".join(t for _o, t in ex["checks"]).lower() + ex["summary"].lower())
        finally:
            w.tearDown()

    def test_no_price_while_the_market_is_open_is_explained_with_the_providers(self):
        w = world()
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            w.mkt.down = {"twelve", "finnhub", "alpaca", "yahoo"}
            w.cyc(TL.T_OPEN)
            ex = w.ex()
            self.assertEqual(ex["headline"], "Ready to buy NVDA, but no fresh price is available yet")
            self.assertIn("no data provider has given a fresh, trustworthy price", w.text(ex))
        finally:
            w.tearDown()

    def test_too_little_edge_says_how_much_more_would_be_needed(self):
        w = world(min_ev_net=0.5)
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            ex = w.ex(now=TL.T_PRE)
            self.assertTrue(ex["headline"].startswith("Passed on NVDA: Expected profit after costs is only"), ex["headline"])
            self.assertTrue([x for x in ex["next"] if "would qualify if the expected profit" in x])
        finally:
            w.tearDown()

    def test_bad_news_is_explained_as_a_stay_out_not_a_failure(self):
        w = world()
        try:
            w.cyc(TL.T_PRE, [TP.BAD])
            ex = w.ex(now=TL.T_PRE)
            self.assertIn("never shorts", w.text(ex))
            self.assertNotIn("bearish event; long-only", w.text(ex))
        finally:
            w.tearDown()


class TestReadability(unittest.TestCase):
    def test_no_code_speak_leaks_into_the_plain_english(self):
        for build in (lambda w: w.day(), lambda w: w.day(later=[TP.BAD]), lambda w: w.day(later=[TP.WAR]), lambda w: w.cyc(TL.T_PRE, [TL.NVDA_NEWS])):
            w = world()
            try:
                build(w)
                ex = w.ex()
                for part in [ex["headline"], ex["summary"]] + [t for _o, t in ex["checks"]] + ex["next"]:
                    self.assertIsNone(JARGON.search(part), part)
            finally:
                w.tearDown()

    def test_translations_of_every_known_gate_message(self):
        cases = {"eligibility 63 < 65": (False, "quality score is 63"), "EV net -0.93% <= 0": (False, "negative on average"), "EV net 0.12% below minimum 0.60%": (False, "at least 0.60%"),
                 "bearish event; long-only book": (False, "never shorts"), "all gates passed": (True, "Every check passed"), "confidence 40% below minimum": (False, "only 40%"),
                 "P(positive) 30% too low": (False, "only 30%"), "ADV $2.1M below minimum": (False, "too thinly traded"),
                 "swing reward:risk 0.80 below minimum 1.20 (target 1.0% vs stop 1.3%)": (False, "too small next to the possible loss"),
                 "too quiet for a swing: it moves 0.50% a day, minimum 0.80%": (False, "only moves about 0.50%"), "market session AFTER_HOURS; will retry while the event is fresh": (None, "re-checked at the open"),
                 "No verified company mapped": (False, "nothing to buy")}
        for raw, (ok, needle) in cases.items():
            got_ok, text = explain.translate_gate(raw)
            self.assertEqual(got_ok, ok, raw)
            self.assertIn(needle, text, raw)
        self.assertEqual(explain.translate_gate("something new")[1], "something new")           # unknown messages pass through untouched, never swallowed

    def test_words_for_numbers(self):
        self.assertEqual([explain.direction_word(x) for x in (0.9, 0.4, 0.1, 0, -0.1, -0.4, -0.9)], ["strongly positive", "positive", "slightly positive", "neutral", "slightly negative", "negative", "strongly negative"])
        self.assertEqual([explain.importance_word(x) for x in (0.8, 0.5, 0.25, 0.1)], ["major", "significant", "moderate", "minor"])


class TestRobustness(unittest.TestCase):
    def test_a_malicious_headline_is_escaped_everywhere(self):
        w = world()
        try:
            w.day()
            did = w.decision()["id"]
            rj = json.loads(w.decision()["reason"])
            rj["candidate"]["headline"] = "<script>alert(1)</script> NVIDIA beats"
            rj["candidate"]["source"] = "<img src=x onerror=alert(2)>"
            w.c.execute("UPDATE decisions SET reason=? WHERE id=?", (json.dumps(rj), did))
            html = explain.render_html(explain.explain(w.c, w.cfg, did, TL.T_OPEN))
            self.assertNotIn("<script>", html)
            self.assertNotIn("<img", html)
            self.assertIn("&lt;script&gt;", html)
        finally:
            w.tearDown()

    def test_old_or_broken_records_still_render(self):
        c = H.fresh_db()
        cfg = H.cfg()
        for reason in (None, "", "plain text reason", "{not json", "[]", json.dumps({"gates": "oops", "candidate": "x", "forecast": 5}), json.dumps({"candidate": {"headline": None, "age_hours": None}})):
            c.execute("INSERT INTO decisions(created,ticker,action,status,reason) VALUES(?,?,?,?,?)", (iso(TL.T_OPEN), "ZZZ", "NO_TRADE", "REJECTED", reason))
            did = c.execute("SELECT MAX(id) FROM decisions").fetchone()[0]
            html = explain.render_html(explain.explain(c, cfg, did, TL.T_OPEN))
            self.assertIn("ZZZ", html)
        self.assertIsNone(explain.explain(c, cfg, 99999))
        c.execute("INSERT INTO decisions(created,ticker,action,status,reason) VALUES(?,?,?,?,?)", (iso(TL.T_OPEN), "", "NO_TRADE", "REJECTED", "No verified company mapped; material:million(+5)"))
        self.assertIn("(no company)", explain.render_html(explain.explain(c, cfg, c.execute("SELECT MAX(id) FROM decisions").fetchone()[0])))


class TestDashboard(unittest.TestCase):
    def test_the_decisions_table_shows_the_plain_headline_and_a_why_link_and_the_detail_page_works(self):
        w = world()
        try:
            w.day()
            html = dashboard.render(w.c, w.cfg, "decisions", {})
            self.assertIn("Bought ", html)
            self.assertIn("Why? &rarr;", html)
            self.assertNotIn("<pre>{", html)                                                # the raw JSON dump is gone from the table
            did = w.decision()["id"]
            page = dashboard.render(w.c, w.cfg, "decisions", {"id": [str(did)]})
            for needle in ("What triggered it", "The checks, in plain words", "How we sized it up", "What we think could happen", "The plan", "Timeline", "all decisions"):
                self.assertIn(needle, page)
            self.assertIn("No such decision", dashboard.render(w.c, w.cfg, "decisions", {"id": ["99999"]}))
        finally:
            w.tearDown()


if __name__ == "__main__":
    unittest.main()
