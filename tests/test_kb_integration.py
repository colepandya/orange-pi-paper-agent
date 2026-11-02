"""Build 45 across module boundaries: the knowledge stage inside a real cycle, the trader hook, read-only dashboards, the command line, the weekly follow-up and the evaluation datasets."""
import contextlib
import datetime as dt
import io
import json
import os
import sys
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
import test_fake_internet as FI_T  # noqa: E402
from paper_agent import dashboard, db, kb, kbbench, kbcli, kbeval, kbstage, net, pipeline, settings as S, settings_ui as U  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

T0 = FI_T.T0
NOW = dt.datetime(2026, 11, 1, 3, 0, tzinfo=dt.timezone.utc)


class KbEnv(FI_T.Env):
    def morning_kb(self, kb_on=True, cycles=10, patch=None):
        c = H.fresh_db()
        cfg = H.cfg()
        cfg["strategy"]["swing_enabled"] = False
        cfg["runtime"]["min_available_memory_mb"] = 0
        cfg["kb"]["enabled"] = kb_on
        errors = []
        inet = FI_T.FI.Internet(T0, self.co, self.heads, 1, 0.0, False, False)
        reports = []
        with inet, (patch or contextlib.nullcontext()):
            for k in range(cycles):
                now = T0 + dt.timedelta(minutes=5 * k)
                inet.now = now
                r = pipeline.run_cycle(c, cfg, self.uni, FI_T.SRC, pipeline.Deps(sleep=lambda s: None), now)
                errors += r["errors"]
                reports.append(r)
        return c, errors, reports

    @staticmethod
    def trades(c):
        return ([tuple(r) for r in c.execute("SELECT ticker,action,status,ROUND(IFNULL(size_pct,0),6) FROM decisions ORDER BY id")],
                [tuple(r) for r in c.execute("SELECT ticker,status,ROUND(shares,6),ROUND(entry_price,6) FROM positions ORDER BY id")],
                [tuple(r) for r in c.execute("SELECT ts,ROUND(equity,4),ROUND(cash,4) FROM portfolio_snapshots ORDER BY id")])


class TestTheKnowledgeStageInsideARealCycle(KbEnv):
    def test_with_every_mode_on_observe_the_knowledge_base_changes_not_one_decision_position_or_dollar(self):
        off, e1, _ = self.morning_kb(kb_on=False)
        on, e2, _ = self.morning_kb(kb_on=True)
        self.assertEqual((e1, e2), ([], []))
        self.assertEqual(self.trades(off), self.trades(on))
        self.assertGreater(len(self.trades(on)[0]), 5)
        self.assertEqual(off.execute("SELECT COUNT(*) FROM kb_entities").fetchone()[0], 0)
        self.assertGreater(on.execute("SELECT COUNT(*) FROM kb_entities").fetchone()[0], 0)

    def test_the_stage_builds_the_universe_identity_once_and_records_what_it_did(self):
        c, errors, reports = self.morning_kb(cycles=4)
        self.assertEqual(errors, [])
        self.assertEqual(kb.verify_universe(c, self.uni.watch), [])
        self.assertTrue(all(r["kb"]["status"] in ("ok", "partial") for r in reports))
        self.assertEqual(sum(1 for r in reports if (r["kb"].get("identity") or {}).get("synced")), 1)      # synced in the first cycle, a no-op afterwards
        self.assertGreater(c.execute("SELECT COUNT(*) FROM event_lifecycle").fetchone()[0], 0)

    def test_a_crashing_knowledge_stage_never_stops_trading(self):
        base, _, _ = self.morning_kb(kb_on=False)

        def boom(*a, **k):
            raise RuntimeError("knowledge gremlin")
        c, errors, _ = self.morning_kb(patch=unittest.mock.patch.object(kbstage, "run", boom))
        self.assertTrue(any("kb" in e for e in errors))                                                   # recorded, visibly
        self.assertEqual(self.trades(base), self.trades(c))                                              # and trading is unaffected

    def test_a_failing_substep_is_isolated_from_the_others(self):
        c = H.fresh_db()
        cfg = H.cfg()
        out = kbstage.run(c, cfg, self.uni.watch, NOW, None, sleep=lambda s: None) if False else None
        import paper_agent.kbsnap as ks
        orig = ks.snapshot_batch
        ks.snapshot_batch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            out = kbstage.run(c, cfg, self.uni.watch, NOW, None, sleep=lambda s: None)
        finally:
            ks.snapshot_batch = orig
        self.assertEqual(out["status"], "partial")
        self.assertTrue(any("snapshots" in e for e in out["errors"]))
        self.assertIn("identity", out)
        self.assertIn("events", out)

    def test_the_time_budget_skips_remaining_steps_instead_of_overrunning(self):
        c = H.fresh_db()
        cfg = H.cfg()
        ticks = iter([0.0] + [999.0] * 100)
        out = kbstage.run(c, cfg, self.uni.watch, NOW, None, clock=lambda: next(ticks), sleep=lambda s: None)
        self.assertTrue(out.get("skipped_for_time"))

    def test_new_events_are_processed_in_bounded_batches_and_never_reprocessed(self):
        c = H.fresh_db()
        cfg = H.cfg()
        cfg["kb"]["propagate_events_per_cycle"] = 5
        kb.bootstrap(c, self.uni.watch, NOW)
        tk = list(self.uni.watch)[0]
        for i in range(30):
            t = iso(NOW - dt.timedelta(hours=1))
            c.execute("INSERT INTO events(news_uid,created,published,source,headline,event_type,scope,direction,magnitude,tickers_json,cluster_id,novelty,credibility,stale) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      ("n%d" % i, t, t, "wire", "%s beats earnings estimates %d" % (tk, i), "earnings_beat", "company", 0.7, 0.7, json.dumps([tk]), i + 100, 1.0, 0.9, 0))
        c.execute("INSERT OR REPLACE INTO meta VALUES('kb_last_event_id','0')")
        r1 = kbstage.process_new_events(c, cfg, NOW, lambda: 0.0, 1e9)
        self.assertEqual(r1["catalysts"], 5)
        r2 = kbstage.process_new_events(c, cfg, NOW, lambda: 0.0, 1e9)
        n = c.execute("SELECT COUNT(DISTINCT event_id) FROM event_lifecycle").fetchone()[0]
        self.assertEqual(n, 10)                                                                           # 5 then 5 more: no event twice
        self.assertFalse(c.execute("SELECT 1 FROM kb_derived_impacts WHERE influence_class='trade'").fetchone())

    def test_identity_follows_a_changed_universe_without_losing_anyone(self):
        c = H.fresh_db()
        kbstage.sync_identity(c, self.uni.watch, NOW)
        smaller = dict(list(self.uni.watch.items())[:-1])
        r = kbstage.sync_identity(c, smaller, NOW + dt.timedelta(days=7))
        self.assertTrue(r["synced"])
        self.assertEqual(kb.verify_universe(c, smaller), [])
        self.assertFalse(kbstage.sync_identity(c, smaller, NOW + dt.timedelta(days=8))["synced"])


class TestTraderHookAndReadOnlyViews(KbEnv):
    def test_every_red_team_review_has_an_intervention_record_and_none_is_invented(self):
        c, errors, _ = self.morning_kb(cycles=12)
        self.assertEqual(errors, [])
        n_rev = c.execute("SELECT COUNT(*) FROM redteam_reviews").fetchone()[0]
        n_iv = c.execute("SELECT COUNT(*) FROM redteam_interventions").fetchone()[0]
        self.assertEqual(n_rev, n_iv)
        for r in c.execute("SELECT final_action,original_size_pct,final_size_pct,combined_mult FROM redteam_interventions"):
            self.assertIn(r["final_action"], ("allowed", "reduced", "blocked"))
            if r["final_action"] == "allowed":
                self.assertEqual(r["original_size_pct"], r["final_size_pct"])

    def test_every_dashboard_ask_and_why_path_works_on_a_read_only_connection(self):
        c, _, _ = self.morning_kb(cycles=8)
        path = c.execute("PRAGMA database_list").fetchone()[2]
        c.commit()
        ro = db.connect(path, readonly=True)
        cfg = H.cfg()
        for tab in dashboard.TABS:
            if tab != "settings":
                self.assertIn("<", dashboard.render(ro, cfg, tab, {}, {"authed": True}), tab)
        ids = [r[0] for r in ro.execute("SELECT id FROM decisions ORDER BY id DESC LIMIT 6")]
        self.assertTrue(ids)
        for did in ids:
            self.assertIn("What happened", dashboard.render(ro, cfg, "decisions", {"id": [str(did)]}, {"authed": True}))
        t = list(self.uni.watch)[0]
        for q in ({"t": [t]}, {"t": ["NOSUCH"]}, {}):
            dashboard.render(ro, cfg, "company", q, {"authed": True})
        self.assertIn("event chain", dashboard.render(ro, cfg, "intelligence", {"ask": ["why %s" % t]}, {"authed": True}))
        before = c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.assertEqual(before, ro.execute("SELECT COUNT(*) FROM decisions").fetchone()[0])

    def test_the_unified_why_page_is_the_same_explanation_from_both_doors(self):
        from paper_agent import whybundle
        c, _, _ = self.morning_kb(cycles=8)
        did = c.execute("SELECT id FROM decisions ORDER BY id LIMIT 1").fetchone()[0]
        cf = whybundle.case_file(c, H.cfg(), decision_id=did)
        keys = [s["key"] for s in cf["sections"]]
        self.assertEqual(keys, whybundle.SECTION_ORDER)
        for s in cf["sections"]:
            self.assertIn(s["status"], ("complete", "partial", "missing"))
            if s["status"] != "complete" and s["key"] not in ("verdict", "missing", "provenance"):
                self.assertTrue(s["missing"] or s["summary"], s["key"])
        html = whybundle.render(cf)
        self.assertIn("What happened", html)
        self.assertIn("Then versus now", html)
        self.assertIn("company knowledge", html.lower())


class TestSettingsForTheKnowledgeBase(unittest.TestCase):
    def test_every_kb_config_key_is_a_setting_on_the_page_and_defaults_are_valid(self):
        cfg = H.cfg()
        paths = {f.path for f in S.FIELDS if f.path.startswith("kb.")}
        self.assertEqual({"kb." + k for k in cfg["kb"]}, paths)
        self.assertEqual(U.layout_problems(), [])
        new, errors, _ = S.apply_form({f.name: S.display_value(f, cfg) for f in S.FIELDS if f.path.startswith("kb.")}, cfg, cfg, {})
        self.assertEqual(errors, [])
        self.assertEqual(new["kb"], cfg["kb"])

    def test_the_influence_modes_default_to_observe_and_none_may_be_active_except_where_implemented(self):
        cfg = H.cfg()
        for k in ("relationship_mode", "expiry_mode", "materiality_mode", "state_mode", "regime_mode"):
            self.assertEqual(cfg["kb"][k], "observe", k)
        self.assertEqual(S.BY_PATH["kb.materiality_mode"].choices, ["off", "observe"])
        self.assertEqual(S.BY_PATH["kb.state_mode"].choices, ["off", "observe"])
        self.assertEqual(S.BY_PATH["kb.regime_mode"].choices, ["off", "observe"])
        self.assertIn("active", S.BY_PATH["kb.relationship_mode"].choices)

    def test_the_strategy_fingerprint_moves_only_when_a_mode_leaves_observe(self):
        from paper_agent import stamp
        cfg = H.cfg()
        base = stamp.fingerprint(cfg)
        cfg["kb"]["max_hops"] = 1
        cfg["kb"]["refresh_companies_per_cycle"] = 99
        self.assertEqual(stamp.fingerprint(cfg), base)                                                    # limits are not strategy
        cfg["kb"]["relationship_mode"] = "active"
        self.assertNotEqual(stamp.fingerprint(cfg), base)


class TestCommandLineAndWeeklyFollowUp(unittest.TestCase):
    def setUp(self):
        self.c = H.fresh_db()
        self.cfg = H.cfg()
        self.uni = FI_T.TR.uni()
        self._u = kbcli._universe
        kbcli._universe = lambda: self.uni
        kb.bootstrap(self.c, self.uni.watch, NOW)

    def tearDown(self):
        kbcli._universe = self._u

    def cli(self, cmd, **kw):
        args = types.SimpleNamespace(cmd=cmd, what=kw.pop("what", None), ticker=kw.pop("ticker", None), dry_run=kw.pop("dry_run", False), limit=None, only="", max_batches=None, json=False, tickers="V", file="", save=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = kbcli.run(args, self.c, self.cfg)
        return rc, buf.getvalue()

    def test_each_kb_command_runs_and_reports(self):
        for what, needle in (("reconcile", "universe_rows"), ("coverage", "COVERAGE BY DATA CATEGORY"), ("unresolved", "no_cik"), ("provenance", "kb_relationships"), ("stale", "never_refreshed_from_sec"), ("universe-report", "operating_companies_by_name")):
            rc, out = self.cli("kb", what=what)
            self.assertEqual(rc, 0, what)
            self.assertIn(needle, out, what)

    def test_bootstrap_dry_run_changes_nothing_and_a_real_run_preserves_the_universe(self):
        c = H.fresh_db()
        self.c = c
        rc, out = self.cli("kb", what="bootstrap", dry_run=True)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM kb_entities").fetchone()[0], 0)
        rc, out = self.cli("kb", what="bootstrap")
        self.assertIn("universe preserved exactly: True", out)

    def test_the_company_command_lists_every_section_and_what_is_missing(self):
        t = list(self.uni.watch)[0]
        rc, out = self.cli("company", ticker=t)
        self.assertEqual(rc, 0)
        for title in ("What the company is", "Related companies", "The red team", "What is missing"):
            self.assertIn(title, out)

    def test_the_resource_report_measures_latency_memory_and_one_stage(self):
        r = kbbench.resources(self.c, self.cfg, self.uni.watch)
        for k in ("database_mb", "kb_rows", "latency", "one_knowledge_stage_seconds", "peak_memory_mb"):
            self.assertIn(k, r)

    def test_the_weekly_follow_up_syncs_refreshes_and_cannot_raise(self):
        sec = lambda url, headers=None, timeout=20: ({"cik": url.rsplit("CIK", 1)[1].split(".")[0], "name": "Co", "sic": "7372", "entityType": "operating", "filings": {"recent": {}}}, {})
        r = kbcli.after_universe(self.c, self.cfg, self.uni.watch, NOW, sec, sleep=lambda s: None)
        self.assertGreater(r["refresh"]["refreshed"], 0)
        self.assertNotIn("errors", r)
        self.assertGreater(self.c.execute("SELECT COUNT(*) FROM kb_entities WHERE sic='7372'").fetchone()[0], 0)

        def blocked(url, headers=None, timeout=20):
            raise net.FetchError("rate limited", 429)
        r2 = kbcli.after_universe(H.fresh_db(), self.cfg, self.uni.watch, NOW, blocked, sleep=lambda s: None)
        self.assertEqual(r2["refresh"]["status"], "stopped")


class TestTheRealEntryPoint(unittest.TestCase):
    """`python3 -m paper_agent kb ...` exactly as an owner runs it: a subprocess with its own config folder, state folder and database. (Tests that call the command module directly cannot notice a missing route.)"""

    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(prefix="pa-entry-")
        os.makedirs(self.d + "/cfg")
        os.makedirs(self.d + "/state")
        W = {"AAA": {"name": "Alpha Corp", "cik": 1, "exchange": "NYSE", "aliases": ["Alpha"], "verified": True}, "FND": {"name": "Foo Income Fund Inc", "cik": 2, "exchange": "NYSE", "aliases": [], "verified": True}, "NOC": {"name": "No Cik Inc", "cik": None, "exchange": "NYSE", "aliases": []}}
        json.dump(W, open(self.d + "/cfg/watchlist.json", "w"))
        self.env = dict(os.environ, PAPER_AGENT_CONFIG_DIR=self.d + "/cfg", PAPER_AGENT_STATE_DIR=self.d + "/state", PAPER_AGENT_DB=self.d + "/state/paper-agent.db", PYTHONDONTWRITEBYTECODE="1")

    def run_cmd(self, *args, timeout=120):
        import subprocess
        r = subprocess.run([sys.executable, "-m", "paper_agent"] + list(args), cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), env=self.env, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr

    def test_each_new_command_is_routed_runs_and_prints(self):
        for args, needle in ((("kb", "reconcile"), "universe_rows"), (("kb", "bootstrap", "--dry-run"), "would_add"), (("kb", "bootstrap"), "universe preserved exactly: True"), (("kb", "coverage"), "COVERAGE BY DATA CATEGORY"),
                             (("kb", "unresolved"), "NOC"), (("kb", "provenance"), "kb_relationships"), (("kb", "stale"), "never_refreshed_from_sec"), (("kb", "universe-report"), "closed_end_funds_by_name"),
                             (("kb", "refresh", "--dry-run"), "dry_run"), (("company", "AAA"), "What the company is"), (("kb", "resources"), "peak_memory_mb")):
            rc, out, err = self.run_cmd(*args)
            self.assertEqual(rc, 0, "%s -> %s %s" % (args, out[-200:], err[-300:]))
            self.assertIn(needle, out, "%s printed nothing useful: %r" % (args, out[:200]))

    def test_the_evaluation_runs_from_the_shipped_datasets_and_never_prints_a_combined_figure(self):
        rc, out, err = self.run_cmd("eval", "--file", os.path.join(os.path.dirname(kbcli.__file__), "evalset", "real_news_validation_v1.json"), timeout=250)
        self.assertEqual(rc, 0, err[-400:])
        self.assertIn("entity_precision", out)
        self.assertIn("abstention_recall", out)
        self.assertIn("No combined accuracy figure", out)

    def test_an_unknown_command_still_fails_loudly(self):
        rc, out, err = self.run_cmd("kb", "nonsense")
        self.assertNotEqual(rc, 0)


class TestWeeklyFollowUpIsolation(unittest.TestCase):
    def test_a_step_that_blows_up_is_reported_and_the_rest_still_runs(self):
        c = H.fresh_db()
        uni = FI_T.TR.uni()
        orig = kb.refresh
        kb.refresh = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk"))
        try:
            r = kbcli.after_universe(c, H.cfg(), uni.watch, NOW, lambda *a, **k: ({}, {}), sleep=lambda s: None)
        finally:
            kb.refresh = orig
        self.assertTrue(any("refresh" in e for e in r["errors"]))
        self.assertIn("identity", r)
        self.assertEqual(kb.verify_universe(c, uni.watch), [])


class TestEvaluationDatasets(unittest.TestCase):
    FILES = ("real_news_v1", "real_news_holdout_v1", "real_news_family_v1", "real_news_validation_v1")

    def load(self):
        return {f: json.load(open(os.path.join(os.path.dirname(kbcli.__file__), "evalset", f + ".json"))) for f in self.FILES}

    def test_the_eval_command_runs_every_dataset_that_ships_and_no_other(self):
        shipped = sorted(f[:-5] for f in os.listdir(os.path.join(os.path.dirname(kbcli.__file__), "evalset")) if f.endswith(".json"))
        self.assertEqual(sorted(kbcli.DATASETS), shipped)
        self.assertEqual(sorted(self.FILES), shipped)

    def test_every_case_is_labelled_with_the_documented_schema_and_vocabulary(self):
        fam = {"results", "contracts", "regulatory", "legal", "operations", "corporate", "distress", "capital", "people", "analyst", "other", "none"}
        for f, d in self.load().items():
            self.assertTrue(d["labeler"])
            for c in d["cases"]:
                L = c["label"]
                self.assertIn(L["family"], fam, (f, c["case_id"]))
                self.assertIn(L["state"], (None, "confirmed", "rumored", "denied", "proposed"))
                self.assertIn(L["direction"], (None, 1, -1, 0))
                self.assertIsInstance(L["abstain"], bool)
                for t, role in L["involved"].items():
                    self.assertIn(role, ("subject", "party", "mentioned_only"))

    def test_no_story_appears_in_two_datasets_so_the_validation_set_is_not_the_tuning_set(self):
        seen = {}
        for f, d in self.load().items():
            for c in d["cases"]:
                self.assertNotIn(c["event_id"], seen, "%s also in %s" % (c["case_id"], seen.get(c["event_id"])))
                seen[c["event_id"]] = f

    def test_scoring_keeps_every_aspect_separate_and_never_returns_one_combined_accuracy(self):
        cases = [{"case_id": "a", "stratum": "s", "headline": "x", "label": {"primary": "AAA", "involved": {"AAA": "subject"}, "family": "results", "state": "confirmed", "direction": 1, "abstain": False}},
                 {"case_id": "b", "stratum": "s", "headline": "424B2 - X (0000000001) (Filer)", "label": {"primary": None, "involved": {}, "family": "none", "state": None, "direction": None, "abstain": True}}]
        reads = {"a": {"tickers": ["AAA", "BBB"], "abstained": False, "family": "results", "state": "confirmed", "direction": {"AAA": 1}, "materiality": "high", "timeframe": "medium", "roles": {"AAA": "named in the event"}},
                 "b": {"tickers": [], "abstained": True, "family": None, "state": None, "direction": {}, "materiality": None, "timeframe": None, "roles": {}}}
        m = kbeval.score(cases, reads, {c["case_id"]: c["label"] for c in cases})
        self.assertEqual((m["entity_precision"]["value"], m["entity_recall"]["value"], m["false_company_match_rate"]["value"]), (0.5, 1.0, 0.5))
        self.assertEqual((m["abstention_precision"]["value"], m["abstention_recall"]["value"]), (1.0, 1.0))
        self.assertEqual(m["direction_accuracy_when_stated"]["value"], 1.0)
        self.assertNotIn("accuracy", m)
        self.assertNotIn("overall", m)

    def test_a_labelled_filing_is_judged_against_its_own_cik_not_a_guess(self):
        self.assertEqual(kbeval.abstain_basis({"headline": "424B2 - X (0000000001) (Filer)", "label": {"abstain": True, "family": "none", "direction": None}}), "filing_no_content")
        self.assertEqual(kbeval.abstain_basis({"headline": "Acme recalls a product", "label": {"abstain": True, "family": "operations", "direction": -1, "materiality": "low"}}), "immaterial_event")
        self.assertEqual(kbeval.abstain_basis({"headline": "Will Acme rise?", "label": {"abstain": True, "family": "none", "direction": None}}), "non_event")
        self.assertIsNone(kbeval.abstain_basis({"headline": "Acme beats", "label": {"abstain": False, "family": "results", "direction": 1}}))


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()
