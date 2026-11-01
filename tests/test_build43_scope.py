"""Build 43: per-metric freshness (point 5), fundamentals that must be relevant to the catalyst and the holding period (point 6), and denials that negate only their own proposition and company (point 3)."""
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_accuracy as TA  # noqa: E402
from test_build42 import NOW  # noqa: E402
from test_build42b import ins  # noqa: E402
from test_facts_ttm import calendar_company, now as nowdt, r  # noqa: E402
from paper_agent import facts as F, nlp, reasoning as R, skeptic as S  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402


# ------------------------------------------------------------------------------------------------ point 5: every figure says how fresh it is
class TestEveryMetricCarriesItsFreshness(unittest.TestCase):
    def facts(self):
        ni = [r("2025-01-01", "2025-12-31", -20, "2026-02-10", "10-K"), r("2026-01-01", "2026-06-30", -8, "2026-08-01"), r("2025-01-01", "2025-06-30", -12, "2026-08-01")]
        cash = [r(None, "2026-06-30", 90, "2026-08-01")]
        cfo = [r("2025-01-01", "2025-12-31", 30, "2026-02-10", "10-K"), r("2026-01-01", "2026-06-30", 14, "2026-08-01")]                          # no prior-year six months: cannot be built
        self.now = nowdt("2026-08-15")
        return F.summarize({"revenue": calendar_company(), "net_income": ni, "cash": cash, "cfo": cfo}, self.now)

    def test_each_figure_has_its_period_its_filing_date_its_retrieval_time_and_age(self):
        f = self.facts()
        m = f["metrics"]["revenue_ttm"]
        self.assertEqual((m["value"], m["period_end"], m["filed"], m["age_days"]), (435, "2026-06-30", "2026-08-01", 46))
        self.assertEqual(m["retrieved"], iso(self.now))
        self.assertEqual(f["metrics"]["cash"]["period_end"], "2026-06-30")
        self.assertEqual(f["metrics"]["net_income_ttm"]["value"], -20 + -8 - -12)

    def test_what_could_not_be_built_is_listed_with_the_reason_and_is_not_in_the_metrics(self):
        f = self.facts()
        self.assertNotIn("cfo_ttm", f["metrics"])
        self.assertIn("prior-year year-to-date", f["unavailable"]["cfo_ttm"])
        self.assertNotIn("cfo_ttm", f)

    def test_a_warning_carries_the_period_and_filing_of_the_numbers_behind_it(self):
        f = self.facts()
        loss = [x for x in f["flags"] if x["flag"] == "loss_making"][0]
        self.assertEqual((loss["period_end"], loss["filed"], loss["metrics"]), ("2026-06-30", "2026-08-01", ["net_income_ttm"]))

    def test_the_valuation_carries_both_timestamps_and_refuses_a_stale_or_untimed_market_value(self):
        f = self.facts()
        f["metrics"]["net_income_ttm"]["value"] = 1e8
        f["metrics"]["revenue_ttm"]["value"] = 1e9
        f["revenue_growth"] = 0.1
        n = nowdt("2026-08-15")
        v = F.valuation(f, 2e11, iso(n - dt.timedelta(days=2)), n)
        self.assertEqual(v[0], "rich")
        d = v[2]
        self.assertEqual((d["revenue_period_end"], d["revenue_filed"], d["market_value_age_days"]), ("2026-06-30", "2026-08-01", 2.0))
        self.assertIn("market_value_as_of", d)
        self.assertIsNone(F.valuation(f, 2e11, iso(n - dt.timedelta(days=9)), n))                          # the market value is too old to compare with these results
        self.assertIsNone(F.valuation(f, 2e11, None, n))                                                   # no timestamp: not used
        self.assertIsNone(F.valuation(dict(f, stale=True), 2e11, iso(n), n))

    def test_the_ask_answer_shows_the_dates_and_what_is_unavailable(self):
        c, cfg = H.fresh_db(), H.cfg()
        f = self.facts()
        ins(c, "companies", ticker="NVDA", name="NVIDIA Corp")
        c.execute("INSERT INTO company_facts(ticker,facts_json,updated) VALUES('NVDA',?,?)", (json.dumps(f, default=str), iso(NOW)))
        c.commit()
        txt = R.ask(c, cfg, "why NVDA", None, NOW)["text"]
        self.assertIn("period ended 2026-06-30", txt)
        self.assertIn("filed 2026-08-01", txt)
        self.assertIn("retrieved 2026-08-15", txt)
        self.assertIn("UNAVAILABLE cfo ttm", txt)


# ------------------------------------------------------------------------------------------------ point 6: fundamentals must be relevant, and never a reason to block a short-term trade
def fl(name, sev, period_end="2026-06-30"):
    return {"flag": name, "text": name.replace("_", " "), "severity": sev, "period_end": period_end, "filed": "2026-08-01", "metrics": []}


class TestFundamentalsAreRelevantAndCapped(unittest.TestCase):
    def review(self, flags, event_type, horizon, side="BUY", comp=None, stale=False):
        c, cfg = H.fresh_db(), H.cfg()
        c.execute("INSERT INTO company_facts(ticker,facts_json,updated) VALUES('NVDA',?,?)", (json.dumps({"stale": stale, "retrieved": iso(NOW), "metrics": {}, "flags": flags}), iso(NOW)))
        c.commit()
        cand = {"ticker": "NVDA", "event_type": event_type, "direction": 0.8 if side == "BUY" else -0.8, "credibility": 0.8, "headline": "NVIDIA announces news", "age_hours": 2.0}
        f = {"verdict": side, "inputs": {}, "stop_return": -0.04, "ev_net": 0.03, "confidence": 0.7, "horizon_days": horizon, "scenarios": {}}
        rv = S.review(c, cfg, cand, f, comp or {}, {}, {}, NOW)
        return rv, cfg

    def fund(self, rv):
        return [b for b in rv["bear_points"] if b["rule"] == "fundamentals"]

    WEAK = [fl("loss_making", .3), fl("dilution", .44), fl("low_liquidity", .45), fl("high_leverage", .35), fl("shrinking_revenue", .35)]

    def test_a_short_trade_on_an_unrelated_catalyst_ignores_slow_moving_weaknesses(self):
        rv, cfg = self.review(self.WEAK, "contract_win", 3)
        self.assertEqual(self.fund(rv), [])
        g = S.gate(rv, cfg, "guard")
        self.assertTrue(g["ok"])
        self.assertNotIn("fundamentals", " ".join(g["reasons"]))                                            # (other, unrelated objections such as the event's time profile may still apply)

    def test_the_same_weaknesses_matter_to_an_earnings_catalyst_but_only_the_three_worst_and_only_as_size(self):
        rv, cfg = self.review(self.WEAK, "earnings_beat", 3)
        rel = {b["point"] for b in self.fund(rv)}
        self.assertEqual(len(self.fund(rv)), 3)
        self.assertTrue(all("liquidity" not in p and "leverage" not in p for p in rel))                     # balance-sheet stress is not relevant to a two-day earnings trade
        for mode in ("guard", "active"):
            g = S.gate(rv, cfg, mode)
            self.assertTrue(g["ok"], mode)
            self.assertGreaterEqual(g["size_mult"], 0.85)
        self.assertFalse(rv["fatal_flaw"])

    def test_with_five_relevant_weaknesses_only_the_three_worst_are_raised(self):
        flags = [fl("loss_making", .30), fl("dilution", .44), fl("shrinking_revenue", .35), fl("margin_squeeze", .30), fl("earnings_quality", .35)]
        rv, cfg = self.review(flags, "earnings_beat", 3)
        points = {b["point"] for b in self.fund(rv)}
        self.assertEqual(len(points), 3)
        self.assertTrue(any("dilution" in p for p in points))
        self.assertFalse(any("loss making" in p or "margin squeeze" in p for p in points))                  # the two mildest are left out

    def test_the_holding_period_decides_whether_a_weakness_matters_to_an_unrelated_catalyst(self):
        short, _ = self.review([fl("loss_making", .3)], "product_launch", 5)
        long_, _ = self.review([fl("loss_making", .3)], "product_launch", 15)
        self.assertEqual((len(self.fund(short)), len(self.fund(long_))), (0, 1))

    def test_only_a_financing_or_solvency_catalyst_with_severe_numbers_can_block_and_only_in_active_mode(self):
        flags = [fl("burning_cash", .4), fl("low_liquidity", .45)]
        rv, cfg = self.review(flags, "capital_raise", 5)
        self.assertEqual([b["rule"] for b in rv["bear_points"] if b["action"] == "veto"], ["fundamentals_direct"])
        self.assertTrue(S.gate(rv, cfg, "guard")["ok"])                                                     # guard never blocks on fundamentals
        g = S.gate(rv, cfg, "active")
        self.assertFalse(g["ok"])
        self.assertIn("fundamentals direct", " ".join(g["reasons"]))
        rv2, cfg = self.review(flags, "earnings_beat", 5)                                                  # the same numbers on an ordinary catalyst: no veto in any mode
        self.assertTrue(all(S.gate(rv2, cfg, m)["ok"] for m in ("guard", "active")))

    def test_the_total_effect_of_weak_fundamentals_is_capped(self):
        rv = {"bear_points": [{"point": "x", "severity": 0.4, "evidence": "", "rule": "fundamentals", "action": "size", "mult": 0.95}] * 8, "net_conviction": 0.9, "fatal_flaw": False}
        g = S.gate(rv, H.cfg(), "guard")
        self.assertEqual(g["size_mult"], 0.85)
        self.assertIn("capped", " ".join(g["reasons"]))

    def test_a_short_is_only_questioned_by_strength_that_bears_on_its_catalyst(self):
        strong = [fl("fast_growth", 0.0), fl("strong_balance_sheet", 0.0)]
        rv, _ = self.review(strong, "guidance_cut", 3, side="SHORT")
        self.assertEqual(len(self.fund(rv)), 2)                                                            # a short on a results catalyst against a growing, cash-rich company
        rv, _ = self.review(strong, "contract_loss", 3, side="SHORT")
        self.assertEqual(self.fund(rv), [])

    def test_old_or_stale_numbers_are_never_used(self):
        rv, _ = self.review([fl("dilution", .44, period_end="2025-01-01")], "earnings_beat", 10)
        self.assertEqual(self.fund(rv), [])                                                                # the period ended long before the 14-month limit
        rv, _ = self.review(self.WEAK, "earnings_beat", 10, stale=True)
        self.assertEqual(self.fund(rv), [])

    def test_each_objection_says_how_fresh_its_numbers_are(self):
        rv, _ = self.review([fl("dilution", .44)], "earnings_beat", 3)
        ev = self.fund(rv)[0]["evidence"]
        for w in ("period ended 2026-06-30", "filed 2026-08-01", "retrieved"):
            self.assertIn(w, ev)

    def test_a_valuation_comment_carries_the_market_value_timestamp_and_is_information_only(self):
        f = {"stale": False, "retrieved": iso(NOW), "revenue_growth": 0.1, "flags": [], "metrics": {"revenue_ttm": {"value": 1e9, "period_end": "2026-06-30", "filed": "2026-08-01"}, "net_income_ttm": {"value": 1e8, "period_end": "2026-06-30", "filed": "2026-08-01"}}}
        c, cfg = H.fresh_db(), H.cfg()
        c.execute("INSERT INTO company_facts(ticker,facts_json,updated) VALUES('NVDA',?,?)", (json.dumps(f), iso(NOW)))
        c.commit()
        cand = {"ticker": "NVDA", "event_type": "earnings_beat", "direction": 0.8, "credibility": 0.8, "headline": "x", "age_hours": 2.0}
        fc = {"verdict": "BUY", "inputs": {}, "stop_return": -0.04, "ev_net": 0.03, "confidence": 0.7, "horizon_days": 3, "scenarios": {}}
        rv = S.review(c, cfg, cand, fc, {"market_cap": 2e11, "updated": iso(NOW - dt.timedelta(days=1))}, {}, {}, NOW)
        self.assertEqual(rv["valuation_view"], "rich")
        v = [b for b in rv["bear_points"] if b["rule"] == "valuation"][0]
        self.assertEqual(v["action"], "info")
        self.assertIn("market value as of", v["evidence"])
        self.assertIn("filed 2026-08-01", v["evidence"])
        old = S.review(c, cfg, cand, fc, {"market_cap": 2e11, "updated": iso(NOW - dt.timedelta(days=9))}, {}, {}, NOW)
        self.assertEqual(old["valuation_view"], "unknown")                                                 # a nine-day-old market value is not compared with these results


# ------------------------------------------------------------------------------------------------ point 3: a denial negates ONE proposition about SPECIFIC companies at a SPECIFIC time
class TestDenialScope(unittest.TestCase):
    def scope(self, text, tickers=("ACME", "BETA"), etype="merger_acquisition"):
        return R.denial_scope(text, list(tickers), TA.U, etype)

    def test_the_central_event_is_denied(self):
        for t in ("Acme denies report it plans to acquire Beta", "Acme denies the report, saying it has no plans to buy Beta"):
            d = self.scope(t)
            self.assertTrue(d["applies"], t)
            self.assertEqual((d["kind"], sorted(d["tickers"])), ("central", ["ACME", "BETA"]))

    def test_a_minor_detail_is_denied_but_the_event_stands(self):
        d = self.scope("Acme confirms talks to acquire Beta but denies the reported $5 billion price")
        self.assertEqual((d["applies"], d["kind"]), (False, "detail"))
        d = self.scope("Acme confirms the deal but disputes the report about its timing")
        self.assertFalse(d["applies"])

    def test_a_different_proposition_is_not_the_event(self):
        d = self.scope("Acme denies wrongdoing as regulators fine it $1 billion", ("ACME",), "regulatory_action")
        self.assertEqual((d["applies"], d["kind"]), (False, "other"))

    def test_a_past_or_quoted_historical_denial_does_not_apply_to_todays_news(self):
        for t in ("Last year Acme denied any wrongdoing; today regulators fined Acme $1 billion", "Acme, which denied the allegations in 2022, now faces a new probe", "Acme previously denied the claims, regulators said on Tuesday it would be fined"):
            d = self.scope(t, ("ACME",), "regulatory_action")
            self.assertFalse(d["applies"], t)
            self.assertEqual(d["kind"], "historical", t)

    def test_a_denial_by_one_company_does_not_deny_the_claimant(self):
        d = self.scope("Beta denies Acme's claim that Beta breached the supply contract", etype="litigation")
        self.assertEqual((d["applies"], d["tickers"]), (True, ["BETA"]))
        d = self.scope("Acme sues Beta; Beta denies the allegations", etype="litigation")
        self.assertEqual((d["applies"], d["tickers"]), (True, ["BETA"]))
        d = self.scope("Beta denies it plans to acquire Gamma", ("ACME", "BETA"))                           # a denial about companies that are not the story's
        self.assertEqual(d["tickers"], ["BETA"])

    def test_a_denial_followed_by_a_confirmation_is_superseded_but_not_the_other_way_round(self):
        d = self.scope("Acme denied the report on Monday but confirmed on Wednesday that it is in talks to acquire Beta")
        self.assertEqual((d["applies"], d["kind"]), (False, "superseded"))
        d = self.scope("Acme confirmed it was in talks to buy Beta, then denied it on Friday")
        self.assertEqual((d["applies"], d["kind"]), (True, "central"))                                      # the LATER statement wins

    def test_a_judge_dismissing_an_unrelated_claim_is_not_the_company_denying_anything(self):
        self.assertEqual(self.scope("Toblerone owner sues Acme over chunky shape. A judge earlier dismissed an unrelated claim.", ("ACME",), "litigation")["kind"], "none")

    def analyse(self, title, tickers, summary=""):
        c, cfg = H.fresh_db(), H.cfg()
        cfg["intelligence"]["influence"] = "active"
        item = {"uid": "x", "title": title, "summary": summary, "analysis": dict(nlp.analyze(title, summary)), "tickers": list(tickers)}
        return R.analyze_item(c, cfg, TA.U, item, TA.NOW), item

    def test_in_the_analyst_only_the_denied_companies_are_flipped_and_the_state_follows_the_scope(self):
        r_, item = self.analyse("Acme denies report it plans to acquire Beta", ["ACME", "BETA"])
        state = r_["state"] if r_ else item["_state"]
        self.assertEqual(state, "denied")
        r_, _ = self.analyse("Acme confirms talks to acquire Beta but denies the reported $5 billion price", ["ACME", "BETA"])
        self.assertNotEqual(r_["state"], "denied")
        self.assertIn("BETA", [e["ticker"] for e in r_["beneficiaries"] if e["order"] == 1])                # the target still gains: the event stands
        r_, _ = self.analyse("Acme denies wrongdoing as regulators fine it $1 billion", ["ACME"])
        self.assertNotEqual(r_["state"], "denied")
        self.assertIn("ACME", [e["ticker"] for e in r_["losers"]])
        r_, _ = self.analyse("Last year Acme denied any wrongdoing; today regulators fined Acme $1 billion", ["ACME"])
        self.assertIn("ACME", [e["ticker"] for e in r_["losers"]])
        self.assertEqual(r_["denial"]["kind"], "historical")
        r_, _ = self.analyse("Acme denied the report on Monday but confirmed on Wednesday that it is in talks to acquire Beta", ["ACME", "BETA"])
        self.assertEqual(r_["state"], "proposed")                                                           # the company itself confirmed the talks: a proposal, not a rumour and not a denial
        self.assertGreaterEqual(r_["certainty"], 0.55)                                                     # certainty comes from the confirmation, not from the earlier denial
        self.assertIn("BETA", [e["ticker"] for e in r_["beneficiaries"] if e["order"] == 1])

    def test_a_cross_company_denial_flips_only_the_denying_company(self):
        base, _ = self.analyse("Acme sues Beta over patent infringement", ["ACME", "BETA"])
        den, item = self.analyse("Acme sues Beta over patent infringement; Beta denies the allegations", ["ACME", "BETA"])
        if den is None:
            self.assertEqual(item["_denial"]["tickers"], ["BETA"])
        else:
            self.assertEqual(den["denial"]["tickers"], ["BETA"])
            acme_base = [e for e in base["beneficiaries"] + base["losers"] if e["ticker"] == "ACME" and e["order"] == 1]
            acme_den = [e for e in den["beneficiaries"] + den["losers"] if e["ticker"] == "ACME" and e["order"] == 1]
            self.assertEqual([e in base["beneficiaries"] for e in acme_base], [e in den["beneficiaries"] for e in acme_den])        # the claimant's side is unchanged


class TestTheRedTeamUsesTheSameScope(unittest.TestCase):
    def setUp(self):
        self.c, self.cfg = H.fresh_db(), H.cfg()
        for t, n in (("ACME", "Acme Corp"), ("BETA", "Beta Industries")):
            ins(self.c, "companies", ticker=t, name=n)

    def rules(self, ticker, headline, event_type="litigation", event_id=None):
        cand = {"ticker": ticker, "event_type": event_type, "direction": 0.8, "credibility": 0.8, "headline": headline, "age_hours": 2.0, "event_id": event_id}
        f = {"verdict": "BUY", "inputs": {}, "stop_return": -0.04, "ev_net": 0.03, "confidence": 0.7, "horizon_days": 3, "scenarios": {}}
        return {b["rule"]: b for b in S.review(self.c, self.cfg, cand, f, {}, {}, {}, NOW, persist=False)["bear_points"]}

    def test_from_the_headline_alone_the_denial_belongs_to_the_denying_company_only(self):
        h = "Beta denies Acme's claim that Beta breached the supply contract"
        self.assertEqual(self.rules("BETA", h)["denied"]["action"], "veto")
        self.assertNotIn("denied", self.rules("ACME", h))                                                  # the claimant's idea is not undermined by the other side's denial
        self.assertNotIn("denied", self.rules("ACME", "Acme confirms the deal but denies the reported price", "merger_acquisition"))
        self.assertNotIn("denied", self.rules("ACME", "Last year Acme denied any wrongdoing; today regulators fined Acme $1 billion", "regulatory_action"))

    def test_the_analysts_own_logged_scope_is_used_when_it_exists(self):
        ins(self.c, "events", id=5, created=iso(NOW), headline="x", direction=0.8, magnitude=0.8, credibility=0.9, event_type="litigation", tickers_json='["ACME","BETA"]', scope="company", source="W", stale=0)
        self.c.execute("INSERT INTO reasoning_events(created,news_uid,event_ref,denied_json) VALUES(?,?,?,?)", (iso(NOW), "u", 5, json.dumps({"applies": True, "kind": "central", "tickers": ["BETA"], "why": "denied", "span": "Beta denies it"})))
        self.c.commit()
        self.assertIn("denied", self.rules("BETA", "headline", event_id=5))
        self.assertNotIn("denied", self.rules("ACME", "headline", event_id=5))


if __name__ == "__main__":
    unittest.main()
