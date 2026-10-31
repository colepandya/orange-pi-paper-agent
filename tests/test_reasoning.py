"""The Financial Reasoning Engine: the bot's own analyst, red team and question answering. No LLM, no network. Every behaviour here replaces something a language model used to be asked to do."""
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import db, event_intel, graph, llm_client, nlp, pipeline, reasoning as R, redteam  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

SEED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "relations.json")
NOW = TL.T_PRE
W = {"NVDA": {"name": "NVIDIA Corp", "aliases": ["NVIDIA", "Nvidia"], "cik": 1045810}, "TSM": {"name": "Taiwan Semiconductor Manufacturing", "aliases": ["TSMC", "Taiwan Semiconductor"], "cik": 1046179},
     "AMD": {"name": "Advanced Micro Devices", "aliases": ["AMD"], "cik": 2488}, "LMT": {"name": "Lockheed Martin Corp", "aliases": ["Lockheed Martin", "Lockheed"], "cik": 936468},
     "XOM": {"name": "Exxon Mobil Corp", "aliases": ["Exxon", "ExxonMobil"], "cik": 34088}, "DAL": {"name": "Delta Air Lines", "aliases": ["Delta"], "cik": 27904},
     "ACME": {"name": "Acme Corp", "aliases": ["Acme"], "cik": 1}, "BETA": {"name": "Beta Industries", "aliases": ["Beta"], "cik": 2}, "ASML": {"name": "ASML Holding", "aliases": ["ASML"], "cik": 937966}}
U = Universe(W)


def item(title, summary="", tickers=None):
    a = nlp.analyze(title, summary)
    a = dict(a) if not isinstance(a, dict) else a
    return {"title": title, "summary": summary, "analysis": a, "tickers": tickers or []}


class Base(unittest.TestCase):
    def setUp(self):
        self.c = H.fresh_db()
        self.cfg = H.cfg()
        self.cfg["intelligence"]["engine"] = "rules"
        self.cfg["graph"]["enabled"] = True
        graph.seed(self.c, NOW, SEED)

    def an(self, title, summary="", tickers=None):
        return R.analyze_item(self.c, self.cfg, U, item(title, summary, tickers), NOW)

    def eff(self, r, side="beneficiaries"):
        return {e["ticker"]: e for e in r[side]}


class TestTheAnalyst(Base):
    def test_good_earnings_at_a_company_help_it_its_supplier_and_hurt_its_rival_with_decaying_confidence(self):
        r = self.an("NVIDIA beats estimates and raises full-year guidance on data center demand", "Revenue of $26 billion topped analyst estimates of $24 billion.", ["NVDA"])
        self.assertIsNotNone(r)
        ben, los = self.eff(r), self.eff(r, "losers")
        self.assertEqual(ben["NVDA"]["order"], 1)
        self.assertEqual(ben["TSM"]["order"], 2)                                                      # its supplier benefits one step removed
        self.assertIn("supplier of NVDA", ben["TSM"]["mechanism"])
        self.assertIn("AMD", los)                                                                      # the competitor loses
        self.assertEqual(los["AMD"]["order"], 2)
        self.assertGreater(ben["NVDA"]["confidence"], ben["TSM"]["confidence"])                        # confidence falls with each hop
        self.assertGreaterEqual(len(r["chain"]), 2)
        self.assertEqual(r["tag"], "RULES-analyst")
        self.assertEqual(r["engine"], R.ENGINE_VERSION)

    def test_bad_news_flips_every_effect(self):
        r = self.an("NVIDIA cuts full-year guidance as data center demand slumps", "", ["NVDA"])
        los, ben = self.eff(r, "losers"), self.eff(r)
        self.assertIn("NVDA", los)
        self.assertIn("TSM", los)                                                                      # fewer chip orders
        self.assertIn("AMD", ben)

    def test_every_claim_is_supported_by_text_that_is_really_in_the_item(self):
        title, summ = "NVIDIA beats estimates and raises full-year guidance", "Revenue rose 40% to $26 billion, above the $24 billion analysts expected."
        r = self.an(title, summ, ["NVDA"])
        text = title + ". " + summ
        self.assertTrue(r["quotes"])
        for q in r["quotes"]:
            self.assertIn(q, text)                                                                     # no invented quotes
        self.assertTrue(r["trace"])
        self.assertTrue(any(t.startswith("status") for t in r["trace"]))

    def test_a_rumour_is_less_certain_than_a_confirmation_and_a_denial_is_least(self):
        conf = self.an("Acme announces it will acquire Beta for $5 billion", "", ["ACME", "BETA"])
        rum = self.an("Acme reportedly in talks to acquire Beta, people familiar say", "", ["ACME", "BETA"])
        den = self.an("Acme denies report it plans to acquire Beta", "", ["ACME", "BETA"])
        self.assertEqual(conf["status"], "confirmed")
        self.assertEqual(rum["status"], "rumor")
        self.assertGreater(conf["certainty"], rum["certainty"])
        self.assertTrue(den is None or den["certainty"] < rum["certainty"])
        self.assertTrue(den is None or den["status"] == "denied")

    def test_the_target_of_a_takeover_gains_and_the_acquirer_is_not_cheered(self):
        r = self.an("Acme to acquire Beta for $5 billion in cash", "", ["ACME", "BETA"])
        self.assertIn("BETA", self.eff(r))
        self.assertIn("target", self.eff(r)["BETA"]["mechanism"])
        self.assertIn("ACME", self.eff(r, "losers"))
        self.assertGreater(self.eff(r)["BETA"]["confidence"], self.eff(r, "losers")["ACME"]["confidence"])

    def test_the_party_being_sued_loses_and_the_plaintiff_does_not(self):
        r = self.an("Acme sues Beta over patent infringement", "", ["ACME", "BETA"])
        self.assertIn("BETA", self.eff(r, "losers"))
        self.assertNotIn("ACME", self.eff(r, "losers"))

    def test_a_headline_that_says_one_thing_and_a_body_that_says_another_is_flagged_and_doubted(self):
        clean = self.an("Acme beats earnings estimates", "Revenue grew strongly and profit topped forecasts.", ["ACME"])
        mixed = self.an("Acme beats earnings estimates", "But the company cut full-year guidance and warned of weak demand and falling margins.", ["ACME"])
        self.assertTrue(mixed["contradictions"])
        self.assertFalse(clean["contradictions"])
        self.assertLess(mixed["certainty"], clean["certainty"])

    def test_oil_rising_helps_producers_and_hurts_airlines_and_falling_does_the_reverse(self):
        up = self.an("Oil prices surge after pipeline attack disrupts supply", "", [])
        dn = self.an("Oil prices plunge as demand fears grow", "", [])
        self.assertIn("XOM", self.eff(up))
        self.assertIn("DAL", self.eff(up, "losers"))
        self.assertIn("DAL", self.eff(dn))
        self.assertIn("XOM", self.eff(dn, "losers"))
        self.assertTrue(any("oil price" in t for t in up["trace"]))

    def test_conflict_helps_defence_stocks(self):
        r = self.an("Missile strikes escalate the war in the region", "", [])
        self.assertIn("LMT", self.eff(r))

    def test_a_third_hop_exists_and_decays_exactly_as_configured(self):
        r = self.an("NVIDIA beats estimates and raises full-year guidance on data center demand", "Revenue topped estimates.", ["NVDA"])
        ben = self.eff(r)
        self.assertEqual(ben["ASML"]["order"], 3)                                                      # NVDA -> its supplier TSM -> TSM's supplier ASML
        tsm, asml = ben["TSM"]["confidence"], ben["ASML"]["confidence"]
        expect = min(0.7, tsm * 0.6 * 0.45 * 1.6) * float(self.cfg["intelligence"]["chain_decay"])      # edge ASML->TSM 0.45, supplier factor 0.6, then one more decay
        self.assertAlmostEqual(asml, round(expect, 3), places=2)
        self.assertLess(asml, tsm)

    def test_it_abstains_when_certainty_is_too_low_and_when_it_cannot_say_which_company(self):
        low = item("Acme denies report it plans to acquire Beta", "", ["ACME", "BETA"])
        low["analysis"] = dict(low["analysis"], confidence=0.1)
        self.assertIsNone(R.analyze_item(self.c, self.cfg, U, low, NOW))                               # a denied rumour of low confidence: nothing to act on
        self.assertIsNone(self.an("A leading chipmaker beats estimates and raises guidance", "", []))     # a direction but no company: it does not guess one

    def test_good_and_bad_facts_together_are_flagged_even_when_headline_and_body_agree(self):
        r = self.an("Acme reports results", "Earnings per share of $1.20 beat the $1.00 consensus estimate. The company lowered its full-year revenue outlook to $4.0 billion from $4.5 billion.", ["ACME"])
        self.assertTrue(any("both good and bad" in c for c in r["contradictions"]), r["contradictions"])

    def test_a_takeover_that_reads_as_neutral_still_has_a_direction_and_a_winner(self):
        it = item("Acme to acquire Beta for $5 billion in cash", "", ["ACME", "BETA"])
        it["analysis"] = dict(it["analysis"], direction=0.0)                                           # the word-count reading is exactly neutral
        r = R.analyze_item(self.c, self.cfg, U, it, NOW)
        self.assertGreater(r["direction_market"], 0)
        self.assertIn("BETA", self.eff(r))
        self.assertIn("ACME", self.eff(r, "losers"))

    def test_thin_evidence_means_it_abstains_not_guesses(self):
        self.assertIsNone(self.an("Quarterly newsletter now available online", "", []))
        self.assertIsNone(self.an("Markets mixed in quiet trading", "", []))

    def test_a_stock_pick_or_an_advertisement_with_no_reported_fact_is_not_an_event(self):
        self.assertIsNone(self.an("Not Nvidia. Not AMD. This Is the Best AI Stock to Buy Right Now", "", ["NVDA", "AMD"]))
        self.assertIsNotNone(self.an("NVIDIA beats estimates and raises guidance", "Revenue topped estimates.", ["NVDA"]))            # a real report about the same company still gets analysed

    def test_an_opinion_piece_is_less_certain_than_a_report(self):
        rep = self.an("NVIDIA beats estimates and raises guidance", "Revenue topped analyst estimates.", ["NVDA"])
        op = self.an("Here's why you should buy NVIDIA stock now", "NVIDIA beats estimates and raises guidance, I think it is cheap.", ["NVDA"])
        self.assertTrue(op is None or op["certainty"] < rep["certainty"])                                  # it abstains, or at least trusts it less

    def test_outputs_are_bounded_and_only_name_stocks_we_cover(self):
        r = self.an("NVIDIA beats estimates and raises guidance", "", ["NVDA"])
        self.assertLessEqual(len(r["beneficiaries"]), 6)
        self.assertLessEqual(len(r["losers"]), 6)
        for e in r["beneficiaries"] + r["losers"]:
            self.assertIn(e["ticker"], U)
            self.assertTrue(0 < e["confidence"] <= 0.95)
            self.assertIn(e["order"], (1, 2, 3))

    def test_the_same_input_always_gives_the_same_answer(self):
        a = self.an("NVIDIA beats estimates and raises guidance", "Revenue topped estimates.", ["NVDA"])
        b = self.an("NVIDIA beats estimates and raises guidance", "Revenue topped estimates.", ["NVDA"])
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_themes_are_supported_or_contradicted(self):
        up = self.an("NVIDIA beats estimates and raises guidance", "", ["NVDA"])
        dn = self.an("NVIDIA cuts guidance as demand slumps", "", ["NVDA"])
        self.assertTrue(any(n["stance"] == "support" for n in up["narratives"]))
        self.assertTrue(any(n["stance"] == "contradict" for n in dn["narratives"]))

    def test_it_never_calls_a_language_model(self):
        calls = []
        orig = llm_client.LLMClient.complete_json
        llm_client.LLMClient.complete_json = lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("an LLM was called"))
        try:
            r = self.an("NVIDIA beats estimates and raises guidance", "", ["NVDA"])
            R.review(self.c, self.cfg, None, {"ticker": "NVDA", "event_type": "earnings_beat", "direction": 0.8, "credibility": 0.8, "headline": "NVIDIA beats", "age_hours": 1}, {"verdict": "BUY", "inputs": {}, "stop_return": -0.03, "ev_net": 0.02, "confidence": 0.7, "scenarios": {}}, {}, {}, {}, NOW)
            R.ask(self.c, self.cfg, "why NVDA", U, NOW)
        finally:
            llm_client.LLMClient.complete_json = orig
        self.assertIsNotNone(r)
        self.assertEqual(calls, [])

    def test_its_output_is_accepted_by_the_code_that_used_to_consume_the_llms(self):
        r = self.an("NVIDIA beats estimates and raises full-year guidance", "Revenue topped estimates.", ["NVDA"])
        v = event_intel.make_validator(U, set(R.THEMES), "NVIDIA beats estimates and raises full-year guidance. Revenue topped estimates.")
        out = v(dict(r, magnitude=r["magnitude_label"], evidence_quotes=r["quotes"], direction_market=r["direction_market"]))
        self.assertIsNotNone(out)                                                                      # the same validator the LLM's JSON had to pass
        self.assertEqual(out["event_type"], r["event_type"])


def fc(verdict="BUY", **inp):
    return {"verdict": verdict, "inputs": dict({"vol_daily": 0.02}, **inp), "stop_return": -0.03, "ev_net": 0.025, "confidence": 0.7, "horizon_days": 3,
            "scenarios": {"bull": {"ret": 0.06, "p": 0.3}, "base": {"ret": 0.02, "p": 0.4}, "bear": {"ret": -0.03, "p": 0.3}}}


def cd(**kw):
    return dict({"ticker": "NVDA", "event_type": "earnings_beat", "direction": 0.8, "credibility": 0.8, "headline": "NVIDIA announces record results", "age_hours": 1.0}, **kw)


class TestTheRedTeam(Base):
    def rv(self, cand=None, f=None, th=None, regime=None, comp=None):
        return R.review(self.c, self.cfg, None, cand or cd(), f or fc(), comp or {"adv_usd": 5e9}, th or {}, regime or {}, NOW)

    def points(self, rv):
        return " | ".join(b["point"] for b in rv["bear_points"])

    def test_it_always_returns_a_valid_review_even_with_nothing_to_object_to(self):
        rv = self.rv()
        self.assertTrue(rv["bear_points"])                                                            # the red team never says "no objection" without a residual risk
        self.assertFalse(rv["fatal_flaw"])
        self.assertTrue(0 <= rv["net_conviction"] <= 1)
        self.assertTrue(rv["falsifiers"])
        self.assertTrue(rv["bull_points"])

    def test_a_move_that_already_happened_is_an_objection_and_a_big_one_is_fatal(self):
        mild = self.rv(f=fc(priced_in=0.05))
        big = self.rv(f=fc(priced_in=0.14))
        self.assertIn("already happened", self.points(mild))
        self.assertFalse(mild["fatal_flaw"])
        self.assertTrue(big["fatal_flaw"])
        self.assertIn("priced in", big["fatal_reason"])
        self.assertLess(big["net_conviction"], mild["net_conviction"])

    def test_every_objection_lowers_the_conviction_itself(self):
        clean, doubted = self.rv(), self.rv(f=fc(priced_in=0.06))
        self.assertLess(doubted["conviction"], clean["conviction"])

    def test_a_recent_contradicting_credible_event_is_an_objection_with_the_headline_as_evidence(self):
        self.c.execute("INSERT INTO events(created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source,stale) VALUES(?,?,?,?,?,?,?,?,?,0)",
                       (iso(NOW - dt.timedelta(hours=5)), "NVIDIA cuts full-year guidance", -0.8, 0.9, 0.9, "guidance_cut", '["NVDA"]', "company", "Wire"))
        rv = self.rv()
        self.assertIn("contradicting news", self.points(rv))
        self.assertTrue(any("cuts full-year guidance" in b["evidence"] for b in rv["bear_points"]))
        self.assertTrue(rv["fatal_flaw"])

    def test_only_real_news_about_the_company_as_of_now_can_contradict(self):
        def ev(headline, et, hours_ago, gate=None, tickers='["NVDA"]'):
            self.c.execute("INSERT INTO events(created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source,stale,gate) VALUES(?,?,?,?,?,?,?,?,?,0,?)",
                           (iso(NOW - dt.timedelta(hours=hours_ago)), headline, -0.8, 0.9, 0.9, et, tickers, "company", "Wire", gate))
        ev("Stocks fall: NVDA, AMD, MSFT lower today", "price_recap", 2)                                 # a recap is not news about the company
        ev("Ten stocks to watch", "general_news", 2, tickers='["NVDA","AMD","MSFT","GOOGL"]', gate="too_many_companies")
        ev("NVIDIA cuts guidance (tomorrow's news)", "guidance_cut", -3)                                  # three hours AFTER the decision: it cannot have been known
        ev("NVIDIA cut guidance last week", "guidance_cut", 100)                                          # too old
        self.assertNotIn("contradicting news", self.points(self.rv()))
        ev("NVIDIA cuts guidance", "guidance_cut", 6)                                                      # real, recent, about the company
        self.assertIn("contradicting news", self.points(self.rv()))

    def test_the_same_event_does_not_contradict_a_short(self):
        self.c.execute("INSERT INTO events(created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source,stale) VALUES(?,?,?,?,?,?,?,?,?,0)",
                       (iso(NOW - dt.timedelta(hours=5)), "NVIDIA cuts full-year guidance", -0.8, 0.9, 0.9, "guidance_cut", '["NVDA"]', "company", "Wire"))
        rv = self.rv(cand=cd(direction=-0.8, event_type="guidance_cut"), f=fc("SHORT"))
        self.assertNotIn("contradicting news", self.points(rv))

    def test_a_rumour_is_doubted_and_a_denial_is_fatal(self):
        self.assertIn("unconfirmed", self.points(self.rv(cand=cd(headline="Nvidia reportedly weighing a big acquisition, sources say"))))
        den = self.rv(cand=cd(headline="Nvidia denies report of a big acquisition"))
        self.assertTrue(den["fatal_flaw"])

    def test_old_news_is_doubted_and_very_old_news_is_fatal(self):
        self.assertFalse(self.rv(cand=cd(age_hours=20))["fatal_flaw"])
        self.assertIn("hours old", self.points(self.rv(cand=cd(age_hours=20))))
        self.assertTrue(self.rv(cand=cd(age_hours=40))["fatal_flaw"])

    def test_a_crowded_theme_hurts_a_long_and_a_strong_theme_hurts_a_short(self):
        self.assertIn("overcrowded", self.points(self.rv(th={"state": "Overcrowded"})))
        self.assertIn("against a strong theme", self.points(self.rv(f=fc("SHORT"), th={"state": "Strong"}, cand=cd(direction=-0.8))))
        self.assertNotIn("overcrowded", self.points(self.rv(th={"state": "Strong"})))

    def test_a_stop_inside_a_normal_days_move_is_an_objection(self):
        self.assertIn("stop is inside", self.points(self.rv(f=dict(fc(), stop_return=-0.012, inputs={"vol_daily": 0.02}))))
        self.assertNotIn("stop is inside", self.points(self.rv(f=dict(fc(), stop_return=-0.05))))

    def test_a_thin_edge_thin_volume_and_a_hostile_market_each_count(self):
        rv = self.rv(f=dict(fc(), ev_net=0.006), comp={"adv_usd": 2e7}, regime={"regime": "risk_off"})
        p = self.points(rv)
        self.assertIn("edge after costs is thin", p)
        self.assertIn("volume is thin", p)
        self.assertIn("regime", p)

    def test_the_tripwires_that_would_prove_a_long_wrong_are_a_price_below_and_bad_news(self):
        rv = self.rv()
        kinds = {(f["kind"], f.get("event_type")) for f in rv["falsifiers"]}
        self.assertTrue(any(k == "price_below" for k, _ in kinds))
        self.assertTrue(any(k == "event" and et in ("earnings_miss", "guidance_cut") for k, et in kinds))
        self.assertTrue(all(f.get("against", "down") == "down" for f in rv["falsifiers"] if f["kind"] == "event"))

    def test_the_tripwires_that_would_prove_a_short_wrong_are_a_price_above_and_good_news(self):
        rv = self.rv(f=fc("SHORT"), cand=cd(direction=-0.8, event_type="guidance_cut"))
        self.assertTrue(any(f["kind"] == "price_above" and f["level_pct"] > 0 for f in rv["falsifiers"]))
        ev_f = [f for f in rv["falsifiers"] if f["kind"] == "event"]
        self.assertTrue(ev_f)
        self.assertTrue(all(f["against"] == "up" for f in ev_f))

    def test_shorts_tripwires_really_fire_on_a_rise_and_on_good_news(self):
        self.c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,status,sector,side) VALUES('NVDA','t',?,100.0,100,10000,'OPEN','X','SHORT')", (iso(NOW),))
        rv = self.rv(f=fc("SHORT"), cand=cd(direction=-0.8, event_type="guidance_cut"))
        redteam.store_tripwires(self.c, 1, 100.0, rv, NOW)
        kinds = {r["kind"] for r in self.c.execute("SELECT kind FROM tripwires")}
        self.assertEqual(kinds, {"price_above", "event_up"})
        self.assertEqual(redteam.check_tripwires(self.c, NOW), [])                                     # nothing yet
        self.c.execute("INSERT OR REPLACE INTO quote_cache(ticker,price,source,quoted_at,updated_epoch) VALUES('NVDA',108.0,'T',?,?)", (iso(NOW), NOW.timestamp()))
        self.c.execute("INSERT INTO events(created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source,stale,novelty) VALUES(?,?,?,?,?,?,?,?,?,0,1.0)",
                       (iso(NOW + dt.timedelta(minutes=5)), "NVIDIA raises guidance", 0.8, 0.9, 0.9, "guidance_raise", '["NVDA"]', "company", "Wire"))
        self.c.commit()
        fired = redteam.check_tripwires(self.c, NOW + dt.timedelta(minutes=10))
        self.assertEqual(len(fired), 2)
        self.assertTrue(self.c.execute("SELECT 1 FROM exit_signals WHERE position_id=1 AND trigger='THESIS_BROKEN'").fetchone())

    def test_the_existing_gate_decides_what_to_do_with_it(self):
        ok = redteam.gate(self.rv(), self.cfg)
        self.assertTrue(ok["ok"])
        bad = redteam.gate(self.rv(f=fc(priced_in=0.14)), self.cfg)
        self.assertFalse(bad["ok"])
        self.assertIn("fatal flaw", bad["reasons"][0])

    def test_a_review_is_saved_once_and_reused_for_retries_of_the_same_decision(self):
        a = R.review(self.c, self.cfg, None, cd(), fc(), {}, {}, {}, NOW, 77)
        n = self.c.execute("SELECT COUNT(*) FROM redteam_reviews").fetchone()[0]
        b = R.review(self.c, self.cfg, None, cd(), fc(priced_in=0.14), {}, {}, {}, NOW + dt.timedelta(hours=1), 77)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM redteam_reviews").fetchone()[0], n)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


class TestInThePipeline(TL.Base):
    """Through the real cycle: the rules engine reads the news, writes evidence tagged as its own, and argues against the trade, with the LLM never touched."""

    def setUp(self):
        super().setUp()
        self.cfg["intelligence"]["engine"] = "rules"
        self.cfg["intelligence"]["redteam_enabled"] = True
        self.cfg["graph"]["enabled"] = True
        graph.seed(self.c, TL.T_PRE, SEED)
        self.uni = Universe(dict(W))
        self.mkt.price.update({"TSM": 180.0, "AMD": 160.0, "ASML": 700.0})
        self.llm_calls = []
        self._orig = llm_client.LLMClient.complete_json
        llm_client.LLMClient.complete_json = lambda *a, **k: self.llm_calls.append(1) or None

    def tearDown(self):
        llm_client.LLMClient.complete_json = self._orig
        super().tearDown()

    def run_pre(self):
        self.mkt.now = TL.T_PRE
        items = [(TL.NVDA_NEWS, "AI capex", TL.RF(TL.T_PRE - dt.timedelta(minutes=40)))]
        return pipeline.run_cycle(self.c, self.cfg, self.uni, TL.SRC, self.deps(items), TL.T_PRE)

    def test_the_analysis_is_stored_as_the_engines_own_with_evidence_for_each_hop(self):
        r = self.run_pre()
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["llm"], {"enabled": False, "engine": "rules"})
        self.assertEqual(self.llm_calls, [])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM intel_events").fetchone()[0], 1)
        srcs = {x[0] for x in self.c.execute("SELECT DISTINCT source FROM evidence WHERE source LIKE 'RULES-analyst%'")}
        self.assertIn("RULES-analyst:o1", srcs)
        self.assertIn("RULES-analyst:o2", srcs)                                                        # the supplier and rival, one hop away
        self.assertFalse(self.c.execute("SELECT 1 FROM evidence WHERE source LIKE 'LLM-analyst%'").fetchone())
        a = json.loads(self.c.execute("SELECT analysis_json FROM intel_events").fetchone()[0])
        self.assertEqual(a["engine"], R.ENGINE_VERSION)

    def test_the_trade_is_argued_against_before_it_is_made_and_the_argument_is_kept(self):
        self.run_pre()
        self.mkt.now = TL.T_OPEN
        pipeline.run_cycle(self.c, self.cfg, self.uni, TL.SRC, self.deps([]), TL.T_OPEN)
        self.assertEqual(self.llm_calls, [])
        d = self.one("SELECT status,reason FROM decisions WHERE ticker='NVDA'")
        reason = json.loads(d["reason"])
        self.assertIn("redteam", reason)
        self.assertTrue(reason["redteam"]["bear"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM redteam_reviews WHERE ticker='NVDA'")[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM tripwires")[0] >= 1, d["status"] == "EXECUTED")   # an executed trade carries its tripwires

    def test_a_demanding_conviction_floor_stops_the_trade_through_the_existing_gate(self):
        self.cfg["intelligence"]["redteam_min_conviction"] = 0.99
        self.run_pre()
        self.mkt.now = TL.T_OPEN
        pipeline.run_cycle(self.c, self.cfg, self.uni, TL.SRC, self.deps([]), TL.T_OPEN)
        d = self.one("SELECT status,reason FROM decisions WHERE ticker='NVDA'")
        self.assertEqual(d["status"], "RISK_REJECTED")
        self.assertIn("red team", d["reason"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)

    def test_choosing_the_llm_engine_without_a_key_means_no_analyst_and_no_red_team_as_before(self):
        self.cfg["intelligence"]["engine"] = "llm"
        r = self.run_pre()
        self.assertEqual(r["llm"]["engine"], "llm")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM intel_events").fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM redteam_reviews").fetchone()[0], 0)

    def test_the_filings_stage_that_needed_a_language_model_is_skipped_not_faked(self):
        r = self.run_pre()
        self.assertIn("skipped", json.dumps(r.get("knowledge") or r.get("stages") or r))


class TestQuestions(Base):
    def ask(self, q):
        return R.ask(self.c, self.cfg, q, U, NOW)

    def test_help_lists_what_it_understands(self):
        a = self.ask("help")
        self.assertEqual(a["intent"], "help")
        self.assertIn("why NVDA", a["text"])

    def test_what_it_does_not_understand_it_says_so(self):
        a = self.ask("what is the meaning of life")
        self.assertFalse(a["ok"])
        self.assertEqual(a["intent"], "unsupported")
        self.assertIn("can't answer", a["title"])

    def test_why_a_stock_gathers_everything_and_the_case_against(self):
        self.c.execute("INSERT INTO events(created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source,stale) VALUES(?,?,?,?,?,?,?,?,?,0)",
                       (iso(NOW - dt.timedelta(hours=3)), "NVIDIA beats estimates", 0.8, 0.8, 0.9, "earnings_beat", '["NVDA"]', "company", "Wire"))
        self.c.commit()
        a = self.ask("why NVDA")
        heads = [s["heading"] for s in a["sections"]]
        for h in ("our position", "evidence in the last 72 hours", "themes", "relationships", "the case against"):
            self.assertIn(h, heads)
        self.assertIn("NVIDIA beats estimates", a["text"])
        self.assertIn("supplier of NVDA", a["text"])
        self.assertIn("not held", a["text"])

    def test_asking_never_writes_to_the_audit_log_of_real_decisions(self):
        self.c.execute("INSERT INTO forecasts(created,ticker,direction,horizon_days,p_up,ev_gross,ev_net,risk_score,confidence,target_return,stop_return,regime,verdict,scenarios_json,inputs_json,hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (iso(NOW), "NVDA", "LONG", 3, 0.6, 0.03, 0.02, 30, 0.7, 0.04, -0.03, "neutral", "BUY", json.dumps({"bull": {"ret": 0.06, "p": 0.3}, "base": {"ret": 0.02, "p": 0.4}, "bear": {"ret": -0.03, "p": 0.3}}), json.dumps({"priced_in": 0.06, "vol_daily": 0.02}), "h"))
        self.c.commit()
        a = self.ask("why NVDA")
        self.assertIn("already happened", a["text"])                                                   # the case against was computed...
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM redteam_reviews").fetchone()[0], 0)      # ...but not saved as if it were a real review

    def test_related_lists_roles(self):
        a = self.ask("related NVDA")
        self.assertEqual(a["intent"], "graph")
        self.assertIn("TSM", a["text"])
        self.assertIn("SUPPLIER", a["text"])

    def test_what_if_a_supplier_cuts_guidance_shows_who_is_hurt_and_who_gains(self):
        a = self.ask("what if TSM cuts guidance")
        self.assertEqual(a["intent"], "whatif")
        self.assertIn("NVDA", a["text"])
        self.assertIn("DOWN", a["text"])
        self.assertIn("guidance cut", a["text"])

    def test_what_if_marks_the_stocks_we_hold_and_whether_it_helps_or_hurts(self):
        self.c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,status,sector,side) VALUES('NVDA','t',?,100.0,100,10000,'OPEN','X','LONG')", (iso(NOW),))
        self.c.commit()
        a = self.ask("what if TSM cuts guidance")
        self.assertIn("we hold this (LONG)", a["text"])
        self.assertIn("hurt", a["text"])

    def test_what_if_needs_a_stock_and_an_event(self):
        self.assertFalse(self.ask("what if the market falls")["ok"])

    def test_compare_two_stocks_shows_whether_they_are_linked(self):
        a = self.ask("compare NVDA AMD")
        self.assertEqual(a["intent"], "compare")
        self.assertIn("competitor", a["text"])

    def test_theme_shows_members_and_our_exposure(self):
        a = self.ask("theme ai infrastructure")
        self.assertEqual(a["intent"], "theme")
        self.assertIn("NVDA", a["text"])

    def test_risk_and_today_work_on_an_empty_book(self):
        r = self.ask("what could hurt us")
        self.assertEqual(r["intent"], "risk")
        self.assertIn("book", [s["heading"] for s in r["sections"]])
        t = self.ask("today")
        self.assertEqual(t["intent"], "today")

    def test_news_lists_recent_events(self):
        self.c.execute("INSERT INTO events(created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source,stale) VALUES(?,?,?,?,?,?,?,?,?,0)",
                       (iso(NOW), "AMD wins a big contract", 0.7, 0.8, 0.9, "contract_win", '["AMD"]', "company", "Wire"))
        self.c.commit()
        self.assertIn("AMD wins a big contract", self.ask("news AMD")["text"])


if __name__ == "__main__":
    unittest.main()
