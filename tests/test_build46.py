"""Build 46: matcher ambiguity and alias hygiene, structural rejection state, share-class tickers, filing retention, the facts pass, and the command-line permission behaviour. Written from the intended behaviour."""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
from paper_agent import db, filingintel as F, kb, kbcli, kbrel, kbsnap, net, reasoning as R, universe  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

NOW = dt.datetime(2026, 11, 3, 3, 0, tzinfo=dt.timezone.utc)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestAliasHygiene(unittest.TestCase):
    def test_a_cut_off_name_is_never_generated_as_an_alias(self):
        for name in ("Cohen & Steers, Inc.", "Smith & Wesson Brands, Inc.", "Flaherty & Crumrine Preferred Income Fund Inc"):
            for a in universe.aliases(name):
                self.assertFalse(a.rstrip().endswith("&"), a)
        self.assertIn("Cohen & Steers", universe.aliases("Cohen & Steers, Inc."))                          # the whole name still is one

    def test_a_fund_does_not_inherit_its_sponsors_name(self):
        al = universe.aliases("BlackRock MuniYield Quality Fund III, Inc.")
        self.assertNotIn("BlackRock MuniYield", al)
        self.assertEqual(universe.aliases("Morgan Stanley China A Share Fund, Inc.")[-1], "Morgan Stanley China A Share Fund")
        self.assertIn("Goldman Sachs", universe.aliases("Goldman Sachs Group, Inc.") + universe.aliases("Goldman Sachs BDC, Inc."))   # an operating company keeps its short name

    def test_the_short_name_loses_a_trailing_connector_so_the_useful_name_survives(self):
        self.assertEqual(universe.aliases("JPMORGAN CHASE & CO"), ["JPMORGAN CHASE & CO", "JPMORGAN CHASE"])
        self.assertEqual(universe.aliases("DEERE & CO"), ["DEERE & CO", "DEERE"])
        self.assertIn("Arthur J Gallagher", universe.aliases("Arthur J. Gallagher & Co."))
        for name in ("KKR & Co. Inc.", "Becton, Dickinson and Company", "Inter & Co, Inc."):
            for a in universe.aliases(name):
                self.assertFalse(a.rstrip().endswith(("&", " and")), (name, a))

    def test_the_matcher_ignores_dangling_aliases_already_sitting_in_a_universe_file(self):
        U = Universe({"CNS": {"name": "Cohen & Steers, Inc.", "aliases": ["Cohen & Steers", "Cohen &", "Bank of"], "cik": 1284812}}, set())
        self.assertEqual([m[0] for m in U.match("Cohen & Steers declares dividend", "")], ["CNS"])
        self.assertEqual(U.match("Bank of America and a Cohen & Wolf filing", ""), [])


class TestAmbiguousPhrases(unittest.TestCase):
    W = {"TTC": {"name": "Toro Co", "aliases": ["Toro"], "cik": 737758}, "TORO": {"name": "Toro Corp.", "aliases": ["Toro"], "cik": 1941131},
         "MS": {"name": "MORGAN STANLEY", "aliases": ["MORGAN STANLEY"], "cik": 895421}, "MSD": {"name": "Morgan Stanley Emerging Markets Debt Fund", "aliases": ["Morgan Stanley Emerging Markets Debt Fund", "Morgan Stanley"], "cik": 99},
         "GS": {"name": "GOLDMAN SACHS GROUP INC", "aliases": ["Goldman Sachs Group", "Goldman Sachs"], "cik": 886982}, "GSBD": {"name": "Goldman Sachs BDC, Inc.", "aliases": ["Goldman Sachs BDC", "Goldman Sachs"], "cik": 1572694},
         "GOOGL": {"name": "Alphabet Inc.", "aliases": ["Alphabet"], "cik": 1652044}, "GOOG": {"name": "Alphabet Inc.", "aliases": ["Alphabet"], "cik": 1652044},
         "BEP": {"name": "Brookfield Renewable Partners L.P.", "aliases": ["Brookfield Renewable"], "cik": 1533232}, "BEPC": {"name": "Brookfield Renewable Corp", "aliases": ["Brookfield Renewable"], "cik": 1791986}}

    def hits(self, h):
        return {t: round(c, 2) for t, c, w in Universe(self.W, set()).match(h, "") if c >= 0.5}

    def test_a_name_two_different_companies_share_names_neither(self):
        self.assertEqual(self.hits("Toro reports third quarter results"), {})

    def test_the_company_whose_own_legal_name_is_the_phrase_keeps_it(self):
        self.assertEqual(list(self.hits("Morgan Stanley raises its price target")), ["MS"])
        self.assertEqual(list(self.hits("Goldman Sachs raises forecast")), ["GS"])

    def test_share_classes_of_one_issuer_stay_together(self):
        self.assertEqual(sorted(self.hits("Alphabet shares rise")), ["GOOG", "GOOGL"])

    def test_an_explicit_ticker_or_exchange_tag_settles_an_ambiguous_name(self):
        self.assertIn("TORO", self.hits("Toro (NASDAQ: TORO) reports results"))
        self.assertLessEqual(len([t for t in self.hits("Brookfield Renewable reports Q3 results")]), 1)  # the same economics under two listings: at most one is named, never an arbitrary pair

    def test_the_ambiguous_matches_are_recorded_below_the_trading_floor_not_lost(self):
        raw = {t: c for t, c, w in Universe(self.W, set()).match("Toro reports third quarter results", "")}
        self.assertTrue(raw)
        self.assertTrue(all(c < 0.5 for c in raw.values()))


class TestStructuralMatcherRules(unittest.TestCase):
    W = {"NE": {"name": "Noble Corp plc", "aliases": ["Noble Corp", "Noble"], "cik": 1}, "MSFT": {"name": "MICROSOFT CORP", "aliases": ["Microsoft"], "cik": 789019}, "LLY": {"name": "ELI LILLY & CO", "aliases": ["Eli Lilly"], "cik": 59478},
         "NDAQ": {"name": "NASDAQ, INC.", "aliases": ["NASDAQ INC", "NASDAQ"], "cik": 1120193}, "GOOGL": {"name": "Alphabet Inc.", "aliases": ["Alphabet"], "cik": 1652044}, "GRMN": {"name": "GARMIN LTD", "aliases": ["Garmin"], "cik": 1121788},
         "TRP": {"name": "Trinity Rail Partners Inc", "aliases": ["Trinity Rail"], "cik": 5}, "NVDA": {"name": "NVIDIA CORP", "aliases": ["Nvidia"], "cik": 1045810}}

    def hits(self, h, s=""):
        return sorted(t for t, c, w in Universe(self.W, set()).match(h, s) if c >= 0.5)

    def test_a_full_name_the_headline_states_that_is_not_the_listed_companys_is_not_a_match(self):
        self.assertEqual(self.hits("NOBLE SUPPLY & LOGISTICS, LLC federal contract award"), [])
        self.assertEqual(self.hits("Noble Supply & Logistics LLC wins federal contract"), [])
        self.assertEqual(self.hits("Noble Corp plc reports third-quarter results"), ["NE"])

    def test_the_same_company_written_in_other_forms_still_matches(self):
        for h in ("Shares Of Microsoft Corp Rise After Earnings", "Microsoft Corp. raises dividend", "Eli Lilly and Co raises outlook", "Eli Lilly & Co raises outlook", "Garmin Ltd beats estimates"):
            self.assertTrue(self.hits(h), h)

    def test_an_exchange_named_as_a_venue_is_not_the_company_that_runs_it(self):
        self.assertEqual(self.hits("Alphabet's stock falls 12%", "The stock fell on the Nasdaq on Friday."), ["GOOGL"])
        self.assertEqual(self.hits("Stocks slide as Nasdaq drops 2%"), [])
        self.assertEqual(self.hits("Nasdaq Inc reports third-quarter earnings"), ["NDAQ"])

    def test_a_lone_body_mention_of_a_one_word_name_is_weak_when_the_headline_names_nobody(self):
        self.assertEqual(self.hits("Heat wave grips the region", "Garmin was mentioned once in passing."), [])
        self.assertEqual(self.hits("Garmin raises forecast", ""), ["GRMN"])
        self.assertEqual(self.hits("Wearables maker raises forecast", "Garmin said demand rose. Garmin also lifted guidance."), ["GRMN"])      # named twice: it is the subject
        self.assertEqual(self.hits("Chipmaker beats estimates", "Nvidia said demand stayed strong."), ["NVDA"])                             # a brand name is not a lone common word


class TestRejectionIsAVerbOnAProposal(unittest.TestCase):
    def test_policy_stories_and_infinitives_are_not_rejections(self):
        for h in ("Nvidia’s Megaspeed ties show blind spots in the U.S. effort to block China from accessing chips", "Judge blocks new rules on chip exports", "Senators want to block the sale of chips to China"):
            self.assertNotEqual(R.event_state(h), "rejected", h)

    def test_a_blocked_or_turned_down_proposal_is_a_rejection(self):
        for h in ("FTC blocks Microsoft's acquisition of Activision", "Regulators turned down the merger application", "The bill was blocked in the Senate", "Shareholders vetoed the plan"):
            self.assertEqual(R.event_state(h), "rejected", h)

    def test_a_denial_of_a_report_is_still_a_denial(self):
        self.assertEqual(R.event_state("Company denies it is in talks to buy Target"), "denied")


class TestShareClassTickerFollowsTheRelationshipsOwnText(unittest.TestCase):
    def setUp(self):
        U = {"NVDA": {"name": "NVIDIA Corp", "cik": 1045810, "exchange": "Nasdaq", "aliases": []}, "GOOG": {"name": "Alphabet Inc.", "cik": 1652044, "exchange": "Nasdaq", "aliases": []}, "GOOGL": {"name": "Alphabet Inc.", "cik": 1652044, "exchange": "Nasdaq", "aliases": []}}
        self.c = db.open_db(":memory:")
        kb.bootstrap(self.c, U, NOW)
        kbsnap.import_relationship_seed(self.c, NOW, seed={"edges": [{"src": "NVDA", "dst": "GOOGL", "kind": "supplies", "weight": 0.3, "note": "AI GPUs"}], "peer_groups": []})

    def test_a_ticker_named_in_the_mechanism_is_the_one_shown(self):
        e = kbsnap.entity_of(self.c, "GOOGL")
        self.assertEqual(kbrel._ticker(self.c, e, "NVDA supplies GOOGL: AI GPUs"), "GOOGL")
        self.assertEqual(kbrel._ticker(self.c, e, "NVDA supplies GOOG: AI GPUs"), "GOOG")
        self.assertIn(kbrel._ticker(self.c, e, "no ticker named here"), ("GOOG", "GOOGL"))                 # the primary

    def test_the_effect_is_attributed_to_the_listing_the_seed_named(self):
        imps = kbrel.propagate(self.c, H.cfg(), {"event_id": 1, "ticker": "NVDA", "event_type": "earnings_beat", "direction": 0.8, "magnitude": 0.8, "state": "confirmed"}, NOW, persist=False)
        self.assertEqual({i["dst_ticker"] for i in imps if i["hops"] == 1}, {"GOOGL"})


class TestFilingRetention(unittest.TestCase):
    def setUp(self):
        self.c = db.open_db(":memory:")
        kb.bootstrap(self.c, {"AAA": {"name": "Alpha Corp", "cik": 1, "exchange": "NYSE", "aliases": []}}, NOW)
        self.eid = kbsnap.entity_of(self.c, "AAA")

    def sub(self, filings):
        return {"cik": 1, "filings": [dict(accession="0000000001-26-%06d" % i, form=f, filed=d, period=None, items=it, doc="x.htm") for i, (f, d, it) in enumerate(filings)]}

    def test_routine_note_supplements_are_not_stored_at_all(self):
        F.ingest_submissions(self.c, self.eid, "AAA", self.sub([("424B2", "2026-10-30", ""), ("424B3", "2026-10-30", ""), ("424B5", "2026-10-30", "")]), NOW)
        self.assertEqual([r[0] for r in self.c.execute("SELECT form FROM kb_filing_events")], ["424B5"])

    def test_noisy_types_are_a_capped_recent_window_and_material_ones_are_never_pruned(self):
        rows = [("4", "2026-10-%02d" % (1 + i % 28), "") for i in range(40)] + [("8-K", "2026-09-15", "4.01"), ("8-K", "2026-09-10", "1.03"), ("NT 10-Q", "2026-08-20", ""), ("8-K", "2026-10-02", "7.01")]
        out = F.ingest_submissions(self.c, self.eid, "AAA", self.sub(rows), NOW, 120, 60)
        by = dict(self.c.execute("SELECT event_type,COUNT(*) FROM kb_filing_events GROUP BY event_type").fetchall())
        self.assertEqual(by["insider_transaction"], 12)
        for material in ("auditor_change", "bankruptcy_or_restructuring", "late_quarterly_report"):
            self.assertEqual(by[material], 1, material)
        self.assertGreater(out["pruned"], 0)

    def test_the_total_cap_drops_the_oldest_routine_rows_first(self):
        rows = [(f, "2026-10-%02d" % (1 + i), "") for i, f in enumerate(["4", "3", "5", "6-K", "SC 13G", "10-K", "10-Q", "S-3", "424B5", "SC 13G/A"] * 2)]
        F.ingest_submissions(self.c, self.eid, "AAA", self.sub(rows), NOW, 120, 10)
        n = self.c.execute("SELECT COUNT(*) FROM kb_filing_events").fetchone()[0]
        self.assertLessEqual(n - self.c.execute("SELECT COUNT(*) FROM kb_filing_events WHERE event_type IN ('shelf_registration')").fetchone()[0], 10)

    def test_material_events_survive_both_the_per_type_cap_and_the_total_cap(self):
        rows = [("8-K", "2026-10-%02d" % (1 + i % 28), "1.01") for i in range(20)] + [("4", "2026-10-%02d" % (1 + i % 28), "") for i in range(30)]
        F.ingest_submissions(self.c, self.eid, "AAA", self.sub(rows), NOW, 120, 15)
        by = dict(self.c.execute("SELECT event_type,COUNT(*) FROM kb_filing_events GROUP BY event_type").fetchall())
        self.assertEqual(by["material_agreement"], 20)                                                      # more than both caps: a material event is never pruned
        self.assertEqual(by.get("insider_transaction", 0), 0)                                               # the routine rows paid for it: 20 material fill the 15-row budget

    def test_ingesting_the_same_filings_twice_changes_nothing(self):
        s = self.sub([("4", "2026-10-20", ""), ("8-K", "2026-10-21", "5.02")])
        F.ingest_submissions(self.c, self.eid, "AAA", s, NOW)
        a = self.c.execute("SELECT COUNT(*) FROM kb_filing_events").fetchone()[0]
        F.ingest_submissions(self.c, self.eid, "AAA", s, NOW)
        self.assertEqual(a, self.c.execute("SELECT COUNT(*) FROM kb_filing_events").fetchone()[0])


class TestFactsPass(unittest.TestCase):
    U = {"OPA": {"name": "Operating A Corp", "cik": 10, "exchange": "NYSE", "aliases": []}, "OPB": {"name": "Operating B Corp", "cik": 11, "exchange": "NYSE", "aliases": []}, "FND": {"name": "Foo Income Opportunity Fund Inc", "cik": 12, "exchange": "NYSE", "aliases": []}}

    def setUp(self):
        from paper_agent import facts
        self.facts = facts
        self.orig = facts.refresh
        self.calls = []
        facts.refresh = lambda conn, cfg, ticker, cik, fetch_json, now, sleep=None: self.calls.append(ticker) or {}
        self.c = db.open_db(":memory:")
        kb.bootstrap(self.c, self.U, NOW)
        kb.assign_priorities(self.c, NOW)
        self.cfg = H.cfg()

    def tearDown(self):
        self.facts.refresh = self.orig

    def test_only_operating_companies_are_fetched_in_priority_order(self):
        self.c.execute("UPDATE knowledge_coverage SET priority=2 WHERE ticker='OPB'")
        r = kb.refresh_facts(self.c, self.cfg, NOW, lambda *a, **k: ({}, {}), limit=5, sleep=lambda s: None)
        self.assertEqual(self.calls, ["OPB", "OPA"])
        self.assertEqual(r["refreshed"], 2)
        self.assertNotIn("FND", self.calls)

    def test_a_company_is_not_fetched_again_until_its_facts_are_due(self):
        kb.refresh_facts(self.c, self.cfg, NOW, lambda *a, **k: ({}, {}), limit=5, sleep=lambda s: None)
        self.assertIsNotNone(self.c.execute("SELECT facts_next_due FROM knowledge_coverage WHERE ticker='OPA'").fetchone()[0])

    def test_a_second_run_fetches_nothing_new_until_the_facts_are_due(self):
        kb.refresh_facts(self.c, self.cfg, NOW, lambda *a, **k: ({}, {}), limit=5, sleep=lambda s: None)
        first = list(self.calls)
        r2 = kb.refresh_facts(self.c, self.cfg, NOW + dt.timedelta(hours=1), lambda *a, **k: ({}, {}), limit=5, sleep=lambda s: None)
        self.assertEqual((self.calls, r2["refreshed"]), (first, 0))
        r3 = kb.refresh_facts(self.c, self.cfg, NOW + dt.timedelta(days=400), lambda *a, **k: ({}, {}), limit=5, sleep=lambda s: None)
        self.assertEqual(r3["refreshed"], 2)                                                                # due again after the data would be stale

    def test_zero_dry_run_and_the_time_budget(self):
        self.assertEqual(kb.refresh_facts(self.c, self.cfg, NOW, None, limit=0)["status"], "off")
        self.assertEqual(kb.refresh_facts(self.c, self.cfg, NOW, None, limit=5, dry_run=True)["count"], 2)
        ticks = iter([0.0, 0.0, 999.0, 999.0, 999.0])
        r = kb.refresh_facts(self.c, self.cfg, NOW, lambda *a, **k: ({}, {}), limit=5, budget_s=10, sleep=lambda s: None, clock=lambda: next(ticks))
        self.assertIn("budget", r["stopped"])

    def test_a_403_pauses_the_source_for_the_facts_pass_and_the_submissions_refresh_too(self):
        def boom(*a, **k):
            raise net.FetchError("blocked", 403)
        self.facts.refresh = boom
        r = kb.refresh_facts(self.c, self.cfg, NOW, None, limit=5, sleep=lambda s: None)
        self.assertIn("paused", r["stopped"])
        self.assertEqual(kb.refresh(self.c, self.cfg, NOW + dt.timedelta(minutes=5), lambda *a, **k: ({}, {}), sleep=lambda s: None)["status"], "source_backoff")

    def test_the_weekly_job_runs_it_only_when_asked_and_never_raises(self):
        sec = lambda url, headers=None, timeout=20: ({"cik": "10", "name": "Co", "sic": "7372", "entityType": "operating", "filings": {"recent": {}}}, {})
        self.cfg["kb"]["weekly_facts_companies"] = 0
        self.assertNotIn("facts", kbcli.after_universe(self.c, self.cfg, self.U, NOW, sec, sleep=lambda s: None))
        self.cfg["kb"]["weekly_facts_companies"] = 5
        self.assertIn("facts", kbcli.after_universe(self.c, self.cfg, self.U, NOW, sec, sleep=lambda s: None))

    def test_the_new_column_arrives_by_migration_on_an_existing_database(self):
        if db.sqlite3.sqlite_version_info < (3, 35, 0):
            self.skipTest("DROP COLUMN needs SQLite 3.35")
        self.c.execute("ALTER TABLE knowledge_coverage DROP COLUMN facts_next_due")
        self.assertNotIn("facts_next_due", [r[1] for r in self.c.execute("PRAGMA table_info(knowledge_coverage)")])
        db.migrate(self.c)
        self.assertIn("facts_next_due", [r[1] for r in self.c.execute("PRAGMA table_info(knowledge_coverage)")])
        self.assertEqual(db.SCHEMA_VERSION, 8)


class TestUniverseCheckReportsWhatTheExchangeFilterExcludes(unittest.TestCase):
    def test_excluded_exchanges_are_counted_with_a_sample_and_nothing_is_added(self):
        payload = {"fields": ["cik", "name", "ticker", "exchange"], "data": [[1, "A", "AAA", "NYSE"], [2, "B", "BBB", "TXSE"], [3, "C", "CCC", "TXSE"], [4, "D", "DDD", "OTC"], [5, "E", "TOOLONGX", "TXSE"]]}
        live = {"AAA": {"cik": 1, "exchange": "NYSE"}}
        r = kb.universe_check(payload, ["BBB"], live)
        self.assertEqual(r["excluded_by_exchange_label"]["TXSE"], {"count": 2, "sample": ["BBB", "CCC"]})
        self.assertEqual(r["excluded_by_exchange_label"]["OTC"]["count"], 1)
        self.assertIn("quote providers", r["exchange_note"])
        self.assertEqual(list(live), ["AAA"])
        self.assertNotIn("BBB", universe.build(payload))                                                  # the filter itself is unchanged


class TestCommandPermissions(unittest.TestCase):
    def a(self, cmd, what=None, **k):
        return types.SimpleNamespace(cmd=cmd, what=what, ticker=k.get("ticker"), dry_run=k.get("dry_run", False), save=k.get("save", False))

    def test_which_commands_only_look(self):
        for a in (self.a("kb", "coverage"), self.a("kb", "reconcile"), self.a("company", ticker="X"), self.a("eval"), self.a("kb", "bootstrap", dry_run=True), self.a("kb", "refresh", dry_run=True), self.a("kb", "universe-check")):
            self.assertTrue(kbcli.is_read_only(a), vars(a))
        for a in (self.a("kb", "bootstrap"), self.a("kb", "refresh"), self.a("kb", "resources"), self.a("eval", save=True)):
            self.assertFalse(kbcli.is_read_only(a), vars(a))

    def test_the_message_names_the_exact_command_to_type(self):
        m = kbcli.permission_message(self.a("kb", "bootstrap"), Exception("attempt to write a readonly database"))
        self.assertIn("sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent kb bootstrap", m)
        self.assertNotIn("Traceback", m)

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which("setpriv"), "needs root and setpriv to become an unprivileged user")
    def test_as_a_user_who_does_not_own_the_database_every_database_command_explains_instead_of_crashing(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="pa-perm-", dir="/tmp")                                                # (the shared helpers point TMPDIR at a private folder the other user cannot enter)
        shutil.copytree(os.path.join(ROOT, "paper_agent"), d + "/paper_agent", ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(d + "/cfg")
        os.makedirs(d + "/state")
        json.dump({"AAA": {"name": "Alpha Corp", "cik": 1, "exchange": "NYSE", "aliases": []}}, open(d + "/cfg/watchlist.json", "w"))
        env = dict(os.environ, PAPER_AGENT_CONFIG_DIR=d + "/cfg", PAPER_AGENT_STATE_DIR=d + "/state", PAPER_AGENT_DB=d + "/state/paper-agent.db", PYTHONDONTWRITEBYTECODE="1", HOME="/tmp")
        subprocess.run([sys.executable, "-m", "paper_agent", "kb", "bootstrap"], cwd=d, env=env, capture_output=True, check=True)
        subprocess.run(["chmod", "-R", "a+rX", d])

        def as_user(*args):
            return subprocess.run(["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", sys.executable, "-m", "paper_agent"] + list(args), cwd=d, env=env, capture_output=True, text=True)
        evalfile = os.path.join(d, "paper_agent", "evalset", "real_news_validation_v1.json")
        for args in (("kb", "coverage"), ("company", "AAA"), ("kb", "bootstrap"), ("kb", "refresh", "--limit", "1"), ("eval", "--file", evalfile)):       # eval WITH a file: the path that used to degrade silently
            r = as_user(*args)
            self.assertEqual(r.returncode, 2, (args, r.stdout[-200:], r.stderr[-200:]))
            self.assertIn("sudo -u paperagent", r.stdout)
            self.assertNotIn("Traceback", r.stdout + r.stderr)
        r = as_user("kb", "reconcile")                                                                     # needs only the universe file
        self.assertEqual(r.returncode, 0)
        self.assertIn("universe_rows", r.stdout)
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
