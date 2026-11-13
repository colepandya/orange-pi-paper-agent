"""Build 54: the research lab and the long price history. Every expected number is worked out by hand from synthetic prices or from the Wilson formula."""
import datetime as dt
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
import test_kb_integration as KI  # noqa: E402
from paper_agent import config, dashboard, db, history, lab, net, settings as S, settings_ui as U, stamp  # noqa: E402
from paper_agent.timeutil import iso, parse_ts  # noqa: E402

D0 = dt.date(2025, 1, 6).toordinal()
NOW = parse_ts("2026-11-07T04:00:00+00:00")                                                              # Saturday: the US market is closed
DEF = lambda: config.load_config("/nonexistent", with_overrides=False)  # noqa: E731


def put_series(c, sym, closes, vols=None, opens=None, highs=None, lows=None, start=D0):
    for i, cl in enumerate(closes):
        c.execute("INSERT OR REPLACE INTO market_series(symbol,day,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?)",
                  (sym, dt.date.fromordinal(start + i).isoformat(), (opens or closes)[i], (highs or closes)[i], (lows or closes)[i], cl, (vols or [1e6] * len(closes))[i]))
    c.commit()


def load(c):
    return lab.load_series(c)


def flat(n, v=100.0):
    return [v] * n


class TestStatistics(unittest.TestCase):
    def test_the_daily_pooled_statistic_by_hand(self):
        a = lab.DailyAcc()
        for day, x in ((1, 0.02), (1, 0.04), (2, 0.01), (3, -0.01)):
            a.add(day, x)
        s = a.stats()
        self.assertEqual((s["obs"], s["days"]), (4, 3))                                                    # day 1 contributes ONE daily mean (0.03), not two observations
        self.assertAlmostEqual(s["mean"], (0.03 + 0.01 - 0.01) / 3)                                         # 0.01
        sd = statistics.stdev([0.03, 0.01, -0.01])                                                          # 0.02
        self.assertAlmostEqual(s["se"], sd / math.sqrt(3))
        self.assertAlmostEqual(s["t"], 0.01 / (sd / math.sqrt(3)))                                          # 0.866
        b = lab.DailyAcc()
        b.add(1, 0.1)
        b.add(2, 0.2)
        self.assertIsNone(b.stats()["t"])                                                                    # two days: no t
        self.assertIsNone(lab.DailyAcc().stats())

    def test_the_train_and_held_out_split(self):
        a = lab.DailyAcc()
        for d in (1000, 1001, 1002, 1003):
            a.add(d, 0.01)
        for d in (1500, 1501, 1502):
            a.add(d, -0.03)
        a.add(1200, 0.05)                                                                                   # a day exactly ON the cut belongs to the held-out period
        s = a.summary(1200, min_days=1)
        self.assertEqual((s["train"]["days"], s["train"]["mean_pct"]), (4, 1.0))
        self.assertEqual(s["test"]["days"], 4)
        self.assertAlmostEqual(s["test"]["mean_pct"], (5.0 - 9.0) / 4.0)                                    # (+5%, -3%, -3%, -3%) / 4 = -1.0
        self.assertEqual(s["days"], 8)
        self.assertIsNone(a.summary(None)["train"])
        self.assertIsNone(a.summary(1200)["train"])                                                         # 4 and 4 days are not enough to call a split evidence: by default each half needs 30
        self.assertIsNone(a.summary(1200)["test"])
        self.assertIsNone(a.summary(1200, min_days=5)["train"])                                             # the threshold applies to BOTH halves (train has 4)
        b = lab.DailyAcc()
        for d in (1000, 1001):
            b.add(d, 0.01)
        for d in range(1500, 1507):
            b.add(d, 0.02)
        self.assertIsNone(b.summary(1200, min_days=5)["test"])                                              # 7 test days but only 2 train days: neither half is shown
        self.assertEqual(b.summary(1200, min_days=2)["test"]["days"], 7)

    def test_wilson_intervals_by_hand(self):
        lo, hi = lab.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=3)
        self.assertAlmostEqual(hi, 0.7634, places=3)
        lo, hi = lab.wilson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.2775, places=3)
        self.assertEqual(lab.wilson(0, 0), (0.0, 1.0))
        lo, hi = lab.wilson(3, 20)
        self.assertAlmostEqual((lo, hi), (0.0524, 0.3604), places=3) if False else None
        self.assertAlmostEqual(lo, 0.0524, places=3)
        self.assertAlmostEqual(hi, 0.3604, places=3)


class TestLoading(unittest.TestCase):
    def test_missing_fields_are_replaced_by_the_close_and_bad_bars_are_skipped(self):
        c = H.fresh_db()
        c.execute("INSERT INTO market_series(symbol,day,open,high,low,close,volume) VALUES('AAA','2025-01-06',NULL,NULL,NULL,10.0,NULL)")
        c.execute("INSERT INTO market_series(symbol,day,open,high,low,close,volume) VALUES('AAA','2025-01-07',10.0,11.0,9.0,10.5,500)")
        c.execute("INSERT INTO market_series(symbol,day,open,high,low,close,volume) VALUES('AAA','2025-01-08',1,1,1,0,1)")                    # close 0: skipped
        c.execute("INSERT INTO market_series(symbol,day,open,high,low,close,volume) VALUES('AAA','not a day',1,1,1,5,1)")                     # bad date: skipped
        c.commit()
        s = lab.load_series(c)["AAA"]
        self.assertEqual(len(s), 2)
        self.assertEqual((s.o[0], s.h[0], s.l[0], s.c[0], s.v[0]), (10.0, 10.0, 10.0, 10.0, 0.0))
        self.assertEqual((s.o[1], s.h[1], s.l[1], s.c[1], s.v[1]), (10.0, 11.0, 9.0, 10.5, 500.0))

    def test_the_market_is_aligned_by_day_with_nan_where_it_has_no_bar(self):
        c = H.fresh_db()
        put_series(c, "SPY", [100, 101, 102], start=D0)
        put_series(c, "AAA", [10, 11, 12, 13], start=D0)
        ser = load(c)
        m = lab.align(ser["AAA"], ser["SPY"])
        self.assertEqual(list(m[:3]), [100.0, 101.0, 102.0])
        self.assertTrue(math.isnan(m[3]))


def stock_with_jump():
    """flat 100 for 30 days, then days 30..: 110,110,110,107,105,107,107 ... (so the 5-day move is +10% only on the three days at 110)."""
    return flat(30) + [110, 110, 110, 107, 105] + [107] * 35


class TestPostMove(unittest.TestCase):
    def test_a_bucket_by_hand(self):
        c = H.fresh_db()
        stock = stock_with_jump()
        put_series(c, "SPY", flat(len(stock)))
        put_series(c, "AAA", stock)
        res = lab.run_study(c, DEF(), "post_move", load(c), NOW)
        row = {r["label"]: r for r in res["rows"]}["rose 8% to 15%"]
        h3 = row["h"]["3"]
        # entries on the three days at 110 (r5 = +10%): forward 3 sessions = 107/110-1, 105/110-1, 107/110-1
        means = [107 / 110 - 1, 105 / 110 - 1, 107 / 110 - 1]
        self.assertEqual((h3["obs"], h3["days"]), (3, 3))
        self.assertAlmostEqual(h3["mean_pct"], round(100 * statistics.mean(means), 3), places=3)
        self.assertAlmostEqual(h3["t"], round(statistics.mean(means) / (statistics.stdev(means) / math.sqrt(3)), 2), places=2)
        self.assertEqual(row["sign"], 1)
        self.assertEqual(row["model_subtracts_pct"], 9.2)                                                   # 0.8 x the 11.5% bucket midpoint
        self.assertEqual({r["label"]: r for r in res["rows"]}["rose more than 15%"]["model_subtracts_pct"], 16.0)       # 0.8 x the 20% cap (the bucket midpoint is 22.5%, so the cap binds: uncapped it would be 18%)
        self.assertEqual({r["label"]: r for r in res["rows"]}["flat (within 3%)"]["model_subtracts_pct"], 0.0)

    def test_the_market_is_subtracted(self):
        c = H.fresh_db()
        stock = stock_with_jump()
        spy = [100 * (1.001 ** i) for i in range(len(stock))]                                                # the market rises 0.1% a day
        put_series(c, "SPY", spy)
        put_series(c, "AAA", stock)
        row = {r["label"]: r for r in lab.run_study(c, DEF(), "post_move", load(c), NOW)["rows"]}["rose 8% to 15%"]
        ab = [(107 / 110 - 1) - (spy[33] / spy[30] - 1), (105 / 110 - 1) - (spy[34] / spy[31] - 1), (107 / 110 - 1) - (spy[35] / spy[32] - 1)]
        self.assertAlmostEqual(row["h"]["3"]["mean_pct"], round(100 * statistics.mean(ab), 3), places=3)

    def test_a_day_the_market_has_no_bar_is_skipped_and_without_spy_there_is_no_study(self):
        c = H.fresh_db()
        put_series(c, "AAA", stock_with_jump())
        res = lab.run_study(c, DEF(), "post_move", load(c), NOW)
        self.assertEqual(res["status"], "no_market")
        self.assertEqual(res["rows"], [])
        put_series(c, "SPY", flat(len(stock_with_jump())), start=D0 + 33)                                    # the market only starts after the three days at 110
        row = {r["label"]: r for r in lab.run_study(c, DEF(), "post_move", load(c), NOW)["rows"]}["rose 8% to 15%"]
        self.assertIsNone(row["h"]["3"])                                                                     # no entry day has a market bar: no observation, not a wrong one

    def test_a_held_out_year_appears_when_there_is_more_than_a_year_of_data(self):
        c = H.fresh_db()
        n = 900
        stock = [100 + 5 * math.sin(i / 7.0) + 0.01 * i for i in range(n)]
        put_series(c, "SPY", [100 + 0.02 * i for i in range(n)])
        put_series(c, "AAA", stock)
        res = lab.run_study(c, DEF(), "post_move", load(c), NOW)
        cells = [r["h"]["3"] for r in res["rows"] if r["h"]["3"]]
        self.assertTrue(any(x["train"] and x["test"] for x in cells))
        for x in cells:
            if x["train"] and x["test"]:
                self.assertEqual(x["train"]["days"] + x["test"]["days"], x["days"])


def with_earnings(c, sym, i0, start=D0):
    c.execute("INSERT INTO kb_filing_events(entity_id,cik,ticker,accession,form,filed,event_type,first_known,retrieved) VALUES(?,?,?,?,?,?,?,?,?)",
              (1, 1, sym, "acc-%s-%d" % (sym, i0), "8-K", dt.date.fromordinal(start + i0).isoformat(), "results_announcement", "2026-11-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00"))
    c.commit()


class TestEarnings(unittest.TestCase):
    def build(self, rx_close_after, drift):
        """flat 100; the release day i0=40: close(39)=100, close(40)=mid, close(41)=rx_close_after; then the drift path from close(41)."""
        p = flat(40) + [(100 + rx_close_after) / 2, rx_close_after] + [rx_close_after] * 40
        for h, v in drift.items():
            p[41 + h] = v
        return p

    def test_the_reaction_bucket_the_start_of_the_drift_and_the_sign_convention(self):
        c = H.fresh_db()
        a = self.build(110, {1: 111.1, 3: 107.8, 5: 104.5, 10: 121.0})                                       # +10% reaction, then +1%, -2%, -5%, +10%
        b = self.build(90, {1: 90.9, 3: 92.7, 5: 94.5, 10: 99.0})                                            # -10% reaction, then it REBOUNDS +1%, +3%, +5%, +10%
        put_series(c, "SPY", flat(len(a)))
        put_series(c, "AAA", a)
        put_series(c, "BBB", b)
        with_earnings(c, "AAA", 40)
        with_earnings(c, "BBB", 40)
        res = lab.run_study(c, DEF(), "earnings", load(c), NOW)
        rows = {r["label"]: r for r in res["rows"]}
        up, down = rows["reaction +8% or more"]["h"], rows["reaction -8% or worse"]["h"]
        self.assertAlmostEqual(up["1"]["mean_pct"], 1.0, places=2)
        self.assertAlmostEqual(up["3"]["mean_pct"], (107.8 / 110 - 1) * 100, places=2)                       # -2.0: it faded
        self.assertAlmostEqual(up["10"]["mean_pct"], 10.0, places=2)
        self.assertAlmostEqual(down["3"]["mean_pct"], -(92.7 / 90 - 1) * 100, places=2)                      # -3.0: a rebound after a drop is a REVERSAL, so negative
        self.assertAlmostEqual(down["10"]["mean_pct"], -10.0, places=2)
        self.assertEqual(res["events"], 2)
        self.assertIsNone(rows["reaction +3% to +8%"]["h"]["3"])

    def test_the_bucket_edges(self):
        c = H.fresh_db()
        put_series(c, "SPY", flat(90))
        for s, v in (("AAA", 107.9), ("BBB", 108.1), ("CCC", 92.1), ("DDD", 91.9)):                         # either side of +8% and -8%
            put_series(c, s, flat(40) + [(100 + v) / 2, v] + [v] * 48)
            with_earnings(c, s, 40)
        rows = {r["label"]: r for r in lab.run_study(c, DEF(), "earnings", load(c), NOW)["rows"]}
        self.assertEqual({k: r["h"]["3"]["obs"] for k, r in rows.items() if r["h"]["3"]}, {"reaction +3% to +8%": 1, "reaction +8% or more": 1, "reaction -3% to -8%": 1, "reaction -8% or worse": 1})

    def test_events_too_near_the_end_or_for_unknown_stocks_are_skipped(self):
        c = H.fresh_db()
        put_series(c, "SPY", flat(80))
        put_series(c, "AAA", flat(75) + [200, 220, 220, 220, 220])
        with_earnings(c, "AAA", 74)                                                                          # no room for a 10-session drift
        with_earnings(c, "NOPE", 40)                                                                         # no prices
        self.assertEqual(lab.run_study(c, DEF(), "earnings", load(c), NOW)["events"], 0)


class TestBigDays(unittest.TestCase):
    def build(self, jump, vol_mult, close_after):
        closes = flat(30) + [100 * (1 + jump)] + [100 * (1 + jump) * close_after] * 70
        vols = [1e6] * 30 + [vol_mult * 1e6] + [1e6] * 70
        return closes, vols

    def drift_row(self, closes, vols, earnings_at=None):
        c = H.fresh_db()
        put_series(c, "SPY", flat(len(closes)))
        put_series(c, "AAA", closes, vols=vols)
        if earnings_at is not None:
            with_earnings(c, "AAA", earnings_at)
        return {r["label"]: r for r in lab.run_study(c, DEF(), "big_days", load(c), NOW)["rows"]}

    def test_a_qualifying_up_day_is_counted_in_its_own_direction(self):
        cl, v = self.build(0.06, 3.0, 0.97)                                                                  # +6% on 3x volume, then -3%
        row = self.drift_row(cl, v)["UP 5%+ on 2x volume"]["h"]["3"]
        self.assertAlmostEqual(row["mean_pct"], -3.0, places=2)                                              # in the direction of the move: it reversed
        self.assertEqual(row["obs"], 1)

    def test_a_down_day_is_normalised_so_a_rebound_is_negative(self):
        cl, v = self.build(-0.06, 3.0, 1.03)
        row = self.drift_row(cl, v)["DOWN 5%+ on 2x volume"]["h"]["3"]
        self.assertAlmostEqual(row["mean_pct"], -3.0, places=2)

    def test_a_small_move_or_light_volume_or_an_earnings_window_does_not_count(self):
        for jump, vm, earn in ((0.04, 3.0, None), (0.06, 1.5, None), (0.06, 3.0, 30), (0.06, 3.0, 31), (0.06, 3.0, 28)):
            cl, v = self.build(jump, vm, 0.97)
            rows = self.drift_row(cl, v, earn)
            self.assertIsNone(rows["UP 5%+ on 2x volume"]["h"]["3"], (jump, vm, earn))
        cl, v = self.build(0.06, 3.0, 0.97)
        self.assertIsNotNone(self.drift_row(cl, v, 20)["UP 5%+ on 2x volume"]["h"]["3"])                    # an earnings release 10 sessions earlier is outside the window


def add_pred(c, prob, hit, et, kind="forecast"):
    c.execute("INSERT INTO predictions(created,kind,subject,claim,probability,horizon_date,source,event_type) VALUES(?,?,?,?,?,?,?,?)", (iso(NOW), kind, "AAA", "abnormal_up", prob, "2026-11-10", "test", et))
    pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute("INSERT INTO outcomes(prediction_id,observed_at,outcome,realized_return,benchmark_return,abnormal_return,brier,detail_json) VALUES(?,?,?,?,?,?,?,?)", (pid, iso(NOW), hit, 0.0, 0.0, 0.0, 0.0, "{}"))


class TestCalibration(unittest.TestCase):
    def world(self):
        c = H.fresh_db()
        for i in range(10):
            add_pred(c, 0.55, 1.0 if i < 5 else 0.0, "alpha")                                                # 10 forecasts at 55%: 5 came true
        for i in range(20):
            add_pred(c, 0.95, 1.0 if i < 3 else 0.0, "beta")                                                 # 20 forecasts at 95%: 3 came true
        for i in range(4):
            add_pred(c, 0.65, 1.0, "tiny")
        add_pred(c, 0.9, 0.0, "alpha", kind="position")                                                      # not a forecast: ignored
        c.commit()
        return c

    def test_bins_intervals_and_flags_by_hand(self):
        res = lab.run_study(self.world(), DEF(), "calibration", {"SPY": lab.Series("SPY")}, NOW) if False else lab.study_calibration({}, None, DEF(), self.world())
        bins = {r["label"]: r for r in res["rows"] if r["kind"] == "bin"}
        a, b = bins["50% to 60%"], bins["90% to 100%"]
        self.assertEqual((a["n"], a["came_true_pct"], a["lo_pct"], a["hi_pct"]), (10, 50.0, 23.7, 76.3))
        self.assertEqual(a["flag"], "no evidence either way")
        self.assertEqual((b["n"], b["came_true_pct"], b["lo_pct"], b["hi_pct"]), (20, 15.0, 5.2, 36.0))
        self.assertEqual(b["flag"], "worse than a coin")
        self.assertEqual(bins["60% to 70%"]["n"], 4)

    def test_brier_and_base_rate_by_hand(self):
        s = lab.study_calibration({}, None, DEF(), self.world())["summary"]
        self.assertEqual(s["n"], 34)
        # (5 x 0.45^2 + 5 x 0.55^2) + (3 x 0.05^2 + 17 x 0.95^2) + 4 x 0.35^2 = 2.525 + 15.35 + 0.49 = 18.365 over 34
        self.assertAlmostEqual(s["brier"], round(18.365 / 34, 4), places=4)
        self.assertEqual(s["base_rate_pct"], round(100 * 12 / 34, 1))
        self.assertAlmostEqual(s["brier_always_base_rate"], round((12 / 34) * (22 / 34), 4), places=4)

    def test_event_types_need_ten_forecasts_and_small_ones_are_counted(self):
        res = lab.study_calibration({}, None, DEF(), self.world())
        types = {r["label"]: r for r in res["rows"] if r["kind"] == "event_type"}
        self.assertEqual(sorted(types), ["alpha", "beta"])                                                   # 'tiny' has 4; the position row is excluded
        self.assertEqual(types["beta"]["flag"], "worse than a coin")
        self.assertEqual(res["summary"]["event_types_under_10"], 1)

    def test_an_empty_or_missing_database_is_fine(self):
        self.assertEqual(lab.study_calibration({}, None, DEF(), H.fresh_db())["summary"]["n"], 0)
        self.assertEqual(lab.study_calibration({}, None, DEF(), None)["rows"], [])


class TestTheExitSimulation(unittest.TestCase):
    def sim(self, opens, highs, lows, closes, T=0.02, S=0.03, H_=3):
        return lab.simulate_long(opens, highs, lows, closes, 0, T, S, H_)

    def test_each_way_a_trade_can_end(self):
        o = [100, 100.5, 100]
        self.assertAlmostEqual(self.sim(o, [101, 103, 101], [99.5, 100, 99], [100, 102, 100]), 0.02)                      # target reached on day 1
        self.assertAlmostEqual(self.sim(o, [101, 101, 101], [99.5, 96.5, 99], [100, 97, 100]), -0.03)                    # stop reached on day 1
        self.assertAlmostEqual(self.sim(o, [101, 103, 101], [99.5, 96, 99], [100, 100, 100]), -0.03)                      # both reachable the same day: the STOP is assumed first
        self.assertAlmostEqual(self.sim([100, 95, 95], [101, 96, 96], [99.5, 94, 94], [100, 95, 95]), -0.05)              # gapped through the stop: fills at the open
        self.assertAlmostEqual(self.sim([100, 104, 104], [101, 105, 105], [99.5, 103, 103], [100, 104, 104]), 0.04)       # gapped over the target: fills at the open
        self.assertAlmostEqual(self.sim(o, [101, 101.5, 101], [99.5, 99.4, 99], [100, 100.5, 101]), 0.01)                 # nothing hit: the close of the last session
        self.assertAlmostEqual(self.sim([100, 100, 100], [101, 101, 101], [97.5, 99, 99], [100, 100, 100]), 0.0)         # day 0 low above the stop (97.5 > 97): no stop
        self.assertAlmostEqual(self.sim([100, 100, 100], [101, 101, 101], [96.9, 99, 99], [100, 100, 100]), -0.03)       # day 0 low through the stop: out the same day

    def test_unusable_entries(self):
        self.assertIsNone(lab.simulate_long([0, 1, 1], [1, 1, 1], [1, 1, 1], [1, 1, 1], 0, 0.02, 0.03, 3))
        self.assertIsNone(lab.simulate_long([100, 100], [100, 100], [100, 100], [100, 100], 0, 0.02, 0.03, 3))          # not enough bars

    def test_no_signal_and_a_flat_market_costs_exactly_the_round_trip(self):
        c = H.fresh_db()
        put_series(c, "SPY", flat(130))
        put_series(c, "AAA", flat(130), vols=[1e6] * 130)                                                    # $100M a day: liquid
        res = lab.run_study(c, DEF(), "exit_plans", load(c), NOW)
        self.assertEqual(res["stocks"], 1)
        for r in res["rows"]:
            self.assertEqual(r["trades"], len(range(1, 130 - 11, 3)))
            self.assertEqual(r["win_pct"], 0.0)
            self.assertAlmostEqual(r["net_per_trade_pct"], -0.3)                                              # 30 bps and nothing else
            self.assertAlmostEqual(r["signal_must_add_pct"], 0.3)
        self.assertTrue(res["rows"][0]["label"].startswith("your plan: target 2.0% / stop 3.0% / 3 sessions"))

    def test_illiquid_stocks_are_left_out(self):
        c = H.fresh_db()
        put_series(c, "SPY", flat(130))
        put_series(c, "AAA", flat(130), vols=[1000] * 130)                                                   # $100k a day
        self.assertEqual(lab.run_study(c, DEF(), "exit_plans", load(c), NOW)["rows"], [])

    def test_a_trending_market_with_a_known_path(self):
        c = H.fresh_db()
        closes = [100 * (1.01 ** i) for i in range(130)]                                                     # +1% a day: the 2% target is hit on day 2 of every trade
        put_series(c, "SPY", flat(130))
        put_series(c, "AAA", closes, opens=closes, highs=closes, lows=closes, vols=[1e6] * 130)
        r = {x["label"].replace("your plan: ", ""): x for x in lab.run_study(c, DEF(), "exit_plans", load(c), NOW)["rows"]}["target 2.0% / stop 3.0% / 3 sessions"]
        self.assertEqual(r["win_pct"], 100.0)
        self.assertAlmostEqual(r["net_per_trade_pct"], 100 * (1.01 ** 2 - 1) - 0.3, places=2)                 # the 2% target is gapped OVER at the open of day 3: it fills at 1.01^2 - 1 = 2.01%


class TestThePersistedRun(unittest.TestCase):
    def synth(self):
        c = H.fresh_db()
        put_series(c, "SPY", [100 + 0.02 * i for i in range(130)])
        put_series(c, "AAA", [100 + 3 * math.sin(i / 5.0) for i in range(130)], vols=[1e6] * 130)
        return c

    def test_a_full_run_is_saved_and_the_report_reads_it(self):
        c = self.synth()
        run = lab.run_all(c, DEF(), NOW)
        self.assertEqual(run["status"], "complete")
        self.assertEqual(sorted(run["studies"]), sorted(lab.STUDY_ORDER))
        self.assertEqual((run["symbols"], run["rows"]), (2, 260))
        self.assertEqual((run["data_from"], run["data_to"]), (dt.date.fromordinal(D0).isoformat(), dt.date.fromordinal(D0 + 129).isoformat()))
        text = lab.format_report(lab.latest_run(c))
        for need in ("RESEARCH LAB", "READ THIS FIRST", "priced in" if False else "5-day move", "NO signal"):
            self.assertIn(need, text)
        self.assertIn("No research run yet", lab.format_report(None))

    def test_one_study_can_be_run_alone(self):
        run = lab.run_all(self.synth(), DEF(), NOW, only=["calibration"])
        self.assertEqual(list(run["studies"]), ["calibration"])

    def test_the_per_cycle_runner_does_one_study_at_a_time_then_waits_a_week(self):
        c = self.synth()
        cfg = DEF()
        closed = lambda n: "CLOSED"  # noqa: E731
        seen = []
        for _ in range(len(lab.STUDY_ORDER)):
            r = lab.maybe_run(c, cfg, NOW, session=closed)
            seen.append((r["status"], r.get("study")))
        self.assertEqual(seen, [("ran", n) for n in lab.STUDY_ORDER])
        run = lab.latest_run(c)
        self.assertEqual(run["status"], "complete")
        self.assertEqual(lab.maybe_run(c, cfg, NOW + dt.timedelta(days=3), session=closed)["status"], "current")
        r = lab.maybe_run(c, cfg, NOW + dt.timedelta(days=8), session=closed)                                 # a week later a new run starts
        self.assertEqual((r["status"], r["study"]), ("ran", "post_move"))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0], 2)

    def test_it_never_runs_while_the_market_is_open_or_when_switched_off(self):
        c = self.synth()
        cfg = DEF()
        self.assertEqual(lab.maybe_run(c, cfg, NOW, session=lambda n: "REGULAR")["status"], "waiting")
        self.assertIsNone(lab.latest_run(c))
        cfg["research"]["enabled"] = False
        self.assertEqual(lab.maybe_run(c, cfg, NOW, session=lambda n: "CLOSED")["status"], "disabled")

    def test_a_study_that_is_too_slow_is_retried_then_given_up_and_the_run_still_completes(self):
        c = self.synth()
        cfg = DEF()
        cfg["research"]["max_seconds"] = 5.0
        ticks = iter(range(0, 10 ** 6, 100))                                                                  # every clock reading is 100 s after the last: always over budget
        closed = lambda n: "CLOSED"  # noqa: E731
        r = lab.maybe_run(c, cfg, NOW, clock=lambda: next(ticks), session=closed)
        self.assertEqual((r["status"], r["study"]), ("timeout", "post_move"))
        e = lab.latest_run(c)["studies"]["post_move"]
        self.assertEqual((e["status"], e["tries"]), ("timeout", 1))
        self.assertEqual(lab.maybe_run(c, cfg, NOW + dt.timedelta(hours=1), clock=lambda: 0.0, session=closed)["study"], "earnings")        # it moves on; the retry is 6 hours away
        for k in range(2, 4):
            lab._save(c, lab.latest_run(c)["id"], dict(lab.latest_run(c)["studies"], post_move=dict(e, tries=k, retry_after=iso(NOW))))                # simulate the 2nd and 3rd timeouts being due
        for step in range(12):
            lab.maybe_run(c, cfg, NOW + dt.timedelta(hours=7 + step), clock=lambda: 0.0, session=closed)
        run = lab.latest_run(c)
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["studies"]["post_move"]["status"], "timeout")                                    # given up, and not holding the run open

    def test_corrupted_saved_runs_are_survived_by_the_report_and_the_page(self):
        c = H.fresh_db()
        for studies in ("x", [], None, {"post_move": "junk"}, {"post_move": 5}, {"post_move": None}, {"post_move": {"rows": "x"}}, {"post_move": {"rows": [None, "x", {"label": 1, "h": "x"}, {"label": "a", "h": {"1": "j"}}]}},
                        {"calibration": {"rows": [{"kind": "bin", "label": "a"}]}}, {"exit_plans": {"rows": [{"label": "a", "trades": "x"}]}}):
            c.execute("INSERT INTO research_runs(started,status,studies_json) VALUES(?,?,?)", (iso(NOW), "complete", json.dumps(studies)))
            self.assertIsInstance(lab.format_report(lab.latest_run(c)), str)
            self.assertIn("Research lab", dashboard.research_section(c, DEF()))
        c.execute("INSERT INTO research_runs(started,status,studies_json) VALUES(?,?,?)", (iso(NOW), "complete", "not json {"))
        self.assertEqual(lab.latest_run(c)["studies"], {})

    def test_junk_never_reaches_the_statistics(self):
        a = lab.DailyAcc()
        for day, x in ((1, float("nan")), (1, float("inf")), (1, "x"), (1, None), (1, True), ("d", 0.1), (None, 0.1), (2, 0.05)):
            a.add(day, x)
        self.assertEqual(a.stats()["obs"], 1)
        self.assertIsNone(a.summary("x")["train"])                                                          # junk parameters are ignored, not raised
        self.assertIsNone(a.summary(True, "many")["test"])
        self.assertEqual(lab.wilson("x", "y"), (0.0, 1.0))
        lo, hi = lab.wilson(50, 10)                                                                          # more successes than trials is clamped, not a math error
        self.assertAlmostEqual(hi, 1.0)
        self.assertIsNone(lab.simulate_long([1, 1], [1, 1], [1, 1], [1, 1], -1, 0.02, 0.03, 1))
        self.assertIsNone(lab.simulate_long([1, 1], [1, 1], [1, 1], [1, 1], 0, 0.02, 0.03, 0))
        self.assertEqual(lab._cell({"mean_pct": "x"}), "n/a")
        self.assertEqual(history._int(float("inf"), 5), 5)
        self.assertEqual(history.range_for(float("inf")), "5y")

    def test_a_broken_database_returns_an_error_instead_of_raising(self):
        c = H.fresh_db()
        c.execute("DROP TABLE research_runs")
        self.assertEqual(lab.maybe_run(c, DEF(), NOW, session=lambda n: "CLOSED")["status"], "error")
        self.assertIsNone(lab.latest_run(c))


def chart_payload(first_ord, n, base=100.0):
    ts, cl = [], []
    for i in range(n):
        d = dt.date.fromordinal(first_ord + i)
        ts.append(int(dt.datetime(d.year, d.month, d.day, 14, 30, tzinfo=dt.timezone.utc).timestamp()))
        cl.append(base + i * 0.1)
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"open": cl, "high": [x + 0.5 for x in cl], "low": [x - 0.5 for x in cl], "close": cl, "volume": [1e6] * n}]}}], "error": None}}


class Fetcher:
    def __init__(self, n=300, fail=None):
        self.calls, self.n, self.fail = [], n, fail or {}

    def __call__(self, url, headers=None):
        sym = url.split("/chart/")[1].split("?")[0]
        self.calls.append((sym, re.search(r"range=([0-9a-z]+)", url).group(1)))
        if sym in self.fail:
            raise self.fail[sym]
        return chart_payload(D0, self.n), None


class TestHistoryBackfill(unittest.TestCase):
    def world(self):
        c = H.fresh_db()
        cfg = DEF()
        cfg["history"].update(enabled=True)
        put_series(c, "ZZZ", flat(5))
        put_series(c, "AAA", flat(5))
        for i in range(3):
            c.execute("INSERT INTO decisions(created,ticker,action,status,reason) VALUES(?,?,?,?,?)", (iso(NOW), "AAA", "BUY", "REJECTED", "{}"))
        c.execute("INSERT INTO decisions(created,ticker,action,status,reason) VALUES(?,?,?,?,?)", (iso(NOW), "MMM", "BUY", "REJECTED", "{}"))
        c.commit()
        return c, cfg

    def test_the_order_is_the_benchmark_then_the_stocks_the_bot_decides_on_then_the_rest(self):
        c, cfg = self.world()
        self.assertEqual(history.wanted_symbols(c, cfg), ["SPY", "AAA", "MMM", "ZZZ"])
        cfg["history"]["max_symbols"] = 2
        self.assertEqual(history.wanted_symbols(c, cfg), ["SPY", "AAA"])

    def test_the_chart_range_follows_the_years(self):
        self.assertEqual([history.range_for(y) for y in (1, 2, 3, 5, 6, 10, 11, "x", None)], ["1y", "2y", "5y", "5y", "10y", "10y", "max", "5y", "5y"])

    def test_a_backfill_stores_the_bars_records_the_status_and_is_not_repeated(self):
        c, cfg = self.world()
        f = Fetcher()
        r = history.run(c, cfg, NOW, fetch=f)
        self.assertEqual((r["status"], r["fetched"], r["failed"]), ("ok", 3, 0))                              # symbols_per_cycle = 3
        self.assertEqual([x[0] for x in f.calls], ["SPY", "AAA", "MMM"])
        self.assertEqual({x[1] for x in f.calls}, {"5y"})
        st = c.execute("SELECT * FROM history_status WHERE symbol='SPY'").fetchone()
        self.assertEqual((st["status"], st["years"], st["rows"], st["first_day"], st["tries"]), ("done", 5, 300, dt.date.fromordinal(D0).isoformat(), 0))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM market_series WHERE symbol='SPY'").fetchone()[0], 300)
        self.assertEqual(history.pending(c, cfg, NOW), ["ZZZ"])
        r2 = history.run(c, cfg, NOW, fetch=f)
        self.assertEqual((r2["fetched"], r2["remaining"]), (1, 0))
        self.assertEqual(history.run(c, cfg, NOW, fetch=f)["fetched"], 0)
        self.assertEqual(len(f.calls), 4)                                                                    # nothing was fetched twice
        history.backfill_one(c, "SPY", 5, NOW, Fetcher())                                                    # an explicit repeat overwrites with the same bars
        self.assertEqual(c.execute("SELECT COUNT(*) FROM market_series WHERE symbol='SPY'").fetchone()[0], 300)

    def test_asking_for_more_years_fetches_again(self):
        c, cfg = self.world()
        for _ in range(2):
            history.run(c, cfg, NOW, fetch=Fetcher())
        self.assertEqual(history.pending(c, cfg, NOW), [])
        cfg["history"]["years"] = 10
        self.assertEqual(history.pending(c, cfg, NOW), ["SPY", "AAA", "MMM", "ZZZ"])
        f = Fetcher()
        history.run(c, cfg, NOW, fetch=f)
        self.assertEqual({x[1] for x in f.calls}, {"10y"})

    def test_a_failure_backs_off_6_12_24_hours_and_a_week_after_five(self):
        c, cfg = self.world()
        cfg["history"]["symbols_per_cycle"] = 1
        boom = {"SPY": net.FetchError("HTTP 500")}
        t = NOW
        waits = []
        for k in range(1, 7):
            history.run(c, cfg, t, fetch=Fetcher(fail=boom))
            st = c.execute("SELECT tries,next_try,status FROM history_status WHERE symbol='SPY'").fetchone()
            waits.append((st["tries"], round((parse_ts(st["next_try"]) - t).total_seconds() / 3600)))
            self.assertEqual(st["status"], "failed")
            self.assertNotIn("SPY", history.pending(c, cfg, t))                                              # not retried before its time
            t = parse_ts(st["next_try"]) + dt.timedelta(minutes=1)
        self.assertEqual(waits, [(1, 6), (2, 12), (3, 24), (4, 48), (5, 168), (6, 168)])
        self.assertIn("SPY", history.pending(c, cfg, t))
        self.assertIn("FetchError", c.execute("SELECT last_error FROM history_status WHERE symbol='SPY'").fetchone()[0])
        history.backfill_one(c, "SPY", 5, t, Fetcher())
        self.assertEqual(c.execute("SELECT tries,status FROM history_status WHERE symbol='SPY'").fetchone()[:], (0, "done"))        # a success clears the failures

    def test_a_rate_limit_stops_the_cycle_and_one_bad_symbol_does_not_stop_the_others(self):
        c, cfg = self.world()
        r = history.run(c, cfg, NOW, fetch=Fetcher(fail={"AAA": net.FetchError("HTTP 429")}))
        self.assertEqual((r["fetched"], r["failed"]), (1, 1))                                                 # SPY ok, AAA rate-limited: MMM was NOT attempted
        self.assertIsNone(c.execute("SELECT 1 FROM history_status WHERE symbol='MMM'").fetchone())
        c2, cfg2 = self.world()
        r2 = history.run(c2, cfg2, NOW, fetch=Fetcher(fail={"AAA": net.FetchError("HTTP 404")}))
        self.assertEqual((r2["fetched"], r2["failed"]), (2, 1))                                               # a 404 is that symbol's problem only

    def test_the_time_budget_and_the_switch(self):
        c, cfg = self.world()
        ticks = iter([0.0, 0.0, 99.0, 99.0, 99.0])
        r = history.run(c, cfg, NOW, fetch=Fetcher(), clock=lambda: next(ticks))
        self.assertEqual(r["fetched"], 1)                                                                     # out of time after the first
        f = Fetcher()
        cfg["history"]["enabled"] = False
        self.assertEqual(history.run(c, cfg, NOW, fetch=f)["status"], "disabled")
        self.assertEqual(f.calls, [])

    def test_garbage_from_the_feed_is_a_recorded_failure_never_an_exception(self):
        c, cfg = self.world()
        for payload in ({}, {"chart": None}, {"chart": {"result": [None], "error": None}}, {"chart": {"result": [{"timestamp": [1], "indicators": {"quote": [{"close": ["x"]}]}}]}}, "text", None):
            r = history.backfill_one(c, "SPY", 5, NOW, lambda url, headers=None, p=payload: (p, None))
            self.assertFalse(r["ok"])
        self.assertEqual(c.execute("SELECT status FROM history_status WHERE symbol='SPY'").fetchone()[0], "failed")

    def test_the_command_line_backfill_pauses_between_requests(self):
        c, cfg = self.world()
        slept = []
        r = history.backfill_now(c, cfg, limit=2, delay=1.5, fetch=Fetcher(), sleep=slept.append, now=NOW)
        self.assertEqual((r["fetched"], r["remaining"]), (2, 2))
        self.assertEqual(slept, [1.5, 1.5])

    def test_coverage(self):
        c, cfg = self.world()
        history.run(c, cfg, NOW, fetch=Fetcher(fail={"AAA": net.FetchError("HTTP 500")}))
        cov = history.coverage(c, cfg)
        self.assertEqual((cov["symbols_wanted"], cov["done"], cov["failed"], cov["pending"]), (4, 2, 1, 1))
        self.assertEqual(cov["median_first_day"], dt.date.fromordinal(D0).isoformat())
        self.assertEqual(cov["failures"][0][0], "AAA")


class TestSurfaces(unittest.TestCase):
    def test_the_tables_exist_and_arrive_by_migration_on_an_existing_database(self):
        d = tempfile.mkdtemp(prefix="pa-mig-", dir="/tmp")
        path = d + "/t.db"
        c = db.open_db(path)
        c.execute("DROP TABLE history_status")
        c.execute("DROP TABLE research_runs")
        c.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
        c.commit()
        c.close()
        c2 = db.open_db(path)
        self.assertEqual(c2.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))
        self.assertGreaterEqual(db.SCHEMA_VERSION, 10)
        for t in ("history_status", "research_runs"):
            self.assertEqual(c2.execute("SELECT COUNT(*) FROM sqlite_master WHERE name=?", (t,)).fetchone()[0], 1)

    def test_settings_defaults_bounds_layout_and_the_fingerprint(self):
        d = DEF()
        self.assertEqual(d["history"], {"enabled": True, "years": 5, "symbols_per_cycle": 3, "max_seconds": 12.0, "max_symbols": 500})
        self.assertEqual(d["research"], {"enabled": True, "rerun_days": 7, "max_seconds": 60.0})
        f = {x.path: x for x in S.FIELDS}
        for p in ("history.enabled", "history.years", "history.symbols_per_cycle", "history.max_seconds", "research.enabled", "research.rerun_days", "research.max_seconds"):
            self.assertIn(p, f)
        self.assertEqual((f["history.years"].lo, f["history.years"].hi), (1, 10))
        self.assertEqual(U.layout_problems(), [])
        cfg = H.cfg()
        base = stamp.fingerprint(cfg)
        cfg["history"]["years"] = 9
        cfg["research"]["enabled"] = True
        self.assertEqual(stamp.fingerprint(cfg), base)                                                       # observation only: never part of the strategy

    def test_the_dashboard_section_empty_and_populated_and_on_a_read_only_connection(self):
        empty = re.sub(r"<[^>]+>", " ", dashboard.render(H.fresh_db(), DEF(), "intelligence", {}, {"authed": True}))
        self.assertIn("Research lab", empty)
        self.assertIn("No study has run yet", empty)
        d = tempfile.mkdtemp(prefix="pa-dash-", dir="/tmp")
        path = d + "/t.db"
        c = db.open_db(path)
        put_series(c, "SPY", [100 + 0.02 * i for i in range(130)])
        put_series(c, "AAA", [100 + 3 * math.sin(i / 5.0) for i in range(130)])
        lab.run_all(c, DEF(), NOW)
        c.commit()
        ro = db.connect(path, readonly=True)
        page = re.sub(r"<[^>]+>", " ", dashboard.render(ro, DEF(), "intelligence", {}, {"authed": True}))
        for need in ("Research lab", "What stocks do after a 5-day move", "Forecast subtracts", "Do the forecast probabilities come true", "What a stop-and-target plan earns", "Read this first", "A signal must add"):
            self.assertIn(need, page)

    def test_the_command_runs_through_the_real_entry_point(self):
        d = tempfile.mkdtemp(prefix="pa-research-", dir="/tmp")
        os.makedirs(d + "/cfg")
        os.makedirs(d + "/state")
        json.dump({"AAA": {"name": "Alpha Corp", "cik": 1, "exchange": "NYSE", "aliases": []}}, open(d + "/cfg/watchlist.json", "w"))
        env = dict(os.environ, PAPER_AGENT_CONFIG_DIR=d + "/cfg", PAPER_AGENT_STATE_DIR=d + "/state", PAPER_AGENT_DB=d + "/state/paper-agent.db", PYTHONDONTWRITEBYTECODE="1")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for args, needle in ((("research", "status"), "history"), (("research", "run", "--study", "calibration"), "RESEARCH LAB"), (("research",), "RESEARCH LAB"), (("research", "history", "--limit", "0"), "fetched")):
            r = subprocess.run([sys.executable, "-m", "paper_agent"] + list(args), cwd=root, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, (args, r.stdout[-200:], r.stderr[-300:]))
            self.assertIn(needle, r.stdout, args)

    def test_a_limit_of_zero_fetches_nothing_and_an_omitted_limit_means_fifty(self):
        import types
        from unittest import mock
        seen = []
        with mock.patch.object(history, "backfill_now", lambda conn, cfg, limit, *a, **k: seen.append(limit) or {"fetched": 0}):
            for lim, want in ((0, 0), (7, 7), (None, 50), ("x", 50), ("12", 12)):
                lab.run_cli(types.SimpleNamespace(what="history", limit=lim), H.fresh_db(), DEF())
                self.assertEqual(seen[-1], want, lim)

    def test_the_real_command_with_a_limit_of_zero_returns_at_once_having_fetched_nothing(self):
        import time
        d = tempfile.mkdtemp(prefix="pa-research0-", dir="/tmp")
        os.makedirs(d + "/cfg")
        os.makedirs(d + "/state")
        json.dump({"AAA": {"name": "Alpha Corp", "cik": 1, "exchange": "NYSE", "aliases": []}}, open(d + "/cfg/watchlist.json", "w"))
        env = dict(os.environ, PAPER_AGENT_CONFIG_DIR=d + "/cfg", PAPER_AGENT_STATE_DIR=d + "/state", PAPER_AGENT_DB=d + "/state/paper-agent.db", PYTHONDONTWRITEBYTECODE="1")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        t0 = time.time()
        r = subprocess.run([sys.executable, "-m", "paper_agent", "research", "history", "--limit", "0"], cwd=root, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        out = json.loads(r.stdout)
        self.assertEqual((out["fetched"], out["failed"], out["errors"]), (0, 0, []))
        self.assertLess(time.time() - t0, 20)                                                               # it used to take 100 s trying 50 stocks

    def test_report_and_status_are_read_only_commands(self):
        import types
        from paper_agent import kbcli
        for what, ro in (("report", True), ("status", True), (None, True), ("run", False), ("history", False)):
            self.assertEqual(kbcli.is_read_only(types.SimpleNamespace(cmd="research", what=what)), ro, what)


class TestInTheCycle(KI.KbEnv):
    def test_both_stages_run_in_a_real_cycle_make_no_error_and_change_no_decision(self):
        over = {"history": {"enabled": True, "symbols_per_cycle": 2}, "research": {"enabled": True}}
        off, e1, rep_off = self.morning_kb()
        on, e2, rep_on = self.morning_kb(cfg_over=over)
        self.assertEqual((e1, e2), ([], []))
        self.assertEqual(self.trades(off), self.trades(on))                                                   # observation only
        self.assertGreaterEqual(on.execute("SELECT COUNT(*) FROM history_status WHERE status='done'").fetchone()[0], 2)               # the history stage really fetched
        self.assertEqual(on.execute("SELECT status FROM research_runs ORDER BY id DESC LIMIT 1").fetchone()[0], "complete")           # and the lab really ran every study
        self.assertEqual(off.execute("SELECT COUNT(*) FROM history_status").fetchone()[0], 0)                                         # switched off, neither does anything


if __name__ == "__main__":
    unittest.main()
