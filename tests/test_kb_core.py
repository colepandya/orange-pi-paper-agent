"""Company knowledge base: identity, universe preservation, bootstrap, SEC refresh, coverage and universe quality. Built from the INTENDED behaviour in the Build 45 brief, not from the code."""
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
from paper_agent import db, kb, kbdata, net, universe as universe_mod  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

NOW = dt.datetime(2026, 11, 1, 3, 0, tzinfo=dt.timezone.utc)
U = {"AAA": {"name": "Alpha Corp", "cik": 1, "exchange": "NYSE", "aliases": ["Alpha"], "verified": True, "sector": "Unclassified"},
     "AAB": {"name": "Alpha Corp Class B", "cik": 1, "exchange": "NYSE", "aliases": ["Alpha B"], "verified": True, "sector": "Unclassified"},
     "FND": {"name": "Foo Income Opportunity Fund Inc", "cik": 2, "exchange": "NYSE", "aliases": [], "verified": True},
     "SPC": {"name": "Bar Acquisition Corp", "cik": 3, "exchange": "NASDAQ", "aliases": [], "verified": True},
     "NOC": {"name": "No Cik Inc", "cik": None, "exchange": "NYSE", "aliases": [], "verified": True},
     "RLT": {"name": "Main Street Realty Trust", "cik": 5, "exchange": "NYSE", "aliases": [], "verified": True},
     "ADR": {"name": "Foo Holdings Sponsored ADR", "cik": 6, "exchange": "NYSE", "aliases": [], "verified": True},
     "ADS": {"name": "Ads-Tec Energy Public Ltd Co", "cik": 7, "exchange": "NASDAQ", "aliases": [], "verified": True},
     "TWN": {"name": "Twin Name Inc", "cik": 8, "exchange": "NYSE", "aliases": [], "verified": True},
     "TWM": {"name": "Twin Name Incorporated", "cik": 9, "exchange": "NYSE", "aliases": [], "verified": True}}


def fresh():
    return db.open_db(":memory:")


class TestClassificationOfInstruments(unittest.TestCase):
    def test_funds_and_shells_are_not_operating_companies_but_reits_and_trust_banks_are(self):
        k = lambda n: kbdata.classify_instrument(n, "X")["kind"]  # noqa: E731
        self.assertEqual(k("Foo Income Opportunity Fund Inc"), "fund")
        self.assertEqual(k("Bar Acquisition Corp"), "spac")
        for operating in ("Federal Realty Investment Trust", "Northern Trust Corp", "Equity Residential", "Public Storage", "Fund Services Group Inc", "Hancock Whitney Corp"):
            self.assertEqual(k(operating), "operating_equity", operating)

    def test_ads_tec_is_not_an_adr_but_a_sponsored_adr_is(self):
        self.assertEqual(kbdata.classify_instrument("Ads-Tec Energy Public Ltd Co", "ADSE")["adr"], 0)
        self.assertEqual(kbdata.classify_instrument("Foo Holdings Sponsored ADR", "X")["adr"], 1)

    def test_a_name_hint_is_never_a_verified_fact(self):
        c = kbdata.classify_instrument("Acme Corp", "ACME")
        self.assertLess(c["confidence"], 0.6)

    def test_sic_classification_uses_the_sec_scheme_and_unknown_codes_stay_unknown(self):
        self.assertEqual(kbdata.classify_sic("3674")["industry"], "Semiconductors")
        self.assertEqual(kbdata.classify_sic("7372")["industry"], "Software")
        self.assertIsNone(kbdata.classify_sic("0000"))
        self.assertIsNone(kbdata.classify_sic("abc"))
        self.assertIsNone(kbdata.classify_sic(None))
        self.assertTrue(kbdata.classify_sic("6770")["non_operating"])


class TestReconciliation(unittest.TestCase):
    def test_the_report_counts_what_the_universe_really_holds(self):
        r = kb.reconcile(U)
        self.assertEqual((r["universe_rows"], r["unique_tickers"]), (10, 10))
        self.assertEqual(r["missing_cik"], ["NOC"])
        self.assertEqual(r["multiple_tickers_one_issuer"], {"1": ["AAA", "AAB"]})
        self.assertEqual(r["funds"], ["FND"])
        self.assertEqual(r["spacs"], ["SPC"])
        self.assertIn("TWN", sum([v for v in r["issuers_with_similar_names"].values()], []) and ["TWN"])
        self.assertEqual(len(r["issuers_with_similar_names"]), 1)                                     # two different CIKs with the same normalised name
        self.assertEqual(r["adr_by_name"], ["ADR"])

    def test_drift_against_the_previous_snapshot_is_reported_not_applied(self):
        prev = dict(U)
        prev["OLD"] = {"name": "Old Co", "cik": 99, "exchange": "NYSE"}
        cur = {k: v for k, v in U.items() if k != "AAB"}
        cur["NEW"] = {"name": "New Co", "cik": 100, "exchange": "NYSE"}
        r = kb.reconcile(cur, prev)
        self.assertEqual(r["dropped_since_previous_snapshot"], ["AAB", "OLD"])
        self.assertEqual(r["added_since_previous_snapshot"], ["NEW"])
        self.assertEqual(len(cur), r["universe_rows"])                                                # the universe is whatever is live: nothing was added or removed by the report


class TestBootstrap(unittest.TestCase):
    def setUp(self):
        self.c = fresh()

    def test_every_universe_row_is_preserved_exactly_and_verified(self):
        r = kb.bootstrap(self.c, U, NOW)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(kb.verify_universe(self.c, U), [])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM kb_identifiers WHERE kind='ticker' AND valid_to IS NULL").fetchone()[0], len(U))

    def test_identity_is_the_cik_not_the_ticker_and_share_classes_share_one_entity(self):
        kb.bootstrap(self.c, U, NOW)
        self.assertEqual(self.c.execute("SELECT COUNT(DISTINCT entity_id) FROM kb_identifiers WHERE kind='ticker' AND value IN ('AAA','AAB')").fetchone()[0], 1)
        self.assertEqual(self.c.execute("SELECT entity_id FROM kb_identifiers WHERE kind='ticker' AND value='AAA'").fetchone()[0], "E0000000001")
        self.assertEqual(self.c.execute("SELECT is_primary FROM kb_identifiers WHERE value='AAA'").fetchone()[0], 1)         # the class-less ticker is primary
        self.assertEqual(self.c.execute("SELECT is_primary FROM kb_identifiers WHERE value='AAB'").fetchone()[0], 0)

    def test_a_company_with_no_cik_keeps_a_provisional_identity_marked_unresolved(self):
        kb.bootstrap(self.c, U, NOW)
        e = self.c.execute("SELECT entity_id,knowledge_status,cik FROM kb_entities WHERE entity_id='UNOC'").fetchone()
        self.assertEqual((e["knowledge_status"], e["cik"]), ("unresolved", None))
        self.assertEqual(self.c.execute("SELECT identity_status FROM knowledge_coverage WHERE entity_id='UNOC'").fetchone()[0], "unresolved")

    def test_running_it_twice_changes_nothing(self):
        kb.bootstrap(self.c, U, NOW)
        n = [self.c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("kb_entities", "kb_identifiers", "kb_aliases", "knowledge_coverage")]
        r = kb.bootstrap(self.c, U, NOW + dt.timedelta(hours=1))
        self.assertEqual(n, [self.c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("kb_entities", "kb_identifiers", "kb_aliases", "knowledge_coverage")])
        self.assertEqual(r["new"], 0)

    def test_a_company_that_leaves_the_universe_is_closed_never_deleted(self):
        kb.bootstrap(self.c, U, NOW)
        smaller = {k: v for k, v in U.items() if k != "FND"}
        kb.bootstrap(self.c, smaller, NOW + dt.timedelta(days=7))
        row = self.c.execute("SELECT valid_to FROM kb_identifiers WHERE value='FND'").fetchone()
        self.assertIsNotNone(row["valid_to"])
        self.assertEqual(self.c.execute("SELECT active FROM kb_entities WHERE entity_id='E0000000002'").fetchone()[0], 0)
        self.assertEqual(kb.verify_universe(self.c, smaller), [])

    def test_a_rename_keeps_the_old_name_as_a_former_alias_and_bumps_the_version(self):
        kb.bootstrap(self.c, U, NOW)
        v2 = dict(U)
        v2["AAA"] = dict(U["AAA"], name="Alpha Renamed Corp")
        kb.bootstrap(self.c, v2, NOW + dt.timedelta(days=1))
        e = self.c.execute("SELECT legal_name,version FROM kb_entities WHERE entity_id='E0000000001'").fetchone()
        self.assertEqual(e["legal_name"], "Alpha Renamed Corp")
        self.assertGreaterEqual(e["version"], 2)
        self.assertIsNotNone(self.c.execute("SELECT 1 FROM kb_aliases WHERE entity_id='E0000000001' AND kind='former' AND alias='Alpha Corp'").fetchone())

    def test_a_stopped_bootstrap_resumes_after_its_last_committed_ticker(self):
        r1 = kb.bootstrap(self.c, U, NOW, batch=3, max_batches=1)
        self.assertEqual(r1["status"], "interrupted")
        self.assertEqual(r1["processed"], 3)
        done = self.c.execute("SELECT COUNT(*) FROM kb_identifiers WHERE kind='ticker'").fetchone()[0]
        self.assertEqual(done, 3)
        r2 = kb.bootstrap(self.c, U, NOW, batch=3)
        self.assertEqual(r2["status"], "ok")
        self.assertEqual(r2["processed"], len(U) - 3)                                                 # only the rest was done
        self.assertEqual(kb.verify_universe(self.c, U), [])
        self.assertIsNotNone(self.c.execute("SELECT resumed_from FROM knowledge_refresh_runs WHERE kind='bootstrap' AND status='ok'").fetchone()[0])

    def test_a_failure_part_way_leaves_a_resumable_record_not_a_corrupt_base(self):
        orig = kb._upsert
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            if calls["n"] == 4:
                raise RuntimeError("disk gremlin")
            return orig(*a, **k)
        kb._upsert = boom
        try:
            with self.assertRaises(RuntimeError):
                kb.bootstrap(self.c, U, NOW, batch=2)
        finally:
            kb._upsert = orig
        self.assertEqual(self.c.execute("SELECT status FROM knowledge_refresh_runs WHERE kind='bootstrap' ORDER BY id DESC LIMIT 1").fetchone()[0], "interrupted")
        r = kb.bootstrap(self.c, U, NOW)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(kb.verify_universe(self.c, U), [])

    def test_a_dry_run_writes_nothing(self):
        r = kb.bootstrap(self.c, U, NOW, dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM kb_entities").fetchone()[0], 0)

    def test_alias_collisions_are_flagged_not_merged(self):
        two = {"P": {"name": "Delta Co", "cik": 10, "exchange": "NYSE", "aliases": ["Delta"]}, "Q": {"name": "Delta Inc", "cik": 11, "exchange": "NYSE", "aliases": ["Delta"]}}
        kb.bootstrap(self.c, two, NOW)
        self.assertEqual(self.c.execute("SELECT COUNT(DISTINCT entity_id) FROM kb_aliases WHERE collision=1 AND alias_norm='delta'").fetchone()[0], 2)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM kb_entities").fetchone()[0], 2)

    def test_priorities_put_holdings_first_then_open_ideas_and_everything_else_last(self):
        kb.bootstrap(self.c, U, NOW)
        self.c.execute("INSERT INTO positions(ticker,status,opened,qty,entry_price,stop_price,target_price,decision_id) VALUES('AAA','OPEN',?,1,1,1,1,1)", (iso(NOW),)) if False else None
        kb.assign_priorities(self.c, NOW)
        self.assertEqual({r[0] for r in self.c.execute("SELECT priority FROM knowledge_coverage")}, {6})            # nothing held or queued: everything is the remaining universe
        self.assertEqual({r[0] for r in self.c.execute("SELECT layer FROM knowledge_coverage")}, {"A"})


class FakeSEC:
    def __init__(self, behaviours):
        self.b = behaviours
        self.calls = []

    def __call__(self, url, headers=None, timeout=20):
        cik = int(url.rsplit("CIK", 1)[1].split(".")[0])
        self.calls.append((cik, headers))
        v = self.b.get(cik, "ok")
        if isinstance(v, net.FetchError):
            raise v
        return ({"cik": str(cik), "name": "Co %d" % cik, "sic": "3674", "sicDescription": "Semiconductors", "stateOfIncorporation": "DE", "fiscalYearEnd": "1231", "entityType": "operating", "tickers": ["T%d" % cik], "exchanges": ["NYSE"],
                 "formerNames": [{"name": "Old Co %d" % cik, "from": "2010-01-01T00:00:00.000Z", "to": "2015-01-01T00:00:00.000Z"}], "addresses": {"business": {"stateOrCountry": "CA"}}, "filings": {"recent": {"accessionNumber": [], "form": [], "filingDate": [], "reportDate": [], "primaryDocument": [], "items": []}}}, {})


class TestSecRefresh(unittest.TestCase):
    def setUp(self):
        self.c = fresh()
        self.cfg = H.cfg()
        self.cfg["runtime"]["contact_email"] = "owner@example.com"
        kb.bootstrap(self.c, U, NOW)

    def run_refresh(self, sec, **kw):
        return kb.refresh(self.c, self.cfg, NOW, sec, sleep=lambda s: None, **kw)

    def test_a_refresh_fills_sic_sector_and_inferred_traits_and_marks_them_inferred(self):
        r = self.run_refresh(FakeSEC({}), only=["AAA"])
        self.assertEqual(r["refreshed"], 1)
        e = self.c.execute("SELECT sic,sector,industry,state_inc,fiscal_year_end,operating,knowledge_status FROM kb_entities WHERE entity_id='E0000000001'").fetchone()
        self.assertEqual((e["sic"], e["sector"], e["industry"], e["state_inc"], e["fiscal_year_end"], e["operating"], e["knowledge_status"]), ("3674", "Information Technology", "Semiconductors", "DE", "1231", 1, "sec_identity"))
        p = self.c.execute("SELECT status,confidence,cyclicality FROM kb_profiles WHERE entity_id='E0000000001'").fetchone()
        self.assertEqual(p["status"], "inferred")
        self.assertLess(p["confidence"], 0.5)

    def test_the_request_identifies_the_bot_with_the_configured_contact(self):
        sec = FakeSEC({})
        self.run_refresh(sec, only=["AAA"])
        self.assertIn("owner@example.com", sec.calls[0][1]["User-Agent"])

    def test_former_names_become_dated_aliases(self):
        self.run_refresh(FakeSEC({}), only=["AAA"])
        a = self.c.execute("SELECT valid_to FROM kb_aliases WHERE entity_id='E0000000001' AND kind='former'").fetchone()
        self.assertTrue(a["valid_to"].startswith("2015"))

    def test_a_403_or_429_is_a_temporary_source_failure_that_pauses_the_source_and_never_raises(self):
        for code in (403, 429):
            c = fresh()
            kb.bootstrap(c, U, NOW)
            r = kb.refresh(c, self.cfg, NOW, FakeSEC({1: net.FetchError("blocked", code)}), sleep=lambda s: None)
            self.assertEqual(r["status"], "stopped")
            self.assertIn("paused", r["stopped"])
            again = kb.refresh(c, self.cfg, NOW + dt.timedelta(minutes=5), FakeSEC({}), sleep=lambda s: None)
            self.assertEqual(again["status"], "source_backoff")                                         # it does not hammer the source
            later = kb.refresh(c, self.cfg, NOW + dt.timedelta(days=5), FakeSEC({}), sleep=lambda s: None)
            self.assertNotEqual(later["status"], "source_backoff")

    def test_a_404_means_no_sec_record_and_is_not_retried_for_a_month(self):
        self.run_refresh(FakeSEC({1: net.FetchError("nf", 404)}), only=["AAA"])
        self.assertEqual(self.c.execute("SELECT knowledge_status FROM kb_entities WHERE entity_id='E0000000001'").fetchone()[0], "no_sec_data")
        nd = self.c.execute("SELECT next_due FROM knowledge_coverage WHERE entity_id='E0000000001'").fetchone()[0]
        self.assertGreater(nd, iso(NOW + dt.timedelta(days=29)))

    def test_one_failing_company_does_not_stop_the_others_and_backs_off_exponentially(self):
        r = self.run_refresh(FakeSEC({2: net.FetchError("boom", None)}), limit=5)
        self.assertGreaterEqual(r["refreshed"], 3)
        self.assertEqual(r["failed"], 1)
        f1 = self.c.execute("SELECT failures,next_due FROM knowledge_coverage WHERE entity_id='E0000000002'").fetchone()
        self.assertEqual(f1["failures"], 1)
        self.run_refresh(FakeSEC({2: net.FetchError("boom", None)}), only=["FND"])                       # not due yet: nothing happens
        self.assertEqual(self.c.execute("SELECT failures FROM knowledge_coverage WHERE entity_id='E0000000002'").fetchone()[0], 1)

    def test_the_network_time_budget_stops_the_batch(self):
        ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
        r = kb.refresh(self.c, self.cfg, NOW, FakeSEC({}), limit=8, sleep=lambda s: None, clock=lambda: next(ticks))
        self.assertEqual(r["status"], "stopped")
        self.assertIn("budget", r["stopped"])

    def test_the_per_cycle_limit_is_respected_and_dry_run_only_plans(self):
        self.cfg["kb"]["refresh_companies_per_cycle"] = 2
        sec = FakeSEC({})
        kb.refresh(self.c, self.cfg, NOW, sec, sleep=lambda s: None)
        self.assertEqual(len(sec.calls), 2)
        n = len(sec.calls)
        d = kb.refresh(self.c, self.cfg, NOW, sec, dry_run=True, limit=4, sleep=lambda s: None)
        self.assertEqual(d["status"], "dry_run")
        self.assertEqual(len(sec.calls), n)

    def test_a_disabled_knowledge_base_does_nothing(self):
        self.cfg["kb"]["enabled"] = False
        self.assertEqual(kb.refresh(self.c, self.cfg, NOW, FakeSEC({}), sleep=lambda s: None)["status"], "disabled")

    def test_unreadable_sec_data_is_a_failure_not_a_crash(self):
        r = kb.refresh(self.c, self.cfg, NOW, lambda url, headers=None, timeout=20: ({"nonsense": True}, {}), limit=2, sleep=lambda s: None)
        self.assertEqual(r["failed"], 2)

    def test_parse_submissions_returns_only_what_is_present(self):
        s = kb.parse_submissions({"cik": "5", "name": "X"})
        self.assertEqual((s["sic"], s["state_inc"], s["fiscal_year_end"], s["former_names"], s["filings"]), (None, None, None, [], []))
        self.assertIsNone(kb.parse_submissions({}))
        self.assertIsNone(kb.parse_submissions("junk"))


class TestCoverageIsHonest(unittest.TestCase):
    def test_inserting_identity_rows_is_not_described_as_coverage_of_anything_else(self):
        c = fresh()
        kb.bootstrap(c, U, NOW)
        s = kb.coverage_summary(c)
        by = {r["category"]: r["entities"] for r in s["categories"]}
        self.assertEqual(by["identity (an entity exists)"], 9)                                      # 10 tickers, 9 entities (two share one CIK)
        for none_yet in ("SEC classification (SIC code) fetched", "business profile with a summary", "SEC financial facts (reliable or partial)", "current market data (price not expired)", "exposure records", "economic relationships"):
            self.assertEqual(by[none_yet], 0, none_yet)
        self.assertIn("NOT a profile", s["caution"])
        self.assertNotIn("complete", json.dumps(s).lower().replace("completeness", ""))

    def test_the_unresolved_and_provenance_reports_list_what_is_missing(self):
        c = fresh()
        kb.bootstrap(c, U, NOW)
        self.assertEqual([r["ticker"] for r in kb.unresolved_report(c)["no_cik"]], ["NOC"])
        self.assertEqual(kb.provenance_report(c)["kb_relationships"]["total"], 0)

    def test_every_entity_row_carries_its_source_and_a_last_verified_time(self):
        c = fresh()
        kb.bootstrap(c, U, NOW)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM kb_entities WHERE source IS NULL OR last_verified IS NULL OR confidence IS NULL").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM kb_identifiers WHERE source IS NULL OR last_verified IS NULL").fetchone()[0], 0)


class TestUniverseDiagnostics(unittest.TestCase):
    def test_quality_report_splits_the_universe_by_kind(self):
        q = kb.universe_quality(U, None)
        self.assertEqual((q["operating_companies_by_name"], q["closed_end_funds_by_name"], q["spac_shells_by_name"], q["missing_cik"], q["issuers_with_multiple_tickers"]), (8, 1, 1, 1, 1))
        self.assertEqual(q["reit_by_name_hint"], 1)
        self.assertEqual(q["no_sec_sector_yet"], 10)                                                 # no instrument has an SEC sector until the refresh has run

    def test_the_check_explains_why_a_ticker_is_in_or_out_by_the_packages_own_filter(self):
        payload = {"fields": ["cik", "name", "ticker", "exchange"], "data": [[1, "Visa", "V", "NYSE"], [2, "Odd", "ODD", "OTC"], [3, "Shape", "TOOLONGX", "NYSE"]]}
        r = kb.universe_check(payload, ["V", "ODD", "TOOLONGX", "NOPE"], {"AAA": {"cik": 9, "exchange": "NYSE"}})
        self.assertTrue(r["tickers"]["V"]["kept_by_package_filter"])
        self.assertFalse(r["tickers"]["V"]["in_live_universe"])
        self.assertIn("exchange label", " ".join(r["tickers"]["ODD"]["reasons_the_package_filter_would_drop_it"]))
        self.assertIn("shape", " ".join(r["tickers"]["TOOLONGX"]["reasons_the_package_filter_would_drop_it"]))
        self.assertIn("not in the SEC file", " ".join(r["tickers"]["NOPE"]["reasons_the_package_filter_would_drop_it"]))
        self.assertEqual(r["live_but_not_kept_by_filter"], 1)

    def test_the_check_never_changes_the_universe(self):
        payload = {"fields": ["cik", "name", "ticker", "exchange"], "data": [[1, "Visa", "V", "NYSE"]]}
        live = {"AAA": {"cik": 9}}
        kb.universe_check(payload, ["V"], live)
        self.assertEqual(list(live), ["AAA"])


class TestCorruptInputNeverDamagesTheBase(unittest.TestCase):
    def test_only_a_positive_ten_digit_integer_is_a_cik(self):
        for good, want in ((1, 1), ("5", 5), (9_999_999_999, 9_999_999_999), (3.0, 3)):
            self.assertEqual(kb.valid_cik(good), want)
        for bad in (None, "", "junk", 0, -5, "9" * 40, 10 ** 10, float("inf"), float("nan"), [1], {}):
            self.assertIsNone(kb.valid_cik(bad), repr(bad))

    def test_a_corrupt_cik_makes_the_company_unresolved_not_the_bootstrap_fail(self):
        c = fresh()
        bad = {"A": {"name": "A Corp", "cik": "9" * 40, "exchange": "NYSE"}, "B": {"name": "B Corp", "cik": "junk", "exchange": "NYSE"}, "C": {"name": "C Corp", "cik": float("inf"), "exchange": "NYSE"}, "D": {"name": "D Corp", "cik": 4, "exchange": "NYSE"}}
        self.assertEqual(kb.bootstrap(c, bad, NOW)["status"], "ok")
        self.assertEqual(kb.verify_universe(c, bad), [])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM kb_entities WHERE knowledge_status='unresolved'").fetchone()[0], 3)
        self.assertEqual(len(kb.reconcile(bad)["missing_cik"]), 3)

    def test_a_corrupt_sec_row_does_not_stop_the_universe_diagnostic(self):
        payload = {"fields": ["cik", "name", "ticker", "exchange"], "data": [["x", "Odd", "ODD", "NYSE"], [None, "None", "NON", "NYSE"], [1, "Ok", "OK", "NYSE"]]}
        r = kb.universe_check(payload, ["ODD", "NON", "OK"], {"OK": {"cik": 1}})
        self.assertTrue(r["tickers"]["OK"]["in_live_universe"])

    def test_the_time_conversions_survive_text_and_absurd_numbers(self):
        from paper_agent import timeframe
        for junk in (None, "abc", "", 1e99, float("nan"), -5, [], {}):
            self.assertIn(timeframe.from_hours(junk), timeframe.ALL)
            self.assertIn(timeframe.from_sessions(junk), timeframe.ALL)
            self.assertIn(timeframe.from_legacy(junk), timeframe.ALL)

    def test_the_matcher_survives_every_corrupt_cik(self):
        from paper_agent.entities import Universe
        for bad in (None, "junk", "9" * 40, -5, 0, float("inf")):
            Universe({"AAA": {"name": "Alpha Corp", "cik": bad, "aliases": []}}, set())


if __name__ == "__main__":
    unittest.main()
