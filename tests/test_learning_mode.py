"""A trial measures one fixed strategy only if the system does not rewrite itself while it runs. By default (observe) it keeps learning and RECORDS what it would change;
decisions stay exactly as they are until the owner reviews the proposals and promotes them. Before this, two paths changed live behaviour automatically: recalibrate() (probability
calibration, event-type power, move scale, thesis priors) and resolve_due() (per-source hit/miss counts that move source credibility from the very first resolved prediction)."""
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import config, db, ev, evidence, learning, settings as S, stamp  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


def seed(c, n=120, hit=0.45, p=0.8, src="BadWire", et="contract_win", resolve_real=False):
    """n forecast predictions with outcomes (the 'resolved' state recalibrate reads)."""
    for i in range(n):
        c.execute("INSERT INTO predictions(created,kind,subject,claim,probability,horizon_date,source,event_type,resolved) VALUES(?,?,?,?,?,?,?,?,1)", (iso(NOW), "forecast", "X", "abnormal_up", p, iso(NOW), src, et))
        pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        out = 1.0 if (i / n) < hit else 0.0
        c.execute("INSERT INTO outcomes(prediction_id,observed_at,outcome,abnormal_return,brier) VALUES(?,?,?,?,?)", (pid, iso(NOW), out, 0.01 if out else -0.01, (p - out) ** 2))


def live_state(c):
    return {"calibration": db.param(c, "calibration"), "power": db.param(c, "power:contract_win"), "scale": db.param(c, "move_scale:contract_win"), "cal_p": ev.calibrate_p(c, 0.8),
            "cred": evidence.source_credibility(c, "BadWire", "contract_win", 0.8)}


class TestObserveIsTheDefaultAndChangesNothingLive(unittest.TestCase):
    def test_the_default_and_the_setting(self):
        self.assertEqual(config.load_config("/nonexistent", with_overrides=False)["intelligence"]["learning_mode"], "observe")
        f = next(x for x in S.FIELDS if x.path == "intelligence.learning_mode")
        self.assertEqual(f.choices, ["observe", "apply"])
        for bad in ("", "APPLY", "yes", "apply; drop"):
            with self.assertRaises(ValueError):
                S._parse(f, bad)
        self.assertTrue(learning.observing({}) and learning.observing(H.cfg()) and not learning.observing({"intelligence": {"learning_mode": "apply"}}))

    def test_recalibration_in_observe_mode_proposes_but_nothing_decisions_use_moves(self):
        c, cfg = H.fresh_db(), H.cfg()
        seed(c)
        before = live_state(c)
        r = learning.recalibrate(c, cfg, NOW)
        self.assertEqual(r["mode"], "observe")
        self.assertIsNotNone(r["calibration"])                                                           # it still LEARNED...
        self.assertEqual(live_state(c), before)                                                          # ...but calibration, power, scale and credibility are all untouched
        self.assertIsNone(db.param(c, "calibration"))
        prop = db.param(c, "proposed:calibration")
        self.assertIsNotNone(prop)
        self.assertLess(prop["a"], 0.0)                                                                  # the overconfidence (80% claimed, 45% achieved) is visible in the proposal as a downward shift
        self.assertIsNotNone(db.param(c, "proposed:power:contract_win"))
        self.assertIn("PROPOSAL ONLY", c.execute("SELECT reason FROM model_params WHERE key='proposed:calibration'").fetchone()[0])

    def test_apply_mode_is_exactly_the_old_behaviour(self):
        c, cfg = H.fresh_db(), H.cfg()
        cfg["intelligence"]["learning_mode"] = "apply"
        seed(c)
        r = learning.recalibrate(c, cfg, NOW)
        self.assertEqual(r["mode"], "apply")
        self.assertIsNotNone(db.param(c, "calibration"))
        self.assertLess(ev.calibrate_p(c, 0.8), 0.8)
        self.assertLess(db.param(c, "power:contract_win"), 0.9)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM model_params WHERE key LIKE 'proposed:%'").fetchone()[0], 0)

    def test_source_credibility_does_not_move_in_observe_mode_from_the_very_first_resolved_prediction(self):
        for mode, moves in (("observe", False), ("apply", True)):
            c, cfg = H.fresh_db(), H.cfg()
            cfg["intelligence"]["learning_mode"] = mode
            before = evidence.source_credibility(c, "BadWire", "contract_win", 0.8)
            md = __import__("paper_agent.marketdata", fromlist=["x"])
            for s in ("AAA", "SPY"):
                md.store_series(c, s, [{"day": (NOW - dt.timedelta(days=20 - i)).date().isoformat(), "open": 100, "high": 100, "low": 100, "close": (100 + (i * 0.5 if s == "AAA" else 0)), "volume": 1e6} for i in range(21)])
            c.execute("INSERT INTO predictions(created,kind,subject,claim,probability,baseline_price,benchmark_price,horizon_date,source,event_type) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (iso(NOW - dt.timedelta(days=20)), "forecast", "AAA", "abnormal_up", 0.7, 100.0, 100.0, iso(NOW - dt.timedelta(hours=1)), "BadWire", "contract_win"))
            r = learning.resolve_due(c, cfg, NOW, fetch=lambda *a, **k: (_ for _ in ()).throw(md.net.FetchError("offline")))
            self.assertEqual(r["resolved"], 1, mode)
            self.assertEqual(evidence.source_credibility(c, "BadWire", "contract_win", 0.8) != before, moves, mode)
            kept = [x[0] for x in c.execute("SELECT source FROM source_stats")]
            self.assertEqual(kept, ["BadWire"] if moves else ["observed:BadWire"], mode)


class TestReviewAndPromotion(unittest.TestCase):
    def setUp(self):
        self.c, self.cfg = H.fresh_db(), H.cfg()
        seed(self.c)
        learning.recalibrate(self.c, self.cfg, NOW)
        self.c.execute("INSERT INTO source_stats(source,event_type,hits,misses) VALUES('observed:BadWire','contract_win',3,7)")
        self.c.execute("INSERT INTO source_stats(source,event_type,hits,misses) VALUES('BadWire','contract_win',1,0)")

    def test_review_lists_each_proposal_beside_the_live_value(self):
        props, obs = learning.proposals(self.c)
        keys = {p["key"]: p for p in props}
        self.assertIn("calibration", keys)
        self.assertIsNone(keys["calibration"]["live"])
        self.assertEqual(obs, [{"source": "observed:BadWire", "event_type": "contract_win", "hits": 3.0, "misses": 7.0}])

    def test_a_dry_run_changes_nothing_and_apply_makes_it_live_once_and_logs_it(self):
        before = live_state(self.c)
        dry = learning.promote(self.c, self.cfg, NOW, apply=False)
        self.assertFalse(dry["applied"])
        self.assertEqual(live_state(self.c), before)
        n0 = len(stamp.history(self.c))
        done = learning.promote(self.c, self.cfg, NOW, apply=True)
        self.assertTrue(done["applied"])
        self.assertIsNotNone(db.param(self.c, "calibration"))
        self.assertLess(ev.calibrate_p(self.c, 0.8), 0.8)
        self.assertEqual(self.c.execute("SELECT hits,misses FROM source_stats WHERE source='BadWire'").fetchone()[:], (4.0, 7.0))        # merged into the live record
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM source_stats WHERE source LIKE 'observed:%'").fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM model_params WHERE key LIKE 'proposed:%'").fetchone()[0], 0)
        self.assertEqual(len(stamp.history(self.c)), n0 + 1)                                                # the moment behaviour changed is on the record
        self.assertIn("learning promoted", stamp.current(self.c)["note"])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM audit_log WHERE action='learning_promote'").fetchone()[0], 1)
        again = learning.promote(self.c, self.cfg, NOW, apply=True)
        self.assertFalse(again["applied"])                                                                   # nothing left: a second run is a no-op


class TestTheStrategyStamp(unittest.TestCase):
    def test_first_record_then_silence_while_nothing_changes(self):
        c, cfg = H.fresh_db(), H.cfg()
        a = stamp.record(c, cfg, NOW)
        self.assertTrue(a["changed"])
        for i in range(5):
            self.assertFalse(stamp.record(c, cfg, NOW + dt.timedelta(minutes=5 * i))["changed"])
        self.assertEqual(len(stamp.history(c)), 1)
        e = stamp.current(c)
        self.assertEqual((e["version"], e["learning_mode"], e["note"]), (stamp.STRATEGY_VERSION, "observe", "first record"))

    def test_a_decision_setting_creates_an_entry_that_says_exactly_what_moved(self):
        c, cfg = H.fresh_db(), H.cfg()
        stamp.record(c, cfg, NOW)
        cfg["risk"]["max_position_pct"] = 0.2
        cfg["reader"]["mode"] = "full"
        self.assertTrue(stamp.record(c, cfg, NOW + dt.timedelta(hours=1))["changed"])
        e = stamp.current(c)
        self.assertEqual(e["moved"], {"reader.mode": ["off", "full"], "risk.max_position_pct": [H.cfg()["risk"]["max_position_pct"], 0.2]})
        self.assertEqual(e["note"], "decision settings changed")
        self.assertEqual(stamp.version_at(c, iso(NOW + dt.timedelta(minutes=30)))["at"], iso(NOW))          # which strategy was in force at a given moment
        self.assertEqual(stamp.version_at(c, iso(NOW + dt.timedelta(hours=2)))["at"], iso(NOW + dt.timedelta(hours=1)))
        self.assertIsNone(stamp.version_at(c, iso(NOW - dt.timedelta(days=1))))

    def test_operational_settings_never_count_as_a_strategy_change(self):
        c, cfg = H.fresh_db(), H.cfg()
        stamp.record(c, cfg, NOW)
        cfg["schedule"]["cycle_minutes"] = 7
        cfg["runtime"]["min_available_memory_mb"] = 99
        cfg["runtime"]["contact_email"] = "x@example.org"
        cfg["quotes"]["probe_minutes"] = 33
        cfg["intelligence"]["company_refresh_hours"] = 2
        cfg["intelligence"]["llm_daily_budget_usd"] = 9.0
        cfg["source_overrides"] = {"some_feed": {"enabled": False}}
        cfg["maintenance"]["enabled"] = False
        self.assertFalse(stamp.record(c, cfg, NOW + dt.timedelta(hours=1))["changed"])
        self.assertEqual(len(stamp.history(c)), 1)

    def test_a_version_bump_creates_an_entry_and_history_is_bounded(self):
        c, cfg = H.fresh_db(), H.cfg()
        stamp.record(c, cfg, NOW)
        old = stamp.STRATEGY_VERSION
        try:
            stamp.STRATEGY_VERSION = old + "-next"
            self.assertTrue(stamp.record(c, cfg, NOW + dt.timedelta(days=1))["changed"])
            self.assertEqual(stamp.current(c)["note"], "strategy version changed")
        finally:
            stamp.STRATEGY_VERSION = old
        for i in range(stamp.MAX_HISTORY + 20):
            stamp.record(c, cfg, NOW + dt.timedelta(days=2, minutes=i), note="n%d" % i)
        self.assertEqual(len(stamp.history(c)), stamp.MAX_HISTORY)

    def test_every_cycle_records_it_and_reports_it(self):
        import test_pretrade as TP
        import test_trade_lifecycle as TL
        w = TP.Scenario()
        w.setUp()
        try:
            rep = w.cyc(TL.T_PRE)
            self.assertEqual(rep["strategy"]["version"], stamp.STRATEGY_VERSION)
            self.assertEqual(len(stamp.history(w.c)), 1)
            w.cyc(TL.T_PRE + dt.timedelta(minutes=5))
            self.assertEqual(len(stamp.history(w.c)), 1)
        finally:
            w.tearDown()


class TestTheDashboardAndCommand(unittest.TestCase):
    def test_the_intelligence_tab_states_the_strategy_and_waiting_proposals(self):
        from paper_agent import dashboard
        c, cfg = H.fresh_db(), H.cfg()
        stamp.record(c, cfg, NOW)
        seed(c)
        learning.recalibrate(c, cfg, NOW)
        html = dashboard.render(c, cfg, "intelligence", {}, {"authed": True})
        self.assertIn("Strategy <b>%s</b>" % stamp.STRATEGY_VERSION, html)
        self.assertIn("learning: <b>observe only</b>", html)
        self.assertIn("proposal(s)", html)
        self.assertIn("not used by decisions", html)

    def test_the_command_reviews_and_promotes(self):
        import io
        import contextlib
        import tempfile
        from paper_agent import __main__ as cli
        d = tempfile.mkdtemp()
        path = os.path.join(d, "t.db")
        c = db.open_db(path)
        seed(c)
        learning.recalibrate(c, H.cfg(), NOW)
        c.close()
        cli.DB_PATH = path
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["learning"]), 0)
        self.assertIn("learning mode: observe (decisions unchanged)", buf.getvalue())
        self.assertIn("parameter proposal(s)", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["learning", "--promote"])
        self.assertIn("dry run: add --apply", buf.getvalue())
        c = db.open_db(path)
        self.assertIsNone(db.param(c, "calibration"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["learning", "--promote", "--apply"])
        self.assertIn("MADE LIVE", buf.getvalue())
        c = db.open_db(path)
        self.assertIsNotNone(db.param(c, "calibration"))


if __name__ == "__main__":
    unittest.main()
