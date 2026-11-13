"""Build 58 (hotfix): Trade decisions waiting sit just before the open positions; the free options feeds are on the System tab; the outcomes section and the merger/acquisition table are gone from the Overview."""
import html as _html
import json
import os
import re
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
from paper_agent import config, dashboard  # noqa: E402
from paper_agent.timeutil import iso, utcnow  # noqa: E402

DEF = lambda: config.load_config("/nonexistent", with_overrides=False)  # noqa: E731


def overview(c, tab="overview", q=None):
    return dashboard.render(c, DEF(), tab, q or {}, {"authed": True})


def plain(h):
    return _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)))


def waiting(c, ticker, status="AWAITING_MARKET", why="waiting for the market to open"):
    c.execute("INSERT INTO decisions(created,ticker,action,status,ev_net,reason) VALUES(?,?,?,?,?,?)", (iso(utcnow()), ticker, "BUY", status, 0.021, json.dumps({"gates": [why]})))


class TestWaitingBuysComeBeforeThePositions(unittest.TestCase):
    def world(self):
        c = H.fresh_db()
        waiting(c, "AAA")
        waiting(c, "BBB", "AWAITING_CAPACITY", "no capacity and no rotation candidate")
        c.commit()
        return c

    def test_the_section_is_the_one_directly_above_open_positions(self):
        page = overview(self.world())
        i, j = page.index("Trade decisions waiting"), page.index("Open positions")
        self.assertLess(i, j)
        self.assertEqual(page[i:j].count("<h2>"), 1)                                                         # nothing but its own heading between it and "Open positions"
        self.assertEqual(page.count("Trade decisions waiting"), 1)                                             # and it is no longer ALSO in the funnel section below
        t = plain(page[i:j])
        for need in ("AAA", "AWAITING_MARKET", "BBB", "AWAITING_CAPACITY", "2.10%", "no capacity and no rotation candidate"):
            self.assertIn(need, t)
        self.assertLess(j, page.index("How ideas flow: news to decisions"))                                  # the funnel stays below the positions
        self.assertLess(page.index("Equity (last"), i)                                                       # and below the equity line

    def test_with_nothing_waiting_there_is_no_heading(self):
        self.assertNotIn("Trade decisions waiting", overview(H.fresh_db()))

    def test_the_old_address_shows_the_same_order(self):
        page = overview(self.world(), "funnel")
        self.assertLess(page.index("Trade decisions waiting"), page.index("Open positions"))

    def test_the_funnel_report_is_computed_once_per_render(self):
        real = dashboard.funnel_engine.report
        with unittest.mock.patch.object(dashboard.funnel_engine, "report", wraps=real) as m:
            overview(self.world())
        self.assertEqual(m.call_count, 1)

    def test_if_the_report_fails_the_page_survives_without_the_waiting_buys(self):
        with unittest.mock.patch.object(dashboard.funnel_engine, "report", side_effect=ValueError("bad")):
            page = overview(self.world())
        t = plain(page)
        for need in ("Equity (mark-to-market)", "Open positions", "How ideas flow: news to decisions", "could not be displayed"):
            self.assertIn(need, t)
        self.assertNotIn("Trade decisions waiting", t)

    def test_the_section_is_total(self):
        for r in (None, {}, {"pending": None}, {"pending": "x"}, {"pending": [{}]}, {"pending": [{"ticker": None, "status": 5, "since": "junk", "ev_net": "x", "waiting_for": None}]}, [], "text", 5):
            self.assertIsInstance(dashboard.pending_buys_section(r), str)


class TestTheTwoSectionsAreGone(unittest.TestCase):
    def test_neither_is_on_the_overview(self):
        c = H.fresh_db()
        for st, g in (("REJECTED", "EV net -1.00% <= 0.00"), ("RISK_REJECTED", "capacity"), ("EXECUTED", "x")):
            c.execute("INSERT INTO decisions(created,ticker,action,status,reason) VALUES(?,?,?,?,?)", (iso(utcnow()), "AAA", "BUY", st, json.dumps({"gates": [g]})))
        c.execute("INSERT INTO events(news_uid,created,published,source,source_id,headline,event_type,scope,direction,magnitude,tickers_json,gate) VALUES('m1',?,?,?,?,?,?,?,?,?,?,?)",
                  (iso(utcnow()), iso(utcnow()), "S", "S", "Chevron to acquire Hess", "merger_acquisition", "company", -0.3, 0.5, '["CVX"]', "merger_role_unknown"))
        c.commit()
        for tab in ("overview", "funnel"):
            t = plain(overview(c, tab))
            self.assertNotIn("What happened to the ideas", t)
            self.assertNotIn("Recent merger / acquisition events", t)
            self.assertNotIn("Reason / gate", t)                                                              # the removed table's own column headings
            self.assertIn("Chevron to acquire Hess", t)                                                       # (the surviving reasons table still lists example headlines)
            self.assertIn("Why events did or did not become candidates", t)                                   # the rest of the funnel is intact
            self.assertIn("News ingested", t)

    def test_the_window_still_applies_to_the_funnel(self):
        raw = overview(H.fresh_db(), q={"hours": ["72"]})
        for h in (6, 24, 72):
            self.assertIn('href="?tab=overview&hours=%d#flow"' % h, raw)


def feed(c, name, ok=True, contracts=40, ms=1200, good=5, bad=1):
    c.execute("INSERT INTO option_feed_health(feed,last_ok,last_error,last_ms,ok_count,fail_count,last_contracts,updated) VALUES(?,?,?,?,?,?,?,?)",
              (name, iso(utcnow()) if ok else None, None if ok else "HTTP 503", ms, good, bad, contracts, iso(utcnow())))


class TestTheOptionsFeedsAreOnTheSystemTab(unittest.TestCase):
    def world(self):
        c = H.fresh_db()
        feed(c, "CBOE")
        feed(c, "YAHOO", ok=False, contracts=0)
        for e in (0.04, 0.06, 0.10):
            c.execute("INSERT INTO option_model_checks(created,ticker,kind,dte,moneyness,market_mid,model_mid,err_pct) VALUES(?,?,?,?,?,?,?,?)", (iso(utcnow()), "AAA", "PUT", 30, 1.0, 2.0, 2.1, e))
        c.commit()
        return c

    def test_the_feeds_table_and_model_check_are_on_the_system_tab(self):
        t = plain(overview(self.world(), "system"))
        for need in ("Free options feeds", "How good is the pure model against the real market", "median of 6%"):
            self.assertIn(need, t)
        self.assertIn("CBOE OK 40 1.2 s 5 / 1", t)                                                           # each status belongs to ITS feed
        self.assertIn("YAHOO FAILING 0 1.2 s 5 / 1 HTTP 503", t)

    def test_and_no_longer_on_the_overview(self):
        c = self.world()
        for tab in ("overview", "funnel", "intelligence"):
            self.assertNotIn("Free options feeds", overview(c, tab))
        self.assertEqual(dashboard.options_section(c, utcnow()), "")                                         # feeds alone no longer make an Options section

    def test_where_it_sits_on_the_system_tab(self):
        page = overview(self.world(), "system")
        self.assertLess(page.index("Free options feeds"), page.index("Recent cycles"))

    def test_no_feeds_no_section_and_text_is_escaped_once(self):
        self.assertNotIn("Free options feeds", overview(H.fresh_db(), "system"))
        c = H.fresh_db()
        feed(c, "a<b&c")
        c.commit()
        raw = overview(c, "system")
        self.assertIn("a&lt;b&amp;c", raw)
        self.assertNotIn("&amp;lt;", raw)

    def test_a_malformed_feed_row_costs_the_section_not_the_tab(self):
        c = self.world()
        with unittest.mock.patch.object(dashboard, "_options_feeds_section", side_effect=RuntimeError("bad")):
            t = plain(overview(c, "system"))
        self.assertIn("The feeds could not be displayed", t)
        self.assertIn("Recent cycles", t)

    def test_open_options_still_show_on_the_overview(self):
        c = H.fresh_db()
        c.execute("INSERT INTO option_positions(underlying,kind,strike,expiry,contracts,multiplier,entry_premium,cost,status,opened,price_source,stop_underlying,target_underlying,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("AAA", "PUT", 50.0, "2027-01-15", 2, 100, 1.5, 300.0, "OPEN", iso(utcnow()), "CHAIN", 48.0, 55.0, "test"))
        c.commit()
        t = plain(overview(c))
        self.assertIn("Open options", t)
        self.assertIn("real contract", t)
        self.assertNotIn("Free options feeds", t)


if __name__ == "__main__":
    unittest.main()


# ================================================================ the trade type on each waiting decision
from paper_agent import funnel  # noqa: E402
from paper_agent.timeutil import parse_ts  # noqa: E402


def waiting_as(c, ticker, action, status="AWAITING_MARKET", extra=None, ev=0.03):
    reason = {"gates": ["market session CLOSED; will retry while the event is fresh"]}
    reason.update(extra or {})
    c.execute("INSERT INTO decisions(created,ticker,action,status,ev_net,reason) VALUES(?,?,?,?,?,?)", (iso(utcnow()), ticker, action, status, ev, json.dumps(reason)))


class TestTheTradeTypeIsShown(unittest.TestCase):
    def test_the_type_by_hand(self):
        for action, reason, want in (("BUY", {}, "Long"), ("SHORT", {}, "Short"), ("OPTION_PUT", {}, "Put"), ("OPTION_CALL", {}, "Call"),
                                     ("SHORT", {"reversal": {"model_expects_the_stock_to_fall_pct": 9.1}}, "Put (the model expects a fall)"),
                                     ("SHORT", {"rescue": {"stock_verdict": "WATCH"}}, "Put (stock rules said no)"),
                                     ("BUY", {"reversal": {"x": 1}}, "Put (the model expects a fall)"),                  # a put tier outranks the plain direction
                                     ("SHORT", {"reversal": {"x": 1}, "rescue": {"y": 2}}, "Put (the model expects a fall)"),
                                     ("buy", {}, "Long"), ("  Short ", {}, "Short"), (None, {}, "-"), ("", {}, "-"), ("NO_TRADE", {}, "-"), ("???", {}, "-"), (5, {}, "-"),
                                     ("BUY", None, "Long"), ("BUY", [], "Long"), ("SHORT", "text", "Short"), ("BUY", {"reversal": None, "rescue": 0}, "Long")):
            self.assertEqual(funnel.trade_type(action, reason), want, (action, reason))

    def world(self):
        c = H.fresh_db()
        waiting_as(c, "AAA", "BUY")
        waiting_as(c, "BBB", "SHORT")
        waiting_as(c, "CCC", "SHORT", "AWAITING_QUOTE", {"reversal": {"model_expects_the_stock_to_fall_pct": 9.1}})
        waiting_as(c, "DDD", "SHORT", "HELD", {"rescue": {"stock_verdict": "WATCH", "stock_blocked_by": ["x"]}})
        waiting_as(c, "EEE", None, "AWAITING_CAPACITY")                                                      # an older record with no action
        c.commit()
        return c

    def test_the_funnel_report_carries_the_type(self):
        rep = funnel.report(self.world(), 24, utcnow())
        self.assertEqual([(p["ticker"], p["type"]) for p in rep["pending"]],
                         [("EEE", "-"), ("DDD", "Put (stock rules said no)"), ("CCC", "Put (the model expects a fall)"), ("BBB", "Short"), ("AAA", "Long")])           # newest first

    def test_the_page_shows_each_type_on_its_own_row(self):
        page = overview(self.world())
        t = plain(page)
        self.assertIn("Trade decisions waiting", t)
        self.assertNotIn("BUY decisions waiting", t)
        self.assertIn("Ticker Type Status Since EV net Waiting for", t)                                       # the column sits right after the ticker
        for row in ("AAA Long AWAITING_MARKET", "BBB Short AWAITING_MARKET", "CCC Put (the model expects a fall) AWAITING_QUOTE", "DDD Put (stock rules said no) HELD", "EEE - AWAITING_CAPACITY"):
            self.assertIn(row, t)
        self.assertIn("decided only when it executes", t)

    def test_the_command_line_report_shows_it_too(self):
        text = funnel.text(funnel.report(self.world(), 24, utcnow()))
        self.assertIn("Trade decisions waiting (5):", text)
        self.assertNotIn("BUY decisions waiting", text)
        lines = {l.split()[0]: l for l in text.splitlines() if l.startswith("   ") and "EV" in l}
        self.assertIn("AAA    Long", lines["AAA"])
        self.assertIn("BBB    Short", lines["BBB"])
        self.assertIn("CCC    Put", lines["CCC"])

    def test_a_decision_whose_record_is_not_valid_json_still_gets_its_type_from_the_action(self):
        c = H.fresh_db()
        for action, raw in (("BUY", ""), ("BUY", None), ("SHORT", "not json {")):                              # the invalid one is the NEWEST, so it is the first the report reads (no earlier record to borrow a value from)
            c.execute("INSERT INTO decisions(created,ticker,action,status,ev_net,reason) VALUES(?,?,?,?,?,?)", (iso(utcnow()), "JJJ", action, "AWAITING_MARKET", 0.02, raw))
        c.commit()
        rep = funnel.report(c, 24, utcnow())
        self.assertEqual([p["type"] for p in rep["pending"]], ["Short", "Long", "Long"])
        self.assertIn("Trade decisions waiting", plain(overview(c)))

    def test_an_entry_without_a_type_shows_a_dash_not_an_error(self):
        t = plain(dashboard.pending_buys_section({"pending": [{"ticker": "ZZZ", "status": "AWAITING_MARKET", "since": "2026-11-13T00:00:00+00:00", "ev_net": None, "waiting_for": ""}]}))
        self.assertIn("ZZZ - AWAITING_MARKET", t)

    def test_the_type_text_is_escaped_once(self):
        raw = dashboard.pending_buys_section({"pending": [{"ticker": "A<B", "type": "x & <y>", "status": "S", "since": "2026-11-13T00:00:00+00:00", "ev_net": 0.01, "waiting_for": ""}]})
        self.assertIn("x &amp; &lt;y&gt;", raw)
        self.assertNotIn("&amp;amp;", raw)


class TestHostileStoredRecordsAreSurvived(unittest.TestCase):
    """Found by a hunt: valid JSON that is not an object, a non-list set of gates, and a non-numeric EV used to crash the funnel report (and so the CLI report). None of these can be written by the trader; a corrupt row could."""

    def world(self):
        c = H.fresh_db()
        newest_first = [("SHORT", json.dumps({"reversal": {"x": 1}, "gates": "not a list", "ref_price": "x"}), "x"),         # newest: the first one the report reads
                        ("BUY", "[]", None), ("BUY", "123", float("inf")), ("BUY", '"just a string"', -1), ("BUY", "null", 0.05),
                        ("SHORT", json.dumps({"gates": None, "ref_price": True}), True), ("BUY", json.dumps({"gates": [5, None], "ref_price": 12.5}), 0.02)]
        for action, raw, ev in reversed(newest_first):
            c.execute("INSERT INTO decisions(created,ticker,action,status,ev_net,reason) VALUES(?,?,?,?,?,?)", (iso(utcnow()), "HHH", action, "AWAITING_MARKET", ev, raw))
        c.execute("INSERT INTO quote_cache(ticker,price,source,quoted_at,updated_epoch) VALUES('HHH',10.0,'test:PRE',?,?)", (iso(utcnow()), utcnow().timestamp()))      # a cached price, so the reference price is actually USED
        c.commit()
        return c

    def test_the_report_text_and_pages_survive_and_still_show_the_types(self):
        c = self.world()
        rep = funnel.report(c, 24, utcnow())
        self.assertEqual([p["type"] for p in rep["pending"]], ["Put (the model expects a fall)", "Long", "Long", "Long", "Long", "Short", "Long"])
        for p in rep["pending"]:
            self.assertTrue(p["ev_net"] is None or (isinstance(p["ev_net"], (int, float)) and not isinstance(p["ev_net"], bool)))     # (a boolean is an int in Python: say so explicitly)
            self.assertIsInstance(p["waiting_for"], str)
        self.assertEqual(rep["pending"][0]["waiting_for"], "")                                               # gates that are not a list are no gates (not their last character)
        self.assertIsNone(rep["pending"][0]["indication"])                                                   # a junk reference price: no gap, no crash
        self.assertAlmostEqual(rep["pending"][-1]["indication"]["gap"], 10.0 / 12.5 - 1.0)                   # a real one still works
        self.assertIsNone(rep["pending"][5]["indication"])                                                   # a JSON true is not a price (it would read as 1.0 and show a +900% gap)
        self.assertEqual(rep["pending"][0]["ev_net"], None)                                                  # "x"
        self.assertEqual(rep["pending"][2]["ev_net"], None)                                                  # infinity
        self.assertEqual(rep["pending"][4]["ev_net"], 0.05)                                                  # real numbers are kept
        self.assertEqual(rep["pending"][-1]["waiting_for"], "None")                                          # a gate that is not text is shown as text
        self.assertIn("Trade decisions waiting (7):", funnel.text(rep))
        for tab in ("overview", "funnel"):
            t = plain(overview(c, tab))
            self.assertIn("Trade decisions waiting", t)
            self.assertNotIn("could not be displayed", t)                                                    # the whole funnel section works, not just the notice
