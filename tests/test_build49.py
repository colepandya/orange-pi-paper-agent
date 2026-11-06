"""Build 49: the shadow book. Every expected number below is worked out by hand from the synthetic bars, not read from the code."""
import datetime as dt
import json
import os
import subprocess
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
import test_kb_integration as KI  # noqa: E402
from paper_agent import dashboard, db, shadow  # noqa: E402
from paper_agent.timeutil import iso, parse_ts  # noqa: E402

NOW = parse_ts("2026-10-30T00:00:00+00:00")
BARS = {   # day: open, high, low, close     (Tue 20 Oct 2026 is a trading day; EDT: open 13:30Z, close 20:00Z)
    "TKR": {"2026-10-20": (100, 103, 99, 102), "2026-10-21": (102, 106, 101, 104), "2026-10-22": (104, 105, 98, 99), "2026-10-23": (99, 100, 97, 98), "2026-10-26": (98, 99, 96, 97)},
    "SPY": {"2026-10-20": (400, 405, 399, 404), "2026-10-21": (404, 407, 403, 406), "2026-10-22": (406, 407, 395, 396), "2026-10-23": (396, 398, 394, 395), "2026-10-26": (395, 397, 393, 394)}}


def world(decisions, now=NOW, cost=30.0):
    c = H.fresh_db()
    cfg = H.cfg()
    cfg["shadow"]["cost_bps"] = cost
    for sym, days in BARS.items():
        for day, (o, h, l, cl) in days.items():
            c.execute("INSERT INTO market_series(symbol,day,open,high,low,close,volume) VALUES(?,?,?,?,?,?,1e6)", (sym, day, o, h, l, cl))
    for i, d in enumerate(decisions, 1):
        reason = {"candidate": {"event_type": d.get("etype", "earnings_beat"), "direction": d.get("dir", 0.6), "conf": 0.7, "magnitude": 0.5, "origin": "event", "source": "wire"},
                  "forecast": {"ev_net": d.get("ev", 0.01), "confidence": 0.7}, "gates": d.get("gates", []), "ref_price": d.get("ref"), "decided_at": d["at"]}
        c.execute("INSERT INTO decisions(created,ticker,action,status,reason,ev_net,event_id) VALUES(?,?,?,?,?,?,?)", (d["at"], d.get("tk", "TKR"), "BUY", d.get("status", "REJECTED"), json.dumps(reason), d.get("ev", 0.01), i))
    c.commit()
    shadow.sync_candidates(c, cfg, now)
    shadow.measure_due(c, cfg, now)
    return c, cfg


def out(c, did, h):
    r = c.execute("SELECT * FROM shadow_outcomes WHERE decision_id=? AND horizon=?", (did, h)).fetchone()
    return dict(r) if r else None


class TestEntryConventions(unittest.TestCase):
    def test_the_three_entry_bases(self):
        for at, day, basis in (("2026-10-20T12:00:00+00:00", "2026-10-20", "open"), ("2026-10-20T15:00:00+00:00", "2026-10-20", "decision_price"), ("2026-10-20T21:00:00+00:00", "2026-10-21", "next_open"),
                               ("2026-10-23T21:00:00+00:00", "2026-10-26", "next_open"), ("2026-10-24T15:00:00+00:00", "2026-10-26", "next_open")):          # after Friday's close, and on a Saturday: Monday
            d, b = shadow.entry_day_and_basis(parse_ts(at))
            self.assertEqual((d.isoformat(), b), (day, basis), at)


class TestHandCalculatedOutcomes(unittest.TestCase):
    def test_a_long_decided_before_the_open_enters_at_the_open_and_every_number_follows(self):
        c, _ = world([{"at": "2026-10-20T12:00:00+00:00", "ref": 99.0}])
        cand = dict(c.execute("SELECT * FROM shadow_candidates").fetchone())
        self.assertEqual((cand["entry_day"], cand["entry_price"], cand["entry_basis"], cand["side"]), ("2026-10-20", 100.0, "open", "LONG"))
        h1, h3 = out(c, 1, 1) or out(c, 1, 1), out(c, 1, 3)
        h1 = out(c, 1, 1)
        self.assertAlmostEqual(h1["gross_ret"], 0.02)                                       # 102/100 - 1
        self.assertAlmostEqual(h1["net_ret"], 0.02 - 0.003)                                  # minus 30 bps
        self.assertAlmostEqual(h1["bench_ret"], 0.01)                                        # SPY 404/400 - 1
        self.assertAlmostEqual(h1["excess_net"], (0.02 - 0.01) - 0.003)
        self.assertAlmostEqual(h3["gross_ret"], -0.01)                                       # close 22 Oct 99 / 100 - 1
        self.assertAlmostEqual(h3["mfe"], 0.06)                                              # highest high 106
        self.assertAlmostEqual(h3["mae"], -0.02)                                             # lowest low 98
        self.assertAlmostEqual(h3["bench_ret"], 396 / 400 - 1)

    def test_a_mid_session_entry_uses_the_decision_price_and_cannot_claim_the_part_of_the_day_before_it(self):
        c, _ = world([{"at": "2026-10-20T15:00:00+00:00", "ref": 101.0}])
        self.assertEqual(c.execute("SELECT entry_price,entry_basis FROM shadow_candidates").fetchone()[:], (101.0, "decision_price"))
        h1, h3 = out(c, 1, 1), out(c, 1, 3)
        self.assertAlmostEqual(h1["gross_ret"], 102 / 101 - 1)
        self.assertIsNone(h1["mfe"])                                                          # no later bar inside a one-session hold
        self.assertAlmostEqual(h3["mfe"], 106 / 101 - 1)                                      # the entry day's own high (103) is NOT counted
        self.assertAlmostEqual(h3["mae"], 98 / 101 - 1)
        self.assertAlmostEqual(h3["bench_ret"], 396 / 404 - 1)                                # the market from the entry day's close

    def test_a_short_wins_when_the_price_falls_and_beats_the_market_by_falling_more(self):
        c, _ = world([{"at": "2026-10-20T12:00:00+00:00", "dir": -0.6}])
        h3 = out(c, 1, 3)
        self.assertAlmostEqual(h3["gross_ret"], 0.01)                                         # price 100 -> 99: a short gains 1%
        self.assertAlmostEqual(h3["excess_net"], ((-1) * (-0.01 - (396 / 400 - 1))) - 0.003)   # short: -(stock - market) - cost
        self.assertAlmostEqual(h3["mfe"], 0.02)                                               # best for a short is the lowest low (98)
        self.assertAlmostEqual(h3["mae"], -0.06)                                              # worst is the highest high (106)

    def test_the_short_side_of_the_market_adjustment_with_numbers_that_cannot_coincide(self):
        c, _ = world([{"at": "2026-10-20T12:00:00+00:00", "dir": -0.6}])
        h1 = out(c, 1, 1)                                                                     # price 100 -> 102 (+2%) while the market rose 1%
        self.assertAlmostEqual(h1["gross_ret"], -0.02)                                        # a short LOSES 2%
        self.assertAlmostEqual(h1["excess_net"], -(0.02 - 0.01) - 0.003)                      # it trailed a market it was betting against by 1%, plus cost: -1.3%
        long_c, _ = world([{"at": "2026-10-20T12:00:00+00:00"}])
        self.assertAlmostEqual(out(long_c, 1, 1)["excess_net"], (0.02 - 0.01) - 0.003)        # the same move is +0.7% for a long

    def test_a_day_that_is_the_current_day_is_used_only_after_the_session_is_over(self):
        dec = [{"at": "2026-10-21T12:00:00+00:00"}]                                           # enters at the 21st's open
        during = parse_ts("2026-10-21T19:30:00+00:00")                                        # 15:30 in New York: the 21st is still trading
        c, _ = world(dec, now=during)
        self.assertIsNone(out(c, 1, 1))
        after = parse_ts("2026-10-21T20:45:00+00:00")                                         # 16:45: it is over
        c2, _ = world(dec, now=after)
        self.assertAlmostEqual(out(c2, 1, 1)["gross_ret"], 104 / 102 - 1)

    def test_after_the_close_the_entry_is_the_next_sessions_open(self):
        c, _ = world([{"at": "2026-10-20T21:00:00+00:00"}])
        self.assertEqual(c.execute("SELECT entry_day,entry_price FROM shadow_candidates").fetchone()[:], ("2026-10-21", 102.0))
        self.assertAlmostEqual(out(c, 1, 1)["gross_ret"], 104 / 102 - 1)

    def test_the_cost_is_a_setting_and_comes_off_every_outcome(self):
        c, _ = world([{"at": "2026-10-20T12:00:00+00:00"}], cost=100.0)
        self.assertAlmostEqual(out(c, 1, 1)["net_ret"], 0.02 - 0.01)

    def test_a_horizon_without_enough_completed_bars_is_not_measured(self):
        c, _ = world([{"at": "2026-10-20T12:00:00+00:00"}])
        self.assertIsNone(out(c, 1, 10))
        self.assertIsNotNone(out(c, 1, 5))                                                    # 20,21,22,23,26 Oct: five bars

    def test_an_unfinished_day_is_never_used(self):
        mid = parse_ts("2026-10-21T19:00:00+00:00")                                           # 15:00 in New York on the 21st: that day is not over
        c, _ = world([{"at": "2026-10-20T12:00:00+00:00"}], now=mid)
        self.assertIsNotNone(out(c, 1, 1))
        self.assertIsNone(out(c, 1, 3))
        after = parse_ts("2026-10-22T20:45:00+00:00")                                         # 16:45 in New York: the 22nd is complete
        c2, _ = world([{"at": "2026-10-20T12:00:00+00:00"}], now=after)
        self.assertIsNotNone(out(c2, 1, 3))

    def test_a_stock_with_no_prices_is_counted_not_guessed(self):
        c, cfg = world([{"at": "2026-10-20T12:00:00+00:00", "tk": "NOPRICE"}])
        self.assertIsNone(out(c, 1, 1))
        r = shadow.measure_due(c, cfg, NOW)
        self.assertEqual((r["measured"], r["no_prices"]), (0, 1))


class TestCapture(unittest.TestCase):
    def test_sync_is_idempotent_skips_ideas_with_no_view_and_follows_status_changes(self):
        c, cfg = world([{"at": "2026-10-20T12:00:00+00:00", "status": "AWAITING_MARKET"}, {"at": "2026-10-20T12:01:00+00:00", "dir": 0.05}])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM shadow_candidates").fetchone()[0], 1)           # the 0.05 direction carries no view
        self.assertEqual(shadow.sync_candidates(c, cfg, NOW)["added"], 0)
        c.execute("UPDATE decisions SET status='EXECUTED' WHERE id=1")
        shadow.sync_candidates(c, cfg, NOW - dt.timedelta(days=9) + dt.timedelta(days=9))
        c.execute("UPDATE shadow_candidates SET status='AWAITING_MARKET' WHERE decision_id=1")
        r = shadow.sync_candidates(c, cfg, parse_ts("2026-10-21T00:00:00+00:00"))
        self.assertEqual(r["status_refreshed"], 1)
        self.assertEqual(c.execute("SELECT status,gate FROM shadow_candidates").fetchone()[:], ("EXECUTED", "traded"))

    def test_a_long_history_is_taken_in_bounded_bites_and_nothing_is_skipped_or_repeated(self):
        c = H.fresh_db()
        cfg = H.cfg()
        for i in range(1, 41):
            reason = {"candidate": {"event_type": "x", "direction": 0.6}, "forecast": {"ev_net": 0.0}, "gates": [], "ref_price": 10.0, "decided_at": "2026-10-20T12:00:00+00:00"}
            c.execute("INSERT INTO decisions(created,ticker,action,status,reason,event_id) VALUES(?,?,?,?,?,?)", ("2026-10-20T12:00:00+00:00", "T%d" % i, "BUY", "REJECTED", json.dumps(reason), i))
        c.commit()
        seen, bites = 0, []
        for _ in range(60):
            ticks = iter([0.0, 0.0, 0.0, 0.0, 0.0, 99.0] + [99.0] * 500)                       # a few rows, then out of time
            r = shadow.sync_candidates(c, cfg, NOW, clock=lambda: next(ticks), budget_s=1.0)
            seen += r["added"]
            bites.append(r["added"])
            if r["added"] == 0:
                break
        self.assertEqual(seen, 40)
        self.assertGreater(len(bites), 5)                                                       # it really took several bounded bites ...
        self.assertLessEqual(max(bites), 5)                                                     # ... none larger than the time allowed
        self.assertEqual(c.execute("SELECT COUNT(*) FROM shadow_candidates").fetchone()[0], 40)
        self.assertEqual(c.execute("SELECT COUNT(DISTINCT decision_id) FROM shadow_candidates").fetchone()[0], 40)

    def test_every_gate_text_the_bot_produces_lands_in_the_right_category(self):
        cases = [("REJECTED", ["EV net -0.01 <= 0.00"], "ev_below_minimum"), ("REJECTED", ["EV net 0.002 below minimum 0.010"], "ev_below_minimum"), ("REJECTED", ["eligibility 0.3 < 0.5"], "eligibility"),
                 ("REJECTED", ["bearish event; long-only book"], "long_only_book"), ("REJECTED", ["swing reward:risk 0.48 below minimum 1.00 (target 1.5% vs stop 3.0%)"], "reward_risk"),
                 ("REJECTED", ["price $4 under the $5 short minimum ; market cap $1B under the short minimum"], "short_minimums"), ("REJECTED", ["market session AFTER_HOURS; will retry"], "after_hours_retry"),
                 ("REJECTED", ["HELD: market-wide shock since this BUY was decided"], "market_shock"), ("RISK_REJECTED", ["the position monitor would close this at once: x"], "monitor_conflict"),
                 ("REJECTED", ["ADV $1M below minimum"], "liquidity_adv"), ("EXECUTED", [], "traded"), ("VETOED", ["VETO: news"], "veto"), ("EXPIRED", [], "expired"), ("DUPLICATE", ["already holding"], "duplicate"),
                 ("REJECTED", ["something new"], "other"), ("RISK_REJECTED", ["something new"], "risk_rejected")]
        for status, gates, want in cases:
            self.assertEqual(shadow.classify_gate(status, gates)[0], want, (status, gates))


class TestOneBadRowNeverStopsTheShadowBook(unittest.TestCase):
    """A hostile-input hunt found that one decision with valid-but-unexpected JSON stopped capture for good: the cursor never moved past it and it failed again every cycle."""

    def test_junk_reasons_are_skipped_the_good_rows_are_captured_and_the_cursor_moves_on(self):
        c = H.fresh_db()
        cfg = H.cfg()
        good = json.dumps({"candidate": {"event_type": "x", "direction": 0.6}, "forecast": {"ev_net": 0.01}, "gates": [], "ref_price": 10.0, "decided_at": "2026-10-20T12:00:00+00:00"})
        reasons = ["[]", "123", "null", '"text"', "{", json.dumps({"candidate": "x", "forecast": [], "gates": "oops"}), json.dumps({"candidate": {"direction": "up"}}), good,
                   json.dumps({"candidate": {"direction": 0.6, "event_type": 5, "conf": "x"}, "forecast": {"ev_net": float("nan")}, "gates": [None, 3], "ref_price": "n/a", "decided_at": "2026-10-20T12:00:00+00:00"}), good]
        for i, rs in enumerate(reasons, 1):
            c.execute("INSERT INTO decisions(created,ticker,action,status,reason,event_id) VALUES(?,?,?,?,?,?)", ("2026-10-20T12:00:00+00:00", "T%d" % i, "BUY", "REJECTED", rs, i))
        c.commit()
        r = shadow.sync_candidates(c, cfg, NOW)
        self.assertEqual(r["cursor"], len(reasons))                                           # past the bad rows
        self.assertEqual(c.execute("SELECT COUNT(*) FROM shadow_candidates WHERE ticker IN ('T8','T10')").fetchone()[0], 2)
        self.assertEqual(shadow.sync_candidates(c, cfg, NOW)["added"], 0)                     # and it does not retry them forever
        row = dict(c.execute("SELECT * FROM shadow_candidates WHERE ticker='T9'").fetchone())
        self.assertIsNone(row["conf"])                                                         # junk is stored as unknown, never as a number
        self.assertIsNone(row["event_type"])
        self.assertIsNone(row["ref_price"])

    def test_a_row_whose_capture_blows_up_unexpectedly_is_skipped_and_the_cursor_still_moves_on(self):
        c = H.fresh_db()
        cfg = H.cfg()
        good = json.dumps({"candidate": {"event_type": "x", "direction": 0.6}, "forecast": {}, "gates": [], "ref_price": 10.0, "decided_at": "2026-10-20T12:00:00+00:00"})
        for i in range(1, 4):
            c.execute("INSERT INTO decisions(created,ticker,action,status,reason,event_id) VALUES(?,?,?,?,?,?)", ("2026-10-20T12:00:00+00:00", "T%d" % i, "BUY", "REJECTED", good, i))
        c.commit()
        orig = shadow._capture_one

        def flaky(conn, r, min_abs, now):
            if r["ticker"] == "T2":
                raise RuntimeError("database gremlin")
            return orig(conn, r, min_abs, now)
        with unittest.mock.patch.object(shadow, "_capture_one", flaky):
            r = shadow.sync_candidates(c, cfg, NOW)
        self.assertEqual((r["added"], r["skipped_no_view"], r["cursor"]), (2, 1, 3))
        self.assertEqual(sorted(x[0] for x in c.execute("SELECT ticker FROM shadow_candidates")), ["T1", "T3"])

    def test_a_candidate_whose_measurement_blows_up_is_counted_and_the_others_are_still_measured(self):
        c, cfg = world([{"at": "2026-10-20T12:00:00+00:00", "tk": "TKR"}, {"at": "2026-10-20T12:00:00+00:00", "tk": "BOOM"}], now=NOW)
        c.execute("DELETE FROM shadow_outcomes")
        orig = shadow._measure_one

        def flaky(conn, cand, now, cost, last):
            if cand["ticker"] == "BOOM":
                raise RuntimeError("bars gremlin")
            return orig(conn, cand, now, cost, last)
        with unittest.mock.patch.object(shadow, "_measure_one", flaky):
            r = shadow.measure_due(c, cfg, NOW)
        self.assertEqual((r["errors"], r["measured"]), (1, 1))

    def test_a_missing_zero_or_negative_price_never_becomes_a_return(self):
        c = H.fresh_db()
        cfg = H.cfg()
        for day, o, h, l, cl in (("2026-10-20", 100, 103, 99, 102), ("2026-10-21", 102, 0, 101, 104), ("2026-10-22", 104, 105, 98, -5)):
            c.execute("INSERT INTO market_series(symbol,day,open,high,low,close,volume) VALUES('TKR',?,?,?,?,?,1e6)", (day, o, h, l, cl))
        for day, o, h, l, cl in (("2026-10-20", 400, 405, 399, 404), ("2026-10-21", 404, 407, 403, 406), ("2026-10-22", 406, 407, 395, 396)):
            c.execute("INSERT INTO market_series(symbol,day,open,high,low,close,volume) VALUES('SPY',?,?,?,?,?,1e6)", (day, o, h, l, cl))
        reason = {"candidate": {"direction": 0.6}, "forecast": {}, "gates": [], "ref_price": None, "decided_at": "2026-10-20T12:00:00+00:00"}
        c.execute("INSERT INTO decisions(created,ticker,action,status,reason,event_id) VALUES(?,?,?,?,?,?)", ("2026-10-20T12:00:00+00:00", "TKR", "BUY", "REJECTED", json.dumps(reason), 1))
        shadow.sync_candidates(c, cfg, NOW)
        shadow.measure_due(c, cfg, NOW)
        self.assertIsNotNone(c.execute("SELECT 1 FROM shadow_outcomes WHERE horizon=1").fetchone())     # the clean first day is measured
        self.assertIsNone(c.execute("SELECT 1 FROM shadow_outcomes WHERE horizon=3").fetchone())        # the horizon whose window holds an impossible price is not

    def test_the_helpers_reject_non_numbers_and_non_finite_values(self):
        for bad in (None, "x", [], {}, float("nan"), float("inf")):
            self.assertIsNone(shadow._n(bad))
        self.assertEqual(shadow._n("1.5"), 1.5)
        self.assertEqual([shadow._ok(x) for x in (1.0, 0, -1, float("nan"), None, "1")], [True, False, False, False, False, False])
        self.assertEqual(shadow._dict([1]), {})


class TestStatistics(unittest.TestCase):
    def test_spearman_matches_known_values_including_ties(self):
        self.assertAlmostEqual(shadow.spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]), 1.0)
        self.assertAlmostEqual(shadow.spearman([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]), -1.0)
        self.assertAlmostEqual(shadow.spearman([1, 2, 3, 4], [1, 3, 2, 4]), 0.8)
        self.assertIsNone(shadow.spearman([1, 1, 1], [1, 2, 3]))
        self.assertIsNone(shadow.spearman([1, 2], [1, 2]))

    def test_bootstrap_and_permutation_are_deterministic_and_the_interval_covers_the_mean(self):
        xs = [0.01 * i for i in range(-10, 21)]
        a, b = shadow.bootstrap_ci(xs), shadow.bootstrap_ci(xs)
        self.assertEqual(a, b)
        self.assertLess(a[0], sum(xs) / len(xs))
        self.assertGreater(a[1], sum(xs) / len(xs))
        self.assertEqual(shadow.bootstrap_ci([0.1, 0.2]), (None, None))
        ys = [x + 0.001 * (i % 3) for i, x in enumerate(xs)]
        self.assertEqual(shadow.perm_p(xs, ys), shadow.perm_p(xs, ys))
        self.assertLess(shadow.perm_p(xs, ys), 0.01)

    def test_maturity_labels(self):
        self.assertEqual([shadow.maturity(n) for n in (0, 19, 20, 49, 50, 99, 100)], ["insufficient", "insufficient", "informational", "informational", "preliminary", "preliminary", "review candidate"])

    def test_two_ideas_about_one_stock_on_one_day_are_one_observation(self):
        rows = [{"cluster": "A|d|LONG", "decision_id": 1, "ticker": "A", "net_ret": 0.02}, {"cluster": "A|d|LONG", "decision_id": 2, "ticker": "A", "net_ret": 0.04}, {"cluster": "B|d|LONG", "decision_id": 3, "ticker": "B", "net_ret": -0.01}]
        self.assertEqual(sorted(shadow._clusters(rows)), [-0.01, 0.03])

    def test_the_verdict_needs_enough_observations_and_an_interval_that_excludes_zero_against_the_market(self):
        mk = lambda n, ex: [{"cluster": "c%d" % i, "decision_id": i, "ticker": "T", "net_ret": ex, "gross_ret": ex, "excess_net": ex + 0.0001 * (i % 5)} for i in range(n)]  # noqa: E731
        self.assertEqual(shadow._stats(mk(10, 0.05))["verdict"], "insufficient evidence")
        self.assertIn("beat the market", shadow._stats(mk(40, 0.05))["verdict"])
        self.assertIn("trailed the market", shadow._stats(mk(40, -0.05))["verdict"])
        noisy = [{"cluster": "c%d" % i, "decision_id": i, "ticker": "T", "net_ret": 0.0, "gross_ret": 0.0, "excess_net": (0.05 if i % 2 else -0.05)} for i in range(40)]
        self.assertEqual(shadow._stats(noisy)["verdict"], "no evidence either way")

    def test_shrinkage_pulls_a_small_sample_toward_the_overall_mean(self):
        rows = [{"cluster": "g%d" % i, "decision_id": i, "ticker": "T", "net_ret": 0.0, "gross_ret": 0.0, "excess_net": 0.0, "ev_net": 0.0, "side": "LONG", "event_type": "common", "gate": "traded", "bench_ret": 0.0} for i in range(60)]
        rows += [{"cluster": "r%d" % i, "decision_id": 100 + i, "ticker": "T", "net_ret": 0.10, "gross_ret": 0.10, "excess_net": 0.10, "ev_net": 0.0, "side": "LONG", "event_type": "rare", "gate": "traded", "bench_ret": 0.0} for i in range(3)]
        c = H.fresh_db()
        shadow.ensure(c)
        for r in rows:
            c.execute("INSERT INTO shadow_candidates(decision_id,ticker,side,decided_at,status,gate,event_type,cluster,ev_net,entry_day) VALUES(?,?,?,?,?,?,?,?,?,?)", (r["decision_id"], r["ticker"], r["side"], "2026-10-20T12:00:00+00:00", "EXECUTED", r["gate"], r["event_type"], r["cluster"], r["ev_net"], "2026-10-20"))
            c.execute("INSERT INTO shadow_outcomes(decision_id,horizon,net_ret,gross_ret,excess_net,bench_ret) VALUES(?,?,?,?,?,?)", (r["decision_id"], 3, r["net_ret"], r["gross_ret"], r["excess_net"], r["bench_ret"]))
        rep = shadow.report(c, H.cfg())
        self.assertEqual(rep["by_event_type"]["rare"]["mean_net_pct"], 10.0)
        self.assertLess(rep["by_event_type"]["rare"]["shrunk_mean_net_pct"], 1.5)                    # 3 observations are not a finding


class TestReportOnARealisticWorld(unittest.TestCase):
    def test_it_reports_gates_sides_calibration_and_always_prints_the_caveats(self):
        decs = []
        for i in range(60):
            decs.append({"at": "2026-10-20T12:00:00+00:00", "tk": "TKR", "gates": ["EV net -0.01 <= 0.00"], "ev": -0.01 + 0.0003 * i, "dir": 0.6 if i % 2 else -0.6})
        c, cfg = world(decs)
        rep = shadow.report(c, cfg)
        self.assertEqual(rep["coverage"]["candidates"], 60)
        self.assertIn("ev_below_minimum", rep["by_gate"])
        self.assertEqual(set(rep["by_side"]), {"LONG", "SHORT"})
        self.assertIsNotNone(rep["market_move_pct"])
        self.assertIn("Daily bars only", rep["caveats"])
        text = shadow.format_report(rep)
        self.assertIn("READ THIS FIRST", text)
        self.assertIn("compared at once", text)

    def test_an_empty_database_reports_nothing_instead_of_failing(self):
        c = H.fresh_db()
        self.assertEqual(shadow.report(c, H.cfg())["coverage"]["candidates"], 0)


class TestObservationOnly(KI.KbEnv):
    def test_with_the_shadow_book_on_or_off_not_one_decision_position_or_dollar_changes(self):
        off, e1, _ = self.morning_kb(shadow_on=False)
        on, e2, _ = self.morning_kb(shadow_on=True)
        self.assertEqual((e1, e2), ([], []))
        self.assertEqual(self.trades(off), self.trades(on))
        self.assertEqual(off.execute("SELECT COUNT(*) FROM shadow_candidates").fetchone()[0], 0)
        self.assertGreater(on.execute("SELECT COUNT(*) FROM shadow_candidates").fetchone()[0], 0)

    def test_a_failing_shadow_stage_never_stops_trading(self):
        base, _, _ = self.morning_kb(shadow_on=False)
        c, errors, _ = self.morning_kb(patch=unittest.mock.patch.object(shadow, "measure_due", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bars gremlin"))))
        self.assertEqual(errors, [])                                                                      # the stage catches its own failure ...
        self.assertEqual(self.trades(base), self.trades(c))                                              # ... and trading is unaffected
        with unittest.mock.patch.object(shadow, "measure_due", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bars gremlin"))):
            r = shadow.run(c, {"shadow": {"enabled": True}})
        self.assertEqual(r["status"], "error")                                                           # recorded, not raised
        self.assertIn("bars gremlin", r["error"])

    def test_the_stage_is_bounded(self):
        c, cfg = world([{"at": "2026-10-20T12:00:00+00:00"} for _ in range(50)])
        ticks = iter([0.0] + [99.0] * 200)
        c.execute("DELETE FROM shadow_outcomes")
        r = shadow.measure_due(c, cfg, NOW, limit=1000, clock=lambda: next(ticks), budget_s=1.0)
        self.assertLessEqual(r["measured"], 1)


class TestSurfaces(unittest.TestCase):
    def test_the_settings_exist_with_the_stated_defaults_and_the_layout_is_valid(self):
        from paper_agent import settings as S, settings_ui as U, config
        d = config.load_config("/nonexistent", with_overrides=False)["shadow"]
        self.assertEqual((d["enabled"], d["cost_bps"], d["min_abs_direction"]), (True, 30.0, 0.15))
        self.assertTrue({"shadow.enabled", "shadow.cost_bps", "shadow.min_abs_direction", "shadow.measure_per_cycle", "shadow.max_seconds"} <= {f.path for f in S.FIELDS})
        self.assertEqual(U.layout_problems(), [])

    def test_changing_a_shadow_setting_never_changes_the_strategy_fingerprint(self):
        from paper_agent import stamp
        cfg = H.cfg()
        base = stamp.fingerprint(cfg)
        cfg["shadow"]["cost_bps"] = 99.0
        cfg["shadow"]["enabled"] = False
        self.assertEqual(stamp.fingerprint(cfg), base)

    def test_the_intelligence_tab_shows_the_shadow_book_on_a_read_only_connection_empty_or_full(self):
        import re
        c, cfg = world([{"at": "2026-10-20T12:00:00+00:00", "gates": ["EV net -0.01 <= 0.00"]}])
        path = c.execute("PRAGMA database_list").fetchone()[2]
        c.commit()
        ro = db.connect(path, readonly=True)
        page = re.sub(r"<[^>]+>", " ", dashboard.render(ro, cfg, "intelligence", {}, {"authed": True}))
        for need in ("Shadow book", "Read this first", "Daily bars only"):
            self.assertIn(need, page)
        empty = re.sub(r"<[^>]+>", " ", dashboard.render(H.fresh_db(), cfg, "intelligence", {}, {"authed": True}))
        self.assertIn("No ideas followed yet", empty)

    def test_the_command_runs_through_the_real_entry_point_and_reads_without_writing(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="pa-shadow-", dir="/tmp")
        os.makedirs(d + "/cfg")
        os.makedirs(d + "/state")
        json.dump({"AAA": {"name": "Alpha Corp", "cik": 1, "exchange": "NYSE", "aliases": []}}, open(d + "/cfg/watchlist.json", "w"))
        env = dict(os.environ, PAPER_AGENT_CONFIG_DIR=d + "/cfg", PAPER_AGENT_STATE_DIR=d + "/state", PAPER_AGENT_DB=d + "/state/paper-agent.db", PYTHONDONTWRITEBYTECODE="1")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for args, needle in ((("shadow", "sync"), "added"), (("shadow", "measure"), "measured"), (("shadow", "status"), "candidates"), (("shadow",), "SHADOW BOOK"), (("shadow", "report", "--json"), "caveats")):
            r = subprocess.run([sys.executable, "-m", "paper_agent"] + list(args), cwd=root, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, (args, r.stdout[-200:], r.stderr[-300:]))
            self.assertIn(needle, r.stdout, args)

    def test_report_and_status_are_read_only_commands_and_sync_and_measure_are_not(self):
        import types
        from paper_agent import kbcli
        for what, ro in (("report", True), ("status", True), (None, True), ("sync", False), ("measure", False)):
            self.assertEqual(kbcli.is_read_only(types.SimpleNamespace(cmd="shadow", what=what)), ro, what)


if __name__ == "__main__":
    unittest.main()
