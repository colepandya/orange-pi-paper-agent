"""Build 58 (hotfix): BUY decisions waiting sit just before the open positions; the free options feeds are on the System tab; the outcomes section and the merger/acquisition table are gone from the Overview."""
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
        i, j = page.index("BUY decisions waiting"), page.index("Open positions")
        self.assertLess(i, j)
        self.assertEqual(page[i:j].count("<h2>"), 1)                                                         # nothing but its own heading between it and "Open positions"
        self.assertEqual(page.count("BUY decisions waiting"), 1)                                             # and it is no longer ALSO in the funnel section below
        t = plain(page[i:j])
        for need in ("AAA", "AWAITING_MARKET", "BBB", "AWAITING_CAPACITY", "2.10%", "no capacity and no rotation candidate"):
            self.assertIn(need, t)
        self.assertLess(j, page.index("How ideas flow: news to decisions"))                                  # the funnel stays below the positions
        self.assertLess(page.index("Equity (last"), i)                                                       # and below the equity line

    def test_with_nothing_waiting_there_is_no_heading(self):
        self.assertNotIn("BUY decisions waiting", overview(H.fresh_db()))

    def test_the_old_address_shows_the_same_order(self):
        page = overview(self.world(), "funnel")
        self.assertLess(page.index("BUY decisions waiting"), page.index("Open positions"))

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
        self.assertNotIn("BUY decisions waiting", t)

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
