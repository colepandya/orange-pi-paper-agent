"""One test per defect found in the v1-v4 codebase."""
import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import db, executor, nlp, pipeline, portfolio as pf, quotes, sizing, sources, timeutil  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402

RF = lambda d: d.strftime("%a, %d %b %Y %H:%M:%S +0000")  # noqa: E731
SRC = [{"id": "t", "name": "TestWire", "type": "rss", "url": "http://x/rss", "trust": 0.9, "interval": 0}]
NVDA_NEWS = "NVIDIA beats estimates and raises full-year guidance as data center demand surges"


def cycle(conn, items, prices, now=H.NOW, **kw):
    return pipeline.run_cycle(conn, H.cfg(), H.universe(), SRC, H.deps(items, prices), now, **kw)


class TestLegacyBuyCrash(unittest.TestCase):
    def test_buy_with_valid_quote_executes(self):
        """Legacy execute_virtual_buy referenced undefined `cash` -> NameError on every buy."""
        c = H.fresh_db()
        r = cycle(c, [(NVDA_NEWS, "AI capex", RF(H.NOW - dt.timedelta(hours=1)))], {"NVDA": 120.0})
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["entries"]["by_status"].get("EXECUTED"), 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0], 1)
        self.assertLess(db.cash(c), 1_000_000)


class TestScoringFalsePositives(unittest.TestCase):
    def test_substring_matches_gone(self):
        a = nlp.analyze("Hartford Bank reports results at the border of a second quarter")
        self.assertEqual(a["event_type"], "general_news")
        self.assertAlmostEqual(a["direction"], 0.0, places=1)

    def test_negation(self):
        self.assertLess(nlp.analyze("Acme fails to beat estimates")["direction"], 0)

    def test_short_tickers_not_matched_as_words(self):
        u = Universe({"ON": {"name": "ON Semiconductor Corp", "aliases": ["ON Semiconductor"]}, "ALL": {"name": "Allstate Corp", "aliases": ["Allstate"]}})
        self.assertEqual(u.match("IT and ON are common words, we are ALL ready"), [])

    def test_price_target_is_not_target_corp(self):
        u = Universe({"TGT": {"name": "Target Corp", "aliases": ["Target"]}, "MSFT": {"name": "Microsoft Corp", "aliases": ["Microsoft"]}})
        m = u.match("Analyst raises price target on Microsoft to $500")
        self.assertEqual([x[0] for x in m], ["MSFT"])

    def test_macro_polarity(self):
        self.assertGreater(nlp.analyze("Fed cuts interest rates by 25 basis points")["direction"], 0.3)
        self.assertGreater(nlp.analyze("Russia and Ukraine agree to ceasefire")["direction"], 0.2)
        self.assertLess(nlp.analyze("China conducts military drills around Taiwan")["direction"], -0.2)


class TestTimestamps(unittest.TestCase):
    def test_rfc822_numeric_offsets(self):
        """Legacy strptime('%Z') failed on +0000/-0400 so stale news looked fresh."""
        for s, h in (("Tue, 27 Oct 2026 14:00:00 -0400", 18), ("Tue, 27 Oct 2026 14:00:00 +0000", 14), ("Tue, 27 Oct 2026 14:00:00 GMT", 14)):
            self.assertEqual(timeutil.parse_ts(s).hour, h)

    def test_nanosecond_iso_and_epochs(self):
        self.assertIsNotNone(timeutil.parse_ts("2026-10-27T10:00:00.123456789Z"))
        self.assertEqual(timeutil.parse_ts(1793000000).year, 2026)
        self.assertEqual(timeutil.parse_ts("20261027T140000Z").hour, 14)
        self.assertIsNone(timeutil.parse_ts("garbage"))

    def test_old_news_is_stale_not_traded(self):
        c = H.fresh_db()
        r = cycle(c, [(NVDA_NEWS, "AI", RF(H.NOW - dt.timedelta(days=4)))], {"NVDA": 120.0})
        self.assertEqual(r["evidence"]["stale"], 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0)

    def test_calendar_is_correct_beyond_2026(self):
        self.assertEqual(timeutil.market_state(timeutil.parse_ts("2027-01-18T15:00:00Z"))["session"], "CLOSED")   # MLK 2027
        self.assertEqual(timeutil.market_state(timeutil.parse_ts("2027-03-26T15:00:00Z"))["session"], "CLOSED")   # Good Friday 2027
        self.assertEqual(timeutil.market_state(timeutil.parse_ts("2027-02-02T15:00:00Z"))["session"], "REGULAR")
        self.assertTrue(timeutil.early_close(dt.date(2026, 11, 27)))


class TestQuotes(unittest.TestCase):
    def setUp(self):
        self.c, self.cfg = H.fresh_db(), H.cfg()

    def prov(self, name, price, stamp):
        f = lambda t, fetch=None: quotes.obs(name, t, price, stamp=stamp)  # noqa: E731
        f.__name__ = name.lower()
        return f

    def test_nanosecond_stale_timestamp_rejected(self):
        """Legacy: Alpaca '...123456789Z' failed to parse -> age None -> accepted as fresh."""
        old = self.prov("ALPACA", 100, "2026-10-27T10:00:00.123456789Z")
        a, b = self.prov("A", 100, H.NOW.timestamp() - 5), self.prov("B", 100.1, H.NOW.timestamp() - 7)
        r = quotes.consensus(self.c, "MSFT", self.cfg, H.NOW, [a, b, old])
        self.assertEqual(r["status"], "PASS")
        self.assertNotIn("ALPACA", r["providers_used"])

    def test_untimestamped_quote_can_value_never_execute(self):
        a, nots = self.prov("A", 100, H.NOW.timestamp() - 3), self.prov("ALPHA_VANTAGE", 100.1, None)
        r = quotes.consensus(self.c, "MSFT", self.cfg, H.NOW, [a, nots])
        self.assertEqual(r["status"], "VALUATION_ONLY")
        self.assertFalse(r["execution_eligible"])

    def test_disagreeing_providers_fail_closed(self):
        a, b = self.prov("A", 100, H.NOW.timestamp() - 3), self.prov("B", 108, H.NOW.timestamp() - 3)
        self.assertNotEqual(quotes.consensus(self.c, "MSFT", self.cfg, H.NOW, [a, b])["status"], "PASS")

    def test_quote_cache_exists_on_fresh_db(self):
        """Legacy fabric.store() wrote to quote_cache which only another module created."""
        a, b = self.prov("A", 100, H.NOW.timestamp() - 3), self.prov("B", 100, H.NOW.timestamp() - 3)
        r = quotes.consensus(self.c, "MSFT", self.cfg, H.NOW, [a, b])
        quotes.store(self.c, r, H.NOW)
        self.assertEqual(self.c.execute("SELECT price FROM quote_cache WHERE ticker='MSFT'").fetchone()[0], 100)

    def test_finnhub_reads_real_env_var(self):
        """Legacy config pointed at an env var literally named 'REDACTED'."""
        os.environ["FINNHUB_API_KEY"] = "k"
        try:
            o = quotes.finnhub("MSFT", lambda url, **kw: ({"c": 100, "t": 1793000000}, 200))
            self.assertEqual(o["price"], 100.0)
        finally:
            del os.environ["FINNHUB_API_KEY"]


class TestWorldNeverSleeps(unittest.TestCase):
    def test_closed_market_still_updates_theses_and_defers_entries(self):
        """Legacy readiness gate required execution-eligible quotes for ALL positions, so the monitor never ran overnight/weekends."""
        c = H.fresh_db()
        sat = dt.datetime(2026, 10, 31, 3, 0, tzinfo=dt.timezone.utc)
        pub = sat - dt.timedelta(hours=1)
        r = cycle(c, [(NVDA_NEWS, "AI capex data center", RF(pub))], {"NVDA": 120.0}, now=sat)
        self.assertEqual(r["errors"], [])
        self.assertGreater(c.execute("SELECT COUNT(*) FROM evidence WHERE thesis_key='ai_infrastructure'").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT status FROM decisions WHERE ticker='NVDA'").fetchone()[0], "AWAITING_MARKET")
        # when the market opens the pending decision is retried and executes
        mon = dt.datetime(2026, 11, 2, 15, 0, tzinfo=dt.timezone.utc)
        c.execute("UPDATE decisions SET created=?", (pipeline.iso(mon - dt.timedelta(hours=2)),))
        r2 = cycle(c, [], {"NVDA": 120.0}, now=mon)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0], 1, r2["entries"])


class TestRiskGateIsLive(unittest.TestCase):
    def test_issuer_limit_blocks_oversize(self):
        """Legacy forecast_gate() was called without portfolio state so exposure limits never applied."""
        cfg = H.cfg()
        f = {"ticker": "NVDA", "ev_net": 0.03, "sigma_h": 0.05, "stop_return": -0.04, "confidence": 0.8, "risk_score": 30}
        hs = [{"ticker": "NVDA", "value": 150_000, "sector": "Semiconductors", "beta": 1.5, "country": "US", "industry": "", "market_cap": 3e12, "thesis_key": "ai_infrastructure"}]
        st = {"equity": 1e6, "cash": 500_000, "holdings": hs, "scenario_probs": {}}
        s = sizing.size_position(f, {"sector": "Semiconductors", "beta": 1.5, "adv_usd": 1e10}, st, cfg, {"risk_mult": 1.0}, {"state": "Strong", "score": 0.5, "primary": "ai_infrastructure"})
        self.assertEqual(s["pct"], 0.0)
        self.assertFalse(s["ok"])

    def test_stress_limit_shrinks_size(self):
        cfg = H.cfg()
        cfg["risk"]["max_stress_loss_pct"] = 0.05
        f = {"ticker": "NVDA", "ev_net": 0.05, "sigma_h": 0.05, "stop_return": -0.04, "confidence": 0.9, "risk_score": 20}
        st = {"equity": 1e6, "cash": 1e6, "holdings": [], "scenario_probs": {}}
        s = sizing.size_position(f, {"sector": "Semiconductors", "beta": 2.0, "adv_usd": 1e10}, st, cfg, {"risk_mult": 1.0}, {"state": "Strong", "score": 0.5, "primary": "ai_infrastructure"})
        self.assertLessEqual(s["pct"] * 0.40, 0.0501)


class TestEquityMarkToMarket(unittest.TestCase):
    def test_equity_uses_market_value_not_cost(self):
        """Legacy book_equity() summed position COST, hiding all unrealised P&L and drawdown."""
        c = H.fresh_db()
        r = cycle(c, [(NVDA_NEWS, "AI", RF(H.NOW - dt.timedelta(hours=1)))], {"NVDA": 120.0})
        shares = c.execute("SELECT shares FROM positions").fetchone()[0]
        c.execute("UPDATE quote_cache SET price=150 WHERE ticker='NVDA'")
        eq = pf.equity_of(c, now=H.NOW)
        self.assertAlmostEqual(eq["market_value"], shares * 150, places=2)
        self.assertGreater(eq["equity"], 1_000_000)


class TestMigration(unittest.TestCase):
    def test_legacy_db_preserved(self):
        p = tempfile.mktemp(suffix=".db")
        l = sqlite3.connect(p)
        l.executescript("""create table theses(decision_id integer primary key, model_version text); insert into theses values(7,'3.0.0');
        create table ledger(id integer primary key,created text,kind text,amount real,ref text,balance real);
        insert into ledger(created,kind,amount,ref,balance) values('x','START',1000000,'i',1000000),('y','VIRTUAL_BUY',-40000,'MSFT',960000);
        create table positions(id integer primary key,ticker text,theme text,opened text,entry_price real,shares integer,cost real,decision_id integer,status text,closed text,exit_price real,pnl real,review_code text);
        insert into positions(ticker,opened,entry_price,shares,cost,status) values('MSFT','2026-10-20T00:00:00+00:00',400,100,40000,'OPEN');""")
        l.commit()
        l.close()
        c = db.open_db(p)
        self.assertEqual(c.execute("SELECT decision_id FROM decision_theses").fetchone()[0], 7)
        self.assertEqual(db.cash(c), 960000)
        self.assertEqual(c.execute("SELECT remaining_shares FROM position_lifecycle").fetchone()[0], 100)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM theses").fetchone()[0], 0)    # new OMEGA table, distinct from legacy
        db.migrate(c)   # idempotent
        self.assertEqual(db.cash(c), 960000)
        # orphan legacy position gets a plan so the executor can manage it
        from paper_agent import monitor
        monitor.ensure_plans(c, H.cfg(), {"regime": "risk_on", "risk_mult": 1}, H.NOW)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM position_forecast_plans WHERE active=1").fetchone()[0], 1)


class TestSources(unittest.TestCase):
    def test_304_is_not_a_failure(self):
        c = H.fresh_db()
        src = {"id": "s", "name": "S", "type": "rss", "url": "http://x", "trust": .5, "interval": 0}
        r = sources.collect(c, [src], lambda url, **k: (b"", 304, {}))
        self.assertEqual((r["unchanged"], r["fetch_failures"]), (1, 0))

    def test_failure_backs_off_and_isolated(self):
        c = H.fresh_db()
        good = {"id": "g", "name": "G", "type": "rss", "url": "http://g", "trust": .5, "interval": 0}
        bad = {"id": "b", "name": "B", "type": "rss", "url": "http://b", "trust": .5, "interval": 0}

        def fetch(url, **k):
            if "b" == url[-1]:
                raise sources.net.FetchError("boom", 500)
            return H.rss([("Hello world headline here", "", RF(H.NOW))]), 200, {}
        r = sources.collect(c, [bad, good], fetch)
        self.assertEqual((r["fetch_failures"], r["new"]), (1, 1))
        self.assertFalse(sources.source_due(c, bad))     # in backoff

    def test_entity_bomb_rejected(self):
        with self.assertRaises(Exception):
            sources.parse_feed(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>')


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.c = H.fresh_db()
        cycle(self.c, [(NVDA_NEWS, "AI", RF(H.NOW - dt.timedelta(hours=1)))], {"NVDA": 120.0})
        self.cfg = H.cfg()
        self.plan = dict(self.c.execute("SELECT * FROM position_forecast_plans WHERE active=1").fetchone())

    def run_at(self, price, minutes=30, signal=None):
        if signal:
            self.c.execute("INSERT INTO exit_signals(position_id,created,trigger,severity,reason) VALUES(1,?,?,?,?)", (pipeline.iso(H.NOW), signal, .9, "test"))
        return executor.run(self.c, self.cfg, H.NOW + dt.timedelta(minutes=minutes), H.Quotes({"NVDA": price}))

    def test_stop(self):
        r = self.run_at(self.plan["stop_price"] - 0.5)
        self.assertEqual(r["committed"], 1)
        self.assertEqual(self.c.execute("SELECT review_code FROM positions").fetchone()[0], "STOP_REACHED")

    def test_hold_between(self):
        self.assertEqual(self.run_at(121)["committed"], 0)

    def test_stale_quote_refused(self):
        q = lambda c, t, p: {"status": "PASS", "price": 1.0, "execution_eligible": True, "age_seconds": 999}  # noqa: E731
        r = executor.run(self.c, self.cfg, H.NOW + dt.timedelta(minutes=30), q)
        self.assertEqual((r["committed"], r["blocked"]), (0, 1))

    def test_closed_market_blocks(self):
        r = executor.run(self.c, self.cfg, dt.datetime(2026, 10, 31, 15, 0, tzinfo=dt.timezone.utc), H.Quotes({"NVDA": 1.0}))
        self.assertEqual(r["committed"], 0)

    def test_thesis_signal_exits_at_market(self):
        r = self.run_at(121, signal="THESIS_BROKEN")
        self.assertEqual(r["committed"], 1)
        self.assertEqual(self.c.execute("SELECT review_code FROM positions").fetchone()[0], "THESIS_INVALIDATED")
        self.assertEqual(self.c.execute("SELECT status FROM exit_signals").fetchone()[0], "CONSUMED")

    def test_partial_target_then_runner(self):
        self.c.execute("UPDATE position_forecast_plans SET confidence=10")   # force 'follow exit plan' rather than hold-extension
        r = self.run_at(self.plan["target_price"] + 1, minutes=30)
        self.assertEqual(r["committed"], 1)
        life = self.c.execute("SELECT phase,remaining_shares FROM position_lifecycle").fetchone()
        self.assertEqual(life["phase"], "RUNNER_OPEN")
        self.assertGreater(life["remaining_shares"], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM position_forecast_plans WHERE active=1").fetchone()[0], 1)

    def test_idempotent_commit(self):
        p = self.c.execute("SELECT p.*,l.phase lphase FROM positions p JOIN position_lifecycle l ON l.position_id=p.id").fetchone()
        life = dict(self.c.execute("SELECT * FROM position_lifecycle").fetchone())
        plan = self.c.execute("SELECT * FROM position_forecast_plans WHERE active=1").fetchone()
        q = H.Quotes({"NVDA": 110.0})(self.c, "NVDA")
        aid = executor.action_id(1, plan["id"], "STOP_REACHED", "OPEN", life["remaining_shares"])
        a = executor.commit_action(self.c, self.cfg, p, life, plan, "STOP_REACHED", "t", q, life["remaining_shares"], aid, None, H.NOW)
        b = executor.commit_action(self.c, self.cfg, p, life, plan, "STOP_REACHED", "t", q, life["remaining_shares"], aid, None, H.NOW)
        self.assertEqual(a["result"], "CLOSED")
        self.assertEqual(b["result"], "ALREADY_COMMITTED")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM ledger WHERE kind='VIRTUAL_SELL_V5'").fetchone()[0], 1)

    def test_ledger_balances(self):
        self.run_at(self.plan["stop_price"] - 1)
        pnl = self.c.execute("SELECT pnl FROM positions").fetchone()[0]
        self.assertAlmostEqual(db.cash(self.c) - 1_000_000, pnl, places=1)


if __name__ == "__main__":
    unittest.main()
