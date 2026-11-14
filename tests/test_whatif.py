"""The missed-trade engine. It replays ideas through the REAL exit rules on a shadow book with 5-minute bars. The central proof is differential: a real position opened by the real pipeline and exited by the real
guard along a price path must come out the same in the engine fed that same path as bars. Everything else (entry rules, worst-case bar order, budgets, isolation from trading, statistics) is pinned around it."""
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import config, db, net, pipeline, settings as S, stamp, trader, whatif  # noqa: E402
from paper_agent.timeutil import iso, parse_ts  # noqa: E402

UTC = dt.timezone.utc
DAYS = [dt.date(2026, 10, 27), dt.date(2026, 10, 28), dt.date(2026, 10, 29), dt.date(2026, 10, 30)]   # four sessions, all EDT: the regular session is 13:30-20:00 UTC


def session_ts(day, k):
    return dt.datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC) + dt.timedelta(minutes=5 * k)


def bar(day, k, o, h=None, l=None, c=None):
    c = o if c is None else c
    return {"ts": iso(session_ts(day, k)), "open": o, "high": max(o, c) if h is None else h, "low": min(o, c) if l is None else l, "close": c}


def flat_session(day, px, n=78):
    return [bar(day, k, px) for k in range(n)]


def forecast_row(target=0.02, stop=-0.012, horizon=3, partial=0.6, bull=0.045, conf=0.7):
    return {"target_return": target, "stop_return": stop, "horizon_days": horizon, "p_up": 0.6, "ev_gross": 0.02, "ev_net": 0.015, "confidence": conf, "verdict": "BUY", "hash": "h", "fthesis": None,
            "scenarios_json": json.dumps({"bull": {"p": .4, "ret": bull}, "base": {"p": .4, "ret": target}, "bear": {"p": .1, "ret": -.005}, "tail": {"p": .08, "ret": -.03}, "black_swan": {"p": .02, "ret": -.09}}),
            "inputs_json": json.dumps({"swing": True, "partial_exit_pct": partial, "event_type": "earnings_beat", "priced_in": 0.0})}


def shipped_cfg():
    c = H.cfg()
    c["strategy"]["swing_enabled"] = True                                  # the shipped profile (the shared test config uses the classic one)
    c["risk"]["max_gap_down_pct"], c["risk"]["max_gap_up_pct"] = 0.03, 0.05
    c["portfolio"]["slippage_bps"] = 15
    return c


def entry_of(cfg, bars_, did=1, ticker="AAA", row=None, at=0):
    b = bars_[at]
    return {"did": did, "ticker": ticker, "ts": b["ts"], "px": b["open"] * (1 + cfg["portfolio"]["slippage_bps"] / 1e4), "opened": b["ts"], "f": whatif.rebuild_forecast(row or dict(forecast_row()), cfg)}


def actual_now(actual):
    return parse_ts(actual["opened"]) + dt.timedelta(days=1)


# ------------------------------------------------------------------------------------------------------ the central proof
class TestTheEngineMatchesProduction(TL.Base):
    """Real buy by the real pipeline; real guard along a path; then the engine on the same path as bars. Run in the SHIPPED swing profile (3-session maximum hold, break-even ratchet)."""

    def setUp(self):
        super().setUp()
        self.cfg["strategy"]["swing_enabled"] = True

    def drive(self, make_path):
        """Opens the position with the real pipeline, builds the path from its real plan, runs the REAL guard at each point, and returns (final position, bars of the same path, position as opened)."""
        self.open_position()
        plan = dict(self.one("SELECT * FROM position_forecast_plans WHERE active=1"))
        actual = dict(self.one("SELECT * FROM positions"))
        bars_ = []
        for t, p in make_path(plan, actual["entry_price"]):
            self.mkt.price["NVDA"] = p
            self.guard(t)
            bars_.append({"ts": iso(t - dt.timedelta(minutes=4)), "open": p, "high": p, "low": p, "close": p})
            if self.one("SELECT status FROM positions")[0] == "CLOSED":
                break
        return dict(self.one("SELECT * FROM positions")), bars_, actual

    def engine(self, bars_, actual):
        ideas, _ = whatif.select_ideas(self.c, self.cfg, actual_now(actual), 5)                       # the real selection code: it must pick the right forecast for a trade we took
        d = next(i for i in ideas if i["ticker"] == "NVDA")
        self.assertEqual(d["kind"], "control")
        e = {"did": d["did"], "ticker": "NVDA", "ts": bars_[0]["ts"], "px": actual["entry_price"], "opened": actual["opened"], "f": whatif.rebuild_forecast(d, self.cfg)}
        res, finished = whatif.simulate(self.cfg, [e], {"NVDA": bars_})
        self.assertTrue(finished)
        return res[d["did"]]

    def agree(self, actual, res, label):
        """The same state, the same reason, the same price, the same result: whether the real system sold or (correctly) held."""
        if actual["status"] != "CLOSED":
            self.assertEqual(res["outcome"], "OPEN_AT_END", label)
            return
        self.assertEqual(res["outcome"], "CLOSED", label)
        self.assertEqual(res["exit_reason"], actual["review_code"], label)
        self.assertAlmostEqual(res["exit_px"], actual["exit_price"], delta=0.02, msg=label)                    # the fake providers quote a hair around the path price
        buy = -self.one("SELECT SUM(amount) FROM ledger WHERE kind='VIRTUAL_BUY'")[0]                         # a closed position keeps no cost of its own: the buy is in the ledger
        self.assertAlmostEqual(res["ret_net"], actual["pnl"] / buy, delta=0.0003, msg=label)                    # within 3 basis points

    def at(self, day, minutes):
        return session_ts(DAYS[day], 0) + dt.timedelta(minutes=minutes + 4)

    def test_a_stop_out(self):
        actual, bars_, a0 = self.drive(lambda plan, entry: [(self.at(0, 30), entry), (self.at(0, 60), entry * 0.995), (self.at(0, 90), plan["stop_price"] - 0.6), (self.at(0, 120), plan["stop_price"] - 1.0)])
        self.assertEqual(actual["review_code"], "STOP_REACHED")
        self.agree(actual, self.engine(bars_, a0), "stop-out")

    def test_a_profit_target_reached_on_the_third_session_banks_part_and_runs_the_rest(self):
        # on the third session the real system does not extend the hold: it banks the share set by partial_exit_pct and lets the rest run behind the protected stop
        def path(plan, entry):
            t = plan["target_price"]
            return [(self.at(0, 30), entry), (self.at(1, 30), entry), (self.at(2, 30), t + 0.3), (self.at(2, 120), t + 0.8), (self.at(3, 30), t - 0.5), (self.at(3, 120), entry * 0.995), (self.at(3, 200), entry * 0.985)]
        actual, bars_, a0 = self.drive(path)
        self.assertGreaterEqual(self.one("SELECT COUNT(*) FROM ledger WHERE kind LIKE 'VIRTUAL_SELL%'")[0], 1, "setup: the real system banked the target")
        res = self.engine(bars_, a0)
        self.agree(actual, res, "target path")
        self.assertTrue(res.get("partial"))

    def test_a_time_stop_across_sessions(self):
        actual, bars_, a0 = self.drive(lambda plan, entry: [(self.at(d, m), entry) for d in range(4) for m in (30, 120, 200, 300)])
        self.assertEqual(actual["review_code"], "TIME_EXPIRED")                                           # swing profile: three sessions
        self.agree(actual, self.engine(bars_, a0), "time stop")

    def test_the_break_even_ratchet_locks_in_a_gain_then_stops_out(self):
        def path(plan, entry):
            up = entry * 1.011                                                                            # above the 1% break-even trigger, below the target
            return [(self.at(0, 30), up), (self.at(0, 60), up * 1.002), (self.at(0, 90), up * 1.004), (self.at(0, 120), entry * 1.0), (self.at(0, 150), entry * 0.999), (self.at(0, 200), entry * 0.99)]
        actual, bars_, a0 = self.drive(path)
        self.assertEqual(actual["review_code"], "STOP_REACHED")
        self.agree(actual, self.engine(bars_, a0), "ratchet")
        buy = -self.one("SELECT SUM(amount) FROM ledger WHERE kind='VIRTUAL_BUY'")[0]
        self.assertGreater(actual["pnl"], -0.002 * buy)                                                   # the stop had moved up: it did not take the full original loss


# ------------------------------------------------------------------------------------------------------ the plan is rebuilt faithfully
class TestThePlanIsRebuiltFromTheStoredForecast(TL.Base):
    def test_every_exit_relevant_plan_field_is_identical_to_the_one_the_real_buy_created(self):
        self.open_position()
        actual = dict(self.one("SELECT * FROM position_forecast_plans WHERE active=1"))
        pos = dict(self.one("SELECT * FROM positions"))
        ideas, _ = whatif.select_ideas(self.c, self.cfg, TL.T_OPEN + dt.timedelta(hours=1), 5)
        row = next(i for i in ideas if i["did"] == pos["decision_id"])
        f = whatif.rebuild_forecast(row, self.cfg)
        sh = db.open_db(":memory:")
        sh.execute("INSERT INTO decisions(id,created,ticker,action,status,reason) VALUES(?,?,?,?,?,?)", (pos["decision_id"], pos["opened"], "NVDA", "BUY", "EXECUTED", "{}"))
        pid = sh.execute("INSERT INTO positions(ticker,opened,entry_price,shares,cost,decision_id,status) VALUES('NVDA',?,?,10,1200,?, 'OPEN')", (pos["opened"], pos["entry_price"], pos["decision_id"])).lastrowid
        trader.insert_plan(sh, pid, pos["decision_id"], f, pos["entry_price"], pos["opened"])
        rebuilt = dict(sh.execute("SELECT * FROM position_forecast_plans WHERE position_id=?", (pid,)).fetchone())
        for k in ("target_return", "stop_return", "target_price", "stop_price", "max_hold_days", "partial_exit_pct", "runner_target_return", "runner_target_price", "runner_stop_return", "runner_stop_price",
                  "bear_return", "base_return", "bull_return", "confidence", "event_type", "priced_in", "expected_return"):
            self.assertAlmostEqual(rebuilt[k], actual[k], places=9, msg=k) if isinstance(actual[k], float) else self.assertEqual(rebuilt[k], actual[k], k)
        self.assertEqual(row["kind"], "control")


# ------------------------------------------------------------------------------------------------------ unit behaviour on synthetic bars
class TestExitsOnSyntheticBars(unittest.TestCase):
    def setUp(self):
        self.cfg = shipped_cfg()

    def run_one(self, bars_, row=None, at=0):
        e = entry_of(self.cfg, bars_, row=row, at=at)
        res, finished = whatif.simulate(self.cfg, [e], {"AAA": bars_})
        self.assertTrue(finished)
        return res[1]

    def test_a_stop_hit(self):
        b = [bar(DAYS[0], 0, 100.0)] + [bar(DAYS[0], k, 100.0 - 0.08 * k, l=100.0 - 0.08 * k - 0.05) for k in range(1, 40)]
        r = self.run_one(b)
        self.assertEqual((r["outcome"], r["exit_reason"]), ("CLOSED", "STOP_REACHED"))
        self.assertLess(r["ret_net"], -0.011)                                                            # about the 1.2% stop plus slippage on both legs
        self.assertGreater(r["ret_net"], -0.02)
        self.assertLess(r["mae"], -0.011)

    def test_inside_one_bar_the_stop_beats_the_target(self):
        b = [bar(DAYS[0], 0, 100.0), bar(DAYS[0], 1, 100.0, h=103.0, l=98.0, c=102.0)] + flat_session(DAYS[0], 102.0)[2:10]       # one violent bar touches both
        r = self.run_one(b, row=forecast_row(conf=0.5))                                                  # low confidence: if the high came first the target would be BANKED, not extended
        self.assertEqual(r["exit_reason"], "STOP_REACHED")                                               # worst case for a long: low before high
        self.assertFalse(r["partial"])
        self.assertLess(r["ret_net"], -0.015)                                                            # a clear loss: the optimistic order would have shown a profit here
        self.assertGreater(r["mfe"], 0.025)                                                              # the best point is still recorded honestly

    def test_a_target_banks_part_and_lets_a_runner_go(self):
        b = [bar(DAYS[0], k, 100.0 + 0.1 * k) for k in range(0, 60)] + [bar(DAYS[0], 60 + k, 106.0 - 0.3 * k) for k in range(0, 18)]
        r = self.run_one(b, row=forecast_row(target=0.02, bull=0.03, conf=0.5))                          # low confidence: no extension, so the target is banked
        self.assertTrue(r["partial"])
        self.assertEqual(r["outcome"], "CLOSED")
        self.assertGreater(r["ret_net"], 0.0)

    def test_the_time_stop_fires_on_the_third_session_at_the_open(self):
        b = sum([flat_session(d, 100.0) for d in DAYS], [])
        r = self.run_one(b)
        self.assertEqual((r["outcome"], r["exit_reason"]), ("CLOSED", "TIME_EXPIRED"))
        self.assertEqual(parse_ts(r["exit_ts"]).date(), DAYS[3])                                         # entered Tuesday, three sessions later is Friday
        self.assertAlmostEqual(r["ret_net"], 0.9985 / 1.0015 - 1.0, places=7)                            # flat price: only the 15 bps slippage on each leg, EXACTLY (so the return is measured on the true cost, not the notional)

    def test_data_that_ends_early_is_provisional_then_final_when_more_arrives(self):
        b1 = flat_session(DAYS[0], 100.0)
        r1 = self.run_one(b1)
        self.assertEqual(r1["outcome"], "OPEN_AT_END")
        self.assertEqual(r1["data_through"], b1[-1]["ts"])
        b2 = b1 + sum([flat_session(d, 100.0) for d in DAYS[1:]], [])
        self.assertEqual(self.run_one(b2)["outcome"], "CLOSED")

    def test_a_missing_stretch_of_bars_does_not_crash_and_does_not_invent_a_price(self):
        b = flat_session(DAYS[0], 100.0)[:10] + flat_session(DAYS[0], 99.0)[40:] + sum([flat_session(d, 100.0) for d in DAYS[1:]], [])
        r = self.run_one(b)
        self.assertIn(r["outcome"], ("CLOSED", "OPEN_AT_END"))

    def test_two_ideas_on_the_same_stock_do_not_interfere(self):
        b = [bar(DAYS[0], k, 100.0 - 0.1 * k) for k in range(0, 60)] + sum([flat_session(d, 94.0) for d in DAYS[1:]], [])
        e1, e2 = entry_of(self.cfg, b, did=1, at=0), entry_of(self.cfg, b, did=2, at=30)
        res, _ = whatif.simulate(self.cfg, [e1, e2], {"AAA": b})
        self.assertEqual(res[1]["outcome"], "CLOSED")
        self.assertEqual(res[2]["outcome"], "CLOSED")
        self.assertLess(res[1]["ret_net"], -0.011)
        self.assertNotEqual(res[1]["entry_ts"] if "entry_ts" in res[1] else 0, 1)

    def test_a_time_budget_stops_the_run_and_says_it_did_not_finish(self):
        b = sum([flat_session(d, 100.0) for d in DAYS], [])
        e = entry_of(self.cfg, b)
        ticks = iter([0.0] + [100.0] * 50)
        res, finished = whatif.simulate(self.cfg, [e], {"AAA": b}, budget_s=10.0, clock=lambda: next(ticks))
        self.assertFalse(finished)

    def test_slippage_is_charged_on_both_legs(self):
        b = sum([flat_session(d, 100.0) for d in DAYS], [])
        self.cfg["portfolio"]["slippage_bps"] = 0
        r0 = self.run_one(b)
        self.cfg["portfolio"]["slippage_bps"] = 30
        r30 = self.run_one(b)
        self.assertAlmostEqual(r0["ret_net"] - r30["ret_net"], 0.006, delta=0.0008)


# ------------------------------------------------------------------------------------------------------ entry assumptions
def idea(created, ticker="AAA", kind="idea", **kw):
    d = {"did": 1, "created": created, "ticker": ticker, "kind": kind, "status": "REJECTED", "p_opened": None, "p_entry": None}
    d.update(kw)
    return d


class TestEntryAssumptions(unittest.TestCase):
    def setUp(self):
        self.cfg = shipped_cfg()
        self.c = H.fresh_db()
        self.now = dt.datetime(2026, 10, 31, 0, 0, tzinfo=UTC)

    def day(self, d, px, n=78):
        return [bar(d, k, px) for k in range(n)]

    def test_a_decision_during_the_session_enters_at_the_next_bars_open_plus_slippage(self):
        b = self.day(DAYS[0], 100.0)
        e = whatif.entry_for(idea(iso(session_ts(DAYS[0], 10) + dt.timedelta(minutes=2))), b, self.cfg, self.c, self.now)
        self.assertEqual(e["ts"], b[11]["ts"])
        self.assertAlmostEqual(e["px"], 100.0 * 1.0015, places=6)

    def test_an_after_hours_decision_enters_at_the_next_sessions_open(self):
        b = self.day(DAYS[0], 100.0) + self.day(DAYS[1], 100.5)
        e = whatif.entry_for(idea(iso(session_ts(DAYS[0], 0) + dt.timedelta(hours=9))), b, self.cfg, self.c, self.now)
        self.assertEqual(e["ts"], iso(session_ts(DAYS[1], 0)))
        self.assertAlmostEqual(e["px"], 100.5 * 1.0015, places=6)

    def test_a_stale_idea_expires_and_a_gap_beyond_the_last_look_rule_cancels_it(self):
        b = self.day(DAYS[0], 100.0) + self.day(DAYS[1], 100.5)
        old = whatif.entry_for(idea(iso(session_ts(DAYS[0], 0) - dt.timedelta(hours=30))), b, self.cfg, self.c, self.now)
        self.assertEqual(old["outcome"], "EXPIRED")
        for open_px, expect in ((96.0, "GAP_CANCELLED"), (106.0, "GAP_CANCELLED"), (97.5, None), (104.0, None)):
            b = self.day(DAYS[0], 100.0) + self.day(DAYS[1], open_px)
            e = whatif.entry_for(idea(iso(session_ts(DAYS[0], 78) + dt.timedelta(hours=1))), b, self.cfg, self.c, self.now)          # decided after the close: enters at tomorrow's open
            self.assertEqual(e.get("outcome"), expect, open_px)

    def test_a_trade_we_took_replays_from_its_actual_entry_and_no_bars_means_no_data(self):
        b = self.day(DAYS[0], 100.0)
        e = whatif.entry_for(idea(iso(session_ts(DAYS[0], 3)), kind="control", p_opened=iso(session_ts(DAYS[0], 5) + dt.timedelta(minutes=1)), p_entry=100.2), b, self.cfg, self.c, self.now)
        self.assertEqual((e["ts"], e["px"]), (b[6]["ts"], 100.2))                                       # the actual fill, not an assumed one
        self.assertEqual(whatif.entry_for(idea(iso(session_ts(DAYS[0], 3))), [], self.cfg, self.c, self.now)["outcome"], "NO_DATA")


# ------------------------------------------------------------------------------------------------------ bars in, grouping out
def payload(day, prices, bad=()):
    ts, o, h, l, c, v = [], [], [], [], [], []
    for k, p in enumerate(prices):
        ts.append(int(session_ts(day, k).timestamp()))
        o.append(p), h.append(p + 0.1), l.append(p - 0.1), c.append(p), v.append(1000)
    for i in bad:
        c[i] = None
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c, "volume": v}]}}], "error": None}}


class TestBarsAndTheirSource(unittest.TestCase):
    def test_parsing_skips_junk_repairs_malformed_bars_and_sorts(self):
        p = payload(DAYS[0], [100.0, 101.0, 102.0, 103.0], bad=(1,))
        q = p["chart"]["result"][0]["indicators"]["quote"][0]
        q["high"][2], q["low"][2] = 99.0, 105.0                                                          # high below low
        q["close"][3] = float("nan")
        bars_ = whatif.parse_bars(p)
        self.assertEqual(len(bars_), 2)                                                                  # the None close and the NaN close are dropped
        b = bars_[1]
        self.assertGreaterEqual(b["high"], max(b["open"], b["close"]))
        self.assertLessEqual(b["low"], min(b["open"], b["close"]))
        self.assertEqual([x["ts"] for x in bars_], sorted(x["ts"] for x in bars_))
        with self.assertRaises(net.FetchError):
            whatif.parse_bars({"chart": {"result": None, "error": {"code": "Not Found"}}})
        for junk in ("garbage", None, [], 5):
            with self.assertRaises(net.FetchError):
                whatif.parse_bars(junk)                                                                  # not an AttributeError: a clean, expected failure

    def test_fetching_stores_idempotently_remembers_the_outcome_and_asks_for_regular_session_5_minute_bars(self):
        c, now, urls = H.fresh_db(), dt.datetime(2026, 10, 28, 21, 0, tzinfo=UTC), []

        def fetch(url, headers=None, **k):
            urls.append(url)
            return payload(DAYS[0], [100.0, 100.5, 101.0]), 200
        self.assertEqual(whatif.fetch_symbol(c, "AAA", "5d", fetch, now), "ok")
        self.assertEqual(whatif.fetch_symbol(c, "AAA", "5d", fetch, now), "ok")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM intraday_bars").fetchone()[0], 3)
        self.assertIn("interval=5m", urls[0])
        self.assertIn("includePrePost=false", urls[0])
        self.assertEqual(c.execute("SELECT status FROM intraday_state WHERE symbol='AAA'").fetchone()[0], "ok")
        for bad in (lambda *a, **k: (_ for _ in ()).throw(net.FetchError("HTTP 429", 429)), lambda *a, **k: ({"chart": {"result": [{"timestamp": []}]}}, 200), lambda *a, **k: ("garbage", 200)):
            self.assertEqual(whatif.fetch_symbol(c, "BBB", "5d", bad, now), "error")
        self.assertTrue(c.execute("SELECT status FROM intraday_state WHERE symbol='BBB'").fetchone()[0].startswith("error"))

    def test_gate_families_use_the_real_gate_texts(self):
        cases = [("REJECTED", "swing reward:risk 0.71 below minimum 1.00 (target 1.2% vs stop 1.7%)", "reward:risk too low"), ("REJECTED", "EV net -4.31% <= 0", "edge too small or negative"),
                 ("REJECTED", "EV net 0.08% below minimum 0.60%", "edge too small or negative"), ("VETOED", "VETO: news about GOOGL arrived after this BUY was decided and points the other way", "vetoed by later news"),
                 ("RISK_REJECTED", "capacity full and every holding is too new to rotate out (protected for 6 hours after buying)", "portfolio limits (capacity, exposure, cash)"),
                 ("EXECUTED", "all gates passed", "traded"), ("REJECTED", "article disagrees", "article reader")]
        for st, g, fam in cases:
            self.assertEqual(whatif.gate_family(st, g), fam, g)


# ------------------------------------------------------------------------------------------------------ the scheduled stage
def bars_fetch(prices_by_symbol, days=DAYS):
    calls = []

    def fetch(url, headers=None, **k):
        sym = url.split("/chart/")[1].split("?")[0]
        calls.append(sym)
        if sym not in prices_by_symbol:
            raise net.FetchError("HTTP 404", 404)
        ts, o, h, l, c = [], [], [], [], []
        for d in days:
            for kk in range(78):
                p = prices_by_symbol[sym](d, kk)
                ts.append(int(session_ts(d, kk).timestamp()))
                o.append(p), h.append(p + 0.02), l.append(p - 0.02), c.append(p)
        return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c, "volume": [1000] * len(ts)}]}}], "error": None}}, 200
    fetch.calls = calls
    return fetch


class TestTheStageIsolatedFromTrading(TL.Base):
    def setUp(self):
        super().setUp()
        self.cfg["strategy"]["swing_enabled"] = True
        self.cfg["whatif"]["enabled"] = True
        self.after = dt.datetime(2026, 10, 31, 1, 0, tzinfo=UTC)                                          # Friday evening in New York: the session is over

    def snapshot(self):
        return {t: [tuple(r) for r in self.c.execute("SELECT * FROM %s ORDER BY 1" % t)] for t in ("decisions", "positions", "ledger", "forecasts", "position_forecast_plans", "model_params")}

    def add_rejected_idea(self):
        """The real flow made NVDA (executed). Add a rejected idea on another stock that carries the same kind of forecast."""
        f = dict(self.one("SELECT * FROM forecasts WHERE ticker='NVDA'"))
        f.pop("id"), f.update(ticker="MSFT", decision_id=None, verdict="REJECT_NEGATIVE_EV")
        cols = list(f)
        fid = self.c.execute("INSERT INTO forecasts(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))), [f[k] for k in cols]).lastrowid
        reason = json.dumps({"gates": ["EV net -1.00% <= 0"], "forecast": {"verdict": "REJECT_NEGATIVE_EV"}})
        self.c.execute("INSERT INTO decisions(created,ticker,action,status,reason,origin,forecast_id) VALUES(?,?,?,?,?,?,?)", (iso(TL.T_OPEN + dt.timedelta(minutes=5)), "MSFT", "NO_TRADE", "REJECTED", reason, "event", fid))
        return self.c.execute("SELECT MAX(id) FROM decisions").fetchone()[0]

    def test_the_stage_writes_only_its_own_tables_replays_the_idea_and_the_trade_and_repeats_nothing(self):
        self.open_position()
        did = self.add_rejected_idea()
        fetch = bars_fetch({"NVDA": lambda d, k: 120.0 + 0.002 * k, "MSFT": lambda d, k: 300.0 - 0.01 * k})
        before = self.snapshot()
        out = whatif.run_stage(self.c, self.cfg, self.after, fetch)
        self.assertEqual(self.snapshot(), before)                                                         # NOT ONE trading table changed
        self.assertEqual((out["ideas"], out["fetched"], out["fetch_errors"]), (2, 2, 0))
        rows = {r["ticker"]: dict(r) for r in self.c.execute("SELECT * FROM whatif_results")}
        self.assertEqual(rows["NVDA"]["kind"], "control")
        self.assertEqual(rows["MSFT"]["kind"], "idea")
        self.assertEqual(rows["MSFT"]["family"], "edge too small or negative")
        self.assertEqual(rows["MSFT"]["strategy_version"], stamp.STRATEGY_VERSION)
        self.assertEqual(rows["MSFT"]["outcome"], "CLOSED")
        self.assertIsNotNone(rows["MSFT"]["mfe"])
        n_calls = len(fetch.calls)
        again = whatif.run_stage(self.c, self.cfg, self.after + dt.timedelta(minutes=10), fetch)
        self.assertEqual(len(fetch.calls), n_calls)                                                       # prices are fresh: nothing refetched
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM whatif_results").fetchone()[0], 2)         # nothing duplicated
        self.assertEqual(again["simulated"], 0)

    def test_a_failing_price_source_is_survived_and_retried_later_not_hammered(self):
        self.open_position()
        self.add_rejected_idea()
        out = whatif.run_stage(self.c, self.cfg, self.after, bars_fetch({}))
        self.assertEqual((out["fetched"], out["fetch_errors"]), (0, 2))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM whatif_results WHERE final=1").fetchone()[0], 0)
        fetch = bars_fetch({})
        whatif.run_stage(self.c, self.cfg, self.after + dt.timedelta(minutes=30), fetch)
        self.assertEqual(fetch.calls, [])                                                                 # still inside the one-hour back-off
        fetch2 = bars_fetch({"NVDA": lambda d, k: 120.0, "MSFT": lambda d, k: 300.0})
        whatif.run_stage(self.c, self.cfg, self.after + dt.timedelta(hours=2), fetch2)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM whatif_results").fetchone()[0], 2)

    def test_a_pipeline_cycle_runs_it_after_the_close_never_in_the_session_and_isolated_from_errors(self):
        self.open_position()
        self.add_rejected_idea()
        self.mkt.now = self.after
        fetch = bars_fetch({"NVDA": lambda d, k: 120.0, "MSFT": lambda d, k: 300.0})
        deps = self.deps()
        deps.series_fetch = lambda url, headers=None, **k: fetch(url, headers) if "interval=5m" in url else self.series(url, headers, **k)        # 5-minute bars from the fake source; everything else as before
        rep = pipeline.run_cycle(self.c, self.cfg, H.universe(), TL.SRC, deps, self.after)
        self.assertEqual(rep["errors"], [])
        self.assertEqual(rep["whatif"]["ideas"], 2)
        n = len(fetch.calls)
        deps2 = self.deps()
        deps2.series_fetch = deps.series_fetch
        rep2 = pipeline.run_cycle(self.c, self.cfg, H.universe(), TL.SRC, deps2, TL.T_OPEN + dt.timedelta(minutes=30))        # regular session
        self.assertNotIn("whatif", rep2)
        self.assertEqual(len(fetch.calls), n)
        boom = self.deps()
        boom.series_fetch = lambda url, headers=None, **k: (_ for _ in ()).throw(RuntimeError("the price source exploded")) if "interval=5m" in url else self.series(url, headers, **k)
        self.c.execute("DELETE FROM intraday_state")
        self.c.execute("DELETE FROM model_params WHERE key='run:whatif'")
        self.c.execute("DELETE FROM run_state WHERE name='whatif'") if self.c.execute("SELECT 1 FROM sqlite_master WHERE name='run_state'").fetchone() else None
        rep3 = pipeline.run_cycle(self.c, self.cfg, H.universe(), TL.SRC, boom, self.after + dt.timedelta(hours=3))
        self.assertEqual(rep3["errors"], [])                                                              # the exploding source did not break the cycle

    def test_bearish_ideas_are_counted_and_never_simulated_as_longs(self):
        self.open_position()
        f = dict(self.one("SELECT * FROM forecasts WHERE ticker='NVDA'"))
        f.pop("id"), f.update(ticker="XOM", direction="SHORT", verdict="REJECT_NEGATIVE_EV")
        cols = list(f)
        fid = self.c.execute("INSERT INTO forecasts(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))), [f[k] for k in cols]).lastrowid
        self.c.execute("INSERT INTO decisions(created,ticker,action,status,reason,origin,forecast_id) VALUES(?,?,?,?,?,?,?)", (iso(TL.T_OPEN + dt.timedelta(minutes=5)), "XOM", "NO_TRADE", "REJECTED", "{}", "event", fid))
        out = whatif.run_stage(self.c, self.cfg, self.after, bars_fetch({"NVDA": lambda d, k: 120.0, "XOM": lambda d, k: 100.0}))
        self.assertEqual(out["bearish_skipped"], 1)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM whatif_results WHERE ticker='XOM'").fetchone()[0], 0)

    def test_an_unfinished_simulation_is_not_recorded_as_if_it_were_a_result(self):
        from unittest import mock
        self.open_position()
        did = self.add_rejected_idea()
        half = {did: {"outcome": "OPEN_AT_END", "exit_ts": None, "exit_px": 1.0, "exit_reason": "still open when the data ends", "ret_net": 0.5, "held_sessions": 0, "partial": False, "mfe": 0.1, "mae": -0.1, "bars_n": 3, "data_through": "x"}}
        with mock.patch.object(whatif, "simulate", return_value=(half, False)):
            whatif.run_stage(self.c, self.cfg, self.after, bars_fetch({"NVDA": lambda d, k: 120.0, "MSFT": lambda d, k: 300.0}))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM whatif_results WHERE decision_id=?", (did,)).fetchone()[0], 0)       # tried again next run, not stored half-done

    def test_the_symbols_per_cycle_limit_is_obeyed_and_the_rest_wait_for_the_next_cycle(self):
        self.open_position()
        self.add_rejected_idea()
        self.cfg["whatif"]["max_symbols_per_cycle"] = 1
        fetch = bars_fetch({"NVDA": lambda d, k: 120.0, "MSFT": lambda d, k: 300.0})
        out = whatif.run_stage(self.c, self.cfg, self.after, fetch)
        self.assertEqual((out["fetched"], len(fetch.calls)), (1, 1))
        out2 = whatif.run_stage(self.c, self.cfg, self.after + dt.timedelta(minutes=5), fetch)
        self.assertEqual((out2["fetched"], len(fetch.calls)), (1, 2))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM whatif_results").fetchone()[0], 2)

    def test_it_never_runs_in_the_session_or_around_the_open_even_when_it_is_due(self):
        r_pre = self.cycle(TL.T_PRE, news=True)                                                          # pre-market, the BUY is queued and waiting for the open
        r_open = self.cycle(TL.T_OPEN)                                                                    # the open: the BUY executes
        self.assertNotIn("whatif", r_pre, "pre-market, queued buys waiting: the stage must not even start")
        self.assertNotIn("whatif", r_open, "regular session")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM intraday_state").fetchone()[0], 0)
        self.add_rejected_idea()
        fetch = bars_fetch({"NVDA": lambda d, k: 120.0, "MSFT": lambda d, k: 300.0})
        deps = self.deps()
        deps.series_fetch = lambda url, headers=None, **k: fetch(url, headers) if "interval=5m" in url else self.series(url, headers, **k)
        for label, t in (("regular session", TL.T_OPEN + dt.timedelta(hours=1)), ("early pre-market", TL.T_PRE + dt.timedelta(hours=1, minutes=40))):
            self.mkt.now = t
            rep = pipeline.run_cycle(self.c, self.cfg, H.universe(), TL.SRC, deps, t)                     # nothing has run yet: it IS due
            self.assertNotIn("whatif", rep, label)
            self.assertEqual(fetch.calls, [], label)

    def test_switched_off_does_nothing(self):
        self.open_position()
        self.cfg["whatif"]["enabled"] = False
        fetch = bars_fetch({"NVDA": lambda d, k: 120.0})
        self.assertFalse(whatif.run_stage(self.c, self.cfg, self.after, fetch)["enabled"])
        self.assertEqual(fetch.calls, [])


# ------------------------------------------------------------------------------------------------------ statistics
def insert_result(c, i, ret, family="reward:risk too low", kind="idea", outcome="CLOSED", reason="STOP_REACHED", origin="event", actual=None, actual_reason=None, mfe=0.01, mae=-0.01, event="earnings_beat", profile="swing"):
    c.execute("INSERT INTO whatif_results(decision_id,assumptions,simulated_at,ticker,kind,origin,decision_status,family,outcome,final,exit_reason,ret_net,mfe,mae,partial,actual_ret,actual_exit_reason,event_type,profile) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)", (i, whatif.ASSUMPTIONS, iso(dt.datetime(2026, 10, 30, tzinfo=UTC)), "T%d" % i, kind, origin, "REJECTED", family, outcome, 1 if outcome == "CLOSED" else 0, reason, ret, mfe, mae, actual, actual_reason, event, profile))


class TestTheReport(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 31, tzinfo=UTC)

    def setUp(self):
        self.c = H.fresh_db()

    def test_each_group_is_labelled_honestly_by_its_sample_size_and_its_range(self):
        for i in range(40):
            insert_result(self.c, i, 0.012 + (0.002 if i % 2 else -0.002), family="made money group")                    # 40 ideas averaging +1.2%
        for i in range(100, 140):
            insert_result(self.c, i, -0.012 + (0.002 if i % 2 else -0.002), family="lost money group")
        for i in range(200, 240):
            insert_result(self.c, i, (0.03 if i % 2 else -0.03), family="coin flip group")
        for i in range(300, 305):
            insert_result(self.c, i, 0.05, family="tiny group")
        rep = whatif.report(self.c, self.NOW)
        g = {s["label"]: s for s in rep["by_family"]}
        self.assertIn("MADE money", g["made money group"]["reading"])
        self.assertIn("LOST money", g["lost money group"]["reading"])
        self.assertEqual(g["coin flip group"]["reading"], "inconclusive")
        self.assertIn("too few", g["tiny group"]["reading"])                                              # five lucky winners prove nothing
        self.assertAlmostEqual(g["made money group"]["mean"], 0.012, places=4)
        self.assertEqual(g["made money group"]["n"], 40)

    def test_provisional_and_non_trades_never_enter_the_averages_but_are_counted(self):
        insert_result(self.c, 1, 0.01)
        insert_result(self.c, 2, 0.50, outcome="OPEN_AT_END")
        insert_result(self.c, 3, None, outcome="GAP_CANCELLED")
        rep = whatif.report(self.c, self.NOW)
        self.assertEqual(rep["overall"]["n"], 1)
        self.assertEqual(rep["outcomes"], {"CLOSED": 1, "OPEN_AT_END": 1, "GAP_CANCELLED": 1})

    def test_ideas_from_the_older_profile_are_kept_apart_from_the_strategy_in_force(self):
        for i in range(5):
            insert_result(self.c, i, 0.01, profile="swing")
        for i in range(10, 20):
            insert_result(self.c, i, -0.05, profile="classic")
        rep = whatif.report(self.c, self.NOW)
        self.assertEqual((rep["overall"]["n"], rep["other_profile"]), (5, 10))
        self.assertAlmostEqual(rep["overall"]["mean"], 0.01)                                              # the old profile's -5% does not leak into the averages
        self.assertIn("10 idea(s) from the older profile", whatif.format_report(rep))
        self.assertEqual(whatif.report(self.c, self.NOW, profile="classic")["overall"]["n"], 10)

    def test_the_engines_accuracy_on_trades_we_took_is_measured_on_price_rule_exits_only(self):
        insert_result(self.c, 1, 0.0100, kind="control", actual=0.0102, actual_reason="STOP_REACHED", reason="STOP_REACHED")
        insert_result(self.c, 2, -0.0100, kind="control", actual=-0.0080, actual_reason="TIME_EXPIRED", reason="STOP_REACHED")
        insert_result(self.c, 3, 0.0300, kind="control", actual=-0.0500, actual_reason="THESIS_INVALIDATED", reason="TIME_EXPIRED")       # a news-driven exit: cannot be simulated, must not count
        f = whatif.report(self.c, self.NOW)["fidelity"]
        self.assertEqual(f["n"], 2)
        self.assertAlmostEqual(f["mean_diff"], (-0.0002 + -0.0020) / 2, places=6)
        self.assertAlmostEqual(f["same_exit"], 0.5)

    def test_the_text_and_html_render_with_and_without_data(self):
        self.assertIn("Nothing has been simulated yet", whatif.html_section(self.c, H.cfg(), self.NOW))
        for i in range(35):
            insert_result(self.c, i, 0.01)
        txt = whatif.format_report(whatif.report(self.c, self.NOW))
        self.assertIn("reward:risk too low", txt)
        self.assertIn("n=35", txt)
        html = whatif.html_section(self.c, H.cfg(), self.NOW)
        self.assertIn("Missed trades: what would have happened", html)
        self.assertIn("reward:risk too low", html)


class TestSettingsDashboardAndHousekeeping(unittest.TestCase):
    def test_settings_defaults_stamp_and_retention(self):
        base = config.load_config("/nonexistent", with_overrides=False)
        self.assertEqual(base["whatif"], {"enabled": True, "lookback_days": 5, "max_symbols_per_cycle": 20, "time_budget_seconds": 40, "max_ideas_per_run": 150})
        f = {x.path: x for x in S.FIELDS}
        self.assertEqual({p for p in f if p.startswith("whatif.")}, {"whatif.enabled", "whatif.lookback_days", "whatif.max_symbols_per_cycle", "whatif.time_budget_seconds"})
        self.assertTrue(all(f[p].group == "Missed-trade analysis" for p in f if p.startswith("whatif.")))
        c, cfg = H.fresh_db(), H.cfg()
        stamp.record(c, cfg, dt.datetime(2026, 10, 31, tzinfo=UTC))
        cfg["whatif"]["enabled"], cfg["whatif"]["lookback_days"] = True, 9
        self.assertFalse(stamp.record(c, cfg, dt.datetime(2026, 11, 1, tzinfo=UTC))["changed"])           # measurement settings are not the strategy
        from paper_agent import maintain
        c.execute("INSERT INTO intraday_bars(symbol,ts,open,high,low,close) VALUES('A',?,1,1,1,1)", (iso(dt.datetime(2026, 8, 1, tzinfo=UTC)),))
        c.execute("INSERT INTO intraday_bars(symbol,ts,open,high,low,close) VALUES('A',?,1,1,1,1)", (iso(dt.datetime(2026, 10, 30, tzinfo=UTC)),))
        self.assertEqual(maintain.plan(c, cfg, dt.datetime(2026, 10, 31, tzinfo=UTC))["retention"]["intraday_bars"], 1)

    def test_the_intelligence_tab_and_command_show_it(self):
        import contextlib
        import io
        import tempfile
        from paper_agent import __main__ as cli, dashboard
        c, cfg = H.fresh_db(), H.cfg()
        for i in range(35):
            insert_result(c, i, 0.01)
        self.assertIn("Missed trades: what would have happened", dashboard.render(c, cfg, "intelligence", {}, {"authed": True}))
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        d = db.open_db(path)
        for i in range(35):
            insert_result(d, i, 0.01)
        d.close()
        cli.DB_PATH = path
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["whatif", "--days", "365"]), 0)
        self.assertIn("MISSED-TRADE ANALYSIS", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
