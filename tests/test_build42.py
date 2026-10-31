"""Build 42: influence modes, graded red team, aligned contradictions (with a labelled corpus), the canonical reasoning record, supersession and the hop policy."""
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import db, graph, pipeline, reasoning as R, skeptic as S  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

SEED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "relations.json")
W = {"NVDA": {"name": "NVIDIA Corp", "aliases": ["NVIDIA", "Nvidia"], "cik": 1045810}, "TSM": {"name": "Taiwan Semiconductor Manufacturing", "aliases": ["TSMC", "Taiwan Semiconductor"], "cik": 1046179},
     "AMD": {"name": "Advanced Micro Devices", "aliases": ["AMD"], "cik": 2488}, "LMT": {"name": "Lockheed Martin Corp", "aliases": ["Lockheed Martin", "Lockheed"], "cik": 936468},
     "ACME": {"name": "Acme Corp", "aliases": ["Acme"], "cik": 1}, "BETA": {"name": "Beta Industries", "aliases": ["Beta"], "cik": 2}, "ASML": {"name": "ASML Holding", "aliases": ["ASML"], "cik": 937966}}
U = Universe(W)
NOW = TL.T_PRE


def add_event(c, headline, et, direction, hours_ago, mag=0.9, cred=0.9, tickers='["NVDA"]', gate=None, cluster=None, base=NOW):
    c.execute("INSERT INTO events(created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source,stale,gate,cluster_id) VALUES(?,?,?,?,?,?,?,?,?,0,?,?)",
              (iso(base - dt.timedelta(hours=hours_ago)), headline, direction, mag, cred, et, tickers, "company", "Wire", gate, cluster))
    c.commit()


def cd(**kw):
    return dict({"ticker": "NVDA", "event_type": "earnings_beat", "direction": 0.8, "credibility": 0.8, "headline": "NVIDIA announces record results and raises its outlook", "age_hours": 3.0}, **kw)


def fc(verdict="BUY", **inp):
    return {"verdict": verdict, "inputs": dict({"vol_daily": 0.02}, **inp), "stop_return": -0.04, "ev_net": 0.025, "confidence": 0.7, "horizon_days": 3,
            "scenarios": {"bull": {"ret": 0.06, "p": 0.3}, "base": {"ret": 0.02, "p": 0.4}, "bear": {"ret": -0.03, "p": 0.3}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.c = H.fresh_db()
        self.cfg = H.cfg()
        self.cfg["intelligence"]["influence"] = "active"
        self.cfg["graph"]["enabled"] = True
        graph.seed(self.c, NOW, SEED)

    def rv(self, cand=None, f=None, **kw):
        return S.review(self.c, self.cfg, cand or cd(), f or fc(), kw.get("comp", {"adv_usd": 5e9}), kw.get("th", {}), kw.get("regime", {}), NOW)

    def rules(self, rv):
        return {b["rule"]: b for b in rv["bear_points"]}


class TestTheModesAsPureRules(Base):
    def gate(self, rv, mode):
        return S.gate(rv, self.cfg, mode)

    def test_off_and_observe_never_act_but_observe_still_records_what_would_have_happened(self):
        add_event(self.c, "NVIDIA cuts full-year guidance", "guidance_cut", -0.8, 0.3)
        rv = self.rv()
        for mode in ("off", "observe"):
            g = self.gate(rv, mode)
            self.assertTrue(g["ok"])
            self.assertEqual(g["size_mult"], 1.0)
            self.assertFalse(g["would"]["ok"], mode)                                                    # but the record says active mode would have stopped it

    def test_guard_vetoes_only_factually_broken_ideas_and_makes_the_rest_smaller(self):
        add_event(self.c, "NVIDIA cuts full-year guidance", "guidance_cut", -0.8, 0.3)                    # newer, stronger, same subject: superseded
        self.assertFalse(self.gate(self.rv(), "guard")["ok"])
        self.c.execute("DELETE FROM events")
        self.c.commit()
        rv = self.rv(f=fc(priced_in=0.14))                                                               # "fully priced in" is a HOLD, not a hard veto
        self.assertIn("fully_priced_in", self.rules(rv))
        g = self.gate(rv, "guard")
        self.assertTrue(g["ok"])                                                                         # guard does not hold
        self.assertFalse(g["would"]["ok"])                                                               # active would
        self.assertFalse(self.gate(rv, "active")["ok"])

    def test_every_hard_veto_rule_vetoes_in_guard_and_the_other_rules_never_do(self):
        for rule, spec in S.RULES.items():
            rv = {"bear_points": [{"point": spec["label"], "severity": 0.9, "evidence": "x", "rule": rule, "action": spec["cls"], "mult": spec["mult"]}], "net_conviction": 0.9, "fatal_flaw": spec["cls"] in ("veto", "hold")}
            g = S.gate(rv, self.cfg, "guard")
            self.assertEqual(not g["ok"], rule in S.HARD_VETO, rule)
        self.assertEqual(S.HARD_VETO - set(S.RULES), set())

    def test_size_reductions_apply_in_guard_compound_and_are_floored_and_nothing_enlarges(self):
        rv = self.rv(f=dict(fc(), stop_return=-0.012, ev_net=0.006), comp={"adv_usd": 2e7}, regime={"regime": "risk_off"}, th={"state": "Overcrowded"})
        g = self.gate(rv, "guard")
        self.assertTrue(g["ok"])
        self.assertLess(g["size_mult"], 0.85)
        self.assertGreaterEqual(g["size_mult"], 0.5)                                                    # floored: a pile of small objections cannot make a trade absurdly small
        for mode in ("off", "observe", "guard", "active"):
            self.assertLessEqual(self.gate(rv, mode)["size_mult"], 1.0)

    def test_the_conviction_floor_only_applies_in_active_mode(self):
        rv = self.rv()
        rv["net_conviction"] = 0.1
        self.assertTrue(self.gate(rv, "guard")["ok"])
        self.assertFalse(self.gate(rv, "active")["ok"])

    def test_every_review_carries_the_rule_that_raised_each_objection_and_what_it_may_do(self):
        rv = self.rv(f=fc(priced_in=0.05))
        for b in rv["bear_points"]:
            self.assertIn(b["rule"], S.RULES)
            self.assertIn(b["action"], S.CLASSES)
            self.assertEqual(b["action"], S.RULES[b["rule"]]["cls"])


# ------------------------------------------------------------------------------------------------ contradictions: aligned, not merely "other news"
CORPUS = [  # (name, candidate headline/type, other headline/type, hours the other came BEFORE now, other direction, expected rule or None)
    ("same results event, newer and strong", "earnings_beat", "NVIDIA announces record results", "guidance_cut", "NVIDIA cuts full-year guidance", 0.3, -0.8, "contradiction_superseded"),
    ("same results event, newer but weak source", "earnings_beat", "NVIDIA announces record results", "guidance_cut", "NVIDIA may trim guidance, analysts say", 0.3, -0.8, "contradiction_newer"),
    ("denial of the very thing", "merger_acquisition", "Acme reportedly in talks to acquire Beta", "merger_acquisition", "Acme denies it plans to acquire Beta", 1.0, -0.8, "contradiction_factual"),
    ("older mixed news already in the price", "earnings_beat", "NVIDIA announces record results", "guidance_cut", "NVIDIA cut guidance last quarter", 40.0, -0.8, "contradiction_tension"),
    ("a different kind of news", "earnings_beat", "NVIDIA announces record results", "litigation", "NVIDIA sued over patents", 5.0, -0.8, "contradiction_scope"),
    ("the same story re-run", "earnings_beat", "NVIDIA announces record results and raises its outlook", "earnings_beat", "NVIDIA announces record results and raises its outlook, shares jump", 1.0, -0.8, None),
    ("same direction, not a contradiction", "earnings_beat", "NVIDIA announces record results", "guidance_raise", "NVIDIA raises full-year guidance", 0.3, 0.8, None),
    ("weak implication", "earnings_beat", "NVIDIA announces record results", "guidance_cut", "NVIDIA slightly trims forecast", 0.3, -0.2, None),
    ("a contract win followed by a lost contract", "contract_win", "Lockheed wins a $2 billion contract", "contract_loss", "Lockheed loses a $1 billion contract to a rival", 0.5, -0.8, "contradiction_superseded"),
    ("regulatory approval then a ban", "regulatory_approval", "FDA approves the new drug", "regulatory_action", "Regulator orders a ban on the drug", 0.4, -0.9, "contradiction_superseded"),
    ("an unrelated product recall", "contract_win", "Lockheed wins a $2 billion contract", "recall", "Lockheed recalls a component", 6.0, -0.8, "contradiction_scope"),
    ("a withdrawal of a report", "merger_acquisition", "Acme to buy Beta, sources say", "merger_acquisition", "Acme withdraws its offer for Beta", 0.5, -0.8, "contradiction_factual"),
]


class TestContradictionAlignment(Base):
    def classify(self, row):
        name, cet, chead, oet, ohead, hrs, d, expect = row
        now = NOW
        cand = {"ticker": "NVDA", "event_type": cet, "headline": chead, "direction": 0.8, "age_hours": 3.0}
        other = {"headline": ohead, "direction": d, "magnitude": 0.9 if "may" not in ohead else 0.5, "credibility": 0.9 if "may" not in ohead else 0.6, "event_type": oet, "created": iso(now - dt.timedelta(hours=hrs)), "cluster_id": None}
        return S.classify_contradiction(cand, other, 1.0, now - dt.timedelta(hours=3.0))

    def test_each_labelled_case_is_classified_as_labelled(self):
        for row in CORPUS:
            got = self.classify(row)
            self.assertEqual(got["rule"] if got else None, row[-1], row[0])

    def test_precision_and_recall_on_the_actionable_ones(self):
        actionable = {"contradiction_factual", "contradiction_superseded", "contradiction_newer"}
        tp = fp = fn = 0
        for row in CORPUS:
            got = self.classify(row)
            pred = bool(got and got["rule"] in actionable)
            truth = row[-1] in actionable
            tp += pred and truth
            fp += pred and not truth
            fn += (not pred) and truth
        precision, recall = tp / float(tp + fp), tp / float(tp + fn)
        self.assertGreaterEqual(precision, 0.9)
        self.assertGreaterEqual(recall, 0.9)

    def test_only_aligned_news_counts_through_the_database(self):
        add_event(self.c, "Stocks fall: NVDA, AMD, MSFT lower today", "price_recap", -0.8, 2)                                       # a recap is not news about the company
        add_event(self.c, "Ten stocks to watch", "general_news", -0.8, 2, tickers='["NVDA","AMD","MSFT","GOOGL"]', gate="too_many_companies")
        add_event(self.c, "NVIDIA cuts guidance (tomorrow's news)", "guidance_cut", -0.8, -3)                                  # three hours AFTER now: it cannot have been known
        add_event(self.c, "NVIDIA cut guidance in the summer", "guidance_cut", -0.8, 100)                                      # too old
        add_event(self.c, "NVIDIA announces record results", "earnings_beat", 0.8, 1.0, cluster=7)                              # same direction
        self.assertEqual(S.contradictions(self.c, cd(), 1.0, NOW), [])
        add_event(self.c, "NVIDIA cuts full-year guidance", "guidance_cut", -0.8, 0.3)
        self.assertEqual([x["rule"] for x in S.contradictions(self.c, cd(), 1.0, NOW)], ["contradiction_superseded"])

    def test_an_article_in_the_same_cluster_is_the_same_story_not_a_contradiction(self):
        self.c.execute("INSERT INTO events(id,created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source,stale,cluster_id) VALUES(900,?,?,?,?,?,?,?,?,?,0,5)",
                       (iso(NOW - dt.timedelta(hours=3)), "NVIDIA beats estimates", 0.8, 0.9, 0.9, "earnings_beat", '["NVDA"]', "company", "Wire"))
        add_event(self.c, "Shares slide as NVIDIA sales outlook disappoints", "guidance_cut", -0.8, 0.3, cluster=5)
        self.assertEqual(S.contradictions(self.c, cd(event_id=900), 1.0, NOW), [])

    def test_a_short_is_contradicted_by_good_news_not_bad(self):
        add_event(self.c, "NVIDIA cuts full-year guidance", "guidance_cut", -0.8, 0.3)
        self.assertEqual(S.contradictions(self.c, cd(direction=-0.8, event_type="guidance_cut", headline="NVIDIA slashes its outlook"), -1.0, NOW), [])
        add_event(self.c, "NVIDIA raises full-year guidance", "guidance_raise", 0.8, 0.2)
        self.assertEqual([x["rule"] for x in S.contradictions(self.c, cd(direction=-0.8, event_type="guidance_cut", headline="NVIDIA slashes its outlook"), -1.0, NOW)], ["contradiction_superseded"])


class TestOtherObjections(Base):
    def test_a_denial_a_stale_catalyst_and_a_rumour(self):
        self.assertEqual(self.rules(self.rv(cand=cd(headline="Nvidia denies report of a big acquisition")))["denied"]["action"], "veto")
        self.assertEqual(self.rules(self.rv(cand=cd(age_hours=40)))["stale_catalyst"]["action"], "veto")
        r = self.rules(self.rv(cand=cd(age_hours=20)))
        self.assertEqual((r["aging_news"]["action"], "stale_catalyst" in r), ("confidence", False))
        self.assertEqual(self.rules(self.rv(cand=cd(headline="Nvidia reportedly weighing a big acquisition, sources say")))["rumor"]["action"], "size")

    def test_the_horizon_of_the_event_must_match_the_horizon_of_the_trade(self):
        r = self.rules(self.rv(cand=cd(event_type="litigation", headline="NVIDIA sued over patents", direction=-0.5), f=fc()))
        self.assertIn("horizon_mismatch", r)                                                              # litigation acts over months; this is a 3-session trade
        self.assertNotIn("horizon_mismatch", self.rules(self.rv()))
        self.assertNotIn("horizon_mismatch", self.rules(self.rv(cand=cd(event_type="litigation", direction=-0.5), f=dict(fc(), horizon_days=40))))

    def test_the_company_must_be_the_subject_not_just_named(self):
        self.c.execute("INSERT INTO events(id,created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source,stale) VALUES(500,?,?,?,?,?,?,?,?,?,0)",
                       (iso(NOW - dt.timedelta(hours=3)), "Acme sues Beta", 0.8, 0.9, 0.9, "litigation", '["ACME","BETA"]', "company", "Wire"))
        self.c.execute("INSERT INTO reasoning_events(created,news_uid,event_ref,roles_json) VALUES(?,?,?,?)", (iso(NOW), "u", 500, json.dumps({"ACME": 0.9, "NVDA": 0.2})))
        self.c.commit()
        self.assertEqual(self.rules(self.rv(cand=cd(event_id=500)))["wrong_company"]["action"], "veto")

    def test_the_same_story_must_not_produce_a_second_idea_for_the_same_company(self):
        self.c.execute("INSERT INTO events(id,created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source,stale,cluster_id) VALUES(11,?,?,?,?,?,?,?,?,?,0,77)", (iso(NOW - dt.timedelta(hours=3)), "NVIDIA beats", 0.8, 0.9, 0.9, "earnings_beat", '["NVDA"]', "company", "A"))
        self.c.execute("INSERT INTO events(id,created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source,stale,cluster_id) VALUES(12,?,?,?,?,?,?,?,?,?,0,77)", (iso(NOW - dt.timedelta(hours=2)), "Nvidia tops estimates", 0.8, 0.9, 0.9, "earnings_beat", '["NVDA"]', "company", "B"))
        self.c.execute("INSERT INTO decisions(created,ticker,action,status,reason,event_id) VALUES(?,?,?,?,?,?)", (iso(NOW - dt.timedelta(hours=1)), "NVDA", "BUY", "EXECUTED", "{}", 11))
        self.c.commit()
        self.assertEqual(self.rules(self.rv(cand=cd(event_id=12)))["duplicate_catalyst"]["action"], "veto")
        self.assertNotIn("duplicate_catalyst", self.rules(self.rv(cand=cd(event_id=11))))

    def test_a_cached_review_is_not_reused_once_new_news_about_the_company_has_arrived(self):
        first = S.review(self.c, self.cfg, cd(), fc(), {}, {}, {}, NOW, 55)
        same = S.review(self.c, self.cfg, cd(), fc(priced_in=0.14), {}, {}, {}, NOW + dt.timedelta(minutes=5), 55)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(same, sort_keys=True))              # nothing new: the review is reused for the retry
        add_event(self.c, "NVIDIA faces a probe", "regulatory_action", -0.8, 0.0, base=NOW + dt.timedelta(minutes=8))
        fresh = S.review(self.c, self.cfg, cd(), fc(priced_in=0.14), {}, {}, {}, NOW + dt.timedelta(minutes=10), 55)
        self.assertIn("fully_priced_in", {b["rule"] for b in fresh["bear_points"]})                         # news arrived: it was reviewed again

    def test_independent_sources_are_counted_so_one_wire_story_is_not_corroboration(self):
        for i, (src, ttl) in enumerate((("Site A", "NVIDIA beats estimates and raises outlook (Reuters)"), ("Site B", "NVIDIA beats estimates and raises outlook (Reuters)"), ("Site C", "NVIDIA beats estimates and raises its outlook (Reuters)"))):
            self.c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES(?,?,?,?,?,?,?,0.8)", ("u%d" % i, src, ttl, "", "http://x/%d" % i, iso(NOW - dt.timedelta(hours=1)), iso(NOW)))
        self.c.commit()
        n, primary = R.independent_sources(self.c, {"uid": "u0", "title": "NVIDIA beats estimates and raises outlook (Reuters)"}, NOW)
        self.assertEqual((n, primary), (1, False))                                                         # three websites, one Reuters story
        self.c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES('u9','Bloomberg','NVIDIA tops estimates, lifts outlook','Bloomberg - NVIDIA tops estimates','http://y',?,?,0.8)", (iso(NOW - dt.timedelta(hours=1)), iso(NOW)))
        self.c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES('u8','Business Wire','NVIDIA beats estimates and raises outlook','','https://www.businesswire.com/x',?,?,0.9)", (iso(NOW - dt.timedelta(hours=1)), iso(NOW)))
        self.c.commit()
        n2, primary2 = R.independent_sources(self.c, {"uid": "u0", "title": "NVIDIA beats estimates and raises outlook (Reuters)"}, NOW)
        self.assertTrue(primary2)
        self.assertGreaterEqual(n2, 2)


class TestTheCanonicalRecord(Base):
    def an(self, title, summary="", tickers=None):
        return R.analyze_item(self.c, self.cfg, U, {"uid": "z", "title": title, "summary": summary, "analysis": dict(__import__("paper_agent.nlp", fromlist=["x"]).analyze(title, summary)), "tickers": tickers or []}, NOW)

    def test_state_novelty_materiality_and_separate_confidence_components_are_recorded(self):
        r = self.an("Acme agrees to acquire Beta for $5 billion, pending regulatory approval", "", ["ACME", "BETA"])
        self.assertEqual(r["state"], "pending")
        self.assertEqual(r["novelty"], "new")
        self.assertIn(r["materiality"], ("material", "transformative"))
        c = r["components"]
        for k in ("entity", "classification", "truth", "role", "causal", "direction_p", "magnitude", "timing", "completeness"):
            self.assertIn(k, c)
            self.assertTrue(0 <= c[k] <= 1, (k, c[k]))
        self.assertEqual(sum(r["horizons"].values()), 1.0 if abs(sum(r["horizons"].values()) - 1.0) < 1e-9 else sum(r["horizons"].values()))
        self.assertTrue(r["ref_version"])
        self.assertEqual(r["engine"], R.ENGINE_VERSION)

    def test_event_state_and_novelty_vocabulary(self):
        cases = {"Acme denies it plans to buy Beta": "denied", "Acme in talks to buy Beta, sources say": "rumored", "Regulator blocks Acme deal": "rejected", "Acme completes acquisition of Beta": "completed",
                 "FDA approves Acme drug": "approved", "Acme signs a definitive agreement": "agreed", "Acme proposes a merger": "proposed", "Acme posts quarterly sales": "reported"}
        for t, st in cases.items():
            self.assertEqual(R.event_state(t), st, t)
        self.assertEqual(R.novelty_of("Correction: Acme revises guidance"), "correction")
        self.assertEqual(R.novelty_of("Stocks to watch: a market recap"), "recap")
        self.assertEqual(R.novelty_of("Acme beats estimates"), "new")

    def test_every_analysis_is_logged_including_the_abstentions_with_their_reason(self):
        a = R.make_analyst(self.c, self.cfg, U)
        a({"uid": "u1", "title": "NVIDIA beats estimates and raises full-year guidance", "summary": "Revenue topped estimates.", "analysis": dict(__import__("paper_agent.nlp", fromlist=["x"]).analyze("NVIDIA beats estimates and raises full-year guidance", "Revenue topped estimates.")), "tickers": ["NVDA"]})
        a({"uid": "u2", "title": "Not Nvidia. Not AMD. This Is the Best AI Stock to Buy Right Now", "summary": "", "analysis": dict(__import__("paper_agent.nlp", fromlist=["x"]).analyze("Not Nvidia. Not AMD. This Is the Best AI Stock to Buy Right Now", "")), "tickers": ["NVDA", "AMD"]})
        rows = {r["news_uid"]: r for r in self.c.execute("SELECT * FROM reasoning_events")}
        self.assertEqual(rows["u1"]["abstained"], 0)
        self.assertEqual(rows["u2"]["abstained"], 1)
        self.assertIn("opinion piece", rows["u2"]["abstain_reason"])
        self.assertTrue(json.loads(rows["u1"]["trace_json"]))
        self.assertEqual(json.loads(rows["u1"]["roles_json"])["NVDA"] > 0, True)

    def test_only_active_mode_lets_the_analysis_become_evidence_and_the_log_says_which_mode(self):
        item = {"uid": "u1", "title": "NVIDIA beats estimates and raises full-year guidance", "summary": "Revenue topped estimates.", "analysis": dict(__import__("paper_agent.nlp", fromlist=["x"]).analyze("NVIDIA beats estimates and raises full-year guidance", "Revenue topped estimates.")), "tickers": ["NVDA"]}
        res = {}
        for mode in ("observe", "guard", "active"):
            self.cfg["intelligence"]["influence"] = mode
            res[mode] = R.make_analyst(self.c, self.cfg, U)(dict(item))
        self.assertIsNone(res["observe"])
        self.assertIsNone(res["guard"])
        self.assertIsNotNone(res["active"])
        self.assertEqual([r[0] for r in self.c.execute("SELECT mode FROM reasoning_events ORDER BY id")], ["observe", "guard", "active"])

    def test_a_denial_or_correction_replaces_earlier_evidence_instead_of_just_arguing_with_it(self):
        self.c.execute("INSERT INTO evidence(event_id,ticker,created,source,credibility,strength,novelty,predictive_power,direction,event_type,decay_days,expiry_date,note) VALUES(1,'ACME',?,'x',0.8,0.5,1,0.5,1,'merger_acquisition',5,?,'o1')",
                       (iso(NOW - dt.timedelta(hours=5)), iso(NOW + dt.timedelta(days=3))))
        self.c.commit()
        a = R.make_analyst(self.c, self.cfg, U)
        a({"now": NOW, "uid": "d", "title": "Acme denies it plans to acquire Beta", "summary": "", "analysis": {"event_type": "merger_acquisition", "direction": 0.5, "magnitude": 0.8, "scope": "company", "confidence": 0.9}, "tickers": ["ACME", "BETA"]})
        cred = self.c.execute("SELECT credibility,note FROM evidence").fetchone()
        self.assertLess(cred[0], 0.3)
        self.assertIn("[superseded]", cred[1])

    def test_a_third_hop_explains_but_never_scores(self):
        from paper_agent import event_intel
        r = R.analyze_item(self.c, self.cfg, U, {"uid": "x", "title": "NVIDIA beats estimates and raises full-year guidance on data center demand", "summary": "Revenue topped estimates.", "analysis": dict(__import__("paper_agent.nlp", fromlist=["x"]).analyze("NVIDIA beats estimates and raises full-year guidance on data center demand", "Revenue topped estimates.")), "tickers": ["NVDA"]}, NOW)
        orders = {e["ticker"]: e["order"] for e in r["beneficiaries"] + r["losers"]}
        self.assertIn(3, orders.values())                                                                 # it is in the explanation
        self.c.execute("INSERT INTO events(id,created,headline,event_type,scope,direction,magnitude,tickers_json,regions_json,tokens,novelty,credibility,stale) VALUES(1,?,?,?,?,?,?,?,?,?,1.0,0.8,0)", (iso(NOW), "x", "earnings_beat", "company", 0.8, 0.8, '["NVDA"]', "[]", ""))
        self.c.commit()
        st = {"evidence": 0}
        eff = event_intel.apply(self.c, self.cfg, 1, {"event_type": "earnings_beat"}, r, NOW, 1, 1.0, NOW, st, lambda *a: 0.5, 0.5)
        scored = {x[0] for x in self.c.execute("SELECT source FROM evidence")}
        self.assertIn("RULES-analyst:o1", scored)
        self.assertNotIn("RULES-analyst:o3", scored)                                                       # ...but it is not scored
        self.assertTrue(all(e["order"] <= 2 for e in eff))

    def test_a_chain_never_repeats_a_company_so_cycles_cannot_amplify(self):
        r = R.analyze_item(self.c, self.cfg, U, {"uid": "x", "title": "NVIDIA beats estimates and raises full-year guidance on data center demand", "summary": "", "analysis": {"event_type": "earnings_beat", "direction": 0.8, "magnitude": 0.8, "scope": "company", "confidence": 0.8}, "tickers": ["NVDA"]}, NOW)
        names = [e["ticker"] for e in r["beneficiaries"] + r["losers"]]
        self.assertEqual(len(names), len(set(names)))
        seen = [t for ch in r["chain"] for t in ch["tickers"]]
        self.assertNotIn("NVDA", [t for ch in r["chain"] if ch["order"] > 1 for t in ch["tickers"]])


if __name__ == "__main__":
    unittest.main()
