"""Why NO queued BUY executed at the open. Found by replaying the owner's real database (68 queued BUYs, 0 executed): the rules were rejecting nearly everything for the wrong reasons.
  A. news aged on the WALL clock, so a story from after the close lost ~14 of 20 eligibility points overnight while the market was shut   -> 27 rejections
  B. any strongly negative macro-scope story counted as a "market shock" ("Bitcoin weakens", an SEC filing header, a video-game row)      -> 7 holds
  C. any story that merely MENTIONED a ticker (wraps, roundups, "Alphabet slides, Microsoft firms") vetoed a BUY on it                      -> 13 cancellations
  D. SEC filing headers are half of all ingested items and were classified from the company's NAME ("Acquisition Corp" = merger, "vote" = election).
After the fixes the same open buys 8 stocks."""
import contextlib
import datetime as dt
import io
import json
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_pretrade as TP  # noqa: E402
import test_review_fixes as RF  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import config, nlp, pretrade, trader  # noqa: E402
from paper_agent.timeutil import iso, market_hours_between  # noqa: E402

UTC = dt.timezone.utc


def at(y, m, d, hh, mm):
    return dt.datetime(y, m, d, hh, mm, tzinfo=UTC)


class TestTheMarketClock(unittest.TestCase):
    def test_only_market_time_counts(self):
        self.assertAlmostEqual(market_hours_between(at(2026, 10, 27, 14, 0), at(2026, 10, 27, 16, 0)), 2.0, places=2)             # 10:00-12:00 ET: the regular session counts fully
        overnight = market_hours_between(at(2026, 10, 27, 20, 30), at(2026, 10, 28, 13, 45))                                      # 16:30 ET -> 09:45 ET next day: 17.25 hours on the wall clock
        self.assertAlmostEqual(overnight, 2.5, delta=0.1)                                                                         # ...but only ~2.5 of market time (extended hours count a quarter)
        weekend = market_hours_between(at(2026, 10, 23, 20, 0), at(2026, 10, 26, 13, 45))                                         # Friday close -> Monday 09:45 ET: 61.75 wall hours
        self.assertAlmostEqual(weekend, 2.6, delta=0.15)
        self.assertEqual(market_hours_between(at(2026, 10, 27, 16, 0), at(2026, 10, 27, 15, 0)), 0.0)


class TestAQueuedBuyIsStillFreshAtTheOpen(unittest.TestCase):
    def test_news_from_just_after_the_close_is_not_stale_the_next_morning(self):
        w = TP.Scenario()
        w.setUp()
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            d = w.c.execute("SELECT id,reason FROM decisions WHERE ticker='NVDA' AND action='BUY' ORDER BY id LIMIT 1").fetchone()
            r = json.loads(d["reason"])
            monday = at(2026, 10, 26, 20, 30)                                         # Monday 16:30 ET: the news arrived right after the close
            r["candidate"]["age_hours"], r["decided_at"] = 0.2, iso(monday)
            w.c.execute("UPDATE decisions SET created=?, reason=? WHERE id=?", (iso(monday), json.dumps(r), d["id"]))
            base = trader.eligibility_score(dict(r["candidate"], age_hours=0.2))
            w.cfg["portfolio"]["entry_score"] = base - 4                              # 17 wall-clock hours would cost ~14 points (fail); ~2.5 market hours cost ~2 (pass)
            w.cyc(TL.T_OPEN)                                                          # Tuesday 09:40 ET
            self.assertEqual(w.buy()["status"], "EXECUTED", w.gate())
            self.assertEqual(w.positions(), 1)
        finally:
            w.tearDown()


    def test_news_nearly_five_days_old_is_never_traded_even_though_little_market_time_has_passed(self):
        """Thursday evening's story, still queued on Monday morning: only ~11 hours of MARKET time have passed (one Friday session), but it is 4.7 days old."""
        w = TP.Scenario()
        w.setUp()
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            d = w.c.execute("SELECT id,reason FROM decisions WHERE ticker='NVDA' AND action='BUY' ORDER BY id LIMIT 1").fetchone()
            r = json.loads(d["reason"])
            thursday = at(2026, 10, 22, 20, 30)
            monday = at(2026, 10, 26, 13, 45)
            self.assertLess(market_hours_between(thursday, monday), 24)               # the market clock alone would call it fresh
            r["candidate"]["age_hours"], r["decided_at"] = 0.2, iso(thursday)
            w.c.execute("UPDATE decisions SET created=?, reason=? WHERE id=?", (iso(thursday), json.dumps(r), d["id"]))
            w.cyc(monday)
            self.assertEqual((w.buy()["status"], w.positions()), ("EXPIRED", 0))
        finally:
            w.tearDown()


def event(c, kind, scope, direction, mag, source, created, cred=0.9, headline="x", gate=None, tickers="[]"):
    c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES(?,?,?,?,?,?,?,0.9)", ("%s-%s-%s" % (headline[:20], source, created), source, headline, "", "u", created, created))
    c.execute("INSERT INTO events(news_uid,created,published,source,headline,event_type,scope,direction,magnitude,credibility,tickers_json,gate) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              ("%s-%s-%s" % (headline[:20], source, created), created, created, source, headline, kind, scope, direction, mag, cred, tickers, gate))


def spy(c, prev, now_price, at_time, probe=True):
    """SPY's previous close and where it trades now (as the 10-minute quote probe recorded it)."""
    c.execute("INSERT OR REPLACE INTO market_series(symbol,day,open,high,low,close,volume) VALUES('SPY','2026-10-28',?,?,?,?,1)", (prev, prev, prev, prev))
    if probe:
        c.execute("INSERT INTO quote_consensus(ticker,created,session,status,consensus_price,provider_count,max_deviation) VALUES('SPY',?,'REGULAR','PASS',?,2,0.0)", (iso(at_time - dt.timedelta(minutes=5)), now_price))


class TestAShockHasToBeAShock(unittest.TestCase):
    NOW = at(2026, 10, 29, 13, 45)
    SINCE = iso(at(2026, 10, 29, 8, 0))
    T = iso(at(2026, 10, 29, 12, 0))

    def shock(self, c, market=-0.02, **risk):
        """market = how far SPY is down today (None = no market data at all)."""
        if market is not None:
            spy(c, prev=100.0, now_price=100.0 * (1 + market), at_time=self.NOW)
        cfg = H.cfg()
        cfg["risk"].update(shock_hold_hours=6, shock_direction=-0.6, **risk)
        return pretrade.market_shock(c, self.SINCE, self.NOW, cfg)

    def test_the_stories_that_froze_the_real_queue_are_not_shocks(self):
        c = H.fresh_db()
        for kind, scope, d, m, h in (("central_bank", "macro", -0.81, 0.60, "Bitcoin weakens to $80k as hawkish Fed, mixed tech earnings weigh"),
                                     ("central_bank", "macro", -0.79, 0.60, "UK government borrowing costs fall as City welcomes no harm budget"),
                                     ("geopolitical_conflict", "macro", -0.81, 0.75, "Outcry after Call of Duty maker uses real footage of deadly Seoul crowd crush"),
                                     ("credit_stress", "macro", -0.65, 0.70, "8-K - Fidelity Private Credit Fund (0001920453) (Filer)")):
            for src in ("Wire", "Wire2", "Wire3"):                                           # even reported by three outlets: the TYPE is not a shock
                event(c, kind, scope, d, m, src, self.T, headline=h)
        self.assertIsNone(self.shock(c))

    def test_a_real_crash_needs_more_than_one_outlet(self):
        c = H.fresh_db()
        event(c, "market_crash", "macro", -0.85, 0.97, "Wire", self.T, headline="Dow plunges 1,500 points as global stock market crash deepens")
        self.assertIsNone(self.shock(c))                                                     # one outlet: could be a mislabel or a rumour
        event(c, "market_crash", "macro", -0.85, 0.97, "Wire2", self.T, headline="Dow plunges 1,500 points as global stock market crash deepens")
        self.assertEqual(self.shock(c)["event_type"], "market_crash")                        # two outlets: it is real
        self.assertIsNotNone(self.shock(c, shock_min_sources=2))
        self.assertIsNone(self.shock(c, shock_min_sources=3))                                # the owner can demand more confirmation

    def test_one_outlet_is_enough_if_the_owner_says_so_and_old_or_company_events_never_count(self):
        c = H.fresh_db()
        event(c, "market_crash", "macro", -0.85, 0.97, "Wire", self.T, headline="Dow plunges 1,500 points as global stock market crash deepens")
        self.assertIsNotNone(self.shock(c, shock_min_sources=1))
        c2 = H.fresh_db()
        for src in ("Wire", "Wire2"):
            event(c2, "market_crash", "macro", -0.85, 0.97, src, iso(at(2026, 10, 29, 3, 0)), headline="Dow plunges 1,500 points as global stock market crash deepens")                      # older than the 6-hour window
            event(c2, "market_crash", "company", -0.85, 0.97, src, self.T, headline="Dow plunges 1,500 points as global stock market crash deepens")                                         # about one company
        self.assertIsNone(self.shock(c2))

    def test_a_war_story_alone_does_not_freeze_trading_the_market_must_actually_be_falling(self):
        """The 30 Oct case: a Ukraine train-station attack (war_escalation, -0.85, many outlets) held an AVGO BUY through the whole session. The headline must NAME the shock (a drone attack); the same
        tragedy worded as 'people killed in attack on Ukraine train station' is ordinary news (build 37: see test_shock_rules.py)."""
        h = "Russian drone attack on Ukrainian train station kills at least 14, officials say"
        for mkt in (-0.002, 0.004, None):                                                    # market flat / up / unknown
            c = H.fresh_db()
            for src in ("Wire", "Wire2", "Wire3"):
                event(c, "war_escalation", "macro", -0.85, 0.8, src, self.T, headline=h)
            self.assertIsNone(self.shock(c, market=mkt), mkt)
        c = H.fresh_db()
        for src in ("Wire", "Wire2"):
            event(c, "war_escalation", "macro", -0.85, 0.8, src, self.T, headline=h)
        self.assertEqual(self.shock(c, market=-0.015)["event_type"], "war_escalation")        # the same story WITH a falling market is a shock
        self.assertIsNone(self.shock(c, market=-0.015, shock_market_drop_pct=0.02))           # the owner can demand a bigger fall
        self.assertIsNotNone(self.shock(c, market=None, shock_market_drop_pct=0))              # 0 = headlines alone suffice (the old behaviour, on request)

    def test_the_market_move_comes_from_the_live_probe_else_todays_bar(self):
        c = H.fresh_db()
        c.execute("INSERT OR REPLACE INTO market_series(symbol,day,open,high,low,close,volume) VALUES('SPY','2026-10-28',100,100,100,100,1)")
        self.assertIsNone(pretrade.market_reaction(c, self.NOW))                                  # no data today: unknown, never assumed
        c.execute("INSERT INTO market_series(symbol,day,open,high,low,close,volume) VALUES('SPY','2026-10-29',99,99,98,98.4,1)")
        self.assertAlmostEqual(pretrade.market_reaction(c, self.NOW), -0.016, places=4)           # today's bar
        c.execute("INSERT INTO quote_consensus(ticker,created,session,status,consensus_price,provider_count,max_deviation) VALUES('SPY',?,'REGULAR','PASS',97.0,2,0)", (iso(self.NOW - dt.timedelta(minutes=10)),))
        self.assertAlmostEqual(pretrade.market_reaction(c, self.NOW), -0.03, places=4)            # a fresh probe quote wins
        self.assertAlmostEqual(pretrade.market_reaction(c, self.NOW + dt.timedelta(hours=2)), -0.016, places=4)      # a probe quote older than 45 minutes is ignored: back to today's bar
        c.execute("DELETE FROM quote_consensus")
        c.execute("INSERT INTO quote_consensus(ticker,created,session,status,consensus_price,provider_count,max_deviation) VALUES('SPY',?,'PRE_MARKET','VALUATION_ONLY',90.0,1,0)", (iso(self.NOW - dt.timedelta(minutes=10)),))
        self.assertAlmostEqual(pretrade.market_reaction(c, self.NOW), -0.016, places=4)           # a stale pre-market valuation is never treated as today's move

    def test_a_confirmed_crash_still_holds_the_buy_end_to_end(self):
        w = TP.Scenario()
        w.setUp()
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            w.cyc(TL.T_PRE + dt.timedelta(minutes=30), [TP.WAR])                              # reported by two outlets (see Scenario.cyc)
            w.cyc(TL.T_OPEN)
            self.assertEqual(w.buy()["status"], "HELD")
        finally:
            w.tearDown()

    def test_the_same_crash_from_a_single_outlet_does_not_freeze_the_buy(self):
        w = TP.Scenario()
        w.setUp()
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            w.cyc(TL.T_PRE + dt.timedelta(minutes=30), [TP.WAR], sources=TL.SRC)              # a single outlet
            w.cyc(TL.T_OPEN)
            self.assertEqual((w.buy()["status"], w.positions()), ("EXECUTED", 1))
        finally:
            w.tearDown()


class TestOnlyNewsAboutTheCompanyCancelsItsBuy(unittest.TestCase):
    SINCE = iso(at(2026, 10, 29, 8, 0))
    T = iso(at(2026, 10, 29, 12, 0))

    def veto(self, c, ticker, universe="uni"):
        return pretrade.adverse_company_news(c, ticker, self.SINCE, H.cfg(), None, RF.uni() if universe == "uni" else None)

    def test_headline_order_decides_who_a_story_is_about(self):
        u = RF.uni()
        self.assertEqual(u.subjects("U.S. stock futures drop as Alphabet slides on soft earnings, Microsoft firms"), ["GOOGL", "MSFT"])
        self.assertEqual(u.subjects("Microsoft earnings beat estimates as Azure growth accelerates"), ["MSFT"])
        self.assertEqual(u.subjects("Q3 results beat expectations"), [])

    def test_alphabet_slides_microsoft_firms_cancels_alphabet_not_microsoft(self):
        c = H.fresh_db()
        event(c, "earnings_miss", "company", -0.7, 0.6, "Wire", self.T, headline="U.S. stock futures drop as Alphabet slides on soft earnings, Microsoft firms", tickers='["GOOGL","MSFT"]')
        self.assertIsNotNone(self.veto(c, "GOOGL"))
        self.assertIsNone(self.veto(c, "MSFT"))                                              # this is the BUY that was wrongly cancelled on the real open

    def test_roundups_commentary_and_recaps_never_cancel_a_buy(self):
        c = H.fresh_db()
        event(c, "earnings_miss", "company", -0.71, 0.7, "Wire", self.T, headline="INTC, AMD, AVGO, MU Decline On Samsung's Profit Miss, Lighter Capex From Microsoft, Alphabet", gate="too_many_companies", tickers='["AVGO","MSFT","GOOGL"]')
        event(c, "opinion_piece", "company", -0.6, 0.6, "Wire", self.T, headline="Broadcom: Is it time to sell?", tickers='["AVGO"]')
        event(c, "price_recap", "company", -0.6, 0.6, "Wire", self.T, headline="Why Broadcom Stock Dropped Today", tickers='["AVGO"]')
        self.assertIsNone(self.veto(c, "AVGO", universe=None))                              # excluded even without a universe

    def test_real_company_news_still_cancels_with_or_without_a_universe(self):
        c = H.fresh_db()
        event(c, "guidance_cut", "company", -0.8, 0.7, "Wire", self.T, headline="NVIDIA cuts full-year outlook as DOJ opens probe into export violations", tickers='["NVDA"]')
        self.assertIsNotNone(self.veto(c, "NVDA"))
        self.assertIsNotNone(self.veto(c, "NVDA", universe=None))

    def test_a_bankruptcy_filing_cancels_the_buy(self):
        c = H.fresh_db()
        a = nlp.analyze("8-K - NVIDIA CORP (0001045810) (Filer)", "Filed: 2026-10-29 Item 1.03: Bankruptcy or Receivership Item 9.01")
        event(c, a["event_type"], a["scope"], a["direction"], a["magnitude"], "SEC", self.T, headline="8-K - NVIDIA CORP (0001045810) (Filer)", tickers='["NVDA"]')
        self.assertEqual(self.veto(c, "NVDA")["event_type"], "bankruptcy_distress")


class TestSecFilingsAreReadForWhatTheySay(unittest.TestCase):
    def test_filing_headers_are_neutral_whatever_the_company_is_called(self):
        for title, summary in (("8-K - JAB Acquisition Corp I (0002128739) (Filer)", "Item 5.07: Submission of Matters to a Vote of Security Holders Item 9.01"),
                               ("8-K - Techlott Inc. (0001568969) (Filer)", "Item 5.07: Submission of Matters to a Vote of Security Holders"),
                               ("8-K - Fidelity Private Credit Fund (0001920453) (Filer)", "Item 8.01: Other Events"),
                               ("4 - StepStone Private Credit Income Fund (0002006100) (Issuer)", ""),
                               ("8-K - Acme Corp (0000000001) (Filer)", "Item 2.02: Results of Operations and Financial Condition Item 9.01"),
                               ("10-Q - Big Bancorp (0000000002) (Filer)", ""), ("SC 13G/A - Holder LLC (0000000003) (Subject)", "")):
            a = nlp.analyze(title, summary)
            self.assertEqual((a["event_type"], a["direction"], a["magnitude"]), ("sec_filing", 0.0, 0.10), title)

    def test_only_the_adverse_items_say_anything_and_only_for_an_8k(self):
        for item, direction in (("1.03", -0.90), ("3.01", -0.60), ("4.02", -0.70), ("2.04", -0.50), ("2.06", -0.40), ("1.02", -0.30)):
            a = nlp.analyze("8-K - Acme Corp (0000000001) (Filer)", "Filed: x Item %s: whatever Item 9.01" % item)
            self.assertEqual((a["event_type"], a["direction"]), ("bankruptcy_distress", direction), item)
        a = nlp.analyze("10-Q - Acme Corp (0000000001) (Filer)", "Item 1.03: Bankruptcy")
        self.assertEqual(a["event_type"], "sec_filing")

    def test_real_news_that_merely_looks_similar_is_untouched(self):
        self.assertEqual(nlp.analyze("Acme Acquisition Corp to buy rival for $2 billion", "")["event_type"], "merger_acquisition")
        self.assertEqual(nlp.analyze("NVIDIA beats estimates and raises full-year guidance as data center demand surges", "")["event_type"], "guidance_raise")
        self.assertEqual(nlp.analyze("4 reasons Nvidia is a buy", "")["event_type"], "opinion_piece")                  # "4 reasons" is not a Form 4


class TestReopeningWhatTheOldRulesGotWrong(unittest.TestCase):
    def queued(self, gate, status="REJECTED"):
        w = TP.Scenario()
        w.setUp()
        w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
        d = w.c.execute("SELECT id,reason FROM decisions WHERE ticker='NVDA' AND action='BUY' ORDER BY id LIMIT 1").fetchone()
        r = json.loads(d["reason"])
        r["gates"] = list(r["gates"]) + [gate]
        w.c.execute("UPDATE decisions SET status=?, reason=? WHERE id=?", (status, json.dumps(r), d["id"]))
        return w, d["id"]

    def test_a_buy_the_old_rules_rejected_is_judged_again_and_trades(self):
        for gate, status in (("eligibility 61.20 < 65.00", "REJECTED"), ('VETO: news about NVDA arrived after this BUY was decided and points the other way (earnings_miss, direction -0.70): "x"', "VETOED"),
                             ('HELD: market-wide shock since this BUY was decided (central_bank, direction -0.81): "Bitcoin weakens"', "HELD")):
            w, did = self.queued(gate, status)
            try:
                self.assertEqual(len(trader.reopen_superseded(w.c, TL.T_OPEN, 36, apply=False)), 1)
                self.assertEqual(w.c.execute("SELECT status FROM decisions WHERE id=?", (did,)).fetchone()[0], status)         # showing it changes nothing
                self.assertEqual(len(trader.reopen_superseded(w.c, TL.T_OPEN, 36, apply=True)), 1)
                self.assertEqual(w.c.execute("SELECT status FROM decisions WHERE id=?", (did,)).fetchone()[0], "AWAITING_MARKET")
                self.assertIn("re-opened", json.loads(w.c.execute("SELECT reason FROM decisions WHERE id=?", (did,)).fetchone()[0])["gates"][-1])        # the page can say why it came back
                w.cyc(TL.T_OPEN)
                self.assertEqual((w.buy()["status"], w.positions()), ("EXECUTED", 1), gate)
            finally:
                w.tearDown()

    def test_real_rejections_stay_final(self):
        for gate, status in (("swing reward:risk 0.82 below minimum 1.00 (target 1.2% vs stop 1.5%)", "REJECTED"), ("VETO: NVDA opened 9.0% below its previous close (120.00 -> 109.20): the market disagrees", "VETOED"),
                             ("VETO: the headline is about MSFT, not NVDA: this BUY came from an older, looser company match", "VETOED"), ("EV net -0.40% <= 0", "REJECTED"),
                             ("already holding NVDA (position 3): a second position in the same stock", "DUPLICATE")):
            w, did = self.queued(gate, status)
            try:
                self.assertEqual(trader.reopen_superseded(w.c, TL.T_OPEN, 36, apply=True), [], gate)
                self.assertEqual(w.c.execute("SELECT status FROM decisions WHERE id=?", (did,)).fetchone()[0], status)
            finally:
                w.tearDown()

    def test_old_decisions_and_ones_that_already_became_positions_are_left_alone(self):
        w, did = self.queued("eligibility 61.20 < 65.00")
        try:
            self.assertEqual(trader.reopen_superseded(w.c, TL.T_OPEN + dt.timedelta(hours=100), 36, apply=True), [])             # too old to matter
            w.c.execute("INSERT INTO positions(ticker,opened,entry_price,shares,cost,status,decision_id) VALUES('NVDA',?,100,10,1000,'OPEN',?)", (iso(TL.T_OPEN), did))
            self.assertEqual(trader.reopen_superseded(w.c, TL.T_OPEN, 36, apply=True), [])
        finally:
            w.tearDown()

    def test_the_command_lists_then_does_it(self):
        from paper_agent import __main__ as cli
        w, did = self.queued("eligibility 61.20 < 65.00")
        try:
            w.c.commit()
            path = w.c.execute("PRAGMA database_list").fetchone()[2]
            out, saved = io.StringIO(), cli.DB_PATH
            cli.DB_PATH = path
            try:
                with mock.patch.object(cli, "utcnow", return_value=TL.T_OPEN), contextlib.redirect_stdout(out):
                    self.assertEqual(cli.main(["requeue"]), 0)
                    self.assertIn("1 decision(s) would be re-opened", out.getvalue())
                    self.assertEqual(sqlite3.connect(path).execute("SELECT status FROM decisions WHERE id=?", (did,)).fetchone()[0], "REJECTED")
                    self.assertEqual(cli.main(["requeue", "--apply"]), 0)
                self.assertIn("1 decision(s) re-opened", out.getvalue())
                self.assertEqual(sqlite3.connect(path).execute("SELECT status FROM decisions WHERE id=?", (did,)).fetchone()[0], "AWAITING_MARKET")
            finally:
                cli.DB_PATH = saved
        finally:
            w.tearDown()


class TestAFailingRetryIsNeverSilent(unittest.TestCase):
    """The owner's Funnel showed re-opened BUYs 'waiting' with their old note and nothing in Positions. A retry that THROWS used to leave the decision untouched and add a new ERROR row each cycle, so it looked stuck with no reason."""

    def boom(self, *a, **k):
        raise RuntimeError("boom: provider returned something unexpected")

    def test_the_failure_is_written_on_the_waiting_decision_without_spawning_rows(self):
        w = TP.Scenario()
        w.setUp()
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            before = w.c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            real = trader.process_candidate
            with mock.patch.object(trader, "process_candidate", side_effect=lambda *a, **k: self.boom() if k.get("existing_decision") else real(*a, **k)):
                w.cyc(TL.T_OPEN)
                w.cyc(TL.T_OPEN + dt.timedelta(minutes=5))
            self.assertEqual(w.c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], before)                 # no new ERROR rows each cycle
            self.assertEqual(w.buy()["status"], "AWAITING_MARKET")                                                 # still waiting, not wrongly dead after two failures
            self.assertRegex(w.gate(), r"^retry failed \(2 in a row\) at 13:45 UTC: RuntimeError: boom")           # ...and the page can say WHY it is stuck
        finally:
            w.tearDown()

    def test_five_failures_in_a_row_become_an_error_and_a_success_clears_the_count(self):
        w = TP.Scenario()
        w.setUp()
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            real = trader.process_candidate
            flaky = lambda *a, **k: self.boom() if k.get("existing_decision") else real(*a, **k)           # noqa: E731
            with mock.patch.object(trader, "process_candidate", side_effect=flaky):
                for i in range(2):
                    w.cyc(TL.T_OPEN + dt.timedelta(minutes=5 * i))
            self.assertIn("retry failed (2 in a row)", w.gate())
            w.cyc(TL.T_OPEN + dt.timedelta(minutes=15))                                                          # the fault clears: it trades, and the failure note is gone
            self.assertEqual((w.buy()["status"], w.positions()), ("EXECUTED", 1))
            self.assertNotIn("retry_failures", w.buy()["reason"])
        finally:
            w.tearDown()
        w = TP.Scenario()
        w.setUp()
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            with mock.patch.object(trader, "process_candidate", side_effect=lambda *a, **k: self.boom()):
                for i in range(5):
                    w.cyc(TL.T_OPEN + dt.timedelta(minutes=5 * i))
            self.assertEqual(w.buy()["status"], "ERROR")                                                         # a permanent fault is eventually called what it is
            self.assertIn("retry failed (5 in a row)", w.gate())
        finally:
            w.tearDown()

    def test_the_funnel_shows_the_failure_as_what_the_decision_is_waiting_for(self):
        w = TP.Scenario()
        w.setUp()
        try:
            w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            real = trader.process_candidate
            with mock.patch.object(trader, "process_candidate", side_effect=lambda *a, **k: self.boom() if k.get("existing_decision") else real(*a, **k)):
                w.cyc(TL.T_OPEN)
            from paper_agent import funnel
            pend = funnel.report(w.c, 6, TL.T_OPEN)["pending"]
            self.assertIn("retry failed", pend[0]["waiting_for"])
        finally:
            w.tearDown()

    def test_a_fresh_candidate_that_fails_is_still_a_visible_error_decision(self):
        w = TP.Scenario()
        w.setUp()
        try:
            with mock.patch.object(trader, "process_candidate", side_effect=lambda *a, **k: self.boom()):
                w.cyc(TL.T_PRE, [TL.NVDA_NEWS])
            self.assertEqual([r[0] for r in w.c.execute("SELECT status FROM decisions")], ["ERROR"])
        finally:
            w.tearDown()


class TestRewardRisk(unittest.TestCase):
    def test_the_default_minimum_is_one_not_one_point_two(self):
        base = config.load_config("/nonexistent", with_overrides=False)
        self.assertEqual(base["strategy"]["min_reward_risk"], 1.0)
        self.assertEqual(base["risk"]["shock_min_sources"], 2)


if __name__ == "__main__":
    unittest.main()
