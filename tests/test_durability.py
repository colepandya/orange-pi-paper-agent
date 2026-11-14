"""'Hundreds of events, no decisions, no error': a cycle killed between the evidence stage and the entries stage used to lose every candidate.
These tests simulate the kill and prove nothing is lost, the cause is visible, and the next cycle recovers."""
import datetime as dt
import json
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import candqueue, company, evidence, funnel, nlp, pipeline, preflight, trader  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

NOW = H.NOW
RF = lambda d: d.strftime("%a, %d %b %Y %H:%M:%S +0000")  # noqa: E731
SRC = [{"id": "t", "name": "Wire", "type": "rss", "url": "http://x/rss", "trust": 0.85, "interval": 0}]
NVDA = "NVIDIA beats estimates and raises full-year guidance as data center demand surges"
LMT = "Lockheed Martin wins $2.4 billion Navy contract for missile systems"
MSFT = "Microsoft raises full-year outlook after strong Azure growth"
PRICES = {"NVDA": 100.0, "LMT": 480.0, "MSFT": 400.0}


def items(*titles, age_h=1):
    return [(t, "", RF(NOW - dt.timedelta(hours=age_h, minutes=i))) for i, t in enumerate(titles)]


def count(c, sql, *a):
    return c.execute(sql, a).fetchone()[0]


class TestKilledCycleLosesNothing(unittest.TestCase):
    def kill_during_company_refresh(self, c, cfg, its):
        with mock.patch.object(company, "refresh_company", side_effect=SystemExit("simulated kill: timeout / out-of-memory")):
            with self.assertRaises(SystemExit):
                pipeline.run_cycle(c, cfg, H.universe(), SRC, H.deps(its, PRICES), NOW)

    def test_the_kill_leaves_events_and_queued_candidates_but_no_decisions(self):
        c, cfg = H.fresh_db(), H.cfg()
        self.kill_during_company_refresh(c, cfg, items(NVDA))
        self.assertEqual(count(c, "SELECT COUNT(*) FROM events WHERE gate='candidate'"), 1)          # the news WAS processed...
        self.assertEqual(count(c, "SELECT COUNT(*) FROM decisions"), 0)                               # ...but no decision exists (the symptom)
        self.assertEqual(count(c, "SELECT COUNT(*) FROM candidate_queue WHERE status='NEW'"), 1)      # ...and the candidate SURVIVED (the fix)
        row = c.execute("SELECT status,report_json FROM cycle_runs").fetchone()
        self.assertEqual(row["status"], "RUNNING")                                                    # a killed cycle never finishes
        self.assertEqual(json.loads(row["report_json"])["current"], "companies")                      # ...but it records WHERE it died

    def test_the_next_cycle_decides_the_leftover_candidate_even_with_no_new_news(self):
        c, cfg = H.fresh_db(), H.cfg()
        self.kill_during_company_refresh(c, cfg, items(NVDA))
        later = NOW + dt.timedelta(minutes=15)
        r = pipeline.run_cycle(c, cfg, H.universe(), SRC, H.deps([], PRICES), later)
        self.assertEqual(count(c, "SELECT COUNT(*) FROM decisions WHERE ticker='NVDA'"), 1)
        self.assertEqual(count(c, "SELECT COUNT(*) FROM candidate_queue WHERE status='DONE'"), 1)
        self.assertEqual(count(c, "SELECT COUNT(*) FROM candidate_queue WHERE status='NEW'"), 0)
        self.assertEqual(c.execute("SELECT status FROM cycle_runs ORDER BY id LIMIT 1").fetchone()[0], "KILLED")      # the dead cycle is now labelled
        self.assertEqual(r["errors"], [])

    def test_a_decision_is_never_made_twice_for_the_same_candidate(self):
        c, cfg = H.fresh_db(), H.cfg()
        self.kill_during_company_refresh(c, cfg, items(NVDA))
        for k in (1, 2, 3):
            pipeline.run_cycle(c, cfg, H.universe(), SRC, H.deps([], PRICES), NOW + dt.timedelta(minutes=15 * k))
        self.assertEqual(count(c, "SELECT COUNT(*) FROM decisions WHERE ticker='NVDA'"), 1)

    def test_the_funnel_proves_what_happened_and_where(self):
        c, cfg = H.fresh_db(), H.cfg()
        self.kill_during_company_refresh(c, cfg, items(NVDA))
        later = NOW + dt.timedelta(minutes=20)
        rep = funnel.report(c, 24, later)
        self.assertEqual(rep["died"][0]["stage"], "companies")
        f = [x for x in rep["findings"] if "NEVER FINISHED" in x["text"]]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["severity"], "HIGH")
        self.assertIn("companies x1", f[0]["text"])
        self.assertIn("STARTED BUT NEVER FINISHED", funnel.text(rep))
        self.assertEqual(rep["queue"], {"NEW": 1})

    def test_the_checklist_blocks_while_cycles_are_being_killed(self):
        c, cfg = H.fresh_db(), H.cfg()
        cfg["runtime"]["min_available_memory_mb"] = 120
        self.kill_during_company_refresh(c, cfg, items(NVDA))
        lines = []
        r = preflight.run(c, cfg, H.universe(), now=NOW + dt.timedelta(minutes=20), live=False, check_units=False, mem_mb=lambda: 600, out=lines.append)
        self.assertEqual(next(x["status"] for x in r["checks"] if x["name"].startswith("No killed cycles")), "FAIL")
        self.assertFalse(r["ready"])

    def test_candidates_too_old_to_trade_expire_visibly_instead_of_lingering(self):
        c, cfg = H.fresh_db(), H.cfg()
        self.kill_during_company_refresh(c, cfg, items(NVDA))
        pipeline.run_cycle(c, cfg, H.universe(), SRC, H.deps([], PRICES), NOW + dt.timedelta(hours=30))
        self.assertEqual(count(c, "SELECT COUNT(*) FROM candidate_queue WHERE status='EXPIRED'"), 1)
        self.assertEqual(count(c, "SELECT COUNT(*) FROM decisions"), 0)


class FakeTime:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class TestTimeBudget(unittest.TestCase):
    def run_with_slow_refresh(self, seconds_per_refresh):
        fake = FakeTime()
        c, cfg = H.fresh_db(), H.cfg()
        cfg["runtime"]["cycle_budget_seconds"] = 300

        def slow(*a, **k):
            fake.t += seconds_per_refresh
        with mock.patch.object(pipeline, "time", fake), mock.patch.object(trader, "time", fake), mock.patch.object(company, "refresh_company", slow):
            r = pipeline.run_cycle(c, cfg, H.universe(), SRC, H.deps(items(NVDA, LMT, MSFT), PRICES), NOW)
        return c, r

    def test_slow_enrichment_stops_early_and_decisions_still_happen_the_rest_follow_next_cycle(self):
        c, r = self.run_with_slow_refresh(200)                                                          # a painfully slow network: every refresh costs 200 s
        self.assertTrue(any("companies: stopped early" in b for b in r["budget"]), r.get("budget"))     # enrichment gave up instead of eating the whole cycle
        first = count(c, "SELECT COUNT(DISTINCT ticker) FROM decisions")
        self.assertGreaterEqual(first, 1)                                                               # decisions were NOT starved
        self.assertEqual(first + count(c, "SELECT COUNT(*) FROM candidate_queue WHERE status='NEW'"), 3)  # every candidate is either decided or still queued: none lost
        for k in (1, 2):                                                                                # the network recovers: the rest are decided
            pipeline.run_cycle(c, H.cfg(), H.universe(), SRC, H.deps([], PRICES), NOW + dt.timedelta(minutes=10 * k))
        self.assertEqual(count(c, "SELECT COUNT(DISTINCT ticker) FROM decisions"), 3)
        self.assertEqual(count(c, "SELECT COUNT(*) FROM candidate_queue WHERE status='NEW'"), 0)

    def test_when_even_that_is_not_enough_the_rest_waits_in_the_queue_not_in_oblivion(self):
        c, r = self.run_with_slow_refresh(500)
        self.assertEqual(count(c, "SELECT COUNT(*) FROM decisions"), 0)                                 # out of time before entries...
        self.assertEqual(count(c, "SELECT COUNT(*) FROM candidate_queue WHERE status='NEW'"), 3)        # ...but all three are safely queued
        r2 = pipeline.run_cycle(c, H.cfg(), H.universe(), SRC, H.deps([], PRICES), NOW + dt.timedelta(minutes=10))
        self.assertEqual(count(c, "SELECT COUNT(DISTINCT ticker) FROM decisions"), 3)


class TestIsolation(unittest.TestCase):
    def test_a_poisonous_headline_cannot_block_the_others(self):
        c, cfg = H.fresh_db(), H.cfg()
        real = nlp.analyze

        def boom(title, summary=""):
            if "POISON" in title:
                raise ValueError("regex blew up")
            return real(title, summary)
        for i, t in enumerate(["POISON headline that crashes the analyser", NVDA, LMT]):
            c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES(?,?,?,?,?,?,?,?)", ("u%d" % i, "W", t, "", "http://x", iso(NOW - dt.timedelta(hours=1)), iso(NOW), 0.8))
        with mock.patch.object(nlp, "analyze", boom):
            r = evidence.process_new_news(c, H.universe(), cfg, NOW)
        self.assertEqual(r["stats"]["errors"], 1)
        self.assertEqual(count(c, "SELECT COUNT(*) FROM events WHERE gate LIKE 'candidate%'"), 2)       # the two good ones went through
        self.assertEqual(c.execute("SELECT gate FROM events WHERE headline LIKE 'POISON%'").fetchone()[0], "error: ValueError: regex blew up")
        again = evidence.process_new_news(c, H.universe(), cfg, NOW)                                    # and the poison item is not retried forever
        self.assertEqual(again["stats"]["events"], 0)

    def test_a_failure_while_saving_one_item_rolls_it_back_completely(self):
        """Event + gate + queued candidates commit together or not at all: no orphan event without its candidate."""
        c, cfg = H.fresh_db(), H.cfg()
        c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES(?,?,?,?,?,?,?,?)", ("u1", "W", NVDA, "", "http://x", iso(NOW - dt.timedelta(hours=1)), iso(NOW), 0.8))
        with mock.patch.object(candqueue, "put", side_effect=RuntimeError("disk full")):
            r = evidence.process_new_news(c, H.universe(), cfg, NOW)
        self.assertEqual(r["stats"]["errors"], 1)
        rows = c.execute("SELECT gate FROM events").fetchall()
        self.assertEqual([x[0] for x in rows], ["error: RuntimeError: disk full"])                       # only the visible placeholder; no half-saved 'candidate' event
        self.assertEqual(count(c, "SELECT COUNT(*) FROM evidence"), 0)

    def test_one_failing_candidate_does_not_stop_the_candidates_after_it(self):
        c, cfg = H.fresh_db(), H.cfg()
        cands = [{"event_id": i + 1, "ticker": t, "direction": 0.8, "magnitude": 0.6 - i * 0.1, "event_type": "earnings_beat", "conf": 0.9, "credibility": 0.9, "age_hours": 1.0,
                  "headline": "%s beats" % t, "source": "W", "news_uid": None, "amount_usd": None, "horizon": 8} for i, t in enumerate(("AAA", "BBB", "CCC"))]
        for cd in cands:
            candqueue.put(c, cd, NOW)
        done = []

        def fake(conn, cfg_, uni, cand, regime, now_, quote_fn, md_fetch=None, existing_decision=None, intel=None):
            if cand["ticker"] == "BBB":
                raise ZeroDivisionError("bad data for BBB")
            done.append(cand["ticker"])
            return {"status": "REJECTED", "decision_id": 0}
        with mock.patch.object(trader, "process_candidate", fake):
            out = trader.run_entries(c, cfg, None, candqueue.load(c, NOW, 24), {"regime": "risk_on"}, NOW, None)
        self.assertEqual(sorted(done), ["AAA", "CCC"])                                                    # CCC was NOT lost because BBB blew up
        self.assertEqual(out["errors"], 1)
        d = c.execute("SELECT ticker,status,reason FROM decisions WHERE status='ERROR'").fetchone()
        self.assertEqual(d["ticker"], "BBB")
        self.assertIn("ZeroDivisionError", d["reason"])                                                   # visible on the Decisions tab, with the reason
        self.assertEqual(count(c, "SELECT COUNT(*) FROM candidate_queue WHERE status='NEW'"), 0)


class TestRecentNewsFirst(unittest.TestCase):
    def test_a_backlog_of_old_items_cannot_starve_fresh_news(self):
        c, cfg = H.fresh_db(), H.cfg()
        for i in range(5):                                                                              # an old backlog (ingested 20 hours ago)
            c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES(?,?,?,?,?,?,?,?)", ("old%d" % i, "W", "Old story number %d about nothing in particular" % i, "", "http://x", iso(NOW - dt.timedelta(hours=20)), iso(NOW - dt.timedelta(hours=20)), 0.8))
        for i, t in enumerate((NVDA, LMT, MSFT)):                                                       # fresh, tradable news arrives AFTER (higher ids)
            c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES(?,?,?,?,?,?,?,?)", ("new%d" % i, "W", t, "", "http://x", iso(NOW - dt.timedelta(minutes=10)), iso(NOW - dt.timedelta(minutes=5)), 0.8))
        evidence.process_new_news(c, H.universe(), cfg, NOW, limit=3)                                   # capacity for only 3 this cycle
        self.assertEqual(sorted(r[0] for r in c.execute("SELECT news_uid FROM events")), ["new0", "new1", "new2"])


if __name__ == "__main__":
    unittest.main()


class TestRecoverLostCandidates(unittest.TestCase):
    """The events processed since the outage exist; their candidates were never saved (older build, gate unrecorded). `recover` rebuilds them."""

    def old_build_event(self, c, title, age_h, decided=False, tickers='["NVDA"]', base=None):
        NOW = base or globals()["NOW"]                                                                    # (the CLI test uses the REAL clock, so it passes its own base)
        c.execute("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES(?,?,?,?,?,?,?,?)", ("n%s" % abs(hash(title)), "Wire", title, "", "http://x", iso(NOW - dt.timedelta(hours=age_h)), iso(NOW - dt.timedelta(hours=age_h)), 0.8))
        eid = c.execute("INSERT INTO events(news_uid,created,published,source,source_id,headline,event_type,scope,direction,magnitude,tickers_json,regions_json,tokens,novelty,credibility,age_hours,stale) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("n%s" % abs(hash(title)), iso(NOW - dt.timedelta(hours=age_h)), iso(NOW - dt.timedelta(hours=age_h)), "Wire", "Wire", title, "general_news", "company", 0.0, 0.1, tickers, "[]", "", 1.0, 0.8, 0.5, 0)).lastrowid
        if decided:
            c.execute("INSERT INTO decisions(created,ticker,action,status,event_id,origin) VALUES(?,?,?,?,?,?)", (iso(NOW), json.loads(tickers)[0], "BUY", "REJECTED", eid, "news"))
        return eid

    def test_recent_unrecorded_events_that_qualify_are_queued_then_decided_by_the_next_cycle(self):
        c, cfg = H.fresh_db(), H.cfg()
        self.old_build_event(c, NVDA, 3)
        self.old_build_event(c, "Fed cuts interest rates by 25 basis points", 3, tickers="[]")
        self.old_build_event(c, "Local bakery wins regional award for sourdough", 3, tickers="[]")
        res = evidence.recover(c, H.universe(), cfg, NOW, 24)
        self.assertEqual((res["examined"], res["queued"]), (3, 1))
        self.assertEqual(res["by_gate"], {"candidate": 1, "market_wide_event": 1, "no_company_matched": 1})
        self.assertEqual(count(c, "SELECT COUNT(*) FROM events WHERE gate IS NULL"), 0)                  # every event now has its reason recorded
        pipeline.run_cycle(c, cfg, H.universe(), SRC, H.deps([], PRICES), NOW + dt.timedelta(minutes=5))
        self.assertEqual(count(c, "SELECT COUNT(*) FROM decisions WHERE ticker='NVDA'"), 1)               # ...and the lost decision now exists

    def test_recovery_is_idempotent_and_skips_what_is_already_decided_or_too_old(self):
        c, cfg = H.fresh_db(), H.cfg()
        self.old_build_event(c, NVDA, 3)
        self.old_build_event(c, LMT, 3, decided=True, tickers='["LMT"]')                                 # a decision already exists for this one
        self.old_build_event(c, MSFT, 40, tickers='["MSFT"]')                                            # outside the 24h window: ignored entirely
        r1 = evidence.recover(c, H.universe(), cfg, NOW, 24)
        r2 = evidence.recover(c, H.universe(), cfg, NOW, 24)
        self.assertEqual((r1["queued"], r2["examined"], r2["queued"]), (1, 0, 0))
        self.assertEqual(count(c, "SELECT COUNT(*) FROM candidate_queue"), 1)
        self.assertEqual(c.execute("SELECT gate FROM events WHERE headline LIKE 'Microsoft%'").fetchone()[0], None)

    def test_events_too_old_to_trade_are_recorded_not_queued(self):
        c, cfg = H.fresh_db(), H.cfg()
        self.old_build_event(c, NVDA, 28)                                                                # inside the 40h-recover window we pass, but past the 24h TRADE window
        res = evidence.recover(c, H.universe(), cfg, NOW, 48)
        self.assertEqual((res["queued"], res["by_gate"]), (0, {"older_than_trade_window": 1}))

    def test_cli_recover_command(self):
        import contextlib
        import io
        import tempfile
        from paper_agent import __main__ as cli
        from paper_agent.timeutil import utcnow
        c = H.fresh_db()
        self.old_build_event(c, NVDA, 2, base=utcnow())
        c.commit()
        path = c.execute("PRAGMA database_list").fetchone()[2]
        old_db, old_load = cli.DB_PATH, cli.pipeline.load_universe
        cli.DB_PATH, cli.pipeline.load_universe = path, (lambda: H.universe())
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(["recover", "--hours", "12"]), 0)
        finally:
            cli.DB_PATH, cli.pipeline.load_universe = old_db, old_load
        self.assertIn("queued 1 trade candidate", buf.getvalue())
