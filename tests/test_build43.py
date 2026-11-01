"""Build 43: every question and every tab on a genuinely READ-ONLY connection (the dashboard's), plus the fixes found in the bug hunt."""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from test_build42 import SEED, NOW  # noqa: E402
from test_build42b import ins  # noqa: E402
from paper_agent import dashboard, db, graph, reasoning as R  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

QUESTIONS = ["why NVDA", "why AMD", "news NVDA", "related NVDA", "what if TSM cuts guidance", "what if NVDA beats earnings", "compare NVDA AMD", "theme ai infrastructure", "what could hurt us", "today", "help", "gibberish ???", "", "why", "why ZZZZ"]


class TestNothingTheDashboardDoesEverWrites(unittest.TestCase):
    """The dashboard opens the database read-only. A question or a page that tries to write fails with 'attempt to write a readonly database' (the bug found by the owner on 'why NVDA')."""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        rw = db.open_db(self.path)
        self.cfg = H.cfg()
        graph.seed(rw, NOW, SEED)
        f = ins(rw, "forecasts", created=iso(NOW), ticker="NVDA", direction="LONG", horizon_days=5, p_up=0.6, ev_net=0.02, confidence=0.7, stop_return=-0.04, target_return=0.05, verdict="BUY",
                scenarios_json=json.dumps({"bull": {"ret": 0.06, "p": 0.3}, "base": {"ret": 0.02, "p": 0.4}, "bear": {"ret": -0.03, "p": 0.3}}), inputs_json=json.dumps({"priced_in": 0.06, "vol_daily": 0.02}), hash="h")
        ins(rw, "decisions", created=iso(NOW), ticker="NVDA", action="BUY", status="EXECUTED", reason="{}", forecast_id=f)
        ins(rw, "events", created=iso(NOW - dt.timedelta(hours=2)), headline="NVIDIA beats estimates", direction=0.8, magnitude=0.8, credibility=0.9, event_type="earnings_beat", tickers_json='["NVDA"]', scope="company", source="W", stale=0)
        rw.execute("INSERT INTO company_facts(ticker,facts_json,updated) VALUES('NVDA',?,?)", (json.dumps({"flags": [{"flag": "dilution", "text": "share count up 12%", "severity": 0.4}], "revenue_ttm": 5e10, "basis": {"revenue": "latest annual"}, "asof": "2026-06-30"}), iso(NOW)))
        rw.commit()
        rw.close()
        self.c = db.connect(self.path, readonly=True)

    def tearDown(self):
        self.c.close()

    def test_the_connection_really_is_read_only(self):
        with self.assertRaises(Exception):
            self.c.execute("INSERT INTO redteam_reviews(created,ticker) VALUES('x','y')")

    def test_every_kind_of_question_answers_without_writing(self):
        for q in QUESTIONS:
            a = R.ask(self.c, self.cfg, q, None, NOW)                                                    # raises if anything tries to write
            self.assertIn("text", a, q)
        why = R.ask(self.c, self.cfg, "why NVDA", None, NOW)
        self.assertIn("the case against", [s["heading"] for s in why["sections"]])                       # and the case against is still shown

    def test_every_tab_and_the_ask_and_graph_panels_render_without_writing(self):
        for t in dashboard.TABS:
            for q in ({}, {"ask": ["why NVDA"], "graph": ["NVDA"]}, {"ask": ["what could hurt us"]}, {"ask": ["what if TSM cuts guidance"]}):
                h = dashboard.render(self.c, self.cfg, t, q, {"authed": True})
                self.assertTrue(len(h) > 100, (t, q))

    def test_a_question_leaves_the_audit_log_exactly_as_it_was(self):
        rw = db.connect(self.path)
        before = [rw.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("redteam_reviews", "reasoning_events", "decisions", "events", "relations")]
        for q in QUESTIONS:
            R.ask(rw, self.cfg, q, None, NOW)
        after = [rw.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("redteam_reviews", "reasoning_events", "decisions", "events", "relations")]
        self.assertEqual(before, after)


# ------------------------------------------------------------------------------------------------ the SEC facts reader (v2), on realistic data
from paper_agent import facts as F, net, pipeline, skeptic as S  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402


def row(start, end, val, filed="2026-08-01", form="10-Q"):
    d = {"end": end, "val": val, "filed": filed, "form": form}
    if start:
        d["start"] = start
    return d


def clean(rows):
    return F._points({"units": {"USD": rows}})


def revenue_rows():
    """A December year-end company as of 30 June 2026: FY2025 annual, Q1-Q3 2025, Q1-Q2 2026, and the cumulative six-month figures a 10-Q also carries."""
    return [row("2025-01-01", "2025-12-31", 400, "2026-02-10", "10-K"), row("2025-01-01", "2025-03-31", 90), row("2025-04-01", "2025-06-30", 95), row("2025-07-01", "2025-09-30", 100),
            row("2026-01-01", "2026-03-31", 105), row("2026-04-01", "2026-06-30", 115), row("2026-01-01", "2026-06-30", 220), row("2025-01-01", "2025-06-30", 185)]


class TestEntityGuessing(unittest.TestCase):
    def test_a_nationality_or_region_adjective_is_not_a_company(self):
        from paper_agent.entities import Universe
        u = Universe({"BTI": {"name": "British American Tobacco p.l.c.", "aliases": ["British American Tobacco Industries, p.l.c. Common Stock"], "cik": 1}, "NCLH": {"name": "Norwegian Cruise Line Holdings Ltd.", "aliases": ["Norwegian Cruise Line ."], "cik": 3},
                      "BROS": {"name": "Dutch Bros Inc.", "aliases": ["Dutch Bros ."], "cik": 4}, "ACME": {"name": "ACME CORP", "aliases": ["ACME"], "cik": 2}})        # SEC-style names, as in the real watchlist
        m = lambda t: [x[0] for x in u.match(t, "")]  # noqa: E731
        for stray in ("Toblerone owner sues British vegan chocolate brand", "Why this British plumber prefers working in Poland", "Finnish and Norwegian leaders hold a call", "Dutch zookeeper dies after being mauled by leopard"):
            self.assertEqual(m(stray), [], stray)                                                          # a nationality word alone is not a company
        for full, tk in (("British American Tobacco raises its dividend", "BTI"), ("Norwegian Cruise Line Holdings lifts its outlook", "NCLH"), ("Dutch Bros beats estimates", "BROS"), ("(NYSE: NCLH) shares surge", "NCLH")):
            self.assertIn(tk, m(full), full)                                                               # the company's own name still is



# ------------------------------------------------------------------------------------------------ the wording reader, grounding, and what is not an event
from paper_agent import nlp  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402
import test_accuracy as TA  # noqa: E402


class TestTheWordingReader(unittest.TestCase):
    def cues(self, text):
        p = R.polarity(text)
        return p["score"], {c[0].lower(): c[3] for c in p["cues"]}, p["adjusted"]

    def test_plain_good_and_bad_cues_have_the_right_sign_and_nothing_is_adjusted(self):
        s, _, adj = self.cues("Acme beats estimates and raises its full-year guidance")
        self.assertGreater(s, 0.5)
        self.assertFalse(adj)
        s, _, adj = self.cues("Acme misses estimates and cuts its outlook")
        self.assertLess(s, -0.5)
        self.assertEqual(R.polarity("Acme holds its annual meeting")["cues"], [])

    def test_a_negator_shrinks_and_reverses_a_cue(self):
        for t in ("Acme did not cut its dividend", "Acme has no plans to cut guidance", "Acme avoids bankruptcy", "Acme sidesteps a recall", "There is no sign Acme will miss targets"):
            s, c, adj = self.cues(t)
            self.assertGreaterEqual(s, -0.05, t)
            self.assertTrue(adj, t)
            self.assertTrue(any("negated" in v for v in c.values()), t)

    def test_the_negation_only_reaches_its_own_clause(self):
        s, c, _ = self.cues("Acme did not cut its dividend, but it missed estimates")
        self.assertLess(s, -0.3)                                                                         # the "not" belongs to the first clause; the miss is real

    def test_hedging_halves_a_cue_and_history_almost_removes_it(self):
        plain, hedged = self.cues("Acme cuts guidance")[0], self.cues("Acme might cut guidance")[0]
        self.assertLess(abs(hedged), abs(plain) * 0.7)
        hist = self.cues("A year ago Acme cut its dividend; it has since fully recovered")[0]
        self.assertGreater(hist, -0.15)
        self.assertLess(self.cues("Last quarter Acme was fine; today Acme cut its dividend")[0], -0.4)    # history in one clause does not excuse news in the next

    def test_several_cues_combine_without_just_adding_up(self):
        one = self.cues("Acme misses estimates")[0]
        many = self.cues("Acme misses estimates, cuts guidance, warns of losses and faces a probe")[0]
        self.assertLess(many, one)
        self.assertGreaterEqual(many, -1.0)


class TestTheWordingReaderExactly(unittest.TestCase):
    def test_several_cues_combine_as_the_strongest_plus_a_third_of_the_rest(self):
        two = R.polarity("Acme misses estimates after a recall")["score"]
        self.assertAlmostEqual(two, -0.6 + 0.3 * -0.55, places=2)                                          # not their sum (-1.15), which would overstate two weak signals

    def test_a_negated_denial_is_read_once(self):
        c, cfg = H.fresh_db(), H.cfg()
        cfg["intelligence"]["influence"] = "active"
        t = "Acme says it has no plans to cut guidance"
        r = R.analyze_item(c, cfg, TA.U, {"uid": "x", "title": t, "summary": "", "analysis": dict(nlp.analyze(t, "")), "tickers": ["ACME"]}, TA.NOW)
        self.assertTrue(r is None or r["direction_market"] >= 0)                                          # "no plans to cut" is mildly good news, not bad news flipped to something else
        if r:
            self.assertGreater(r["direction_market"], 0.1)

    def test_conditional_and_confirmed_wording_get_different_certainty(self):
        self.assertEqual(R.report_status("Acme might be forced to cut its dividend if sales keep slipping")[:2], ("hedged", 0.6))
        self.assertIn(R.report_status("Acme may raise guidance, analysts say")[0], ("hedged", "rumor"))                 # either way: not a confirmed result
        self.assertEqual(R.report_status("Acme announced it may raise guidance")[0], "confirmed")


class TestGrounding(unittest.TestCase):
    def setUp(self):
        self.u = Universe({"MSFT": {"name": "Microsoft Corp", "aliases": ["Microsoft"]}, "NDAQ": {"name": "Nasdaq Inc", "aliases": ["Nasdaq"]}, "MLM": {"name": "Martin Marietta Materials", "aliases": ["Martin Marietta"]}})

    def test_a_company_is_the_subject_only_as_firmly_as_the_text_says(self):
        g = lambda title, summ, t: R.grounding(title, summ, self.u, t)  # noqa: E731
        self.assertEqual(g("Microsoft posts record profit", "", "MSFT"), 1.0)
        self.assertEqual(g("Cloud profits surge", "Microsoft said revenue rose", "MSFT"), 0.7)
        self.assertEqual(g("Budget announces an energy payment", "households will receive money", "MLM"), 0.35)
        self.assertEqual(g("Acme buys a stake, $MSFT rises", "", "MSFT"), 1.0)                          # a cashtag is a name
        self.assertLess(g("Reindeer joins the Microsoft for Startups program", "", "MSFT"), 0.5)         # named only as the platform

    def test_the_exchange_a_company_is_listed_on_is_not_the_company_that_runs_it(self):
        self.assertLess(R.grounding("Acme reports record profit (NASDAQ: ACME)", "", self.u, "NDAQ"), 0.5)

    def test_a_poorly_grounded_company_gets_a_weak_effect_not_a_strong_one(self):
        c, cfg = H.fresh_db(), H.cfg()
        cfg["intelligence"]["influence"] = "active"
        t = "Healey's budget slashes the profit forecast for energy suppliers"
        r = R.analyze_item(c, cfg, TA.U, {"uid": "x", "title": t, "summary": "", "analysis": dict(nlp.analyze(t, "")), "tickers": ["MLM"]}, TA.NOW)
        self.assertIsNotNone(r)
        mlm = [e for e in r["beneficiaries"] + r["losers"] if e["ticker"] == "MLM" and e["order"] == 1]
        self.assertTrue(mlm)
        self.assertLess(mlm[0]["confidence"], 0.4)                                                         # the matcher guessed Martin Marietta; nothing in the text names it
        self.assertTrue(any("grounding" in x for x in r["trace"]))


class TestWhatIsNotAnEvent(unittest.TestCase):
    def setUp(self):
        self.c, self.cfg = H.fresh_db(), H.cfg()
        self.cfg["intelligence"]["influence"] = "active"

    def an(self, title, summ="", tk=("ACME",)):
        it = {"uid": "x", "title": title, "summary": summ, "analysis": dict(nlp.analyze(title, summ)), "tickers": list(tk)}
        return R.analyze_item(self.c, self.cfg, TA.U, it, TA.NOW), it

    def test_each_kind_of_non_event_abstains_and_says_why(self):
        cases = {"Why Is Acme Offering Buyouts To 60,000 Employees?": "question", "Acme Market Report 2026: a 10.7% CAGR to $63 billion": "market-research", "These 2 Stocks Are Most at Risk If AI Slows": "stock picks",
                 "Midday movers: Acme jumps, Beta slides": "recap", "Acme CEO is optimistic about the year ahead": "optimism", "Acme, Beta, Gamma, Delta and Epsilon shares rise as markets rally": "roundup"}
        for title, word in cases.items():
            r, it = self.an(title, "", ["ACME", "BETA", "GAMMA", "DELTA", "EPS"] if "Epsilon" in title else ["ACME"])
            self.assertIsNone(r, title)
            self.assertIn(word, it["_why_not"], title)

    def test_a_denial_in_the_headline_flips_a_rumour_but_one_in_the_body_does_not(self):
        body = self.an("Toblerone owner sues Acme over chunky shape", "A judge earlier dismissed an unrelated claim.", ["ACME"])[0]
        self.assertNotEqual(body["state"], "denied")
        self.assertIn("ACME", [e["ticker"] for e in body["losers"]])                                     # still bad news for Acme, not flipped to good
        head = self.an("Acme denies it plans to acquire Beta", "", ["ACME", "BETA"])[0]
        self.assertTrue(head is None or head["state"] == "denied")

    def test_an_expectation_is_not_a_result(self):
        r, _ = self.an("Analysts expect Acme to beat estimates next week")
        self.assertTrue(r is None or (r["state"] == "expected" or r["certainty"] <= 0.7))
        self.assertEqual(R.report_status("Analysts expect Acme to beat estimates")[0], "expected")
        self.assertNotEqual(R.report_status("Acme reported results that beat what analysts expect")[0], "expected")

    def test_a_faint_reading_is_no_direction_at_all(self):
        r, _ = self.an("A year ago Acme cut its dividend; it has since fully recovered")
        self.assertTrue(r is None or "ACME" not in [e["ticker"] for e in r["losers"]])

    def test_an_abstained_story_still_records_its_state(self):
        r, it = self.an("Acme explores a sale of its consumer unit, sources say")
        self.assertEqual(it.get("_state") if r is None else r["state"], "rumored")

    def test_the_weak_event_reader_does_not_outvote_a_clear_downgrade(self):
        r, _ = self.an("Bank of America downgrades Acme citing slowing growth")
        self.assertIn("ACME", [e["ticker"] for e in r["losers"]])
        self.assertTrue(any("trusting the wording" in t for t in r["trace"]))

    def test_a_bid_that_may_happen_still_has_an_acquirer_and_a_target(self):
        r, _ = self.an("Amazon could buy Viasat as a rival tries a bid, a consultant says", "", ["AMZN", "VSAT"])
        self.assertEqual(({e["ticker"] for e in r["beneficiaries"] if e["order"] == 1}, {e["ticker"] for e in r["losers"] if e["order"] == 1}), ({"VSAT"}, {"AMZN"}))

    def test_winning_a_case_against_someone_makes_them_the_loser(self):
        r, _ = self.an("Acme wins its appeal in the patent case against Beta", "", ["ACME", "BETA"])
        self.assertEqual(({e["ticker"] for e in r["beneficiaries"] if e["order"] == 1}, {e["ticker"] for e in r["losers"] if e["order"] == 1}), ({"ACME"}, {"BETA"}))


class TestTheRefinedRedTeam(unittest.TestCase):
    def setUp(self):
        self.c, self.cfg = H.fresh_db(), H.cfg()

    def cand(self, **kw):
        return dict({"ticker": "NVDA", "event_type": "earnings_beat", "direction": 0.8, "credibility": 0.8, "headline": "NVIDIA announces record results", "age_hours": 3.0}, **kw)

    def f(self):
        return {"verdict": "BUY", "inputs": {}, "stop_return": -0.04, "ev_net": 0.03, "confidence": 0.7, "horizon_days": 3, "scenarios": {}}

    def test_a_story_in_the_same_release_window_is_the_same_announcement_not_newer_news(self):
        for hrs, want in ((0.8, "contradiction_newer"), (0.3, "contradiction_tension")):
            c = H.fresh_db()
            c.execute("INSERT INTO events(created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source,stale) VALUES(?,?,?,?,?,?,?,?,?,0)",
                      (iso(NOW - dt.timedelta(hours=hrs)), "NVIDIA says it cut its guidance", -0.8, 0.5, 0.7, "guidance_cut", '["NVDA"]', "company", "W"))
            c.commit()
            r = S.contradictions(c, self.cand(age_hours=1.2 if hrs == 0.3 else 3.0), 1.0, NOW)
            self.assertEqual(r[0]["rule"], want, hrs)

    def test_several_stories_saying_the_same_opposite_thing_shrink_the_trade_once(self):
        c = H.fresh_db()
        for i, h in enumerate(("NVIDIA cuts its guidance", "Nvidia lowers its forecast, shares fall", "Nvidia trims outlook on weak demand")):
            c.execute("INSERT INTO events(created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source,stale) VALUES(?,?,?,?,?,?,?,?,?,0)", (iso(NOW - dt.timedelta(hours=1 + i * 0.3)), h, -0.8, 0.5, 0.7, "guidance_cut", '["NVDA"]', "company", "W"))
        c.commit()
        rv = S.review(c, self.cfg, self.cand(age_hours=6.0), self.f(), {"adv_usd": 5e9}, {}, {}, NOW)
        newer = [b for b in rv["bear_points"] if b["rule"] == "contradiction_newer"]
        self.assertEqual(len(newer), 3)                                                                     # all three are listed as evidence
        g = S.gate(rv, self.cfg, "guard")
        self.assertEqual(g["size_mult"], 0.75)                                                              # ...but they are one fact, so the trade is shrunk once, not 0.75 x 0.75 x 0.75

    def test_one_company_named_among_several_is_made_smaller_not_blocked(self):
        c = H.fresh_db()
        c.execute("INSERT INTO events(id,created,headline,direction,magnitude,credibility,event_type,tickers_json,scope,source,stale) VALUES(7,?,?,?,?,?,?,?,?,?,0)", (iso(NOW), "x", 0.8, 0.8, 0.9, "earnings_beat", '["NVDA"]', "company", "W"))
        c.execute("INSERT INTO reasoning_events(created,news_uid,event_ref,roles_json) VALUES(?,?,?,?)", (iso(NOW), "u", 7, json.dumps({"NVDA": 0.2, "AMD": 0.25})))
        c.commit()
        rv = S.review(c, self.cfg, self.cand(event_id=7), self.f(), {}, {}, {}, NOW)
        rules = {b["rule"]: b for b in rv["bear_points"]}
        self.assertEqual(rules["weak_subject"]["action"], "size")
        self.assertNotIn("wrong_company", rules)
        self.assertTrue(S.gate(rv, self.cfg, "guard")["ok"])

    def test_most_ordinary_ideas_pass_the_guard_at_full_size(self):
        rv = S.review(self.c, self.cfg, self.cand(), self.f(), {"adv_usd": 5e9}, {}, {}, NOW)
        g = S.gate(rv, self.cfg, "guard")
        self.assertTrue(g["ok"])
        self.assertEqual(g["size_mult"], 1.0)
        self.assertTrue(S.gate(rv, self.cfg, "active")["ok"])



# ------------------------------------------------------------------------------------------------ malformed data must never stop a cycle (found by fuzzing with thousands of hostile inputs)
from paper_agent import graph as G, pathways as PW  # noqa: E402
from test_build42b import PipeBase  # noqa: E402


class TestMalformedDataIsTolerated(unittest.TestCase):
    def test_a_daily_bar_with_holes_is_repaired_or_skipped_not_fatal(self):
        bars = [{"day": "d1", "open": None, "high": None, "low": None, "close": 102}, {"day": "d2", "open": 102, "high": 106, "low": 0, "close": 105}, {"day": "d3", "open": 1, "high": 2, "low": 1, "close": None}]
        m = PW.measure(100.0, 1.0, bars, 0.04, -0.03, 2)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(m["ret"], 0.05)
        self.assertAlmostEqual(m["mfe"], 0.06)                                                              # the high of day 2; day 1's missing high is its close
        self.assertIsNone(PW.measure(100.0, 1.0, bars, 0.04, -0.03, 3))                                     # the bar with no close is dropped, so there are only two usable sessions
        self.assertIsNone(PW.measure("x", 1.0, bars, None, None, 1))
        self.assertIsNone(PW.measure(100.0, 1.0, [], None, None, 1))

    def test_a_candidate_with_non_numeric_fields_is_ignored_by_the_graph(self):
        c, cfg = H.fresh_db(), H.cfg()
        cfg["graph"]["enabled"] = True
        G.seed(c, NOW, SEED)
        bad = [{"ticker": "NVDA", "direction": "x", "magnitude": None, "conf": "?", "event_type": "earnings_beat", "origin": "event"}, {"direction": 0.8}, {"ticker": "NVDA", "direction": 0.8, "magnitude": 0.8, "conf": 0.8, "credibility": 0.9, "event_type": "earnings_beat", "origin": "event", "headline": "x"}, "not a dict", None]
        out = G.derive(c, cfg, bad, None, NOW)
        self.assertTrue(out)                                                                                # the one good candidate still works
        self.assertTrue(all(d["graph_from"] == "NVDA" for d in out))

    def test_the_red_team_survives_malformed_numbers_and_has_nothing_to_say_without_a_company(self):
        c = H.fresh_db()
        cand = {"ticker": "NVDA", "headline": "NVIDIA beats", "age_hours": "x", "credibility": "?", "event_type": "earnings_beat", "direction": 0.8}
        f = {"verdict": "BUY", "inputs": {"priced_in": "x", "vol_daily": None}, "stop_return": "?", "ev_net": None, "confidence": "high", "horizon_days": "soon", "scenarios": None}
        rv = S.review(c, H.cfg(), cand, f, {"adv_usd": "lots"} and {}, {}, {}, NOW, persist=False)
        self.assertIsNotNone(rv)
        self.assertTrue(rv["bear_points"])
        self.assertIsNone(S.review(c, H.cfg(), {"headline": "no company"}, f, {}, {}, {}, NOW, persist=False))
        for mode in ("off", "observe", "guard", "active"):
            self.assertIn("ok", S.gate(rv, H.cfg(), mode))


class TestSecRefusalBacksOff(PipeBase):
    mode = "guard"

    def test_a_429_stops_the_stage_and_it_stays_quiet_for_an_hour_then_tries_again(self):
        ins(self.c, "positions", ticker="NVDA", theme="t", opened=iso(TL.T_PRE - dt.timedelta(days=1)), entry_price=100.0, shares=10, cost=1000.0, status="OPEN", sector="Tech", side="LONG")   # a held company is always due
        self.c.commit()
        calls = []

        def sec(url, headers=None, timeout=0, **k):
            if "data.sec.gov" in url:
                if "companyconcept" in url:
                    calls.append(url)                                                                         # (the filer lookup used by another stage is a different request)
                raise net.FetchError("too many requests", 429)
            raise net.FetchError("offline")
        self.mkt.now = TL.T_PRE
        deps = self.deps([(TL.NVDA_NEWS, "AI capex", TL.RF(TL.T_PRE - dt.timedelta(minutes=40)))])
        deps.fetch_json = sec
        r = pipeline.run_cycle(self.c, self.cfg, self.uni, TL.SRC, deps, TL.T_PRE)
        self.assertEqual(r["errors"], [])
        self.assertEqual(len(calls), 1)                                                                     # it stopped at the first refusal instead of asking for every concept of every company
        self.assertIn("429", json.dumps(r["facts"]))
        d2 = self.deps([])
        d2.fetch_json = sec
        r2 = pipeline.run_cycle(self.c, self.cfg, self.uni, TL.SRC, d2, TL.T_PRE + dt.timedelta(minutes=10))
        self.assertEqual(len(calls), 1)                                                                     # still backing off
        self.assertIn("backing off", json.dumps(r2["facts"]))
        d3 = self.deps([])
        d3.fetch_json = sec
        pipeline.run_cycle(self.c, self.cfg, self.uni, TL.SRC, d3, TL.T_PRE + dt.timedelta(minutes=70))
        self.assertGreater(len(calls), 1)                                                                   # an hour later it tries again

    def test_a_placeholder_contact_email_is_called_out(self):
        self.cfg["runtime"]["contact_email"] = "paper-agent@example.invalid"
        self.mkt.now = TL.T_PRE
        deps = self.deps([(TL.NVDA_NEWS, "AI capex", TL.RF(TL.T_PRE - dt.timedelta(minutes=40)))])
        deps.fetch_json = lambda url, **k: (_ for _ in ()).throw(net.FetchError("nf", 404))
        r = pipeline.run_cycle(self.c, self.cfg, self.uni, TL.SRC, deps, TL.T_PRE)
        self.assertIn("placeholder", r["facts"].get("_warning", ""))


if __name__ == "__main__":
    unittest.main()
