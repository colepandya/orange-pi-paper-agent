"""The relationship graph: an event at one company becomes a smaller, discounted idea at its suppliers / customers / competitors / peers, with the RIGHT SIGN for each kind of event, no crowds, no
contradictions, nothing from noise. Plus the correlation gate: two stocks that move together are one bet."""
import datetime as dt
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import db, graph, pipeline, trader  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

SEED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "relations.json")
NOW = TL.T_PRE


def cand(ticker="NVDA", et="earnings_beat", direction=0.8, mag=0.8, **kw):
    c = {"ticker": ticker, "event_type": et, "direction": direction, "magnitude": mag, "conf": 0.8, "credibility": 0.9, "age_hours": 1.0, "horizon": 6, "event_id": 7, "news_uid": "u1",
         "headline": "%s news" % ticker, "origin": "event", "source": "Wire"}
    c.update(kw)
    return c


class Seeded(unittest.TestCase):
    def setUp(self):
        self.c = H.fresh_db()
        graph.seed(self.c, NOW, SEED)
        self.cfg = H.cfg()
        self.cfg["graph"]["enabled"] = True

    def derive(self, cands, universe=None):
        return graph.derive(self.c, self.cfg, cands, universe, NOW)

    def by(self, out):
        return {d["ticker"]: d for d in out}


class TestTheMap(Seeded):
    def test_roles_are_from_the_targets_point_of_view(self):
        r = {e["ticker"]: e["role"] for e in graph.related(self.c, "NVDA")}
        self.assertEqual(r["TSM"], "supplier")                        # TSM makes NVDA's chips
        self.assertEqual(r["SMCI"], "customer")                       # SMCI buys NVDA's chips
        r2 = {e["ticker"]: e["role"] for e in graph.related(self.c, "TSM")}
        self.assertEqual(r2["NVDA"], "customer")                      # and the same edge read from the other end

    def test_a_rival_in_the_same_peer_group_is_a_competitor_not_both(self):
        r = [e for e in graph.related(self.c, "NVDA") if e["ticker"] == "AMD"]
        self.assertEqual([e["role"] for e in r], ["competitor"])      # the specific relationship wins: peer and competitor would pull opposite ways

    def test_seeding_is_idempotent_and_skips_when_the_file_has_not_changed(self):
        n = self.c.execute("SELECT COUNT(*) FROM relations WHERE source='seed'").fetchone()[0]
        again = graph.seed(self.c, NOW, SEED)
        self.assertTrue(again.get("unchanged"))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM relations WHERE source='seed'").fetchone()[0], n)
        self.assertGreater(n, 500)

    def test_the_whole_seed_file_is_well_formed(self):
        d = graph.load_seed(SEED)
        for e in d["edges"]:
            self.assertIn(e["kind"], ("supplies", "competes", "peer"))
            self.assertTrue(0 < float(e["weight"]) <= 1)
            self.assertNotEqual(e["src"], e["dst"])
        for g in d["peer_groups"]:
            self.assertGreaterEqual(len(g["tickers"]), 2)
            self.assertEqual(len(set(g["tickers"])), len(g["tickers"]), g["name"])


class TestPropagation(Seeded):
    def test_a_supplier_follows_a_customers_good_news_and_a_competitor_takes_the_opposite_side(self):
        d = self.by(self.derive([cand("NVDA", "earnings_beat", 0.8)]))
        self.assertGreater(d["TSM"]["direction"], 0)                   # TSM supplies NVDA: a beat means more chip orders
        self.assertLess(d["AMD"]["direction"], 0)                      # AMD competes with NVDA: share lost
        self.assertEqual((d["TSM"]["graph_role"], d["AMD"]["graph_role"]), ("supplier", "competitor"))
        self.assertNotIn("SMCI", d)                                    # a customer does not benefit from its supplier's earnings

    def test_bad_news_flips_both(self):
        d = self.by(self.derive([cand("NVDA", "guidance_cut", -0.8)]))
        self.assertLess(d["TSM"]["direction"], 0)                      # fewer orders for the supplier
        self.assertGreater(d["AMD"]["direction"], 0)                   # the rival gains

    def test_a_supply_side_outage_hurts_the_suppliers_customers_too(self):
        d = self.by(self.derive([cand("TSM", "production_disruption", -0.8)]))
        self.assertLess(d["NVDA"]["direction"], 0)                     # NVDA cannot get its chips
        self.assertLess(d["AAPL"]["direction"], 0)
        self.assertLess(d["ASML"]["direction"], 0)                     # and TSM's own suppliers sell less

    def test_a_regulatory_action_spreads_to_the_sector(self):
        d = self.by(self.derive([cand("JPM", "regulatory_action", -0.8)]))
        self.assertLess(d["BAC"]["direction"], 0)                      # peers share the rule

    def test_company_only_news_never_spreads(self):
        for et in ("insider_filing", "analyst_action", "leadership_change", "buyback_dividend", "capital_raise", "thesis_signal"):
            self.assertEqual(self.derive([cand("NVDA", et, 0.9)]), [], et)

    def test_weak_or_doubtful_news_does_not_spread(self):
        self.assertEqual(self.derive([cand(mag=0.3)]), [])
        self.assertEqual(self.derive([cand(conf=0.4)]), [])
        self.assertEqual(self.derive([cand(direction=0.2)]), [])
        self.assertEqual(self.derive([cand(origin="thesis")]), [])
        self.assertEqual(self.derive([cand(origin="graph")]), [])      # a derived idea never spreads again

    def test_a_derived_idea_is_smaller_and_says_where_it_came_from(self):
        src = cand("NVDA", "earnings_beat", 0.8, 0.8)
        d = self.by(self.derive([src]))["TSM"]
        self.assertLess(abs(d["direction"]), abs(src["direction"]))
        self.assertLess(d["magnitude"], src["magnitude"])
        self.assertLess(d["conf"], src["conf"])
        self.assertLess(d["credibility"], src["credibility"])
        self.assertEqual((d["origin"], d["source"], d["chain_order"], d["graph_from"]), ("graph", "graph:supplier", 2, "NVDA"))
        self.assertIn("supplier of NVDA", d["mechanism"])
        self.assertEqual(d["event_id"], 7)

    def test_a_stronger_relationship_gives_a_stronger_idea(self):
        d = self.by(self.derive([cand("NVDA", "earnings_beat", 0.8)]))
        self.assertGreater(abs(d["TSM"]["direction"]), abs(d["MU"]["direction"]))     # TSM 0.55 vs MU 0.35

    def test_one_event_makes_at_most_a_few_ideas_and_a_cycle_at_most_twelve(self):
        self.assertLessEqual(len(self.derive([cand("NVDA", "earnings_beat", 0.8)])), 4)
        many = [cand(t, "earnings_beat", 0.8, event_id=i) for i, t in enumerate(("NVDA", "AMD", "AVGO", "MSFT", "JPM", "XOM", "LMT", "UNH", "WMT", "HD"))]
        self.assertLessEqual(len(self.derive(many)), 12)

    def test_no_duplicates_and_nothing_already_a_candidate(self):
        out = self.derive([cand("NVDA", "earnings_beat", 0.8), cand("TSM", "earnings_beat", 0.9, event_id=8)])
        t = [d["ticker"] for d in out]
        self.assertEqual(len(t), len(set(t)))
        self.assertNotIn("TSM", t)                                     # it has its own news already
        self.assertNotIn("NVDA", t)

    def test_only_stocks_in_the_universe_are_proposed(self):
        u = Universe({"NVDA": {"name": "NVIDIA Corp", "aliases": ["NVIDIA"]}, "TSM": {"name": "Taiwan Semiconductor", "aliases": ["TSMC"]}})
        t = [d["ticker"] for d in self.derive([cand("NVDA", "earnings_beat", 0.8)], u)]
        self.assertEqual(t, ["TSM"])

    def test_it_can_be_switched_off(self):
        self.cfg["graph"]["enabled"] = False
        self.assertEqual(self.derive([cand()]), [])


class TestMeasuredCoMovement(Seeded):
    def series(self, sym, rets, start=100.0):
        p, base = start, dt.date(2026, 8, 1)
        for i, r in enumerate(rets):
            p *= 1 + r
            self.c.execute("INSERT OR REPLACE INTO market_series(symbol,day,close,volume) VALUES(?,?,?,?)", (sym, (base + dt.timedelta(days=i)).isoformat(), p, 1000))
        self.c.commit()

    def test_correlation_is_measured_from_daily_returns(self):
        rnd = random.Random(1)
        a = [rnd.gauss(0, 0.01) for _ in range(80)]
        self.series("AAA", a)
        self.series("BBB", [x * 1.5 + rnd.gauss(0, 0.001) for x in a])               # moves with AAA
        self.series("CCC", [-x for x in a])                                         # moves against it
        self.series("DDD", [rnd.gauss(0, 0.01) for _ in range(80)])                 # unrelated
        self.assertGreater(graph.correlation(self.c, "AAA", "BBB"), 0.95)
        self.assertLess(graph.correlation(self.c, "AAA", "CCC"), -0.95)
        self.assertLess(abs(graph.correlation(self.c, "AAA", "DDD")), 0.4)
        self.assertIsNone(graph.correlation(self.c, "AAA", "NOPE"))                  # no history: no answer, not a guess

    def test_data_edges_link_strong_co_movers_only_and_are_refreshed(self):
        rnd = random.Random(2)
        a = [rnd.gauss(0, 0.01) for _ in range(80)]
        self.series("AAA", a)
        self.series("BBB", [x + rnd.gauss(0, 0.001) for x in a])
        self.series("DDD", [rnd.gauss(0, 0.01) for _ in range(80)])
        out = graph.refresh_data_edges(self.c, self.cfg, NOW, force=True)
        self.assertGreaterEqual(out["edges"], 2)
        rel = {(r["src"], r["dst"]) for r in self.c.execute("SELECT src,dst FROM relations WHERE source='data'")}
        self.assertIn(("AAA", "BBB"), rel)
        self.assertIn(("BBB", "AAA"), rel)
        self.assertFalse([1 for s, d in rel if "DDD" in (s, d)])
        self.assertEqual([e["role"] for e in graph.related(self.c, "AAA") if e["ticker"] == "BBB"], ["corr"])
        self.assertEqual(graph.refresh_data_edges(self.c, self.cfg, NOW)["skipped"], "not due")                                                   # once a day, not every cycle

    def test_the_gate_only_compares_with_holdings_on_the_same_side(self):
        rnd = random.Random(3)
        a = [rnd.gauss(0, 0.01) for _ in range(80)]
        self.series("AAA", a)
        self.series("BBB", a)
        hs = [{"ticker": "BBB", "side": "LONG"}]
        self.assertGreater(graph.max_same_side_corr(self.c, "AAA", hs, "LONG")[0], 0.99)
        self.assertEqual(graph.max_same_side_corr(self.c, "AAA", hs, "SHORT"), (0.0, None))       # a short against a correlated long is a HEDGE, not a double bet
        self.assertEqual(graph.max_same_side_corr(self.c, "BBB", hs, "LONG"), (0.0, None))        # itself does not count


class TestEndToEnd(TL.Base):
    """Through the real pipeline: bullish NVDA news also produces decisions for its supplier and (as a bearish idea) its rival, labelled as coming from the graph."""

    def setUp(self):
        super().setUp()
        self.cfg["graph"]["enabled"] = True
        graph.seed(self.c, TL.T_PRE, SEED)
        w = dict(H.WATCH)
        w["TSM"] = {"name": "Taiwan Semiconductor Manufacturing", "aliases": ["TSMC", "Taiwan Semiconductor"], "cik": 1046179}
        w["AMD"] = {"name": "Advanced Micro Devices", "aliases": ["AMD"], "cik": 2488}
        self.uni = Universe(w)
        self.mkt.price.update({"TSM": 180.0, "AMD": 160.0})

    def run_pre(self):
        self.mkt.now = TL.T_PRE
        items = [(TL.NVDA_NEWS, "AI capex", TL.RF(TL.T_PRE - dt.timedelta(minutes=40)))]
        return pipeline.run_cycle(self.c, self.cfg, self.uni, TL.SRC, self.deps(items), TL.T_PRE)

    def test_the_suppliers_and_rivals_of_a_beating_company_are_considered_and_labelled(self):
        r = self.run_pre()
        self.assertEqual(r["errors"], [])
        self.assertGreaterEqual(r["graph"]["derived"], 1)
        rows = {x["ticker"]: x for x in self.c.execute("SELECT ticker,origin,action,status,reason FROM decisions WHERE origin='graph'")}
        self.assertIn("TSM", rows)
        self.assertEqual(rows["TSM"]["action"], "BUY")
        self.assertIn("supplier of NVDA", rows["TSM"]["reason"])
        self.assertEqual(self.c.execute("SELECT origin FROM decisions WHERE ticker='NVDA'").fetchone()[0] in (None, "event"), True)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM decisions WHERE ticker='TSM'").fetchone()[0], 1)
        r2 = self.run_pre()                                                                       # the same news again must not create a second idea
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM decisions WHERE ticker='TSM'").fetchone()[0], 1)

    def test_with_the_graph_off_there_are_no_related_ideas(self):
        self.cfg["graph"]["enabled"] = False
        self.run_pre()
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM decisions WHERE origin='graph'").fetchone()[0], 0)


class TestTheCorrelationGate(TL.Base):
    def setUp(self):
        super().setUp()
        self.mkt.price["LMT"] = 450.0
        self.prev_close["LMT"] = 450.0
        self.c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,decision_id,status,thesis_key,sector,side) VALUES('NVDA','AI',?,120.0,1000,120000,NULL,'OPEN',NULL,'Semiconductors','LONG')", (iso(TL.T_PRE),))
        self.c.execute("INSERT INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(1,'OPEN',1000,120000,?)", (iso(TL.T_PRE),))
        self.c.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)", (iso(TL.T_PRE), "VIRTUAL_BUY", -120000.0, "NVDA:0", 880000.0))
        self.c.commit()

    def run_lmt(self, corr, side_dir=1.0):
        orig = graph.correlation
        graph.correlation = lambda *a, **k: corr
        try:
            c = {"ticker": "LMT", "event_type": "guidance_raise" if side_dir > 0 else "guidance_cut", "direction": side_dir, "magnitude": 1.0, "conf": 0.9, "credibility": 0.95, "age_hours": 0.5, "horizon": 6, "origin": "event",
                 "headline": "Lockheed %s its outlook" % ("raises" if side_dir > 0 else "cuts"), "source": "Wire"}
            self.mkt.now = TL.T_OPEN
            q = lambda conn, t, purpose: {"status": "PASS", "execution_eligible": True, "price": 450.0, "providers_used": ["X"], "quoted_at": iso(TL.T_OPEN), "age_seconds": 3}      # noqa: E731
            return trader.process_candidate(self.c, self.cfg, H.universe(), c, {"regime": "neutral", "risk_mult": 0.8}, TL.T_OPEN, q, self.series)
        finally:
            graph.correlation = orig

    def test_a_near_twin_of_a_holding_is_refused(self):
        r = self.run_lmt(0.97)
        self.assertEqual(r["status"], "RISK_REJECTED")
        self.assertIn("double one bet", r["why"])
        self.assertIn("NVDA", r["why"])

    def test_a_correlated_stock_is_taken_smaller(self):
        none = self.run_lmt(0.10)
        size_free = self.c.execute("SELECT shares FROM decisions WHERE ticker='LMT' ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(none["status"], "EXECUTED")
        self.c.execute("PRAGMA foreign_keys=OFF")
        for tbl, col in (("position_forecast_plans", "position_id"), ("position_lifecycle", "position_id"), ("virtual_exit_actions", "position_id")):
            self.c.execute("DELETE FROM %s WHERE %s>1" % (tbl, col))
        self.c.execute("DELETE FROM positions WHERE id>1")
        self.c.execute("DELETE FROM decisions WHERE ticker='LMT'")
        self.c.execute("DELETE FROM ledger WHERE kind='VIRTUAL_BUY' AND ref LIKE 'LMT:%'")
        self.c.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)", (iso(TL.T_PRE), "RESET", 0.0, "x", 880000.0))
        self.c.commit()
        self.c.execute("PRAGMA foreign_keys=ON")
        half = self.run_lmt(0.80)
        self.assertEqual(half["status"], "EXECUTED")
        size_cut = self.c.execute("SELECT shares FROM decisions WHERE ticker='LMT' ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertLess(size_cut, size_free)
        self.assertGreater(size_cut, size_free * 0.4)

    def test_a_correlated_hedge_on_the_other_side_is_not_penalised(self):
        self.cfg["instruments"]["shorts_enabled"] = True
        self.c.execute("INSERT OR REPLACE INTO companies(ticker,name,sector,market_cap,adv_usd,vol_daily,completeness,updated) VALUES('LMT','Lockheed','Defense',1e11,5e8,0.015,0.8,?)", (iso(TL.T_PRE),))
        self.c.commit()
        r = self.run_lmt(0.97, side_dir=-0.8)
        self.assertNotEqual(r.get("why", "") and "double one bet" in r.get("why", ""), True)


if __name__ == "__main__":
    unittest.main()
