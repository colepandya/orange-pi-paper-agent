"""Build 47: entry/exit consistency (the bot never opens a position its own monitor would close at once), issuer-level scoring, matcher corrections found on the live universe, filing prune, derivative securities."""
import datetime as dt
import itertools
import json
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
import test_fake_internet as FI_T  # noqa: E402
from paper_agent import db, monitor, pipeline  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

T_OPEN = dt.datetime(2026, 10, 20, 14, 0, tzinfo=dt.timezone.utc)
REG = {"regime": "neutral", "risk_mult": 0.8}


class TestEntryAndMonitorCannotDisagree(unittest.TestCase):
    """The same judgement, applied to a position that does not exist yet and to one that does."""

    def setUp(self):
        self.orig = monitor.evidence_engine.ticker_evidence

    def tearDown(self):
        monitor.evidence_engine.ticker_evidence = self.orig

    def build(self, side, state, net_strength, prob):
        c, cfg = H.fresh_db(), H.cfg()
        c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,status,thesis_key,sector,side) VALUES('TKR','t',?,100,100,10000,'OPEN','tk1','X',?)", (iso(T_OPEN), side))
        pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(?,?,?,?,?)", (pid, "OPEN", 100, 10000, iso(T_OPEN)))
        if state:
            c.execute("INSERT INTO theses(key,name,prior,probability,confidence_quality,uncertainty,trend,state,support_score,contradiction_score,net_strength,evidence_mass,active) VALUES('tk1','Datacentres',0.5,?,0.8,0.2,0.0,?,0.5,0.1,?,5.0,1)", (prob, state, net_strength))
        c.commit()
        return c, cfg

    def test_for_every_combination_the_entry_gate_refuses_exactly_what_the_monitor_would_close(self):
        n = 0
        for side, state, net_strength, ticker_net in itertools.product(("LONG", "SHORT"), (None, "Strong", "Active", "Weakening", "Broken"), (-0.5, 0.0, 0.6), (-1.5, 0.0, 1.5)):
            c, cfg = self.build(side, state, net_strength, 0.85)
            monitor.evidence_engine.ticker_evidence = lambda conn, t, now, _n=ticker_net: {"net": _n}
            ent = monitor.entry_check(c, cfg, "TKR", "BUY" if side == "LONG" else "SHORT", "tk1" if state else None, REG, T_OPEN + dt.timedelta(hours=1))
            monitor.monitor(c, cfg, REG, T_OPEN + dt.timedelta(hours=1))
            sigs = [r["trigger"] for r in c.execute("SELECT trigger FROM exit_signals")]
            self.assertEqual(ent["would_exit"], bool(sigs), (side, state, net_strength, ticker_net, ent["why"], sigs))
            n += 1
        self.assertEqual(n, 90)

    def test_the_judgement_itself_not_just_its_agreement_with_the_monitor(self):
        cases = [("LONG", -1.5, True, "position health"), ("LONG", 1.5, False, ""), ("LONG", 0.0, False, ""), ("SHORT", 1.5, True, "seen from the short side"), ("SHORT", -1.5, False, ""), ("SHORT", 0.0, False, "")]
        for side, net, expect, why in cases:
            c, cfg = self.build(side, None, 0.0, 0.5)
            monitor.evidence_engine.ticker_evidence = lambda conn, t, now, _n=net: {"net": _n}
            ent = monitor.entry_check(c, cfg, "TKR", "BUY" if side == "LONG" else "SHORT", None, REG, T_OPEN)
            self.assertEqual(ent["would_exit"], expect, (side, net, ent))
            self.assertIn(why, ent["why"])

    def test_a_short_inside_a_strong_bullish_thesis_is_refused_with_the_reason(self):
        c, cfg = self.build("SHORT", "Strong", 0.4, 0.9)
        monitor.evidence_engine.ticker_evidence = lambda conn, t, now: {"net": 0.0}
        v = [{"key": "tk1", "name": "Datacentres", "sign": 1, "state": "Strong", "probability": 0.9, "tilted_edge": 0.4}]
        with unittest.mock.patch.object(monitor.thesis_engine, "views_for_ticker", lambda conn, t, r: v):
            ent = monitor.entry_check(c, cfg, "TKR", "SHORT", "tk1", REG, T_OPEN)
        self.assertTrue(ent["would_exit"])
        self.assertIn("victim of strong thesis Datacentres", ent["why"])

    def test_a_long_in_the_same_thesis_is_not_refused(self):
        c, cfg = self.build("LONG", "Strong", 0.4, 0.9)
        monitor.evidence_engine.ticker_evidence = lambda conn, t, now: {"net": 0.2}
        v = [{"key": "tk1", "name": "Datacentres", "sign": 1, "state": "Strong", "probability": 0.9, "tilted_edge": 0.4}]
        with unittest.mock.patch.object(monitor.thesis_engine, "views_for_ticker", lambda conn, t, r: v):
            self.assertFalse(monitor.entry_check(c, cfg, "TKR", "BUY", "tk1", REG, T_OPEN)["would_exit"])


import test_guards as TG  # noqa: E402


class TestAddingIsNeverAccidental(unittest.TestCase):
    """Your live settings allow adding to a stock. Three ideas about AME queued overnight all filled at the open: one 14.5% position that lost $4,750 at once."""

    def run_world(self, **risk):
        w = TG.World()
        w.setUp()
        try:
            w.cfg["risk"]["allow_add_to_position"] = True
            w.cfg["risk"].update(risk)
            w.cyc(TG.TL.T_PRE, [TG.TL.NVDA_NEWS, TG.SECOND])
            w.cyc(TG.TL.T_OPEN)
            return [d["status"] for d in w.decisions()], w.one("SELECT COUNT(*) FROM positions WHERE ticker='NVDA'")[0]
        finally:
            w.tearDown()

    def test_two_ideas_queued_before_the_open_make_one_position_not_two(self):
        statuses, n = self.run_world()
        self.assertEqual(n, 1)
        self.assertIn("DUPLICATE", statuses)

    def test_switching_both_protections_off_restores_the_old_behaviour_on_purpose(self):
        statuses, n = self.run_world(add_cooldown_hours=0.0, add_only_when_winning=False)
        self.assertNotIn("DUPLICATE", statuses)
        self.assertEqual(n, 2)

    def test_the_cooldown_is_a_setting_with_a_sensible_default_and_the_settings_page_lists_it(self):
        from paper_agent import settings as S, config
        d = config.load_config("/nonexistent", with_overrides=False)
        self.assertEqual((d["risk"]["add_cooldown_hours"], d["risk"]["add_only_when_winning"]), (6.0, True))
        self.assertTrue({"risk.add_cooldown_hours", "risk.add_only_when_winning"} <= {f.path for f in S.FIELDS})


class TestAddRuleBranchByBranch(unittest.TestCase):
    """Each protection alone, long and short: the two used to mask each other."""

    def test_the_cooldown_alone(self):
        from paper_agent.trader import add_block
        self.assertIn("same bet bought twice", add_block("AME", 6, "LONG", 100.0, 105.0, 0.2, 6.0, False))        # a WINNER bought minutes ago still waits
        self.assertIsNone(add_block("AME", 6, "LONG", 100.0, 105.0, 7.0, 6.0, False))
        self.assertIsNone(add_block("AME", 6, "LONG", 100.0, 105.0, 0.2, 0.0, False))                            # 0 turns it off

    def test_never_add_to_a_loser_alone_for_a_long_and_for_a_short(self):
        from paper_agent.trader import add_block
        self.assertIn("losing 5.0%", add_block("AME", 6, "LONG", 100.0, 95.0, 50.0, 0.0, True))
        self.assertIsNone(add_block("AME", 6, "LONG", 100.0, 105.0, 50.0, 0.0, True))
        self.assertIn("losing 5.0%", add_block("DOV", 9, "SHORT", 100.0, 105.0, 50.0, 0.0, True))                # a short LOSES when the price rises
        self.assertIsNone(add_block("DOV", 9, "SHORT", 100.0, 95.0, 50.0, 0.0, True))
        self.assertIsNone(add_block("AME", 6, "LONG", 100.0, 95.0, 50.0, 0.0, False))                            # setting off
        self.assertIsNone(add_block("AME", 6, "LONG", 100.0, None, 50.0, 0.0, True))                             # no price: the guard does not guess

    def test_in_a_real_cycle_the_cooldown_alone_stops_the_second_fill_and_off_it_does_not(self):
        for cool, expect in ((6.0, 1), (0.0, 2)):
            w = TG.World()
            w.setUp()
            try:
                w.cfg["risk"].update(allow_add_to_position=True, add_cooldown_hours=cool, add_only_when_winning=False)
                w.cyc(TG.TL.T_PRE, [TG.TL.NVDA_NEWS, TG.SECOND])
                w.cyc(TG.TL.T_OPEN)
                self.assertEqual(w.one("SELECT COUNT(*) FROM positions WHERE ticker='NVDA'")[0], expect, cool)
            finally:
                w.tearDown()

    def test_the_rule_is_total_no_input_can_raise_into_a_trade(self):
        from paper_agent.trader import add_block
        bad = [None, "x", [], {}, float("nan"), float("inf"), -float("inf"), 1e308, -1e308]
        for a, b, c_, d in itertools.product(bad, bad, bad, bad):
            r = add_block("T", a, "SHORT", b, c_, d, 6.0, True)
            self.assertTrue(r is None or isinstance(r, str))
        self.assertIsNone(add_block("T", 1, "LONG", 100.0, float("nan"), 50.0, 0.0, True))                      # no usable price: it does not guess
        self.assertIsNone(add_block("T", 1, "LONG", 100.0, 105.0, None, 6.0, False))                              # unknown age counts as old

    def test_the_side_the_monitor_judges_is_the_one_the_forecast_took(self):
        from paper_agent.trader import entry_side
        self.assertEqual((entry_side({"verdict": "SHORT"}), entry_side({"verdict": "BUY"}), entry_side({})), ("SHORT", "BUY", "BUY"))


class TestTheNewOverview(unittest.TestCase):
    def test_worth_now_is_the_current_value_not_the_cost_and_a_short_is_its_buy_back_cost(self):
        import re
        from paper_agent import dashboard
        c = H.fresh_db()
        c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,status,sector,side) VALUES('DOV','t',?,212.43,221,46948,'OPEN','X','SHORT')", (iso(T_OPEN),))
        pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        h = {"position_id": pid, "ticker": "DOV", "side": "SHORT", "shares": 221.0, "cost": 46948.0, "price": 202.56, "value": -44766.0, "unrealized": 2182.0, "price_source": "TEST", "price_age": 5, "thesis_key": None}
        out = re.sub(r"<[^>]+>", " ", dashboard.positions_section(c, H.cfg(), {"hs": [h], "now": T_OPEN}))
        self.assertIn("$46,948", out)
        self.assertIn("$44,766", out)
        self.assertIn("$2,182", out)
        self.assertIn("4.65%", out)

    def test_the_strategy_version_moved_because_decision_logic_changed(self):
        from paper_agent import stamp
        self.assertEqual(stamp.STRATEGY_VERSION, "S16")

    def render(self, tab):
        import re
        from paper_agent import config, dashboard
        c = H.fresh_db()
        c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,status,sector,side) VALUES('DOV','t',?,212.43,221,46948,'OPEN','X','SHORT')", (iso(T_OPEN),))
        pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(?,?,?,?,?)", (pid, "OPEN", 221, 46948, iso(T_OPEN)))
        c.commit()
        return dashboard.render(c, H.cfg(), tab, {}, {"authed": True})

    def test_the_dossier_and_positions_tabs_are_gone_and_old_links_land_on_the_overview(self):
        from paper_agent import dashboard
        self.assertNotIn("positions", dashboard.TABS)
        self.assertNotIn("dossiers", dashboard.TABS)
        for tab in ("positions", "dossiers"):
            self.assertIn("Open positions", self.render(tab))
        self.assertNotIn("tab=dossiers", self.render("overview"))
        self.assertNotIn("tab=positions", self.render("overview"))

    def test_the_overview_has_positions_in_dollars_and_no_theses_or_event_lists(self):
        page = self.render("overview")
        for need in ("Open positions", "Invested $", "Worth now $", "Gain / loss $", "Closed positions", "Invested now"):
            self.assertIn(need, page)
        for gone in ("Leading theses", "Latest events"):
            self.assertNotIn(gone, page)

    def test_a_short_shows_the_dollar_value_sold_and_the_cost_to_buy_it_back_with_a_total(self):
        import re
        page = re.sub(r"<[^>]+>", " ", self.render("overview"))
        self.assertIn("$46,948", page)                                                                     # invested: what was sold short
        self.assertIn("Total", page)

    def test_the_cash_card_separates_short_sale_proceeds_from_cash_that_is_yours(self):
        import re
        c = H.fresh_db()
        c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,status,sector,side) VALUES('DOV','t',?,212.43,221,46948,'OPEN','X','SHORT')", (iso(T_OPEN),))
        pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(?,?,?,?,?)", (pid, "OPEN", 221, 46948, iso(T_OPEN)))
        c.commit()
        from paper_agent import dashboard
        page = re.sub(r"<[^>]+>", " ", dashboard.render(c, H.cfg(), "overview", {}, {"authed": True}))
        for need in ("Cash in account", "Owed back on shorts", "Your cash", "$46,948"):
            self.assertIn(need, page)
        cash = float(c.execute("SELECT cash FROM daily_values ORDER BY day DESC LIMIT 1").fetchone()[0]) if c.execute("SELECT COUNT(*) FROM daily_values").fetchone()[0] else None
        self.assertTrue(cash is None or cash > 0)

    def test_a_loss_is_written_minus_dollar_not_dollar_minus(self):
        from paper_agent import dashboard
        self.assertEqual((dashboard.dm(-1459.4), dashboard.dm(2181.6), dashboard.dm(0)), ("-$1,459", "$2,182", "$0"))


class TestTheGateStopsAPositionFromOpeningInARealCycle(FI_T.Env):
    def morning(self, patch=None, cycles=10):
        c = H.fresh_db()
        cfg = H.cfg()
        cfg["strategy"]["swing_enabled"] = False
        cfg["runtime"]["min_available_memory_mb"] = 0
        inet = FI_T.FI.Internet(FI_T.T0, self.co, self.heads, 1, 0.0, False, False)
        errors = []
        with inet, (patch or unittest.mock.patch.object(monitor, "entry_check", monitor.entry_check)):
            for k in range(cycles):
                now = FI_T.T0 + dt.timedelta(minutes=5 * k)
                inet.now = now
                errors += pipeline.run_cycle(c, cfg, self.uni, FI_T.SRC, pipeline.Deps(sleep=lambda s: None), now)["errors"]
        return c, errors

    def test_normally_trades_happen_and_when_the_monitor_would_close_them_none_open_and_the_reason_is_recorded(self):
        base, e1 = self.morning()
        opened = base.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        self.assertGreater(opened, 0)
        fake = {"would_exit": True, "why": "position is a victim of strong thesis X", "health": 20.0, "state": "Strong", "signals": [("RISK_ESCALATION", 0.6, "position is a victim of strong thesis X")]}
        blocked, e2 = self.morning(unittest.mock.patch.object(monitor, "entry_check", lambda *a, **k: fake))
        self.assertEqual((e1, e2), ([], []))
        self.assertEqual(blocked.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 0)
        rows = blocked.execute("SELECT reason FROM decisions WHERE status='RISK_REJECTED'").fetchall()
        self.assertTrue(rows)
        self.assertTrue(any("the position monitor would close this at once" in r["reason"] for r in rows))
        self.assertTrue(any("monitor_check" in json.loads(r["reason"]) for r in rows))

    def test_the_gate_asks_about_the_side_the_forecast_took_and_the_stock_it_is_about_to_buy(self):
        from paper_agent import trader
        seen = []

        def recorder(conn, cfg, ticker, side, thesis_key, regime, now=None):
            seen.append((ticker, side))
            return {"would_exit": False, "why": "", "health": 50.0, "state": "n/a", "signals": []}
        with unittest.mock.patch.object(trader, "entry_side", lambda f: "SENTINEL_SIDE"):
            c, errors = self.morning(unittest.mock.patch.object(monitor, "entry_check", recorder))
        self.assertEqual(errors, [])
        self.assertTrue(seen)
        self.assertEqual({s for _t, s in seen}, {"SENTINEL_SIDE"})
        self.assertTrue({t for t, _s in seen} <= {r[0] for r in c.execute("SELECT DISTINCT ticker FROM decisions")})

    def test_a_check_that_cannot_run_never_blocks_trading(self):
        def boom(*a, **k):
            raise RuntimeError("no thesis tables")
        c, errors = self.morning(unittest.mock.patch.object(monitor, "entry_check", boom))
        self.assertEqual(errors, [])
        self.assertGreater(c.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()


class TestFilingPruneMigration(unittest.TestCase):
    def setUp(self):
        from paper_agent import kb, kbsnap
        self.kb, self.c = kb, db.open_db(":memory:")
        kb.bootstrap(self.c, {"BIG": {"name": "Big Bank Corp", "cik": 1, "exchange": "NYSE", "aliases": []}, "SML": {"name": "Small Co", "cik": 2, "exchange": "NYSE", "aliases": []}}, dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc))
        self.big, self.small = kbsnap.entity_of(self.c, "BIG"), kbsnap.entity_of(self.c, "SML")
        now = "2026-11-03T00:00:00+00:00"
        rows = [(self.big, "BIG", "0000000001-26-%06d" % i, "424B2", "2026-10-%02d" % (1 + i % 28), "securities_offering") for i in range(3000)]
        rows += [(self.big, "BIG", "0000000001-26-9%05d" % i, "4", "2026-10-%02d" % (1 + i % 28), "insider_transaction") for i in range(500)]
        rows += [(self.big, "BIG", "0000000001-26-8%05d" % i, "8-K", "2026-09-%02d" % (1 + i), "auditor_change") for i in range(3)]
        rows += [(self.small, "SML", "0000000002-26-%06d" % i, "8-K", "2026-10-%02d" % (1 + i), "material_agreement") for i in range(5)]
        for eid, tk, acc, form, filed, et in rows:
            self.c.execute("INSERT INTO kb_filing_events(entity_id,cik,ticker,accession,form,filed,event_type,first_known,retrieved) VALUES(?,?,?,?,?,?,?,?,?)", (eid, 1, tk, acc, form, filed, et, now, now))
        self.c.commit()
        self.cfg = H.cfg()

    def test_the_prune_removes_routine_supplements_caps_the_noisy_and_never_touches_material_events(self):
        r = self.kb.prune_filings(self.c, self.cfg)
        self.assertEqual(r["routine_note_supplements_removed"], 3000)
        by = dict(self.c.execute("SELECT event_type,COUNT(*) FROM kb_filing_events WHERE entity_id=? GROUP BY 1", (self.big,)).fetchall())
        self.assertEqual(by["insider_transaction"], 12)
        self.assertEqual(by["auditor_change"], 3)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM kb_filing_events WHERE entity_id=?", (self.small,)).fetchone()[0], 5)
        self.assertLessEqual(r["largest_company_now"], 60)

    def test_twenty_material_events_of_one_type_all_survive_even_though_the_noisy_cap_is_twelve(self):
        for i in range(20):
            self.c.execute("INSERT INTO kb_filing_events(entity_id,cik,ticker,accession,form,filed,event_type,first_known,retrieved) VALUES(?,?,?,?,?,?,?,?,?)", (self.small, 2, "SML", "0000000002-26-7%05d" % i, "8-K", "2026-08-%02d" % (1 + i), "bankruptcy_or_restructuring", "2026-11-03T00:00:00+00:00", "2026-11-03T00:00:00+00:00"))
        self.c.commit()
        self.kb.prune_filings(self.c, self.cfg)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM kb_filing_events WHERE entity_id=? AND event_type='bankruptcy_or_restructuring'", (self.small,)).fetchone()[0], 20)

    def test_it_is_idempotent_and_the_stage_runs_it_exactly_once(self):
        from paper_agent import kbstage
        self.kb.prune_filings(self.c, self.cfg)
        n = self.c.execute("SELECT COUNT(*) FROM kb_filing_events").fetchone()[0]
        self.assertEqual(self.kb.prune_filings(self.c, self.cfg)["pruned_to_policy"], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM kb_filing_events").fetchone()[0], n)
        c2 = db.open_db(":memory:")
        self.assertFalse(kbstage._prune_once(c2, self.cfg).get("done_before"))
        self.assertTrue(kbstage._prune_once(c2, self.cfg)["done_before"])


class TestMatcherCorrectionsFromTheLiveUniverse(unittest.TestCase):
    from paper_agent.entities import Universe as _U
    W = {"JPM": {"name": "JPMORGAN CHASE & CO", "aliases": ["JPMORGAN CHASE & CO", "JPMORGAN CHASE"], "cik": 19617, "exchange": "NYSE"}, "VYLD": {"name": "JPMORGAN CHASE & CO", "aliases": ["JPMORGAN CHASE & CO", "JPMORGAN CHASE"], "cik": 19617, "exchange": "NYSE"},
         "ABCD": {"name": "Abc Acquisition Corp", "aliases": ["Abc Acquisition"], "cik": 77, "exchange": "Nasdaq"}, "ABCDW": {"name": "Abc Acquisition Corp", "aliases": ["Abc Acquisition"], "cik": 77, "exchange": "Nasdaq"}, "ABCDU": {"name": "Abc Acquisition Corp", "aliases": ["Abc Acquisition"], "cik": 77, "exchange": "Nasdaq"},
         "GOOGL": {"name": "Alphabet Inc.", "aliases": ["Alphabet"], "cik": 1652044, "exchange": "Nasdaq"}, "GOOG": {"name": "Alphabet Inc.", "aliases": ["Alphabet"], "cik": 1652044, "exchange": "Nasdaq"},
         "ICE": {"name": "Intercontinental Exchange, Inc.", "aliases": ["Intercontinental Exchange"], "cik": 1571949, "exchange": "NYSE"}, "NVDA": {"name": "NVIDIA CORP", "aliases": ["Nvidia"], "cik": 1045810, "exchange": "Nasdaq"},
         "NYT": {"name": "NEW YORK TIMES CO", "aliases": ["New York Times"], "cik": 71691, "exchange": "NYSE"}, "CBOE": {"name": "Cboe Global Markets, Inc.", "aliases": ["Cboe Global Markets", "Cboe"], "cik": 1374310, "exchange": "CBOE"},
         "TSLA": {"name": "Tesla, Inc.", "aliases": ["Tesla"], "cik": 1318605, "exchange": "Nasdaq"}}

    def hits(self, h, s=""):
        return sorted(t for t, c, w in self._U(self.W, set()).match(h, s) if c >= 0.5)

    def test_a_filing_header_resolves_to_the_issuers_own_ticker_not_whichever_was_listed_last(self):
        U = self._U(self.W, set())
        self.assertEqual(U.by_cik[19617], "JPM")
        self.assertEqual(U.by_cik[77], "ABCD")

    def test_warrants_and_units_are_securities_of_the_issuer_and_never_match_its_name(self):
        U = self._U(self.W, set())
        self.assertEqual(U.derivative, {"ABCDW", "ABCDU"})
        self.assertEqual(self.hits("Abc Acquisition Corp announces IPO pricing"), ["ABCD"])
        self.assertIn("ABCDW", self.hits("Abc Acquisition warrants (NASDAQ: ABCDW) begin trading"))

    def test_share_classes_that_are_real_common_stock_are_not_treated_as_derivatives(self):
        self.assertEqual(self.hits("Alphabet shares rise"), ["GOOG", "GOOGL"])

    def test_a_bare_ticker_needs_a_market_context_or_company_to_count(self):
        self.assertEqual(self.hits("Sources: tech companies resisting broad ICE demands"), [])
        self.assertEqual(self.hits("NVDA drops 22% after hours"), ["NVDA"])
        self.assertEqual(self.hits("TSLA, NVDA lead gains"), ["NVDA", "TSLA"])

    def test_only_a_news_organisation_can_be_a_mere_source_and_a_company_that_said_something_is_the_subject(self):
        self.assertEqual(self.hits("Trump rages at a senator", "It was first reported by the New York Times, which cited aides."), [])
        self.assertEqual(self.hits("Tesla recalls cars - The New York Times"), ["TSLA"])
        self.assertEqual(self.hits("Chipmaker beats estimates", "Nvidia said demand stayed strong."), ["NVDA"])

    def test_the_firm_that_rates_a_company_is_not_the_company(self):
        self.assertEqual(self.hits("JPMORGAN CHASE Maintains Underweight on Cboe Global Markets, Raises Price Target to $332".title()), ["CBOE"])
        self.assertEqual(self.hits("JPMorgan Chase reports record profit"), ["JPM", "VYLD"])


class TestTilesAreEven(unittest.TestCase):
    """The Overview tiles must be the same size in every row, with labels and values that can never wrap."""

    def test_the_tile_css_fixes_width_height_and_wrapping(self):
        from paper_agent import dashboard
        css = dashboard.PAGE_CSS if hasattr(dashboard, "PAGE_CSS") else open(dashboard.__file__).read()
        self.assertIn("repeat(auto-fill,minmax(200px,1fr))", css)                      # auto-fit stretched each row differently
        for need in ("min-height:72px", "white-space:nowrap", "text-overflow:ellipsis", "justify-content:space-between", "min-width:0"):
            self.assertIn(need, css)

    def test_every_overview_tile_label_is_short_enough_to_fit_on_one_line(self):
        import re
        from paper_agent import dashboard
        c = H.fresh_db()
        c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,status,sector,side) VALUES('DOV','t',?,212.43,221,46948,'OPEN','X','SHORT')", (iso(T_OPEN),))
        pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(?,?,?,?,?)", (pid, "OPEN", 221, 46948, iso(T_OPEN)))
        c.commit()
        page = dashboard.render(c, H.cfg(), "overview", {}, {"authed": True})
        labels = re.findall(r"<div class=card[^>]*><span>([^<]*)</span>", page)
        self.assertGreaterEqual(len(labels), 12)
        for lab in labels:
            self.assertLessEqual(len(lab), 24, lab)                                       # at 12px that is under 160px: it fits a 200px tile with room to spare

    def test_a_tile_carries_its_full_text_as_a_tooltip_for_the_rare_ellipsis(self):
        from paper_agent import dashboard
        self.assertIn('title="Equity: $1"', dashboard.card("Equity", "$1"))
