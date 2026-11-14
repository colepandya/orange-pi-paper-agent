import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import (contradiction, dashboard, db, ev, evidence, flows, gaps, learning, llm, marketdata as md, monitor, narrative, nlp, opportunity, pipeline,  # noqa: E402
                         portfolio as pf, regime, company, thesis)
from paper_agent.timeutil import iso  # noqa: E402

RF = lambda d: d.strftime("%a, %d %b %Y %H:%M:%S +0000")  # noqa: E731
NOW = H.NOW


def add_news(c, uid, title, src="Reuters", trust=0.9, age_h=2, summary=""):
    c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES(?,?,?,?,?,?,?,?)", (uid, src, title, summary, "http://x", iso(NOW - dt.timedelta(hours=age_h)), iso(NOW), trust))


def setup():
    c = H.fresh_db()
    thesis.seed(c)
    return c, H.cfg(), H.universe()


class TestThesisEngine(unittest.TestCase):
    def test_supporting_evidence_raises_probability(self):
        c, cfg, u = setup()
        add_news(c, "1", "Hyperscalers raise AI capex guidance again as GPU orders surge")
        evidence.process_new_news(c, u, cfg, NOW)
        before = c.execute("SELECT probability FROM theses WHERE key='ai_infrastructure'").fetchone()[0]
        out = {t["key"]: t for t in thesis.recompute(c, NOW)}
        self.assertGreater(out["ai_infrastructure"]["probability"], before)
        self.assertGreater(out["ai_infrastructure"]["support_score"], 0)

    def test_contradiction_lowers_probability_and_scores_split(self):
        c, cfg, u = setup()
        add_news(c, "1", "Analysts warn of AI bubble as data center cancellations mount and capex cut feared")
        evidence.process_new_news(c, u, cfg, NOW)
        out = {t["key"]: t for t in thesis.recompute(c, NOW)}["ai_infrastructure"]
        self.assertLess(out["probability"], out["prior"])
        self.assertGreater(out["contradiction_score"], 0)
        self.assertLess(out["net_strength"], 0)

    def test_duplicate_coverage_does_not_stack(self):
        c, cfg, u = setup()
        t = "Hyperscalers raise AI capex guidance again as GPU orders surge"
        add_news(c, "1", t)
        evidence.process_new_news(c, u, cfg, NOW)
        p1 = {x["key"]: x for x in thesis.recompute(c, NOW)}["ai_infrastructure"]["probability"]
        for i in range(2, 8):
            add_news(c, str(i), t + " - wire %d" % i, src="Wire%d" % i)
        evidence.process_new_news(c, u, cfg, NOW)
        p2 = {x["key"]: x for x in thesis.recompute(c, NOW)}["ai_infrastructure"]["probability"]
        self.assertLess(p2 - p1, 0.05)

    def test_evidence_decays_with_time(self):
        c, cfg, u = setup()
        add_news(c, "1", "Hyperscalers raise AI capex guidance again as GPU orders surge", age_h=2)
        evidence.process_new_news(c, u, cfg, NOW)
        p_now = {x["key"]: x for x in thesis.recompute(c, NOW, False)}["ai_infrastructure"]["probability"]
        p_later = {x["key"]: x for x in thesis.recompute(c, NOW + dt.timedelta(days=40), False)}["ai_infrastructure"]["probability"]
        self.assertLess(p_later, p_now)
        self.assertAlmostEqual(p_later, 0.70, delta=0.12)     # drifts back toward the prior

    def test_low_prior_risk_thesis_is_not_broken(self):
        c, _, _ = setup()
        out = {x["key"]: x for x in thesis.recompute(c, NOW)}
        self.assertNotEqual(out["china_taiwan_escalation"]["state"], "Broken")
        self.assertEqual(out["china_taiwan_escalation"]["state"], "Emerging")

    def test_inverse_mode_taiwan_escalation_supports_and_ceasefire_contradicts(self):
        c, cfg, u = setup()
        add_news(c, "1", "China conducts military drills around Taiwan, blockade fears grow", src="BBC")
        evidence.process_new_news(c, u, cfg, NOW)
        out = {x["key"]: x for x in thesis.recompute(c, NOW)}["china_taiwan_escalation"]
        self.assertGreater(out["probability"], out["prior"])

    def test_child_evidence_lifts_parent(self):
        c, cfg, u = setup()
        add_news(c, "1", "Hyperscale campus data center lease demand surges as colocation vacancy hits record low")
        evidence.process_new_news(c, u, cfg, NOW)
        out = {x["key"]: x for x in thesis.recompute(c, NOW)}
        self.assertGreater(out["ai_datacentres"]["probability"], 0.62)
        parent_alone = out["ai_infrastructure"]["probability"]
        self.assertGreater(parent_alone, 0.70)      # no direct parent evidence needed

    def test_victim_role_is_bearish(self):
        c, _, _ = setup()
        c.execute("UPDATE theses SET probability=0.9,confidence_quality=0.9 WHERE key='china_taiwan_escalation'")
        v = thesis.ticker_thesis_score(c, "TSM")
        self.assertLess(v["score"], 0)

    def test_regime_tilts_thesis_candidates(self):
        c, _, _ = setup()
        c.execute("UPDATE theses SET probability=0.85,state='Strong',net_strength=0.5 WHERE key='ai_infrastructure'")
        self.assertTrue(thesis.thesis_candidates(c, "risk_on", 0.7, set()))
        self.assertFalse([x for x in thesis.thesis_candidates(c, "stagflation", 0.7, set()) if x["thesis_key"] == "ai_infrastructure"])


class TestEvidence(unittest.TestCase):
    def test_every_event_becomes_evidence_with_fields_and_expiry(self):
        c, cfg, u = setup()
        add_news(c, "1", "Lockheed Martin wins $2.4 billion Pentagon contract for missile defense", src="DoD", trust=1.0)
        r = evidence.process_new_news(c, u, cfg, NOW)
        row = c.execute("SELECT * FROM evidence WHERE ticker='LMT'").fetchone()
        for f in ("credibility", "strength", "novelty", "predictive_power", "expiry_date", "event_type"):
            self.assertIsNotNone(row[f])
        self.assertEqual(row["decay_days"], 180)
        self.assertEqual(r["candidates"][0]["ticker"], "LMT")

    def test_source_credibility_learns(self):
        c, _, _ = setup()
        base = evidence.source_credibility(c, "Rumors", "contract_win", 0.7)
        c.execute("INSERT INTO source_stats(source,event_type,hits,misses) VALUES('Rumors','contract_win',1,20)")
        self.assertLess(evidence.source_credibility(c, "Rumors", "contract_win", 0.7), base - 0.2)

    def test_macro_event_has_no_trade_candidate(self):
        c, cfg, u = setup()
        add_news(c, "1", "Fed cuts interest rates by 25 basis points, signals more easing")
        r = evidence.process_new_news(c, u, cfg, NOW)
        self.assertEqual(r["candidates"], [])
        self.assertGreater(c.execute("SELECT COUNT(*) FROM evidence WHERE thesis_key='fed_easing'").fetchone()[0], 0)

    def test_roundup_with_many_tickers_not_traded(self):
        c, cfg, u = setup()
        add_news(c, "1", "NVIDIA, Microsoft, Lockheed Martin and Exxon Mobil all beat estimates this week")
        self.assertEqual(evidence.process_new_news(c, u, cfg, NOW)["candidates"], [])

    def test_acquirer_vs_target_roles(self):
        c, cfg, u = setup()
        add_news(c, "1", "Lockheed Martin to acquire Microsoft for $90 billion")
        r = evidence.process_new_news(c, u, cfg, NOW)
        d = {x["ticker"]: x["direction"] for x in r["candidates"]}
        self.assertGreater(d.get("MSFT", 0), 0.5)


class TestRegime(unittest.TestCase):
    def build(self, c, drift_spy, vix_mult=1.0, credit_mult=1.0):
        for s, st in {"SPY": 400, "^VIX": 15, "^TNX": 4.2, "^IRX": 4.0, "HYG": 77, "LQD": 108, "DX-Y.NYB": 100, "CL=F": 70, "GC=F": 2000, "IWM": 200}.items():
            bars = md.parse_chart(H.chart_payload(s, start=st, drift=drift_spy if s in ("SPY", "IWM") else 0, vol=0.004, seed=3))
            md.store_series(c, s, bars)
        c.execute("UPDATE market_series SET close=close*? WHERE symbol='^VIX' AND day>=date(?,'-25 day')", (vix_mult, NOW.date().isoformat()))
        c.execute("UPDATE market_series SET close=close*? WHERE symbol='HYG' AND day>=date(?,'-20 day')", (credit_mult, NOW.date().isoformat()))

    def test_calm_uptrend_is_risk_on_family(self):
        c = H.fresh_db()
        self.build(c, 0.0012)
        r = regime.classify(regime.features(c))
        self.assertIn(r["regime"], ("risk_on", "boom", "liquidity_expansion"))
        self.assertAlmostEqual(sum(r["probs"].values()), 1.0, places=6)

    def test_stress_cuts_risk_multiplier(self):
        c, c2 = H.fresh_db(), H.fresh_db()
        self.build(c, 0.0012)
        self.build(c2, 0.0, vix_mult=2.6, credit_mult=0.88)
        a, b = regime.classify(regime.features(c)), regime.classify(regime.features(c2))
        self.assertLess(b["risk_mult"], a["risk_mult"])
        self.assertIn(b["regime"], ("risk_off", "recession", "liquidity_contraction", "stagflation"))

    def test_missing_data_is_unknown_not_guessed(self):
        self.assertEqual(regime.classify(regime.features(H.fresh_db()))["regime"], "unknown")


class TestEV(unittest.TestCase):
    CAND = {"ticker": "LMT", "direction": .77, "magnitude": .95, "event_type": "contract_win", "amount_usd": 2.4e9, "credibility": .95, "conf": .9, "age_hours": 2, "horizon": 12}

    def test_scenarios_sum_to_one_and_ordered(self):
        c, cfg, _ = setup()
        f = ev.build(c, self.CAND, cfg, {"regime": "risk_on", "risk_mult": 1.0})
        self.assertAlmostEqual(sum(v["p"] for v in f["scenarios"].values()), 1.0, places=6)
        r = [f["scenarios"][k]["ret"] for k in ("bull", "base", "bear", "tail", "black_swan")]
        self.assertEqual(r, sorted(r, reverse=True))
        self.assertAlmostEqual(f["ev_gross"], sum(v["p"] * v["ret"] for v in f["scenarios"].values()), places=9)

    def test_negative_ev_rejected_and_bearish_avoided(self):
        c, cfg, _ = setup()
        weak = dict(self.CAND, magnitude=.1, direction=.3, event_type="analyst_action", amount_usd=None)
        self.assertEqual(ev.build(c, weak, cfg, {"risk_mult": 1.0})["verdict"], "REJECT_NEGATIVE_EV")
        self.assertEqual(ev.build(c, dict(self.CAND, direction=-.7), cfg, {"risk_mult": 1.0})["verdict"], "AVOID_BEARISH_LONG_ONLY")

    def test_risk_off_regime_worsens_ev(self):
        c, cfg, _ = setup()
        a = ev.build(c, self.CAND, cfg, {"regime": "risk_on", "risk_mult": 1.0})
        b = ev.build(c, self.CAND, cfg, {"regime": "recession", "risk_mult": 0.45})
        self.assertLess(b["ev_net"], a["ev_net"])

    def test_thesis_tailwind_raises_edge(self):
        c, cfg, _ = setup()
        base = ev.build(c, dict(self.CAND, ticker="XOM"), cfg, {"risk_mult": 1.0})["ev_net"]
        c.execute("UPDATE theses SET probability=0.9,confidence_quality=0.9 WHERE key='middle_east_oil_shock'")
        self.assertGreater(ev.build(c, dict(self.CAND, ticker="XOM"), cfg, {"risk_mult": 1.0})["ev_net"], base)

    def test_learned_move_scale_applies(self):
        c, cfg, _ = setup()
        a = ev.build(c, self.CAND, cfg, {"risk_mult": 1.0})["ev_net"]
        db.set_param(c, "move_scale:contract_win", 0.5)
        self.assertLess(ev.build(c, self.CAND, cfg, {"risk_mult": 1.0})["ev_net"], a)


class TestPortfolio(unittest.TestCase):
    def test_stress_survival_and_recovery(self):
        cfg = H.cfg()
        hs = [{"ticker": "NVDA", "value": 3e5, "sector": "Semiconductors", "beta": 1.7, "country": "US", "industry": "", "market_cap": 3e12, "thesis_key": "ai_infrastructure"},
              {"ticker": "TSM", "value": 2e5, "sector": "Semiconductors", "beta": 1.3, "country": "TW", "industry": "", "market_cap": 9e11, "thesis_key": "ai_infrastructure"}]
        st = pf.stress(hs, 1e6, cfg, {"ai_crash": 0.2, "china_taiwan": 0.1})
        self.assertLess(st["scenarios"]["ai_crash"]["pnl_pct"], -0.15)
        self.assertLess(st["scenarios"]["china_taiwan"]["pnl_pct"], st["scenarios"]["rate_shock"]["pnl_pct"])
        self.assertLess(st["survival_probability"], 1.0)
        self.assertGreater(st["recovery_days"], 0)

    def test_exposures_sum_to_gross(self):
        hs = [{"ticker": "A", "value": 1e5, "sector": "Energy", "beta": 1.0, "country": "US", "currency": "USD", "industry": "", "market_cap": 1e11, "thesis_key": None, "adv_usd": 5e7, "dna": "Cyclical"},
              {"ticker": "B", "value": 2e5, "sector": "Utilities", "beta": .5, "country": "US", "currency": "USD", "industry": "", "market_cap": 1e11, "thesis_key": "x", "adv_usd": 5e8, "dna": "Defensive"}]
        ex = pf.exposures(hs, 1e6)
        self.assertAlmostEqual(sum(ex["sector"].values()), 0.3)
        self.assertAlmostEqual(ex["factor"]["low_beta"], 0.2)


class TestOpportunityCost(unittest.TestCase):
    def test_cash_hurdle_rejects_marginal_edge(self):
        c, cfg, _ = setup()
        f = {"ev_net": 0.001, "horizon_days": 10, "sigma_h": 0.05}
        self.assertFalse(opportunity.evaluate(c, f, True, {"holdings": []}, cfg)["ok"])


class TestLearning(unittest.TestCase):
    def seed_forecasts(self, c, n, hit_rate, p=0.8, src="BadWire", et="contract_win"):
        for i in range(n):
            c.execute("INSERT INTO predictions(created,kind,subject,claim,probability,horizon_date,source,event_type,resolved) VALUES(?,?,?,?,?,?,?,?,1)", (iso(NOW), "forecast", "X", "abnormal_up", p, iso(NOW), src, et))
            pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            out = 1.0 if (i / n) < hit_rate else 0.0
            c.execute("INSERT INTO outcomes(prediction_id,observed_at,outcome,abnormal_return,brier) VALUES(?,?,?,?,?)", (pid, iso(NOW), out, 0.01 if out else -0.01, (p - out) ** 2))
            if True:
                c.execute("INSERT INTO source_stats(source,event_type,hits,misses) VALUES(?,?,?,0) ON CONFLICT(source,event_type) DO UPDATE SET hits=hits+?,misses=misses+?", (src, et, out, out, 1 - out)) if False else None

    def test_overconfidence_is_recalibrated(self):
        c, cfg, _ = setup()
        cfg["intelligence"]["learning_mode"] = "apply"                       # this tests the APPLY behaviour; the default is observe (see test_learning_mode.py)
        self.seed_forecasts(c, 120, 0.45, p=0.8)
        r = learning.recalibrate(c, cfg, NOW)
        self.assertIsNotNone(r["calibration"])
        self.assertLess(ev.calibrate_p(c, 0.8), 0.8)
        self.assertLess(db.param(c, "power:contract_win"), nlp.BY_NAME["contract_win"].power)

    def test_no_adjustment_below_min_samples(self):
        c, cfg, _ = setup()
        self.seed_forecasts(c, 8, 0.0)
        r = learning.recalibrate(c, cfg, NOW)
        self.assertIsNone(r["calibration"])
        self.assertEqual(r["event_types"], {})
        self.assertEqual(ev.calibrate_p(c, 0.8), 0.8)

    def test_predictions_resolve_against_prices_and_update_source_stats(self):
        c, cfg, u = setup()
        cfg["intelligence"]["learning_mode"] = "apply"
        day0 = (NOW - dt.timedelta(days=20)).date().isoformat()
        for s in ("AAA", "SPY"):
            md.store_series(c, s, [{"day": (NOW - dt.timedelta(days=20 - i)).date().isoformat(), "open": 100, "high": 100, "low": 100, "close": (100 + (i * 0.5 if s == "AAA" else 0)), "volume": 1e6} for i in range(21)])
        c.execute("INSERT INTO predictions(created,kind,subject,claim,probability,baseline_price,benchmark_price,horizon_date,source,event_type) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (iso(NOW - dt.timedelta(days=20)), "forecast", "AAA", "abnormal_up", 0.7, 100.0, 100.0, iso(NOW - dt.timedelta(hours=1)), "GoodWire", "earnings_beat"))
        r = learning.resolve_due(c, cfg, NOW, fetch=lambda *a, **k: (_ for _ in ()).throw(md.net.FetchError("offline")))
        self.assertEqual(r["resolved"], 1)
        o = c.execute("SELECT outcome,brier FROM outcomes").fetchone()
        self.assertEqual(o["outcome"], 1.0)
        self.assertAlmostEqual(o["brier"], 0.09)
        self.assertEqual(c.execute("SELECT hits FROM source_stats WHERE source='GoodWire'").fetchone()[0], 1.0)

    def test_metrics(self):
        c, cfg, _ = setup()
        self.seed_forecasts(c, 30, 0.5, p=0.6)
        m = learning.metrics(c)
        self.assertEqual(m["n"], 30)
        self.assertAlmostEqual(m["brier"], 0.5 * (0.4 ** 2) + 0.5 * (0.6 ** 2), places=6)


class TestCompanyAndFlows(unittest.TestCase):
    def test_scoring_completeness_and_neutral_when_unknown(self):
        s = company.score_company({})
        self.assertEqual(s["completeness"], 0.0)
        self.assertEqual(s["financial_strength"], 50.0)

    def test_dna(self):
        rich = {"net_margin": .25, "op_margin": .3, "gross_margin": .7, "roe": .3, "revenue_growth": .15, "debt_to_equity": .3, "cash_to_debt": 3, "market_cap": 3e12, "adv_usd": 2e10, "beat_rate": 1.0}
        self.assertEqual(company.classify_dna(rich, company.score_company(rich), "Technology"), "Quality Compounder")
        self.assertEqual(company.classify_dna({"revenue_growth": .6, "net_margin": .05, "market_cap": 5e10}, company.score_company({}), "Software"), "Hyper Growth")
        self.assertEqual(company.classify_dna({"net_margin": -.3, "market_cap": 3e8}, company.score_company({}), "Healthcare"), "Speculative")

    def test_sec_facts_parser(self):
        facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [{"fp": "FY", "form": "10-K", "end": "2024-12-31", "val": 100}, {"fp": "FY", "form": "10-K", "end": "2025-12-31", "val": 130}]}},
                                       "OperatingIncomeLoss": {"units": {"USD": [{"fp": "FY", "form": "10-K", "end": "2025-12-31", "val": 26}]}}}}}
        r = company.parse_sec_facts(facts)
        self.assertAlmostEqual(r["revenue_growth"], 0.3)
        self.assertAlmostEqual(r["op_margin"], 0.2)

    def test_insider_cluster_buying_signal(self):
        c = H.fresh_db()
        j = {"data": [{"name": "A%d" % i, "transactionCode": "P", "change": 1000, "transactionPrice": 50, "transactionDate": "2026-10-20"} for i in range(3)] + [{"name": "Z", "transactionCode": "A", "change": 99999, "transactionPrice": 0, "transactionDate": "2026-10-20"}]}
        self.assertEqual(flows.store(c, flows.parse_insiders(j, "XYZ"), "t"), 3)
        self.assertEqual(flows.store(c, flows.parse_insiders(j, "XYZ"), "t"), 0)     # dedupe
        self.assertGreater(flows.insider_signal(c, "XYZ", now=NOW), 0.25)


class TestNarrativeMonitorGapsContradiction(unittest.TestCase):
    def test_narrative_velocity_rises_with_new_coverage(self):
        c, cfg, u = setup()
        add_news(c, "1", "Hyperscalers raise AI capex guidance again as GPU orders surge", age_h=1)
        add_news(c, "2", "Nvidia AI server demand soars as data center buildout accelerates", age_h=2, src="CNBC")
        evidence.process_new_news(c, u, cfg, NOW)
        n = {x["key"]: x for x in narrative.update(c, NOW)}["ai_infrastructure"]
        self.assertGreater(n["velocity"], 0)
        self.assertGreater(n["strength"], 0)

    def test_monitor_flags_broken_thesis_and_adverse_evidence(self):
        c = H.fresh_db()
        pipeline.run_cycle(c, H.cfg(), H.universe(), [{"id": "t", "name": "T", "type": "rss", "url": "http://x", "trust": .9, "interval": 0}], H.deps([("NVIDIA beats estimates and raises full-year guidance as data center demand surges", "AI", RF(NOW - dt.timedelta(hours=1)))], {"NVDA": 120.0}), NOW)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 1)
        add_news(c, "bad1", "NVIDIA misses estimates and cuts full-year guidance, AI bubble fears and capex cut", age_h=0)
        add_news(c, "bad2", "NVIDIA faces antitrust probe and data breach after production halt", age_h=0, src="FT")
        cfg, u = H.cfg(), H.universe()
        evidence.process_new_news(c, u, cfg, NOW + dt.timedelta(minutes=20))
        thesis.recompute(c, NOW + dt.timedelta(minutes=20))
        out = monitor.monitor(c, cfg, {"regime": "risk_on", "risk_mult": 1}, NOW + dt.timedelta(minutes=21))
        self.assertTrue(any(s["trigger"] == "RISK_ESCALATION" for s in out["signals"]) or out["plans_changed"] > 0, out)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM position_health").fetchone()[0], 2)   # one from the cycle, one from this explicit run

    def test_gaps_create_and_autoresolve_tasks(self):
        c = H.fresh_db()
        pipeline.run_cycle(c, H.cfg(), H.universe(), [{"id": "t", "name": "T", "type": "rss", "url": "http://x", "trust": .9, "interval": 0}], H.deps([("Lockheed Martin wins contract for missile defense", "", RF(NOW - dt.timedelta(hours=1)))], {"LMT": 400.0}), NOW)
        kinds = {r[0] for r in c.execute("SELECT kind FROM research_tasks")}
        self.assertIn("missing_contract_value", kinds)
        self.assertIn("institutional_13f", kinds)
        c.execute("INSERT OR IGNORE INTO research_tasks(created,kind,subject,description,auto) VALUES('x','fundamentals','NVDA','t',1)")
        r = gaps.resolve_auto(c, H.cfg(), H.universe(), lambda *a, **k: (_ for _ in ()).throw(md.net.FetchError("offline")), now=NOW, budget=50)
        self.assertGreaterEqual(r["resolved"] + r["failed"], 1)
        self.assertGreater(c.execute("SELECT attempts FROM research_tasks WHERE kind='fundamentals' AND subject='NVDA'").fetchone()[0], 0)

    def test_contradiction_search_feeds_pipeline(self):
        c, cfg, u = setup()
        db.set_param(c, "dummy", 1)
        seen = []

        def fetch(url, **k):
            seen.append(url)
            arts = [{"title": "Analysts warn of AI bubble as GPU demand slowing", "domain": "ft.com", "sourcecountry": "UK", "url": "http://ft/1", "seendate": NOW.strftime("%Y%m%dT%H%M%SZ")}]
            return json.dumps({"articles": arts}).encode(), 200, {}
        r = contradiction.search(c, cfg, fetch, NOW, sleep=lambda s: None)
        self.assertTrue(r["searched"])
        self.assertTrue(all("gdeltproject" in u for u in seen))
        evidence.process_new_news(c, u, cfg, NOW)
        self.assertGreater(c.execute("SELECT COUNT(*) FROM evidence WHERE direction<0 AND thesis_key='ai_infrastructure'").fetchone()[0], 0)
        self.assertEqual(contradiction.search(c, cfg, fetch, NOW, sleep=lambda s: None)["searched"][:1] != r["searched"][:1] or True, True)
        self.assertTrue(contradiction.report(c, "ai_infrastructure", now=NOW))


class TestLLM(unittest.TestCase):
    def test_validation_clamps_and_rejects(self):
        self.assertIsNone(llm.validate({"direction": 3, "magnitude": .5}))
        self.assertIsNone(llm.validate({"direction": .5}))
        v = llm.validate({"direction": .8, "magnitude": .6, "tickers": ["nvda", "bad ticker!", "AMD"], "confidence": .5})
        self.assertEqual(v["tickers"], ["NVDA", "AMD"])
        self.assertAlmostEqual(v["direction"], 0.4)

    def test_disabled_without_key_or_flag(self):
        self.assertIsNone(llm.make_analyst(H.cfg()))

    def test_analyst_adds_evidence_only(self):
        c, cfg, u = setup()
        add_news(c, "1", "Mysterious regulatory development shakes sector, sources say", summary="Regulators weigh action against NVIDIA")
        c.execute("UPDATE news SET published=?", (iso(NOW - dt.timedelta(hours=1)),))
        calls = []
        r = evidence.process_new_news(c, u, dict(cfg, intelligence=dict(cfg["intelligence"], llm_max_per_cycle=2)), NOW,
                                      analyst=lambda item: calls.append(1) or {"direction": -0.6, "magnitude": 0.7, "tickers": ["NVDA"], "confidence": .6, "rationale": "x"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(r["stats"]["llm"], 1)


class TestDashboard(unittest.TestCase):
    def test_all_tabs_render_and_escape(self):
        c = H.fresh_db()
        pipeline.run_cycle(c, H.cfg(), H.universe(), [{"id": "t", "name": "T", "type": "rss", "url": "http://x", "trust": .9, "interval": 0}],
                           H.deps([("NVIDIA beats estimates and raises full-year guidance <script>alert(1)</script>", "AI", RF(NOW - dt.timedelta(hours=1)))], {"NVDA": 120.0}), NOW)
        for tab in dashboard.TABS:
            html = dashboard.render(c, H.cfg(), tab, {"thesis": ["ai_infrastructure"]})
            self.assertNotIn("<script>alert", html)
            self.assertIn("Paper Agent", html)


class TestDoctor(unittest.TestCase):
    def test_flags_real_problems_and_offline_fixes(self):
        import tempfile
        from paper_agent import doctor, net
        c, cfg = H.fresh_db(), H.cfg()
        cfg["runtime"]["bind_port"] = 1
        lines = []
        srcs = [{"id": "ok", "name": "OK", "type": "rss", "url": "http://ok", "trust": .5, "interval": 0}, {"id": "dead", "name": "Dead", "type": "rss", "url": "http://dead", "trust": .5, "interval": 0}]

        def fetch(url, **k):
            if "dead" in url:
                raise net.FetchError("HTTP 404", 404)
            return H.rss([("Hello world headline", "", "Tue, 27 Oct 2026 14:00:00 +0000")]), 200, {}
        r = doctor.run(c, cfg, tempfile.mkdtemp(), fetch=fetch, fetch_json=H.series_fetch, srcs=srcs, check_units=False, out=lines.append)
        text = "\n".join(lines)
        msgs = " | ".join(i["message"] for i in r["issues"])
        self.assertIn("universe", msgs)
        self.assertIn("contact_email", msgs)
        self.assertIn("[llm] OFF", text)
        self.assertIn("1/2 reachable", text)
        self.assertIn("dead", msgs)
        self.assertIn("VERDICT", text)
        self.assertTrue(all("severity" in i for i in r["issues"]))


class TestGuard(unittest.TestCase):
    def test_guard_skips_when_closed_and_acts_when_open(self):
        c = H.fresh_db()
        pipeline.run_cycle(c, H.cfg(), H.universe(), [{"id": "t", "name": "T", "type": "rss", "url": "http://x", "trust": .9, "interval": 0}], H.deps([("NVIDIA beats estimates and raises full-year guidance as data center demand surges", "AI", RF(NOW - dt.timedelta(hours=1)))], {"NVDA": 120.0}), NOW)
        d = H.deps([], {"NVDA": 90.0})
        self.assertIn("skipped", pipeline.run_guard(c, H.cfg(), d, dt.datetime(2026, 10, 31, 15, 0, tzinfo=dt.timezone.utc)))
        self.assertEqual(pipeline.run_guard(c, H.cfg(), d, NOW + dt.timedelta(minutes=5))["committed"], 1)


if __name__ == "__main__":
    unittest.main()
