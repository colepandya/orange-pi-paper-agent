import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_recall as TR  # noqa: E402
from paper_agent import net, preflight, timeutil  # noqa: E402
from paper_agent.timeutil import iso, parse_ts  # noqa: E402

NOW = H.NOW                                          # Tue 27 Oct 2026 11:00 New York: regular session
KEYS = ("TWELVE_DATA_API_KEY", "FINNHUB_API_KEY", "ALPACA_API_KEY", "ALPACA_API_SECRET")


def healthy_db(now=NOW, positions=False):
    c = H.fresh_db()
    c.execute("INSERT INTO cycle_runs(started,finished,status,mode) VALUES(?,?,?,?)", (iso(now - dt.timedelta(minutes=4)), iso(now), "OK", "full"))
    c.execute("INSERT INTO events(created,event_type,scope,direction,magnitude,gate) VALUES(?,?,?,?,?,?)", (iso(now - dt.timedelta(minutes=30)), "earnings_beat", "company", .8, .5, "candidate"))
    if positions:
        c.execute("INSERT INTO positions(ticker,opened,entry_price,shares,cost,status) VALUES('MSFT',?,400,10,4000,'OPEN')", (iso(now - dt.timedelta(days=1)),))
        c.execute("INSERT INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(1,'OPEN',10,4000,?)", (iso(now),))
        c.execute("INSERT INTO position_forecast_plans(position_id,decision_id,created,plan_version,active,event_type,forecast_decision,target_return,stop_return,target_price,stop_price,max_hold_days,estimated_exit,target_probability,confidence,completeness,partial_exit_pct,runner_target_return,runner_target_price,runner_stop_return,runner_stop_price,bear_return,base_return,bull_return,expected_return,residual_return,priced_in,rationale,invalidation_json,forecast_hash) "
                  "VALUES(1,0,?,'5',1,'x','BUY',.04,-.03,416,388,8,?,.5,70,80,.7,.07,428,.005,402,-.03,.03,.06,.02,.02,0,'r','[]','h')", (iso(now), iso(now + dt.timedelta(days=8))))
    return c


def big_uni():
    """A production-sized universe: the real brand/legal-name entries plus filler so the size check sees ~2,100 companies."""
    from paper_agent.entities import Universe
    base = TR.uni().watch
    filler = {"ZZ%d" % i: {"name": "Filler Company %d Corp" % i, "aliases": [], "cik": 900000 + i} for i in range(2100)}
    return Universe({**base, **filler})


def cfg_real():
    cfg = H.cfg()
    cfg["runtime"]["min_available_memory_mb"] = 120            # the test helper disables this floor; the checklist must see the real one
    return cfg


def run(c, now=NOW, **kw):
    lines = []
    cfg = cfg_real()
    kw.setdefault("live", False)
    kw.setdefault("check_units", False)
    kw.setdefault("mem_mb", lambda: 600)
    r = preflight.run(c, cfg, big_uni(), now=now, out=lines.append, **kw)
    return r, "\n".join(lines)


class Env(unittest.TestCase):
    def setUp(self):
        self._old = {k: os.environ.get(k) for k in KEYS}
        for k in KEYS:
            os.environ[k] = "x" * 20

    def tearDown(self):
        for k, v in self._old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def status(self, r, name):
        return next(c["status"] for c in r["checks"] if c["name"].startswith(name))


class TestVerdicts(Env):
    def test_healthy_system_is_ready(self):
        r, text = run(healthy_db())
        self.assertTrue(r["ready"], text)
        self.assertIn("READY FOR MARKET OPEN", text)

    def test_stale_cycle_is_a_blocker(self):
        c = healthy_db()
        c.execute("UPDATE cycle_runs SET started=?", (iso(NOW - dt.timedelta(minutes=90)),))
        r, text = run(c)
        self.assertFalse(r["ready"])
        self.assertEqual(self.status(r, "Cycle running"), "FAIL")
        self.assertIn("NOT READY", text)

    def test_skipped_cycles_are_a_blocker_with_the_cleanup_fix(self):
        c = healthy_db()
        c.execute("INSERT INTO cycle_runs(started,finished,status,mode,report_json) VALUES(?,?,?,?,?)", (iso(NOW - dt.timedelta(minutes=20)), iso(NOW), "SKIPPED", "full", json.dumps({"skipped": "low memory"})))
        r, text = run(c)
        self.assertEqual(self.status(r, "No skipped cycles"), "FAIL")
        self.assertIn("cleanup-ram", text)

    def test_low_memory_is_flagged(self):
        r, _ = run(healthy_db(), mem_mb=lambda: 130)
        self.assertEqual(self.status(r, "Memory"), "FAIL")
        r, _ = run(healthy_db(), mem_mb=lambda: 200)
        self.assertEqual(self.status(r, "Memory"), "WARN")

    def test_no_news_in_three_hours_is_a_blocker(self):
        c = healthy_db()
        c.execute("UPDATE events SET created=?", (iso(NOW - dt.timedelta(hours=5)),))
        r, _ = run(c)
        self.assertEqual(self.status(r, "News flowing"), "FAIL")

    def test_open_positions_need_a_recent_exit_guard_run_while_the_market_is_open(self):
        c = healthy_db(positions=True)
        r, _ = run(c)
        self.assertEqual(self.status(r, "Exit guard"), "FAIL")                       # position open, market open, guard never ran
        c.execute("INSERT INTO executor_runs(created,status,positions_checked,actions_committed,holds_extended,blocked_stale,errors,detail_json) VALUES(?,?,?,?,?,?,?,?)", (iso(NOW - dt.timedelta(minutes=2)), "OK", 1, 0, 0, 0, 0, "[]"))
        r, _ = run(c)
        self.assertEqual(self.status(r, "Exit guard"), "PASS")
        closed = dt.datetime(2026, 10, 31, 15, 0, tzinfo=dt.timezone.utc)           # Saturday: the guard legitimately sleeps
        c2 = healthy_db(closed, positions=True)
        self.assertEqual(self.status(run(c2, closed)[0], "Exit guard"), "PASS")

    def test_missing_plan_on_an_open_position_is_a_warning(self):
        c = healthy_db(positions=True)
        c.execute("DELETE FROM position_forecast_plans")
        self.assertEqual(self.status(run(c)[0], "Open positions have exit plans"), "WARN")

    def test_paused_is_a_warning_not_a_blocker(self):
        import tempfile
        pf = tempfile.mktemp()
        open(pf, "w").close()
        c = healthy_db()
        lines = []
        cfg = cfg_real()
        cfg["runtime"]["pause_file"] = pf
        r = preflight.run(c, cfg, big_uni(), now=NOW, live=False, check_units=False, mem_mb=lambda: 600, out=lines.append)
        self.assertEqual(self.status(r, "Trading not paused"), "WARN")
        self.assertTrue(r["ready"])

    def test_too_few_provider_keys_blocks(self):
        for k in KEYS:
            os.environ.pop(k, None)
        r, _ = run(healthy_db())
        self.assertEqual(self.status(r, "Quote provider keys"), "FAIL")

    def test_real_universe_recognition_is_checked(self):
        r, _ = run(healthy_db())
        self.assertEqual(self.status(r, "Company recognition"), "PASS")
        from paper_agent.entities import Universe
        weak = Universe({"AAPL": {"name": "Apple Inc", "aliases": []}})
        lines = []
        r2 = preflight.run(healthy_db(), cfg_real(), weak, now=NOW, live=False, check_units=False, mem_mb=lambda: 600, out=lines.append)
        self.assertIn(self.status(r2, "Company universe"), ("FAIL",))


class TestLiveChecks(Env):
    def fj(self, good_providers=("twelve", "alpaca", "yahoo"), age=5):
        ts = NOW.timestamp() - age

        def f(url, headers=None, **k):
            if "twelvedata" in url and "twelve" in good_providers:
                return {"close": "500.1", "timestamp": ts, "symbol": "SPY"}, 200
            if "alpaca" in url and "alpaca" in good_providers:
                return {"latestTrade": {"p": 500.0, "t": dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()}, "latestQuote": {}}, 200
            if "finnhub" in url and "finnhub" in good_providers:
                return {"c": 500.05, "t": ts}, 200
            if "query1.finance.yahoo.com" in url and "yahoo" in good_providers:
                p = H.chart_payload("SPY", last_price=500.0)
                p["chart"]["result"][0]["meta"]["regularMarketTime"] = int(ts)
                return p, 200
            raise net.FetchError("HTTP 401", 401)
        return f

    def feeds(self, ok=True):
        return lambda url, **k: (H.rss([("Hello world headline here", "", "Tue, 27 Oct 2026 14:00:00 +0000")]), 200, {}) if ok else (_ for _ in ()).throw(net.FetchError("HTTP 500", 500))

    SRCS = [{"id": "r%d" % i, "name": "R%d" % i, "type": "rss", "url": "http://x/%d" % i, "trust": .5, "interval": 600} for i in range(6)]

    def go(self, c=None, **kw):
        lines = []
        r = preflight.run(c or healthy_db(), cfg_real(), big_uni(), srcs=self.SRCS, live=True, check_units=False, now=NOW, mem_mb=lambda: 600, out=lines.append, **kw)
        return r, "\n".join(lines)

    def test_open_market_with_agreeing_fresh_providers_is_ready(self):
        r, text = self.go(fetch=self.feeds(), fetch_json=self.fj())
        self.assertTrue(r["ready"], text)
        self.assertEqual(self.status(r, "Verified fresh quote"), "PASS")
        self.assertEqual(self.status(r, "News feeds reachable"), "PASS")

    def test_open_market_with_stale_quotes_blocks_trading(self):
        r, text = self.go(fetch=self.feeds(), fetch_json=self.fj(age=1200))
        self.assertEqual(self.status(r, "Verified fresh quote"), "FAIL")
        self.assertFalse(r["ready"])

    def test_one_provider_is_not_enough(self):
        r, _ = self.go(fetch=self.feeds(), fetch_json=self.fj(good_providers=("twelve",)))
        self.assertEqual(self.status(r, "Quote providers returning data"), "FAIL")

    def test_dead_feeds_are_flagged(self):
        r, _ = self.go(fetch=self.feeds(ok=False), fetch_json=self.fj())
        self.assertEqual(self.status(r, "News feeds reachable"), "FAIL")

    def test_closed_market_cannot_prove_freshness_so_it_asks_for_a_recheck(self):
        closed = dt.datetime(2026, 10, 31, 3, 0, tzinfo=dt.timezone.utc)
        c = healthy_db(closed)
        lines = []
        r = preflight.run(c, cfg_real(), big_uni(), srcs=self.SRCS, live=True, check_units=False, now=closed, mem_mb=lambda: 600, out=lines.append, fetch=self.feeds(), fetch_json=self.fj(age=40000))
        self.assertEqual(self.status(r, "Fresh quote at the open"), "WARN")
        self.assertIn("Re-run this check", "\n".join(lines))


class TestNextOpen(unittest.TestCase):
    def test_weekend_holiday_and_clock_change(self):
        f = lambda s: parse_ts(s)  # noqa: E731
        self.assertEqual(timeutil.next_market_open(f("2026-10-28T02:00:00Z")), f("2026-10-28T13:30:00Z"))      # Wed NZ morning -> Wed 09:30 EDT (UTC-4)
        self.assertEqual(timeutil.next_market_open(f("2026-10-30T22:00:00Z")), f("2026-11-02T14:30:00Z"))      # Fri night -> Monday after the US clock change (EST, UTC-5)
        self.assertEqual(timeutil.next_market_open(f("2026-11-25T22:00:00Z")), f("2026-11-27T14:30:00Z"))      # day before Thanksgiving: skips the holiday
        self.assertEqual(timeutil.next_market_open(f("2026-10-27T13:30:00Z")), f("2026-10-28T13:30:00Z"))      # exactly at the open: the NEXT one
        self.assertEqual(timeutil.next_market_open(f("2027-01-15T22:00:00Z")), f("2027-01-19T14:30:00Z"))      # MLK Monday 2027 skipped


if __name__ == "__main__":
    unittest.main()
