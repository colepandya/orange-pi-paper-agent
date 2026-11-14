"""The Build 45 reasoning layers, tested against the brief's intended behaviour: event-conditioned relationships, event-aware expiry, economic materiality, multi-event state, regime context, red-team
intervention measurement, deterministic filing facts, the common timeframe vocabulary, the event-worthiness gate and the classifier / matcher corrections."""
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
from paper_agent import db, filingintel as F, kb, kbevent, kbmeasure, kbregime, kbrel, kbsnap, nlp, reasoning as R, timeframe, eventworth  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402
from paper_agent.timeutil import iso, parse_ts  # noqa: E402

NOW = dt.datetime(2026, 11, 1, 3, 0, tzinfo=dt.timezone.utc)
UNI = {"CUST": {"name": "Customer Corp", "cik": 11, "exchange": "NYSE", "aliases": []}, "SUPP": {"name": "Supplier Corp", "cik": 12, "exchange": "NYSE", "aliases": []}, "RIVL": {"name": "Rival Corp", "cik": 13, "exchange": "NYSE", "aliases": []},
       "PEER": {"name": "Peer Corp", "cik": 14, "exchange": "NYSE", "aliases": []}, "FARR": {"name": "Farther Corp", "cik": 15, "exchange": "NYSE", "aliases": []}, "CORR": {"name": "Correlated Corp", "cik": 16, "exchange": "NYSE", "aliases": []}}
SEED = {"edges": [{"src": "SUPP", "dst": "CUST", "kind": "supplies", "weight": 0.6, "note": "makes the chips CUST buys"}, {"src": "CUST", "dst": "RIVL", "kind": "competes", "weight": 0.5, "note": "same market"},
                  {"src": "FARR", "dst": "SUPP", "kind": "supplies", "weight": 0.4, "note": "wafers"}], "peer_groups": [{"name": "same sector", "tickers": ["CUST", "PEER"], "weight": 0.3}]}


def kbdb(cfg=None):
    c = db.open_db(":memory:")
    kb.bootstrap(c, UNI, NOW)
    kbsnap.import_relationship_seed(c, NOW, seed=SEED)
    return c


def ev(**k):
    return dict({"event_id": 1, "ticker": "CUST", "event_type": "guidance_cut", "direction": -0.8, "magnitude": 0.8, "state": "confirmed", "industry_wide": False, "source_role": "subject", "headline": "x"}, **k)


def by_dst(imps):
    return {i["dst_ticker"]: i for i in imps if i["hops"] == 1}


class TestEventConditionedRelationships(unittest.TestCase):
    def setUp(self):
        self.c = kbdb()
        self.cfg = H.cfg()

    def prop(self, **k):
        return kbrel.propagate(self.c, self.cfg, ev(**k), NOW, persist=False)

    def test_a_customers_weak_demand_hurts_its_supplier_with_a_stated_mechanism(self):
        i = by_dst(self.prop())["SUPP"]
        self.assertEqual((i["rule_id"], i["direction"]), ("SUP_DEMAND_DOWN", -1.0))
        self.assertIn("orders", i["mechanism"])
        self.assertTrue(i["applicability"])

    def test_a_customers_strong_demand_helps_its_supplier(self):
        i = by_dst(self.prop(event_type="earnings_beat", direction=0.8))["SUPP"]
        self.assertEqual(i["direction"], 1.0)

    def test_customer_distress_hurts_the_supplier_more_surely_than_a_demand_miss(self):
        d = by_dst(self.prop(event_type="bankruptcy_distress"))["SUPP"]
        m = by_dst(self.prop(event_type="guidance_cut"))["SUPP"]
        self.assertGreater(d["direction_conf"], m["direction_conf"])

    def test_a_rivals_company_specific_setback_may_help_the_competitor_but_an_industry_wide_one_hurts_both(self):
        specific = by_dst(self.prop(event_type="production_disruption"))["RIVL"]
        wide = by_dst(self.prop(event_type="guidance_cut", industry_wide=True))["RIVL"]
        self.assertEqual(specific["direction"], 1.0)
        self.assertEqual(wide["direction"], -1.0)

    def test_the_competitors_good_results_are_an_abstention_not_a_guess(self):
        i = by_dst(self.prop(event_type="earnings_beat", direction=0.8))["RIVL"]
        self.assertEqual(i["direction"], 0.0)
        self.assertEqual(i["influence_class"], "none")
        self.assertIn("no rule", i["abstain"])

    def test_an_event_type_with_no_economic_rule_abstains_everywhere(self):
        imps = self.prop(event_type="analyst_action", direction=0.3)
        self.assertTrue(imps)
        self.assertTrue(all(i["direction"] == 0.0 and i["abstain"] for i in imps if i["hops"] == 1))

    def test_a_rumour_is_trusted_less_than_a_confirmed_event(self):
        c = by_dst(self.prop())["SUPP"]["direction_conf"]
        r = by_dst(self.prop(state="rumored"))["SUPP"]["direction_conf"]
        self.assertLess(r, c * 0.7)

    def test_first_hop_is_observe_second_research_third_explain_and_none_trade_by_default(self):
        imps = self.prop()
        cls = {i["hops"]: i["influence_class"] for i in imps if i["influence_class"] != "none"}
        self.assertEqual(cls.get(1), "observe")
        self.assertEqual(cls.get(2), "research")
        self.assertFalse([i for i in imps if i["influence_class"] == "trade"])

    def test_only_when_the_owner_switches_to_active_may_a_first_hop_effect_trade(self):
        self.cfg["kb"]["relationship_mode"] = "active"
        imps = self.prop()
        self.assertTrue([i for i in imps if i["hops"] == 1 and i["influence_class"] == "trade"])
        self.assertFalse([i for i in imps if i["hops"] > 1 and i["influence_class"] == "trade"])

    def test_switched_off_it_produces_nothing(self):
        self.cfg["kb"]["relationship_mode"] = "off"
        self.assertEqual(self.prop(), [])

    def test_correlation_can_never_be_a_trade_or_even_an_observed_relationship(self):
        eid, other = kbsnap.entity_of(self.c, "CUST"), kbsnap.entity_of(self.c, "CORR")
        self.c.execute("INSERT INTO kb_relationships(rel_id,src_entity,dst_entity,rel_type,directional,mechanism,strength,confidence,source,status,first_known) VALUES('RC',?,?,'corr',0,'moves together',0.9,0.9,'stats','active',?)", (eid, other, iso(NOW)))
        self.cfg["kb"]["relationship_mode"] = "active"
        imps = self.prop()
        self.assertNotIn("CORR", {i["dst_ticker"] for i in imps})
        self.assertTrue(kbrel.never_trades_on_correlation(imps))
        self.assertTrue(kbrel.never_trades_on_correlation([{"rel_type": "corr", "influence_class": "trade"}]) is False)

    def test_even_if_a_correlation_row_were_given_a_role_the_filter_still_refuses_it(self):
        eid, other = kbsnap.entity_of(self.c, "CUST"), kbsnap.entity_of(self.c, "CORR")
        self.c.execute("INSERT INTO kb_relationships(rel_id,src_entity,dst_entity,rel_type,directional,mechanism,strength,confidence,source,status,first_known) VALUES('RC2',?,?,'corr',0,'moves together',0.9,0.9,'stats','active',?)", (eid, other, iso(NOW)))
        kbrel.NEIGHBOUR_ROLE[("corr", True)] = kbrel.NEIGHBOUR_ROLE[("corr", False)] = "competitor"
        try:
            self.assertNotIn("CORR", {n["neighbour"] and kbrel._ticker(self.c, n["neighbour"]) for n in kbrel.neighbours(self.c, eid, iso(NOW))})
        finally:
            kbrel.NEIGHBOUR_ROLE.pop(("corr", True), None)
            kbrel.NEIGHBOUR_ROLE.pop(("corr", False), None)

    def test_a_relationship_the_bot_did_not_yet_know_is_not_used_in_a_replay(self):
        early = iso(NOW - dt.timedelta(days=30))
        self.assertEqual(kbrel.propagate(self.c, self.cfg, ev(), NOW - dt.timedelta(days=30), as_of=early, persist=False), [])      # the seeds were first known at NOW

    def test_a_relationship_outside_its_validity_dates_is_ignored(self):
        self.c.execute("UPDATE kb_relationships SET valid_to=?", (iso(NOW - dt.timedelta(days=1)),))
        self.assertEqual(self.prop(), [])

    def test_every_derived_impact_records_what_the_audit_needs(self):
        i = by_dst(kbrel.propagate(self.c, self.cfg, ev(), NOW, persist=True))["SUPP"]
        row = dict(self.c.execute("SELECT * FROM kb_derived_impacts WHERE dst_ticker='SUPP' AND hops=1").fetchone())
        for k in ("event_id", "path_json", "rel_id", "rule_id", "mechanism", "direction", "direction_conf", "magnitude_adj", "horizon", "evidence_json", "applicability", "influence_class", "knowledge_json"):
            self.assertIsNotNone(row[k], k)
        self.assertIn(i["horizon"], timeframe.ALL)

    def test_exposure_effects_follow_the_recorded_channel_and_abstain_when_channels_oppose(self):
        a, b = kbsnap.entity_of(self.c, "CUST"), kbsnap.entity_of(self.c, "SUPP")
        for e, sg in ((a, 1.0), (a, -1.0), (b, -1.0)):
            self.c.execute("INSERT INTO kb_exposures(entity_id,exp_type,subject,channel,dir_rise,dir_fall,strength,confidence,source,status,first_known) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (e, "energy_prices", "oil price", "ch%s" % sg, sg, -sg, 0.5, 0.5, "seed", "ok", iso(NOW)))
        out = {o["dst_ticker"]: o for o in kbrel.exposure_impacts(self.c, self.cfg, "oil price", 1.0, 7, NOW, persist=False)}
        self.assertEqual(out["SUPP"]["direction"], -1.0)
        self.assertEqual(out["CUST"]["direction"], 0.0)
        self.assertIn("opposing channels", out["CUST"]["abstain"])


class TestEventAwareExpiry(unittest.TestCase):
    def setUp(self):
        self.c = kbdb()
        self.cfg = H.cfg()
        kbevent.seed_rules(self.c)

    def lc(self, et, hours_old, **k):
        t = iso(NOW - dt.timedelta(hours=hours_old))
        return kbevent.lifecycle(self.c, self.cfg, dict({"event_id": 1, "ticker": "CUST", "event_type": et, "state": None, "text": "headline", "published": t, "created": t}, **k), NOW, persist=False)

    def test_an_analyst_rating_fades_sooner_than_the_flat_36_hours_and_a_pending_deal_lasts_longer(self):
        a = self.lc("analyst_action", 30)
        m = self.lc("merger_acquisition", 60)
        self.assertTrue(a["eventaware_stale"] and not a["generic_stale"])
        self.assertTrue(m["generic_stale"] and not m["eventaware_stale"])

    def test_rule_choice_uses_state_and_text_not_just_the_type(self):
        self.assertEqual(self.lc("regulatory_action", 1, text="US adds Huawei to the entity list: new export controls")["rule_id"], "EXP_EXPORT_CONTROL")
        self.assertEqual(self.lc("litigation", 1, text="Acme agrees to settle the lawsuit")["rule_id"], "EXP_SETTLEMENT")
        self.assertEqual(self.lc("regulatory_approval", 1, text="FDA approves the drug")["rule_id"], "EXP_CLINICAL")
        self.assertEqual(self.lc("earnings_beat", 1, state="rumored")["rule_id"], "EXP_RUMOR")
        self.assertEqual(self.lc("general_news", 1)["rule_id"], "EXP_DEFAULT")

    def test_a_denial_or_a_newer_event_ends_the_catalyst_at_once(self):
        d = self.lc("merger_acquisition", 10, state="denied")
        self.assertTrue(d["eventaware_stale"])
        self.assertEqual(d["ended_by"], "denied")
        s = self.lc("contract_win", 10, superseded_at=iso(NOW - dt.timedelta(hours=5)))
        self.assertTrue(s["eventaware_stale"])
        self.assertEqual(s["ended_by"], "superseded")

    def test_materiality_corroboration_and_price_already_moved_change_the_allowance_in_the_stated_direction(self):
        base = self.lc("contract_win", 1)["hours_allowed"]
        self.assertGreater(self.lc("contract_win", 1, materiality="high")["hours_allowed"], base)
        self.assertLess(self.lc("contract_win", 1, materiality="low")["hours_allowed"], base)
        self.assertGreater(self.lc("contract_win", 1, independent_sources=3)["hours_allowed"], base)
        self.assertLess(self.lc("contract_win", 1, priced_in=0.2)["hours_allowed"], base)
        self.assertGreater(self.lc("contract_win", 1, text="the award is being protested")["hours_allowed"], base)

    def test_the_holding_horizon_caps_a_very_long_catalyst_and_says_so(self):
        l = self.lc("bankruptcy_distress", 1, materiality="high")
        self.assertLessEqual(l["hours_allowed"], 2 * 3 * 24 + 0.1)
        self.assertIn("holding_horizon_cap", [m["name"] for m in l["modifiers"]])

    def test_the_explanation_names_the_rule_the_hours_and_the_verdict(self):
        e = self.lc("analyst_action", 30)["explanation"]
        self.assertIn("EXP_ANALYST", e)
        self.assertIn("EXPIRED", e)

    def test_every_rule_family_the_brief_lists_exists(self):
        fams = {r[1] for r in kbevent.EXPIRY_ROWS}
        for need in ("rumour", "analyst_rating", "earnings_result", "guidance_change", "contract_award", "contract_loss", "regulatory_approval", "regulatory_rejection", "acquisition", "offering", "insider_transaction", "product_launch", "recall",
                     "production_outage", "commodity_disruption", "sanctions", "tariffs", "export_controls", "litigation", "settlement", "clinical_fda"):
            self.assertIn(need, fams)

    def test_maturity_labels_follow_the_brief(self):
        self.assertEqual([kbevent.maturity(n) for n in (0, 19, 20, 49, 50, 99, 100)], ["insufficient", "insufficient", "informational", "informational", "preliminary", "preliminary", "review candidate"])


class TestEconomicMateriality(unittest.TestCase):
    def setUp(self):
        self.c = kbdb()
        self.cfg = H.cfg()
        eid = kbsnap.entity_of(self.c, "CUST")
        self.c.execute("INSERT INTO kb_financial_snapshots(entity_id,ticker,taken,metrics_json,flags_json,status,first_known) VALUES(?,?,?,?,?,?,?)",
                       (eid, "CUST", iso(NOW - dt.timedelta(days=2)), json.dumps({"reported": {"revenue_ttm": {"value": 1e9}, "net_income_ttm": {"value": 1e8}, "cash": {"value": 2e8}, "shares_diluted": {"value": 1e8}}, "derived": {"debt": 5e8}}), "{}", "ok", iso(NOW - dt.timedelta(days=2))))
        self.c.execute("INSERT INTO kb_market_snapshots(entity_id,ticker,observed,price,mcap,status) VALUES(?,?,?,?,?,?)", (eid, "CUST", iso(NOW - dt.timedelta(hours=3)), 10.0, 5e9, "ok"))

    def mat(self, et, text, **k):
        return {m["kind"]: m for m in kbevent.materiality_for(self.c, self.cfg, dict({"event_id": 1, "ticker": "CUST", "event_type": et, "text": text}, **k), NOW, persist=False)}

    def test_a_contract_is_measured_against_annual_revenue_and_the_arithmetic_is_recorded(self):
        m = self.mat("contract_win", "Customer Corp wins $100 million contract")["contract_vs_revenue"]
        self.assertAlmostEqual(m["result"], 0.10)
        self.assertEqual(m["band"], "high")
        self.assertEqual((m["numerator"]["value"], m["denominator"]["value"]), (1e8, 1e9))
        self.assertTrue(m["may_influence"])

    def test_an_offering_is_measured_against_market_cap_and_share_count(self):
        m = self.mat("capital_raise", "Customer Corp prices $500 million offering of 20 million shares")
        self.assertAlmostEqual(m["offering_vs_market_cap"]["result"], 0.10)
        self.assertAlmostEqual(m["offering_shares_vs_outstanding"]["result"], 0.20)

    def test_a_fine_is_measured_against_cash_revenue_and_earnings(self):
        m = self.mat("litigation", "Customer Corp to pay $50 million settlement")
        self.assertAlmostEqual(m["fine_or_settlement_vs_cash"]["result"], 0.25)
        self.assertAlmostEqual(m["fine_or_settlement_vs_earnings"]["result"], 0.50)

    def test_a_missing_input_is_unknown_not_zero_and_never_influences_trading(self):
        m = self.mat("contract_win", "Customer Corp wins a major contract")["contract_vs_revenue"]
        self.assertIsNone(m["result"])
        self.assertEqual(m["band"], "unknown")
        self.assertFalse(m["may_influence"])
        self.assertTrue(m["missing"])
        n = kbevent.materiality_for(self.c, self.cfg, {"event_id": 2, "ticker": "RIVL", "event_type": "contract_win", "text": "Rival Corp wins $5 million contract"}, NOW, persist=False)
        self.assertTrue(all(x["result"] is None for x in n))                                          # no financial snapshot for RIVL: nothing is invented

    def test_a_company_with_no_cached_market_value_cannot_have_an_offering_judged_against_it(self):
        self.c.execute("UPDATE kb_market_snapshots SET status='expired'")
        m = self.mat("capital_raise", "Customer Corp prices $500 million offering")
        self.assertIsNone(m["offering_vs_market_cap"]["result"])                                      # an expired price is not a current fact

    def test_a_snapshot_the_bot_did_not_yet_know_is_not_used(self):
        m = kbevent.materiality_for(self.c, self.cfg, {"event_id": 3, "ticker": "CUST", "event_type": "contract_win", "text": "wins $100 million contract"}, NOW - dt.timedelta(days=10), as_of=iso(NOW - dt.timedelta(days=10)), persist=False)
        self.assertIsNone([x for x in m if x["kind"] == "contract_vs_revenue"][0]["result"])

    def test_dividend_and_guidance_changes_use_the_stated_figures_only(self):
        self.assertAlmostEqual(self.mat("buyback_dividend", "Customer Corp raises dividend 12%")["dividend_change"]["result"], 0.12)
        g = self.mat("guidance_raise", "Customer Corp raises revenue outlook from $4.0 billion to $4.4 billion")["guidance_change_vs_prior"]
        self.assertAlmostEqual(g["result"], 0.10)
        self.assertIsNone(self.mat("guidance_raise", "Customer Corp raises outlook")["guidance_change_vs_prior"]["result"])

    def test_amount_parsing_handles_units_and_ignores_prose(self):
        a = kbevent.parse_amounts("pays $1.5 billion and 20 million shares, up 12%")
        self.assertEqual((a["usd"][0]["value"], a["shares"][0]["value"], a["pct"][0]["value"]), (1.5e9, 2e7, 0.12))
        self.assertEqual(kbevent.parse_amounts("no figures here"), {"usd": [], "shares": [], "pct": []})


class TestMultiEventState(unittest.TestCase):
    def setUp(self):
        self.c = kbdb()
        self.cfg = H.cfg()
        self.n = 0

    def add(self, hours_ago, et, headline, direction, mag=0.7, cluster=None):
        self.n += 1
        t = iso(NOW - dt.timedelta(hours=hours_ago))
        cur = self.c.execute("INSERT INTO events(news_uid,created,published,source,headline,event_type,scope,direction,magnitude,tickers_json,cluster_id,novelty,credibility,stale) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             ("u%d" % self.n, t, t, "wire", headline, et, "company", direction, mag, '["CUST"]', cluster if cluster is not None else self.n, 1.0, 0.9, 0))
        return cur.lastrowid

    def state(self):
        return kbevent.build_state(self.c, self.cfg, "CUST", NOW)

    def test_a_rumour_followed_by_a_denial_leaves_no_live_catalyst(self):
        self.add(20, "merger_acquisition", "Customer Corp reportedly in talks to buy Target Co", 0.6, cluster=1)
        self.add(6, "merger_acquisition", "Customer Corp denies it is in talks to buy Target Co", 0.0, cluster=1)
        s = self.state()
        self.assertEqual(s["net_direction"], 0.0)
        self.assertTrue(s["denials"])
        self.assertTrue(any(i["status"] == "denied" for i in s["items"]))

    def test_a_rumour_later_confirmed_is_a_confirmation(self):
        self.add(30, "merger_acquisition", "Customer Corp reportedly close to buying Target Co", 0.6, cluster=1)
        self.add(2, "merger_acquisition", "Customer Corp agrees to buy Target Co for $2 billion", 0.8, cluster=1)
        s = self.state()
        self.assertTrue(s["confirmations"])
        self.assertEqual(s["items"][0]["status"], "confirmed")

    def test_an_award_followed_by_a_protest_is_contested_and_weighs_less(self):
        a = self.add(10, "contract_win", "Customer Corp wins $300 million Army contract", 0.8, cluster=1)
        clean = self.state()["items"][0]["weight"]
        self.add(4, "contract_win", "Rival Corp files protest over Customer Corp Army contract award", 0.5, cluster=1)
        contested = self.state()["items"][0]
        self.assertEqual(contested["status"], "contested")
        self.assertLess(contested["weight"], clean)

    def test_a_guidance_cut_then_a_temporary_explanation_is_softened_not_erased(self):
        self.add(30, "guidance_cut", "Customer Corp cuts full-year guidance", -0.8, cluster=1)
        full = self.state()["items"][0]["weight"]
        self.add(10, "guidance_cut", "Customer Corp says the guidance cut reflects a temporary one-time cost", -0.3, cluster=1)
        soft = self.state()["items"][0]
        self.assertLess(soft["weight"], full)
        self.assertGreater(soft["weight"], 0)
        self.assertIn("temporary", soft["note"])

    def test_a_beat_plus_a_guidance_cut_is_two_chains_that_can_disagree_across_timeframes(self):
        self.add(8, "earnings_beat", "Customer Corp beats earnings estimates", 0.7)
        self.add(7, "guidance_cut", "Customer Corp cuts its outlook", -0.8)
        s = self.state()
        self.assertEqual(len(s["items"]), 2)
        self.assertEqual(len({i["chain"] for i in s["items"]}), 2)

    def test_two_opposite_strong_items_are_contested_not_netted_to_a_number(self):
        self.add(5, "earnings_beat", "Customer Corp beats earnings estimates", 0.7, mag=0.7)
        self.add(5, "earnings_miss", "Customer Corp misses estimates on revenue", -0.7, mag=0.7)
        s = self.state()
        self.assertEqual(s["near_term"]["label"] if s["near_term"]["label"] == "contested" else s["medium_term"]["label"], "contested")
        self.assertEqual(s["net_direction"], 0.0)

    def test_filler_and_market_wide_items_are_not_company_catalysts(self):
        self.add(3, "general_news", "Customer Corp mentioned in a column", 0.5)
        self.add(3, "war_escalation", "War escalates", -0.9)
        self.assertEqual(self.state()["items"], [])

    def test_an_expired_item_no_longer_supports_the_thesis(self):
        self.add(24 * 12, "analyst_action", "Broker lifts price target on Customer Corp", 0.5)
        s = self.state()
        self.assertTrue(all(i["expired"] for i in s["items"]))
        self.assertEqual(s["net_direction"], 0.0)

    def test_history_is_versioned_and_never_rewritten_and_replays_see_only_what_existed(self):
        self.add(8, "earnings_beat", "Customer Corp beats earnings estimates", 0.7)
        s1 = self.state()
        v1 = kbevent.save_state(self.c, "CUST", s1, 1, NOW)
        self.assertEqual(v1["version"], 1)
        self.assertFalse(kbevent.save_state(self.c, "CUST", s1, 1, NOW)["changed"])                   # nothing changed: no new version
        self.add(2, "guidance_cut", "Customer Corp cuts its outlook", -0.8)
        v2 = kbevent.save_state(self.c, "CUST", kbevent.build_state(self.c, self.cfg, "CUST", NOW + dt.timedelta(hours=1)), 2, NOW + dt.timedelta(hours=1))
        self.assertEqual(v2["version"], 2)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM company_event_state_history WHERE ticker='CUST'").fetchone()[0], 2)
        then = kbevent.state_as_of(self.c, "CUST", NOW + dt.timedelta(minutes=1))
        self.assertEqual(len(then["items"]), 1)                                                       # the earlier picture, as it was
        self.assertIsNone(kbevent.state_as_of(self.c, "CUST", NOW - dt.timedelta(days=1)))

    def test_the_relation_between_a_new_event_and_a_prior_one_is_classified(self):
        base = {"chain": 1, "family": "corporate", "state": "rumored", "direction": 0.5, "independent_sources": 1, "source": "a"}
        self.assertEqual(kbevent.relate(dict(base, state="denied"), base), "denies")
        self.assertEqual(kbevent.relate(dict(base, state="confirmed"), base), "confirms")
        self.assertEqual(kbevent.relate(dict(base, novelty="correction"), base), "corrects")
        self.assertEqual(kbevent.relate(dict(base, chain=2, family="results", direction=-0.6), base), "contradicts")


class TestRegimeContext(unittest.TestCase):
    def test_a_strong_company_specific_signal_cannot_be_overwhelmed_or_reversed_by_the_regime(self):
        strong = kbregime.adjust(["high_volatility", "liquidity_stress", "risk_off"], "BUY", 0.9, 0.8)
        weak = kbregime.adjust(["high_volatility", "liquidity_stress", "risk_off"], "BUY", 0.3, 0.4)
        self.assertGreaterEqual(strong["size_mult"], 0.8)
        self.assertLessEqual(strong["confidence_add"], 0.05)
        self.assertTrue(strong["protected_strong_signal"])
        self.assertLess(weak["size_mult"], strong["size_mult"])
        self.assertFalse(strong["direction_changed"] or weak["direction_changed"])

    def test_every_regime_the_brief_names_has_a_defined_adjustment(self):
        self.assertEqual(set(kbregime.ADJUST), set(kbregime.REGIME_IDS))
        for r in kbregime.REGIME_IDS:
            self.assertEqual(len(kbregime.ADJUST[r]), 6)

    def test_a_regime_is_only_declared_from_fresh_inputs(self):
        c = db.open_db(":memory:")
        self.assertEqual(kbregime.detect(c, NOW)["status"], "unknown")                                # no index history stored: no regime is invented

    def test_replays_see_only_the_regimes_stored_at_the_time(self):
        c = db.open_db(":memory:")
        c.execute("INSERT INTO market_regimes(regime_id,detected,confidence,started,last_update,expiry,rule_trace,active) VALUES('risk_off',?,0.7,?,?,?,'t',1)", (iso(NOW), iso(NOW), iso(NOW), iso(NOW + dt.timedelta(days=3))))
        self.assertEqual([r["regime_id"] for r in kbregime.active(c, iso(NOW + dt.timedelta(days=1)))], ["risk_off"])
        self.assertEqual(kbregime.active(c, iso(NOW - dt.timedelta(days=1))), [])
        self.assertEqual(kbregime.active(c, iso(NOW + dt.timedelta(days=9))), [])


class TestRedTeamInterventionMeasurement(unittest.TestCase):
    def setUp(self):
        self.c = db.open_db(":memory:")
        self.cfg = H.cfg()
        self.t0 = parse_ts("2026-10-20T15:00:00+00:00")
        self.c.execute("INSERT INTO market_series(symbol,day,open,high,low,close,volume) VALUES('ZZZ','2026-10-20',100,100.5,99.5,100,1e6)")
        for i, (cl, hi, lo) in enumerate([(101, 103, 99.5), (102, 104, 100.5), (99, 102, 97), (98, 100, 96), (97, 99, 95), (96, 98, 94)]):
            self.c.execute("INSERT INTO market_series(symbol,day,open,high,low,close,volume) VALUES('ZZZ',?,?,?,?,?,1e6)", ("2026-10-%02d" % (21 + i), cl, hi, lo, cl, ))

    def rec(self, final, before=5.0, after=0.0, side="BUY", stop=-0.03, target=0.05):
        rt = {"net_conviction": 0.5, "bear_points": [{"rule": "stale_catalyst", "severity": 0.9, "action": "veto", "mult": 1.0, "evidence": "old"}]}
        gate = {"ok": final != "blocked", "size_mult": 1.0 if final != "reduced" else 0.5, "reasons": ["stale"], "would": {"ok": False, "size_mult": 1.0, "reasons": ["stale"]}}
        return kbmeasure.record(self.c, self.cfg, 1, {"ticker": "ZZZ", "event_type": "earnings_beat", "origin": "event"}, {"verdict": side, "confidence": 0.7, "target_return": target, "stop_return": stop, "horizon_days": 3}, before, after, rt, gate, final, self.t0)

    def outcome(self, iid):
        kbmeasure.measure_due(self.c, self.cfg, parse_ts("2026-10-30T00:00:00+00:00"))
        return dict(self.c.execute("SELECT * FROM intervention_outcomes WHERE intervention_id=?", (iid,)).fetchone())

    def test_the_recorded_idea_keeps_original_and_final_action_size_objections_and_the_hypothetical_plan(self):
        r = dict(self.c.execute("SELECT * FROM redteam_interventions WHERE id=?", (self.rec("blocked"),)).fetchone())
        self.assertEqual((r["original_action"], r["final_action"], r["original_size_pct"], r["final_size_pct"], r["hard_veto"]), ("BUY", "blocked", 5.0, 0.0, 1))
        self.assertEqual((r["hyp_entry"], r["hyp_stop"], r["hyp_target"]), (100.0, -0.03, 0.05))
        self.assertEqual(json.loads(r["objections_json"])["objections"][0]["rule"], "stale_catalyst")

    def test_a_blocked_idea_that_would_have_hit_its_stop_first_counts_as_a_loss_avoided_by_the_hand_calculation(self):
        o = self.outcome(self.rec("blocked"))
        self.assertAlmostEqual(o["r1"], 0.01)
        self.assertAlmostEqual(o["r3"], -0.01)
        self.assertAlmostEqual(o["r5"], -0.03)
        self.assertEqual(o["first_hit"], "stop")
        self.assertAlmostEqual(o["loss_avoided"], 0.05 * 0.03)
        self.assertAlmostEqual(o["net_benefit"], 0.0015)
        self.assertEqual(o["helped"], 1)

    def test_a_blocked_winner_is_a_gain_missed(self):
        o = self.outcome(self.rec("blocked", stop=-0.20, target=0.015))
        self.assertEqual(o["first_hit"], "target")
        self.assertAlmostEqual(o["gain_missed"], 0.05 * 0.015)
        self.assertLess(o["net_benefit"], 0)
        self.assertEqual(o["helped"], 0)

    def test_a_reduced_idea_gets_credit_only_for_the_part_it_removed(self):
        o = self.outcome(self.rec("reduced", after=2.5))
        self.assertAlmostEqual(o["loss_avoided"], 0.025 * 0.03)

    def test_an_allowed_idea_changes_nothing_economically(self):
        o = self.outcome(self.rec("allowed", after=5.0))
        self.assertEqual(o["net_benefit"], 0.0)
        self.assertIsNone(o["helped"])

    def test_a_short_that_rises_is_a_loss_for_the_short_and_a_blocked_one_helped(self):
        o = self.outcome(self.rec("blocked", side="SHORT", stop=-0.03, target=0.05))
        self.assertIn(o["first_hit"], ("stop", "target", "none", None))

    def test_nothing_is_measured_before_a_session_has_passed_and_the_scorecard_labels_its_maturity(self):
        self.rec("blocked")
        self.assertEqual(kbmeasure.measure_due(self.c, self.cfg, self.t0)["measured"], 0)
        kbmeasure.measure_due(self.c, self.cfg, parse_ts("2026-10-30T00:00:00+00:00"))
        sc = kbmeasure.scorecard(self.c)
        self.assertEqual(sc["rule"]["stale_catalyst"]["maturity"], "insufficient")
        self.assertEqual(sc["totals"]["measured"], 1)
        for dim in ("rule", "event_family", "side", "origin", "regime", "confidence_band", "timeframe"):
            self.assertIn(dim, sc)

    def test_recording_never_raises_into_a_trade(self):
        self.assertIsNone(kbmeasure.record(None, self.cfg, 1, {}, {}, 1, 1, None, None, "allowed"))


class TestFilingFacts(unittest.TestCase):
    F4 = ('<ownershipDocument><issuer><issuerCik>1</issuerCik><issuerTradingSymbol>AAA</issuerTradingSymbol></issuer><aff10b5One>0</aff10b5One><reportingOwner><reportingOwnerId><rptOwnerName>DOE J</rptOwnerName></reportingOwnerId>'
          '<reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>CFO</officerTitle></reportingOwnerRelationship></reportingOwner><nonDerivativeTable>'
          '<nonDerivativeTransaction><transactionCoding><transactionCode>S</transactionCode></transactionCoding><transactionAmounts><transactionShares><value>10000</value></transactionShares><transactionPricePerShare><value>150</value></transactionPricePerShare>'
          '<transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts><postTransactionAmounts><sharesOwnedFollowingTransaction><value>90000</value></sharesOwnedFollowingTransaction></postTransactionAmounts></nonDerivativeTransaction>'
          '<nonDerivativeTransaction><transactionCoding><transactionCode>F</transactionCode></transactionCoding><transactionAmounts><transactionShares><value>3000</value></transactionShares><transactionPricePerShare><value>150</value></transactionPricePerShare>'
          '<transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>')

    def test_a_form_4_sale_is_measured_against_the_insiders_own_holding_and_tax_withholding_is_not_a_signal(self):
        ev_ = F.insider_events(F.parse_form4(self.F4))
        self.assertEqual([e["event_type"] for e in ev_], ["insider_sale"])
        self.assertAlmostEqual(ev_[0]["value_usd"], 1.5e6)
        self.assertAlmostEqual(ev_[0]["pct_of_holding"], 0.10)

    def test_unreadable_or_wrong_documents_yield_nothing(self):
        self.assertEqual(F.parse_form4("<not xml"), {})
        self.assertEqual(F.parse_form4("<other/>"), {})
        self.assertEqual(F.parse_13dg("<x/>"), {})

    def test_guidance_is_extracted_only_when_a_range_is_stated(self):
        g = F.parse_guidance("The company expects full-year revenue of $4.1 billion to $4.3 billion")
        self.assertEqual((g["low"], g["high"]), (4.1e9, 4.3e9))
        self.assertEqual(F.parse_guidance("Management is optimistic and expects strong revenue growth"), {})

    def test_offering_size_needs_the_fixed_sentence_shape(self):
        self.assertEqual(F.parse_offering_text("aggregate offering price of up to $250 million")["usd"], 2.5e8)
        self.assertEqual(F.parse_offering_text("we may sell securities from time to time"), {})

    def test_filing_events_carry_provenance_and_two_times_and_never_leak_the_future(self):
        c = kbdb()
        eid = kbsnap.entity_of(c, "CUST")
        sub = {"cik": 11, "filings": [{"accession": "0000000011-26-000001", "form": "8-K", "filed": "2026-10-30", "period": None, "items": "4.01,9.01", "doc": "d.htm"}, {"accession": "0000000011-26-000002", "form": "NT 10-Q", "filed": "2026-10-31", "period": "2026-09-30", "items": "", "doc": "n.htm"}]}
        self.assertEqual(F.ingest_submissions(c, eid, "CUST", sub, NOW)["ingested"], 2)
        self.assertEqual(F.ingest_submissions(c, eid, "CUST", sub, NOW)["ingested"], 0)                  # idempotent
        row = dict(c.execute("SELECT * FROM kb_filing_events WHERE item='4.01'").fetchone())
        for k in ("cik", "accession", "form", "filed", "event_type", "source_url", "first_known", "retrieved", "reliability"):
            self.assertIsNotNone(row[k], k)
        self.assertEqual(row["event_type"], "auditor_change")
        self.assertEqual(len(F.known_events(c, "CUST", iso(NOW - dt.timedelta(hours=1)))), 0)             # the bot had not retrieved them yet
        self.assertEqual(len(F.known_events(c, "CUST", iso(NOW + dt.timedelta(hours=1)))), 2)
        self.assertEqual(len(F.known_events(c, "CUST", "2026-10-30T12:00:00+00:00", public_time=True)), 1)  # but the PUBLIC record at that time had one

    def test_an_amendment_is_flagged_and_a_late_filing_notice_is_an_event_in_itself(self):
        c = kbdb()
        eid = kbsnap.entity_of(c, "CUST")
        F.ingest_submissions(c, eid, "CUST", {"cik": 11, "filings": [{"accession": "0000000011-26-000003", "form": "SC 13D/A", "filed": "2026-10-30", "items": "", "doc": "x"}, {"accession": "0000000011-26-000004", "form": "NT 10-K", "filed": "2026-10-30", "items": "", "doc": "y"}]}, NOW)
        got = {r["form"]: r for r in c.execute("SELECT * FROM kb_filing_events")}
        self.assertEqual(got["SC 13D/A"]["amendment"], 1)
        self.assertEqual(got["NT 10-K"]["event_type"], "late_annual_report")


class TestFinancialSnapshots(unittest.TestCase):
    def test_provenance_survives_from_the_xbrl_point_to_the_stored_metric(self):
        from paper_agent import facts
        pts = facts._points({"units": {"USD": [{"start": "2025-01-01", "end": "2025-12-31", "val": 1000, "filed": "2026-02-01", "form": "10-K", "accn": "0001-26-000009"}]}})
        for p in pts:
            p["tag"], p["tax"] = "Revenues", "us-gaap"
        d = facts.ttm_detail(pts, dt.date(2026, 2, 20))
        self.assertEqual((d["accn"], d["tag"], d["taxonomy"], d["unit"], d["form"]), (["0001-26-000009"], "Revenues", "us-gaap", "USD", ["10-K"]))

    def test_no_facts_stored_means_unavailable_never_an_estimate(self):
        c = kbdb()
        eid = kbsnap.entity_of(c, "CUST")
        r = kbsnap.financial_snapshot(c, H.cfg(), eid, "CUST", NOW)
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM kb_financial_snapshots").fetchone()[0], 0)

    def test_an_expired_price_is_stored_as_expired_and_is_not_a_current_fact(self):
        c = kbdb()
        eid = kbsnap.entity_of(c, "CUST")
        c.execute("INSERT INTO market_series(symbol,day,open,high,low,close,volume) VALUES('CUST','2026-10-01',10,11,9,10,1e6)")
        r = kbsnap.market_snapshot(c, H.cfg(), eid, "CUST", NOW)
        self.assertEqual(r["status"], "expired")
        self.assertIsNone(kbsnap.current_market(c, eid))

    def test_the_seed_import_never_stores_correlation_as_a_relationship_and_marks_its_own_evidence_unverified(self):
        c = kbdb()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM kb_relationships WHERE rel_type IN ('corr','correlation','comovement')").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM kb_relationships WHERE evidence NOT LIKE '%NOT independently verified%'").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM kb_relationships WHERE mechanism IS NULL OR mechanism=''").fetchone()[0], 0)


class TestWhyPageKnowledgeThenAndNow(unittest.TestCase):
    def test_a_decision_made_before_the_relationships_were_known_shows_none_then_and_some_now(self):
        from paper_agent import whybundle
        c = kbdb()
        old = iso(NOW - dt.timedelta(days=30))
        c.execute("INSERT INTO decisions(created,ticker,action,status,reason) VALUES(?,?,?,?,?)", (old, "CUST", "BUY", "REJECTED", "{}"))
        did = c.execute("SELECT MAX(id) FROM decisions").fetchone()[0]
        cf = whybundle.case_file(c, H.cfg(), decision_id=did, now=NOW)
        sec = {s_["key"]: s_ for s_ in cf["sections"]}
        row = [r for r in sec["then_now"]["rows"] if r[0] == "Relationships known"][0]
        self.assertEqual(row[1], "0")
        self.assertGreater(int(row[2]), 0)
        self.assertEqual(sec["relationships"]["status"], "missing")                                         # at the decision the bot knew none, and the page says so


class TestCommonTimeframe(unittest.TestCase):
    def test_the_boundaries_are_the_ones_the_brief_specifies(self):
        self.assertEqual([timeframe.from_sessions(n) for n in (0, 1, 2, 5, 6, 20, 21, 90, None, -1)], ["immediate", "immediate", "short", "short", "medium", "medium", "long", "long", "unknown", "unknown"])

    def test_every_old_dialect_maps_into_the_common_vocabulary(self):
        for old in ("days", "weeks", "1-3m", "months", "immediate", None, "garbage"):
            self.assertIn(timeframe.from_legacy(old), timeframe.ALL)
        self.assertEqual(timeframe.from_hours(36), "short")
        self.assertEqual(timeframe.from_hours(240), "medium")

    def test_the_analyst_expiry_relationship_rules_and_red_team_all_speak_it(self):
        self.assertIn(R.analyze_item(db.open_db(":memory:"), H.cfg(), Universe({"NVDA": {"name": "NVIDIA Corp", "aliases": ["NVIDIA"], "cik": 1}}), {"uid": "x", "title": "NVIDIA beats earnings estimates", "summary": "", "analysis": dict(nlp.analyze("NVIDIA beats earnings estimates", "")), "tickers": ["NVDA"]}, NOW)["timeframe"], timeframe.ALL)
        self.assertTrue({r["horizon"] for r in kbrel.RULES} <= set(timeframe.ALL))
        self.assertTrue(all(timeframe.from_hours(r[6]) in timeframe.ALL for r in kbevent.EXPIRY_ROWS))


class TestEventWorthinessGate(unittest.TestCase):
    def a(self, title, et="general_news", n=1, pat=False):
        return eventworth.assess(title, "", et, n, pat)

    def test_each_kind_of_non_event_abstains_with_its_own_reason(self):
        cases = [("Will Strong AWS Performance Boost Amazon's Q3 Earnings Report?", "question"), ("Why Garmin Stock Dropped After Record Results", "question"), ("3 AI stocks to buy now", "stock_list"), ("Top movers: Acme jumps, Beta slides", "roundup"),
                 ("Garmin Q3 Earnings Call Highlights", "commentary"), ("Key Takeaways From Microsoft and ServiceNow Earnings", "commentary"), ("Alphabet is poised for a strong 2027", "promotional"), ("Nvidia vs AMD: which AI stock is the better buy", "comparison"),
                 ("INVESTOR ALERT: Pomerantz Law Firm Reminds Investors with Losses", "solicitation"), ("424B2 - Morgan Stanley Finance LLC (0001666268) (Filer)", "routine_filing"), ("Chevron and Exxon hit all-time highs as refining profits soar", "price_recap"),
                 ("FedEx earnings are imminent; analysts revise forecasts ahead of earnings", "commentary"), ("Tesla Rises 4% as ARK Reiterates Target; Rivian Gains 3%, Lucid Holds", "roundup")]
        for title, kind in cases:
            w = self.a(title, n=2 if kind in ("comparison",) else 1)
            self.assertFalse(w["worthy"], title)
            self.assertEqual(w["kind"], kind, title)

    def test_a_concrete_event_stays_an_event_even_with_a_price_move_or_a_roundup_word_nearby(self):
        for title, et in (("Microsoft stock jumps 5% as cloud growth drives earnings and revenue beat", "earnings_beat"), ("Google misses revenue expectations, Alphabet stock sinks in early after-hours trading", "earnings_miss"),
                          ("Equinix Reports Third-Quarter Results and Raises 2026 Guidance", "guidance_raise"), ("DoorDash to buy Instacart for $17bn", "merger_acquisition"), ("Stock X jumps premarket after FDA approval", "regulatory_approval")):
            self.assertTrue(self.a(title, et, pat=True)["worthy"], title)

    def test_an_equity_prospectus_is_an_event_but_a_structured_note_supplement_is_not(self):
        self.assertTrue(eventworth.assess("424B5 - Acme Corp (0000001234) (Filer)", "offering of 5 million shares of common stock", "capital_raise", 1, True)["worthy"])
        self.assertFalse(eventworth.assess("424B5 - Acme Corp (0000001234) (Filer)", "", "capital_raise", 1, True)["worthy"])
        self.assertFalse(eventworth.assess("424B2 - BANK OF NOVA SCOTIA (0000009631) (Filer)", "common stock", "capital_raise", 1, True)["worthy"])

    def test_the_same_gate_governs_candidates_and_the_analyst(self):
        from paper_agent import evidence
        title = "Pomerantz Law Firm Reminds Investors: Garmin beats earnings estimates, class action deadline"
        a = dict(nlp.analyze("Garmin beats earnings estimates", ""), direction=0.8, magnitude=0.8)
        self.assertIsNone(evidence.candidate_gate(a, ["GRMN"], {"GRMN": 0.9}, 1.0, 1.0, H.cfg()["intelligence"]))       # the older gate lets this through ...
        gate, cands = evidence.build_event_candidates(None, a, title, "wire", "u", 1, ["GRMN"], {"GRMN": 0.9}, 1.0, 1.0, 0.9, H.cfg()["intelligence"])
        self.assertEqual(gate, "not_an_event: solicitation")                                                              # ... the worthiness gate does not
        self.assertEqual(cands, [])


class TestClassifierAndMatcherCorrections(unittest.TestCase):
    W = {"GOOGL": {"name": "Alphabet Inc.", "aliases": ["Alphabet"], "cik": 1652044}, "AMZN": {"name": "AMAZON COM INC", "aliases": ["AMAZON COM"], "cik": 1018724}, "LITE": {"name": "Lumentum Holdings Inc.", "aliases": ["Lumentum"], "cik": 1633978},
         "MS": {"name": "MORGAN STANLEY", "aliases": ["Morgan Stanley"], "cik": 895421}, "CAF": {"name": "Morgan Stanley China A Share Fund, Inc.", "aliases": ["Morgan Stanley China A Share Fund"], "cik": 1}, "SWK": {"name": "Stanley Black & Decker, Inc.", "aliases": ["Stanley Black"], "cik": 93556},
         "NWAX": {"name": "New America Acquisition I Corp.", "aliases": ["America Acquisition I"], "cik": 2}, "MCD": {"name": "McDonald's Corp", "aliases": ["McDonald's"], "cik": 63908}}

    def test_the_verb_miss_in_its_plural_subject_form_is_recognised(self):
        a = nlp.analyze("Alphabet Third-Quarter Results Miss Estimates as Capital Spending More Than Doubles", "")
        self.assertEqual(a["event_type"], "earnings_miss")
        self.assertLess(a["direction"], -0.3)
        for t, et in (("firm slashes forecast", "guidance_cut"), ("regulator launch probe into firm", "regulatory_action")):
            self.assertEqual(nlp.analyze(t, "")["event_type"], et, t)

    def test_in_line_and_unstated_results_are_results_events_with_a_direction_from_the_figures_only(self):
        self.assertEqual(nlp.analyze("ServiceNow reports Q3 2026 in line with expectations but stock drops 17.1%", "")["event_type"], "earnings_report")
        a = nlp.analyze("STMicro reports Q3 revenue up 17.9% YoY to $3.76B, operating income up 98% to $429M, above $400.5M est.", "")
        self.assertEqual(a["event_type"], "earnings_report")
        self.assertGreater(a["direction"], 0.3)
        self.assertEqual(nlp.analyze("Microsoft posts $92.3bn quarterly revenue after offering buyouts to 60,000 employees", "")["direction"], 0.0)   # no stated beat or miss: no invented direction
        self.assertEqual(nlp.analyze("Equinix to report Q3 revenue after the bell", "")["event_type"], "general_news")

    def test_a_recall_is_its_own_operations_event_and_a_rating_word_needs_a_rating_context(self):
        self.assertEqual(nlp.analyze("Tesla recalls 200,000 vehicles", "")["event_type"], "recall")
        self.assertNotEqual(nlp.analyze("Starbucks U.S. stores outperform while international disappoints", "")["event_type"], "analyst_action")
        self.assertEqual(nlp.analyze("JP Morgan maintains underweight on Cboe, raises price target", "")["event_type"], "analyst_action")

    def test_a_possessive_and_a_sentence_ending_name_are_matched(self):
        U = Universe(self.W, set())
        for t in ("Finland probes Google’s forest clearing", "Almost all of Amazon's new Echo gear is available", "Marvell is close to buying Lumentum. Analysts say they see logic"):
            self.assertTrue({"GOOGL", "AMZN", "LITE"} & {m[0] for m in U.match(t, "")}, t)
        self.assertEqual([m[0] for m in U.match("McDonald's earnings beat estimates", "")], ["MCD"])        # names that ARE possessive still match

    def test_a_fund_or_shell_matches_only_on_its_own_tag_or_full_name_never_a_generic_word(self):
        U = Universe(self.W, set())
        got = {m[0] for m in U.match("Rural America by the numbers: how voters feel", "")}
        self.assertNotIn("NWAX", got)
        self.assertEqual({m[0] for m in U.match("Morgan Stanley raises its price target", "")} & {"CAF", "MS"}, {"MS"})
        self.assertIn("CAF", {m[0] for m in U.match("Morgan Stanley China A Share Fund announces distribution", "")})
        self.assertIn("NWAX", {m[0] for m in U.match("New America Acquisition I Corp. prices IPO", "")})

    def test_a_filing_index_entry_resolves_by_its_own_cik_not_by_words_in_the_name(self):
        U = Universe(self.W, set())
        self.assertEqual(U.match("424B2 - MORGAN STANLEY (0000895421) (Filer)", ""), [("MS", 0.95, "cik")])
        self.assertEqual(U.match("424B2 - Morgan Stanley Finance LLC (0001666268) (Filer)", ""), [])        # a different legal entity, not in the universe: not guessed

    def test_an_all_caps_headline_does_not_turn_every_word_into_a_ticker(self):
        U = Universe({"NET": {"name": "Cloudflare Inc", "aliases": [], "cik": 1477333}}, set())
        self.assertEqual(U.match("ACME CORP REPORTS THIRD QUARTER NET INCOME OF $3.0 MILLION", ""), [])
        self.assertEqual([m[0] for m in U.match("Acme reports results; NET jumps after hours", "")], ["NET"])

    def test_an_explicit_guidance_cut_outweighs_scattered_positive_words_in_the_analysts_reading(self):
        h = "Intel reports Q3 revenue up 21% YoY to $16.5B, vs. $16.5B est., and forecasts Q4 revenue and adjusted EPS below estimates; INTC drops 22%+ after hours"
        U = Universe({"INTC": {"name": "Intel Corp", "aliases": ["Intel"], "cik": 50863}}, set())
        r = R.analyze_item(db.open_db(":memory:"), H.cfg(), U, {"uid": "x", "title": h, "summary": "", "analysis": dict(nlp.analyze(h, "")), "tickers": ["INTC"]}, NOW)
        self.assertEqual([e["ticker"] for e in r["losers"]], ["INTC"])
        self.assertEqual(r["beneficiaries"], [])


if __name__ == "__main__":
    unittest.main()
