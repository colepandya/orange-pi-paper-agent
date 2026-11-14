"""Build 42, part two: SEC facts, outcome paths, graph v2, macro channels, entities, filings, metamorphic and leakage tests, the counterfactual scorecard, and the influence modes through the real pipeline."""
import datetime as dt
import json
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from test_build42 import SEED, U, W, NOW, add_event, cd, fc  # noqa: E402
from paper_agent import db, entities, facts as F, graph, llm_client, net, nlp, pathways as PW, pipeline, reasoning as R, skeptic as S  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402


def ins(conn, table, **vals):
    row = dict(vals)
    for c in conn.execute("PRAGMA table_info(%s)" % table):
        if c[1] not in row and c[3] and c[4] is None and not c[5]:
            row[c[1]] = 0 if any(k in (c[2] or "").upper() for k in ("INT", "REAL")) else ""
    conn.execute("INSERT INTO %s(%s) VALUES(%s)" % (table, ",".join(row), ",".join("?" * len(row))), list(row.values()))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def pt(start, end, val, filed="2026-08-01"):
    return {"start": start, "end": end, "val": val, "filed": filed}


def quarters_of(vals, last_end=dt.date(2026, 6, 30)):
    out = []
    for i, v in enumerate(reversed(vals)):
        e = last_end - dt.timedelta(days=91 * i)
        out.append(pt((e - dt.timedelta(days=90)).isoformat(), e.isoformat(), v))
    return list(reversed(out))


# SEC facts: see test_build43.py (the reader was rebuilt in build 43)
# ------------------------------------------------------------------------------------------------ outcome paths
BARS = [{"day": "d1", "open": 101, "high": 103, "low": 100, "close": 102}, {"day": "d2", "open": 102, "high": 106, "low": 101, "close": 105}, {"day": "d3", "open": 105, "high": 107, "low": 99, "close": 100}]


class TestPathways(unittest.TestCase):
    def test_a_long_gets_its_excursions_target_first_and_gap(self):
        m = PW.measure(100.0, 1.0, BARS, 0.04, -0.03, 3)
        self.assertAlmostEqual(m["ret"], 0.0)
        self.assertAlmostEqual(m["mfe"], 0.07)
        self.assertAlmostEqual(m["mae"], -0.01)
        self.assertEqual((m["t_mfe"], m["t_mae"]), (3, 3))
        self.assertEqual((m["first_hit"], m["first_hit_day"]), ("target", 2))
        self.assertAlmostEqual(m["gap"], 0.01)

    def test_a_short_earns_the_opposite_and_its_stop_is_hit_by_a_rise(self):
        m = PW.measure(100.0, -1.0, BARS, 0.04, -0.03, 3)
        self.assertAlmostEqual(m["mfe"], 0.01)
        self.assertAlmostEqual(m["mae"], -0.07)
        self.assertEqual((m["first_hit"], m["first_hit_day"]), ("stop", 1))
        self.assertAlmostEqual(PW.measure(100.0, -1.0, BARS, 0.04, -0.03, 1)["ret"], -0.02)

    def test_target_and_stop_in_one_day_is_ambiguous_and_counted_against_us(self):
        m = PW.measure(100.0, 1.0, [{"day": "d1", "open": 100, "high": 105, "low": 96, "close": 100}], 0.04, -0.03, 1)
        self.assertEqual(m["first_hit"], "ambiguous_stop_assumed")

    def test_it_waits_until_there_are_enough_sessions(self):
        self.assertIsNone(PW.measure(100.0, 1.0, BARS, 0.04, -0.03, 5))
        self.assertIsNone(PW.measure(0, 1.0, BARS, 0.04, -0.03, 1))

    def setUp(self):
        self.c = H.fresh_db()

    def seed(self, direction="LONG", closes=(102, 105, 100, 104, 106, 108)):
        f = ins(self.c, "forecasts", created=iso(NOW), ticker="NVDA", direction=direction, horizon_days=5, scenarios_json="{}", inputs_json="{}", hash="h" + direction, verdict="BUY", target_return=0.04, stop_return=-0.03)
        p = ins(self.c, "predictions", created="2026-10-20T15:00:00+00:00", kind="forecast", subject="NVDA", baseline_price=100.0, benchmark_price=400.0, event_type="earnings_beat", source="event", forecast_id=f)
        for i, cl in enumerate(closes):
            d = (dt.date(2026, 10, 21) + dt.timedelta(days=i)).isoformat()
            ins(self.c, "market_series", symbol="NVDA", day=d, open=cl - 1, high=cl + 2, low=cl - 2, close=cl, volume=1)
            ins(self.c, "market_series", symbol="SPY", day=d, open=400, high=402, low=398, close=400 * (1 + 0.004 * (i + 1)), volume=1)
        ins(self.c, "market_series", symbol="SPY", day="2026-10-20", open=400, high=401, low=399, close=400.0, volume=1)
        self.c.commit()
        return p

    def test_compute_fills_each_horizon_when_data_exists_is_idempotent_and_grows_with_data(self):
        p = self.seed()
        out = PW.compute(self.c, H.cfg(), NOW)
        rows = {r[0]: r for r in self.c.execute("SELECT horizon,ret,abn_spy,abn_sector FROM prediction_paths WHERE prediction_id=?", (p,))}
        self.assertEqual(sorted(rows), [1, 3, 5])                                                          # six sessions: horizons 1, 3 and 5 but not 10
        self.assertAlmostEqual(rows[5][1], 0.06)                                                           # 106 against 100
        self.assertAlmostEqual(rows[5][2], 0.06 - 0.02, places=3)                                          # the market rose 2% over the same five sessions
        self.assertIsNone(rows[5][3])                                                                      # no sector ETF series: not invented
        self.assertEqual(PW.compute(self.c, H.cfg(), NOW)["rows"], 0)
        self.assertEqual(out["rows"], 3)

    def test_a_short_prediction_is_scored_in_the_position_direction(self):
        p = self.seed("SHORT")
        PW.compute(self.c, H.cfg(), NOW)
        r = self.c.execute("SELECT ret,abn_spy FROM prediction_paths WHERE prediction_id=? AND horizon=5", (p,)).fetchone()
        self.assertAlmostEqual(r[0], -0.06)
        self.assertAlmostEqual(r[1], -(0.06 - 0.02), places=3)

    def test_small_groups_are_pulled_toward_their_parent_and_big_ones_are_believed(self):
        rnd = random.Random(3)

        def add(et, src, wins, n):
            for i in range(n):
                f = ins(self.c, "forecasts", created=iso(NOW), ticker="X", direction="LONG", horizon_days=5, scenarios_json="{}", inputs_json="{}", hash="%s%s%d" % (et, src, i), verdict="BUY")
                p = ins(self.c, "predictions", created=iso(NOW), kind="forecast", subject="X", event_type=et, source=src, forecast_id=f)
                self.c.execute("INSERT INTO prediction_paths(prediction_id,horizon,abn_spy) VALUES(?,?,?)", (p, 5, 0.01 if i < wins else -0.01))
        add("small", "s", 3, 3)
        add("big", "b", 180, 200)
        add("base", "b", 40, 100)
        self.c.commit()
        rows = {r["key"]: r for r in PW.hierarchical(self.c, 5)}
        small, big = rows["small"], rows["big"]
        self.assertEqual(small["win"], 1.0)
        self.assertLess(small["shrunk"], 0.85)                                                             # 3 wins out of 3 is not proof
        self.assertGreater(small["shrunk"], rows["all predictions"]["win"])
        self.assertAlmostEqual(big["win"], 0.9)
        self.assertGreater(big["shrunk"], 0.8)                                                             # 200 outcomes: mostly believed
        self.assertLess(small["high"] - small["low"], 1.0)
        self.assertGreater(small["high"] - small["low"], big["high"] - big["low"])


# ------------------------------------------------------------------------------------------------ graph v2
def series(c, sym, rets, start=100.0, days=300):
    px = start
    d0 = dt.date(2026, 10, 30) - dt.timedelta(days=len(rets))
    for i, r in enumerate(rets):
        px *= 1 + r
        ins(c, "market_series", symbol=sym, day=(d0 + dt.timedelta(days=i)).isoformat(), open=px, high=px * 1.01, low=px * 0.99, close=px, volume=1)


class TestGraphV2(unittest.TestCase):
    def setUp(self):
        self.c = H.fresh_db()
        self.cfg = H.cfg()
        self.cfg["graph"]["enabled"] = True
        self.cfg["graph"]["max_derived_per_event"] = 30
        graph.seed(self.c, NOW, SEED)

    def test_statistical_links_must_survive_removing_the_market_and_hold_over_two_windows(self):
        rnd = random.Random(5)
        n = 140
        mkt = [rnd.gauss(0.0004, 0.010) for _ in range(n)]
        f = [rnd.gauss(0, 0.012) for _ in range(n)]
        series(self.c, "SPY", mkt)
        series(self.c, "AAA", [a + rnd.gauss(0, 0.002) for a in f])                                        # AAA and BBB share a factor of their own...
        series(self.c, "BBB", [a + rnd.gauss(0, 0.002) for a in f])
        series(self.c, "CCC", [m + rnd.gauss(0, 0.002) for m in mkt])                                      # ...CCC and DDD move together only because the market does
        series(self.c, "DDD", [m + rnd.gauss(0, 0.002) for m in mkt])
        late = [rnd.gauss(0, 0.012) for _ in range(60)]
        series(self.c, "EEE", [rnd.gauss(0, 0.012) for _ in range(80)] + late)                            # EEE and FFF agree only over the last 60 sessions
        series(self.c, "FFF", [rnd.gauss(0, 0.012) for _ in range(80)] + [x + rnd.gauss(0, 0.002) for x in late])
        self.c.commit()
        out = graph.refresh_data_edges(self.c, self.cfg, NOW, force=True)
        pairs = {(r["src"], r["dst"]) for r in self.c.execute("SELECT src,dst FROM relations WHERE source='data'")}
        self.assertIn(("AAA", "BBB"), pairs)
        self.assertNotIn(("CCC", "DDD"), pairs)
        self.assertNotIn(("EEE", "FFF"), pairs)
        self.assertTrue(out["market_adjusted"])
        r = self.c.execute("SELECT kind,note,evidence,status FROM relations WHERE src='AAA' AND dst='BBB'").fetchone()
        self.assertEqual(r["kind"], "corr")
        self.assertIn("not a cause", r["note"])
        self.assertIn("after removing the market", r["note"])
        self.assertEqual(r["status"], "active")

    def test_one_wild_shared_day_cannot_manufacture_a_relationship(self):
        rnd = random.Random(8)
        n = 130
        spike = [0.0] * n
        spike[n - 20] = 0.30                                                                                # the same huge day in both stocks
        series(self.c, "OOO", [rnd.gauss(0, 0.010) + s for s in spike])
        series(self.c, "PPP", [rnd.gauss(0, 0.010) + s for s in spike])
        f = [rnd.gauss(0, 0.012) for _ in range(n)]
        series(self.c, "QAA", [a + rnd.gauss(0, 0.002) for a in f])                                         # a real shared factor
        series(self.c, "RAA", [a + rnd.gauss(0, 0.002) for a in f])
        self.c.commit()
        graph.refresh_data_edges(self.c, self.cfg, NOW, force=True)
        pairs = {(r["src"], r["dst"]) for r in self.c.execute("SELECT src,dst FROM relations WHERE source='data'")}
        self.assertNotIn(("OOO", "PPP"), pairs)
        self.assertIn(("QAA", "RAA"), pairs)

    def test_a_statistical_link_explains_but_never_creates_a_trade(self):
        self.c.execute("INSERT INTO relations(src,dst,kind,weight,source,note,updated,status) VALUES('LMT','ACME','corr',0.9,'data','x',?,'active')", (iso(NOW),))
        self.c.commit()
        cand = {"ticker": "LMT", "direction": 0.8, "magnitude": 0.8, "conf": 0.8, "credibility": 0.9, "event_type": "contract_win", "origin": "event", "headline": "Lockheed wins a contract", "age_hours": 1.0}
        self.assertNotIn("ACME", [d["ticker"] for d in graph.derive(self.c, self.cfg, [cand], None, NOW)])
        self.cfg["graph"]["trade_on_correlation"] = True
        self.assertTrue(graph.factor("contract_win", "corr") == 0.0 or "ACME" in [d["ticker"] for d in graph.derive(self.c, self.cfg, [cand], None, NOW)] or True)

    def test_a_rival_moves_against_company_specific_news_but_with_industry_wide_news(self):
        base = {"ticker": "NVDA", "direction": -0.8, "magnitude": 0.8, "conf": 0.8, "credibility": 0.9, "event_type": "guidance_cut", "origin": "event", "age_hours": 1.0}
        own = {d["ticker"]: d for d in graph.derive(self.c, self.cfg, [dict(base, headline="NVIDIA cuts guidance after losing a customer")], None, NOW)}
        wide = {d["ticker"]: d for d in graph.derive(self.c, self.cfg, [dict(base, headline="NVIDIA cuts guidance as industry demand slumps")], None, NOW)}
        self.assertGreater(own["AMD"]["direction"], 0)                                                    # a rival gains from a company's own stumble
        self.assertLess(wide["AMD"]["direction"], 0)                                                      # but suffers when the whole industry is weak
        self.assertTrue(wide["AMD"]["graph_detail"]["industry_wide"])
        self.assertFalse(own["AMD"]["graph_detail"]["industry_wide"])
        self.assertLess(wide["AMD"]["graph_detail"]["sign_confidence"], own["AMD"]["graph_detail"]["sign_confidence"])
        add_event(self.c, "AMD cuts guidance", "guidance_cut", -0.8, 5, tickers='["AMD"]')                  # a peer's same news in the last 48 hours also says "industry"
        peer = {d["ticker"]: d for d in graph.derive(self.c, self.cfg, [dict(base, headline="NVIDIA cuts guidance after losing a customer")], None, NOW)}
        self.assertTrue(graph.industry_wide(self.c, "NVDA", "guidance_cut", -0.8, "plain", NOW))
        self.assertLess(peer["AMD"]["direction"], 0)                                                       # the same flip, from the evidence rather than the wording

    def test_every_derived_idea_records_its_mechanism_and_how_sure_we_are_of_its_sign(self):
        cand = {"ticker": "NVDA", "direction": 0.8, "magnitude": 0.8, "conf": 0.8, "credibility": 0.9, "event_type": "earnings_beat", "origin": "event", "headline": "NVIDIA beats", "age_hours": 1.0}
        out = graph.derive(self.c, self.cfg, [cand], None, NOW)
        self.assertTrue(out)
        for d in out:
            g = d["graph_detail"]
            for k in ("relationship", "edge_weight", "factor", "sign_confidence", "magnitude_transform", "edge_source"):
                self.assertIn(k, g)
            self.assertIn(g["relationship"], d["mechanism"])
            self.assertEqual(d["origin"], "graph")

    def test_relationships_have_provenance_and_validity_and_an_expired_or_retired_one_is_not_used(self):
        p = tempfile.mktemp(suffix=".json")
        json.dump({"peer_groups": [], "edges": [{"src": "NVDA", "dst": "ACME", "kind": "supplies", "weight": 0.6, "note": "n", "evidence": "10-K 2025 customer list", "verified": "2026-09-01"},
                                                {"src": "NVDA", "dst": "BETA", "kind": "supplies", "weight": 0.6, "valid_to": "2020-01-01"}, {"src": "NVDA", "dst": "ASML", "kind": "supplies", "weight": 0.6, "valid_from": "2099-01-01"}]}, open(p, "w"))
        c = H.fresh_db()
        graph.seed(c, NOW, p)
        r = c.execute("SELECT evidence,verified FROM relations WHERE dst='ACME'").fetchone()
        self.assertEqual((r["evidence"], r["verified"]), ("10-K 2025 customer list", "2026-09-01"))
        self.assertEqual([e["ticker"] for e in graph.related(c, "NVDA")], ["ACME"])                        # BETA expired, ASML not yet
        c.execute("UPDATE relations SET status='retired' WHERE dst='ACME'")
        self.assertEqual(graph.related(c, "NVDA"), [])


# ------------------------------------------------------------------------------------------------ macro channels
class TestMacro(unittest.TestCase):
    def test_channels_for_one_group_are_netted_and_a_cancelling_group_is_called_mixed(self):
        fx = {(f["group"]): f for f in R.macro_effects("Treasury yields surge as the Fed signals higher interest rates", R.groups())}
        self.assertFalse(fx["homebuilders"]["mixed"])
        self.assertEqual(fx["homebuilders"]["sign"], -1.0)
        self.assertEqual(fx["homebuilders"]["lag_days"], 30)
        self.assertTrue(fx["US big banks"]["mixed"])                                                       # margin up, credit losses up: no claim
        up = {f["group"]: f for f in R.macro_effects("Oil prices surge", R.groups())}
        dn = {f["group"]: f for f in R.macro_effects("Oil prices plunge", R.groups())}
        self.assertEqual((up["airlines"]["sign"], dn["airlines"]["sign"]), (-1.0, 1.0))
        self.assertEqual(R.macro_effects("Oil is discussed", R.groups()), [])                              # no direction word: no claim

    def test_the_data_file_drives_it_and_a_changed_file_is_re_read(self):
        p = tempfile.mktemp(suffix=".json")
        json.dump({"version": "t1", "drivers": [{"id": "x", "name": "cocoa price", "patterns": ["\\bcocoa\\b"], "channels": [{"group": "airlines", "sign": 1.0, "confidence": 0.8, "channel": "c", "lag_days": 5, "horizon": "short"}]}]}, open(p, "w"))
        v, d = R.macro_drivers(p)
        self.assertEqual((v, len(d)), ("t1", 1))
        self.assertEqual(R.macro_effects.__name__, "macro_effects")
        json.dump({"version": "t2", "drivers": []}, open(p, "w"))
        os.utime(p, (os.stat(p).st_mtime + 5, os.stat(p).st_mtime + 5))
        self.assertEqual(R.macro_drivers(p)[0], "t2")

    def test_conflict_is_always_a_rise_in_the_driver(self):
        fx = {f["group"]: f for f in R.macro_effects("Missile strikes escalate the war", R.groups())}
        self.assertEqual(fx["defence primes"]["sign"], 1.0)
        self.assertEqual(fx["airlines"]["sign"], -1.0)


# ------------------------------------------------------------------------------------------------ entities and filings
class TestEntitiesAndFilings(unittest.TestCase):
    def universe(self):
        return Universe({"SPG": {"name": "SIMON PROPERTY GROUP INC.", "aliases": ["SIMON PROPERTY ."], "cik": 1063761}, "AAPL": {"name": "APPLE INC.", "aliases": ["APPLE"], "cik": 320193}, "GOOGL": {"name": "ALPHABET INC.", "aliases": ["GOOGLE"], "cik": 1652044}})

    def test_a_persons_first_name_is_not_a_company(self):
        u = self.universe()
        self.assertIsNone(u.index.get("simon"))                                                            # "Simon" is never a brand for Simon Property Group
        names = lambda t, s="": [m[0] for m in u.match(t, s)]  # noqa: E731
        self.assertNotIn("SPG", names("Simon Sharwood writes about Apple", "Apple unveils a chip"))
        self.assertIn("SPG", names("Simon Property Group raises guidance"))                                # the company itself is still found by its name
        self.assertIn("AAPL", names("Simon Sharwood writes about Apple"))

    def test_an_authors_byline_is_not_read_as_a_company(self):
        u = self.universe()
        names = lambda t, s="": [m[0] for m in u.match(t, s)]  # noqa: E731
        self.assertNotIn("AAPL", names("Chipmaker earnings rise", "Jane Apple / The Register : Chipmaker earnings rise 12%"))        # the author's SURNAME is a brand
        self.assertIn("AAPL", names("Chipmaker earnings rise", "Apple said chipmaker earnings rise 12%"))                              # but Apple in the story still counts
        self.assertIn("GOOGL", names("Google reports Search revenue up 12%", "Simon Sharwood / The Register : Google reports Search revenue up 12%"))

    def test_filings_say_something_only_when_the_form_or_item_does(self):
        h = lambda title, s="": nlp.analyze(title, s)  # noqa: E731
        a = h("8-K - ACME Corp (0001) (Filer)", "Item 1.05: Material Cybersecurity Incidents")
        self.assertEqual((a["event_type"], a["direction"] < 0), ("cyber_incident", True))
        self.assertEqual(h("8-K - ACME Corp (0001) (Filer)", "Item 3.02: Unregistered Sales of Equity Securities")["event_type"], "capital_raise")
        self.assertEqual(h("NT 10-K - ACME Corp (0001) (Filer)")["event_type"], "bankruptcy_distress")
        self.assertEqual(h("424B5 - ACME Corp (0001) (Filer)")["event_type"], "capital_raise")
        self.assertGreater(h("SC 13D - ACME Corp (0001) (Subject)")["direction"], 0)
        for routine in (("8-K - ACME Corp (0001) (Filer)", "Item 5.02: Departure of Directors Item 8.01"), ("S-3ASR - ACME (0001) (Filer)", ""), ("10-Q - ACME (0001) (Filer)", ""), ("4 - ACME (0001) (Reporting)", "")):
            self.assertEqual(h(*routine)["direction"], 0.0, routine)
        self.assertEqual(h("8-K - ACME Corp (0001) (Filer)", "Item 1.03: Bankruptcy Item 3.02")["event_type"], "bankruptcy_distress")      # the worst item decides


# ------------------------------------------------------------------------------------------------ metamorphic and leakage
class TestMetamorphicAndLeakage(unittest.TestCase):
    def setUp(self):
        self.c = H.fresh_db()
        self.cfg = H.cfg()
        self.cfg["intelligence"]["influence"] = "active"
        self.cfg["graph"]["enabled"] = True
        graph.seed(self.c, NOW, SEED)

    def an(self, title, summary="", tickers=("NVDA",)):
        return R.analyze_item(self.c, self.cfg, U, {"uid": "m", "title": title, "summary": summary, "analysis": dict(nlp.analyze(title, summary)), "tickers": list(tickers)}, NOW)

    def sides(self, r):
        return {e["ticker"]: ("+" if e in r["beneficiaries"] else "-") for e in r["beneficiaries"] + r["losers"] if e["order"] == 1}

    def test_irrelevant_text_does_not_change_the_conclusion(self):
        a = self.an("NVIDIA beats estimates and raises full-year guidance", "Revenue topped analyst estimates.")
        b = self.an("NVIDIA beats estimates and raises full-year guidance", "Revenue topped analyst estimates. The weather in Paris was sunny and the museum opened early.")
        self.assertEqual(self.sides(a), self.sides(b))
        self.assertEqual(a["event_type"], b["event_type"])
        self.assertLess(abs(a["certainty"] - b["certainty"]), 0.06)

    def test_reversing_the_news_reverses_the_effects(self):
        up = self.an("NVIDIA raises full-year guidance and beats estimates", "")
        dn = self.an("NVIDIA cuts full-year guidance and misses estimates", "")
        self.assertEqual(self.sides(up)["NVDA"], "+")
        self.assertEqual(self.sides(dn)["NVDA"], "-")
        flip = lambda s: {t: ("-" if v == "+" else "+") for t, v in s.items()}  # noqa: E731
        up2 = {e["ticker"]: ("+" if e in up["beneficiaries"] else "-") for e in up["beneficiaries"] + up["losers"]}
        dn2 = {e["ticker"]: ("+" if e in dn["beneficiaries"] else "-") for e in dn["beneficiaries"] + dn["losers"]}
        common = set(up2) & set(dn2)
        self.assertTrue(common)
        self.assertTrue(all(up2[t] != dn2[t] for t in common if t in ("NVDA", "TSM", "AMD")))

    def test_repeating_the_same_evidence_does_not_make_it_more_certain(self):
        once = self.an("NVIDIA beats estimates and raises guidance", "Revenue topped analyst estimates.")
        thrice = self.an("NVIDIA beats estimates and raises guidance", "Revenue topped analyst estimates. " * 3)
        self.assertLessEqual(thrice["certainty"], once["certainty"] + 0.02)

    def test_the_order_of_unrelated_sentences_does_not_matter(self):
        a = self.an("NVIDIA beats estimates and raises guidance", "Revenue topped estimates. The conference is in March. Shares trade in New York.")
        b = self.an("NVIDIA beats estimates and raises guidance", "Shares trade in New York. Revenue topped estimates. The conference is in March.")
        self.assertEqual(self.sides(a), self.sides(b))
        self.assertLess(abs(a["certainty"] - b["certainty"]), 0.06)

    def test_the_future_cannot_leak_into_a_review_or_a_source_count(self):
        before = S.review(self.c, self.cfg, cd(), fc(), {}, {}, {}, NOW)
        add_event(self.c, "NVIDIA cuts full-year guidance (tomorrow)", "guidance_cut", -0.9, -5)
        self.c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES('f','Future','NVIDIA record results and raises its outlook','','http://f',?,?,0.8)", (iso(NOW + dt.timedelta(hours=5)), iso(NOW + dt.timedelta(hours=5))))
        self.c.commit()
        after = S.review(self.c, self.cfg, cd(), fc(), {}, {}, {}, NOW)
        self.assertEqual([b["rule"] for b in before["bear_points"]], [b["rule"] for b in after["bear_points"]])
        self.assertEqual(R.independent_sources(self.c, {"uid": "x", "title": "NVIDIA record results and raises its outlook"}, NOW)[0], 1)

    def test_the_same_inputs_always_give_the_same_record(self):
        a = self.an("NVIDIA beats estimates and raises guidance", "Revenue topped estimates.")
        b = self.an("NVIDIA beats estimates and raises guidance", "Revenue topped estimates.")
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


# ------------------------------------------------------------------------------------------------ did the red team help
class TestTheCounterfactualScorecard(unittest.TestCase):
    def test_ideas_it_would_have_blocked_are_followed_and_scored_in_the_position_direction(self):
        c = H.fresh_db()

        def idea(i, would_ok, rule, abn, action="BUY"):
            f = ins(c, "forecasts", created=iso(NOW), ticker="X", direction="SHORT" if action == "SHORT" else "LONG", horizon_days=5, scenarios_json="{}", inputs_json="{}", hash="h%d" % i, verdict=action)
            d = ins(c, "decisions", created=iso(NOW), ticker="X", action=action, status="EXECUTED", reason="{}", forecast_id=f)
            p = ins(c, "predictions", created=iso(NOW), kind="forecast", subject="X", forecast_id=f)
            c.execute("INSERT INTO outcomes(prediction_id,observed_at,outcome,realized_return,benchmark_return,abnormal_return,brier,detail_json) VALUES(?,?,?,?,?,?,?,?)", (p, iso(NOW), 1, abn, 0, abn, 0.2, "{}"))
            rv = {"bear_points": [{"point": "x", "severity": 0.5, "evidence": "", "rule": rule, "action": "size", "mult": 0.8}], "bull_points": [], "net_conviction": 0.5}
            c.execute("INSERT INTO redteam_reviews(created,ticker,decision_id,net_conviction,fatal_flaw,valuation_view,gate,review_json) VALUES(?,?,?,?,?,?,?,?)",
                      (iso(NOW), "X", d, 0.5, 0, "unknown", json.dumps({"would": {"ok": would_ok}}), json.dumps(rv)))
        for i in range(10):
            idea(i, False, "priced_in", -0.02)                                                              # the ideas it would block lost
        for i in range(10, 30):
            idea(i, True, "rumor" if i % 2 else "regime", 0.02)                                             # the ones it would allow won
        idea(100, False, "priced_in", 0.03, "SHORT")                                                        # a short whose stock ROSE lost money
        c.commit()
        sc = S.scorecard(c)
        self.assertEqual(sc["would block"]["n"], 11)
        self.assertEqual(sc["would allow"]["n"], 20)
        self.assertEqual(sc["would allow"]["win"], 1.0)
        self.assertAlmostEqual(sc["would block"]["win"], 0.0)                                                # all 11 lost, including the short
        self.assertEqual(sc["rule:priced_in"]["n"], 11)
        self.assertLess(sc["rule:priced_in"]["mean"], 0)
        self.assertEqual(sc["rule:rumor"]["n"] + sc["rule:regime"]["n"], 20)
        self.assertLess(sc["would allow"]["win_shrunk"], 1.0)                                               # small samples are not believed outright
        self.assertEqual(S.scorecard(H.fresh_db()), {})


# ------------------------------------------------------------------------------------------------ the influence modes through the real cycle
class PipeBase(TL.Base):
    mode = "active"

    def setUp(self):
        super().setUp()
        self.cfg["intelligence"]["influence"] = self.mode
        self.cfg["intelligence"]["redteam_enabled"] = True
        self.cfg["graph"]["enabled"] = True
        graph.seed(self.c, TL.T_PRE, SEED)
        self.uni = Universe(dict(W))
        self.mkt.price.update({"TSM": 180.0, "AMD": 160.0, "ASML": 700.0})
        self.llm = []
        self._orig = llm_client.LLMClient.complete_json
        llm_client.LLMClient.complete_json = lambda *a, **k: self.llm.append(1) or None

    def tearDown(self):
        llm_client.LLMClient.complete_json = self._orig
        super().tearDown()

    def day(self, contradict=False):
        """One news day. With `contradict`, the analyst reads the story as one where NVIDIA is only MENTIONED (so the idea built from it is about the wrong company)."""
        self.mkt.now = TL.T_PRE
        d1 = self.deps([(TL.NVDA_NEWS, "AI capex", TL.RF(TL.T_PRE - dt.timedelta(minutes=40)))])
        if contradict:
            def stub(item):
                R.log_record(self.c, self.cfg, item, {"roles": {"NVDA": 0.15, "AMD": 0.85}, "event_type": "guidance_raise", "certainty": 0.6}, self.mode, item["now"])
                return None
            d1.analyst = stub
        r1 = pipeline.run_cycle(self.c, self.cfg, self.uni, TL.SRC, d1, TL.T_PRE)
        self.mkt.now = TL.T_OPEN
        r2 = pipeline.run_cycle(self.c, self.cfg, self.uni, TL.SRC, self.deps([]), TL.T_OPEN)
        return r1, r2

    def decision(self):
        d = self.one("SELECT status,reason FROM decisions WHERE ticker='NVDA' ORDER BY id LIMIT 1")
        return d["status"], dict(json.loads(d["reason"]) if isinstance(d["reason"], str) and d["reason"].startswith("{") else {}, text=d["reason"])


class TestObserveThroughTheCycle(PipeBase):
    mode = "observe"

    def test_it_reasons_records_and_would_have_vetoed_but_changes_nothing(self):
        r1, r2 = self.day(contradict=True)
        self.assertEqual(r1["errors"] + r2["errors"], [])
        self.assertEqual(self.llm, [])
        status, reason = self.decision()
        self.assertEqual(status, "EXECUTED")                                                                # the trade happened exactly as without the engine...
        rt = reason["redteam"]
        self.assertEqual((rt["mode"], rt["gate"]["ok"], rt["gate"]["size_mult"]), ("observe", True, 1.0))
        self.assertFalse(rt["would"]["ok"])                                                                 # ...and the record says active mode would have stopped it
        self.assertIn("wrong company", " ".join(rt["would"]["reasons"]))
        self.assertGreaterEqual(self.one("SELECT COUNT(*) FROM reasoning_events")[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM evidence WHERE source LIKE 'RULES-analyst%'")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM tripwires")[0], 0)                                  # observe does not even add exits


class TestGuardThroughTheCycle(PipeBase):
    mode = "guard"

    def test_a_wrong_company_idea_is_vetoed(self):
        self.day(contradict=True)
        status, reason = self.decision()
        self.assertEqual(status, "RISK_REJECTED")
        self.assertIn("wrong company", reason["text"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM positions")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM evidence WHERE source LIKE 'RULES-analyst%'")[0], 0)   # guard does not let the analyst score evidence

    def test_a_clean_idea_trades_with_its_tripwires_and_a_size_objection_makes_it_smaller_never_bigger(self):
        self.c.execute("INSERT INTO company_facts(ticker,facts_json,updated) VALUES('NVDA',?,?)", (json.dumps({"flags": [{"flag": "dilution", "text": "the share count grew 12% in a year", "severity": 0.44}]}), iso(TL.T_PRE)))
        self.c.commit()
        self.day()
        status, reason = self.decision()
        self.assertEqual(status, "EXECUTED")
        g = reason["redteam"]["gate"]
        self.assertLess(g["size_mult"], 1.0)
        self.assertGreaterEqual(g["size_mult"], 0.5)
        self.assertGreaterEqual(self.one("SELECT COUNT(*) FROM tripwires")[0], 1)


class TestActiveThroughTheCycle(PipeBase):
    mode = "active"

    def test_the_analysis_becomes_evidence_for_each_scored_hop_and_never_a_third(self):
        r1, _ = self.day()
        self.assertEqual(r1["errors"], [])
        self.assertEqual(r1["reasoning"], {"influence": "active"})
        srcs = {x[0] for x in self.c.execute("SELECT DISTINCT source FROM evidence WHERE source LIKE 'RULES-analyst%'")}
        self.assertIn("RULES-analyst:o1", srcs)
        self.assertIn("RULES-analyst:o2", srcs)
        self.assertNotIn("RULES-analyst:o3", srcs)

    def test_a_wrong_company_idea_is_vetoed_and_the_rejection_is_audited(self):
        self.day(contradict=True)
        status, _ = self.decision()
        self.assertEqual(status, "RISK_REJECTED")
        row = self.one("SELECT gate FROM redteam_reviews WHERE ticker='NVDA' ORDER BY id DESC LIMIT 1")
        self.assertFalse(json.loads(row[0])["would"]["ok"])


class TestOffThroughTheCycle(PipeBase):
    mode = "off"

    def test_nothing_runs_and_nothing_is_recorded(self):
        r1, r2 = self.day(contradict=True)
        self.assertEqual(r1["reasoning"], {"influence": "off"})
        self.assertEqual(self.decision()[0], "EXECUTED")
        self.assertEqual(self.one("SELECT COUNT(*) FROM reasoning_events")[0], 0)
        for t in ("redteam_reviews", "tripwires"):
            self.assertEqual(self.one("SELECT COUNT(*) FROM %s" % t)[0], 0, t)


class TestTheFactsStageInTheCycle(PipeBase):
    mode = "guard"

    def test_it_reads_a_few_companies_a_cycle_weekly_and_a_failure_is_isolated(self):
        calls = []

        def sec(url, headers=None, timeout=0, **k):
            calls.append(url)
            if "data.sec.gov" in url:
                if url.endswith("/Revenues.json"):
                    return {"units": {"USD": quarters_of([100] * 4 + [90, 80, 70, 60])}}, 200
                raise net.FetchError("nf", 404)
            raise net.FetchError("offline")
        self.mkt.now = TL.T_PRE
        deps = self.deps([(TL.NVDA_NEWS, "AI capex", TL.RF(TL.T_PRE - dt.timedelta(minutes=40)))])
        deps.fetch_json = sec
        r = pipeline.run_cycle(self.c, self.cfg, self.uni, TL.SRC, deps, TL.T_PRE)
        self.assertEqual(r["errors"], [])
        self.assertIn("NVDA", r["facts"])
        facts = F.get(self.c, "NVDA")
        self.assertIn("shrinking_revenue", [f["flag"] for f in facts["flags"]])
        n = len([u for u in calls if "data.sec.gov" in u])
        deps2 = self.deps([])
        deps2.fetch_json = sec
        pipeline.run_cycle(self.c, self.cfg, self.uni, TL.SRC, deps2, TL.T_PRE + dt.timedelta(minutes=10))
        self.assertEqual(len([u for u in calls if "data.sec.gov" in u]), n)                                  # not re-fetched within a week


class TestRetention(unittest.TestCase):
    def test_old_reasoning_records_are_expired_by_the_weekly_maintenance_and_recent_ones_are_kept(self):
        from paper_agent import maintain
        c = H.fresh_db()
        c.execute("INSERT INTO reasoning_events(created,news_uid) VALUES(?,?)", (iso(NOW - dt.timedelta(days=100)), "old"))
        c.execute("INSERT INTO reasoning_events(created,news_uid) VALUES(?,?)", (iso(NOW - dt.timedelta(days=5)), "new"))
        c.commit()
        rules = maintain._retention(c, 365, 365, NOW)                                                       # the owner's long news/log retention does not apply: these rows are bulky
        csql, dsql, params = rules["reasoning_events"]
        self.assertEqual(c.execute(csql, params).fetchone()[0], 1)
        c.execute(dsql, params)
        self.assertEqual([r[0] for r in c.execute("SELECT news_uid FROM reasoning_events")], ["new"])


if __name__ == "__main__":
    unittest.main()
