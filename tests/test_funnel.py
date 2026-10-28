"""'Why are there no decisions?' - every reason an item can drop out is recorded and reported."""
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import dashboard, db, doctor, funnel, pipeline  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

NOW = H.NOW
RF = lambda d: d.strftime("%a, %d %b %Y %H:%M:%S +0000")  # noqa: E731
SRC = [{"id": "t", "name": "Wire", "type": "rss", "url": "http://x/rss", "trust": 0.85, "interval": 0}]
UNI = Universe({"CVX": {"name": "Chevron Corp", "aliases": ["Chevron"], "cik": 93410}, "HES": {"name": "Hess Corp", "aliases": ["Hess"], "cik": 4447},
                "XOM": {"name": "Exxon Mobil Corp", "aliases": ["Exxon Mobil", "Exxon"], "cik": 34088}, "NVDA": {"name": "NVIDIA Corp", "aliases": ["NVIDIA"], "cik": 1045810},
                "TGT": {"name": "Target Corp", "aliases": ["Target"], "cik": 27419}, "MSFT": {"name": "Microsoft Corp", "aliases": ["Microsoft"], "cik": 789019},
                "LMT": {"name": "Lockheed Martin Corp", "aliases": ["Lockheed Martin"], "cik": 936468}})


def run(items, now=NOW, prices=None):
    c = H.fresh_db()
    d = H.deps(items, prices or {})
    pipeline.run_cycle(c, H.cfg(), UNI, SRC, d, now)
    return c


def gate_of(c, text):
    r = c.execute("SELECT gate FROM events WHERE headline LIKE ?", ("%" + text + "%",)).fetchone()
    return r[0] if r else "NO EVENT"


class TestGateReasons(unittest.TestCase):
    def setUp(self):
        fresh = RF(NOW - dt.timedelta(hours=1))
        self.items = [
            ("Chevron to acquire Hess in $53 billion all-stock deal", "", fresh),
            ("Exxon Mobil to acquire a private shale producer for $5 billion", "", fresh),
            ("Target agrees to be acquired by private equity firm", "", fresh),
            ("Hess merger talks continue, sources say", "", fresh),
            ("Fed cuts interest rates by 25 basis points", "", fresh),
            ("Private equity firm buys regional bakery chain for $200 million", "", fresh),
            ("NVIDIA, Microsoft, Exxon Mobil and Lockheed Martin all beat estimates this week", "", fresh),
            ("8-K - NVIDIA CORP (0001045810) (Filer)", "", fresh),
            ("Lockheed Martin wins $2.4 billion Pentagon contract", "", RF(NOW - dt.timedelta(hours=30))),
            ("Lockheed Martin wins $1.1 billion Army contract for missiles", "", RF(NOW - dt.timedelta(days=5))),
            ("NVIDIA beats estimates and raises full-year guidance as data center demand surges", "", fresh),
            ("NVIDIA beats estimates and raises full-year guidance as data center demand surges - Bloomberg", "", fresh),
        ]
        self.c = run(self.items)

    def test_every_event_records_exactly_why_it_did_or_did_not_qualify(self):
        g = lambda t: gate_of(self.c, t)  # noqa: E731
        self.assertEqual(g("Chevron to acquire Hess"), "candidate")                  # both listed: the TARGET (+) qualifies
        self.assertEqual(g("Exxon Mobil to acquire a private"), "candidate")         # acquirer-only: now VISIBLE (as a no-trade decision) instead of silently dropped
        self.assertEqual(g("Target agrees to be acquired"), "company_match_uncertain")   # 'Target' is an ambiguous word
        self.assertEqual(g("Hess merger talks"), "merger_role_unknown")
        self.assertEqual(g("Fed cuts interest rates"), "market_wide_event")
        self.assertEqual(g("regional bakery chain"), "no_company_matched")
        self.assertEqual(g("all beat estimates this week"), "too_many_companies")
        self.assertEqual(g("8-K - NVIDIA"), "direction_too_weak")
        self.assertEqual(g("$2.4 billion Pentagon"), "older_than_trade_window")
        self.assertEqual(g("$1.1 billion Army"), "stale")
        self.assertEqual(g("data center demand surges"), "candidate")
        self.assertEqual(g("Bloomberg"), "duplicate_story")

    def test_merger_events_are_never_silently_invisible(self):
        """REGRESSION: an acquirer-only merger headline had direction -0.15 < 0.25, so it produced NO decision row at all."""
        rows = {r["headline"][:20]: r for r in self.c.execute("SELECT e.headline,e.gate,(SELECT status FROM decisions d WHERE d.event_id=e.id LIMIT 1) st FROM events e WHERE event_type='merger_acquisition'")}
        acquirer_only = next(r for h, r in rows.items() if h.startswith("Exxon Mobil to acqu"))
        self.assertEqual(acquirer_only["gate"], "candidate")
        self.assertIsNotNone(acquirer_only["st"])                                    # a decision exists, so you can SEE it and why it was not traded
        d = self.c.execute("SELECT ticker,action,status,reason FROM decisions WHERE ticker='XOM'").fetchone()
        self.assertIn("long-only", d["reason"])
        both = self.c.execute("SELECT ticker FROM decisions d JOIN events e ON e.id=d.event_id WHERE e.headline LIKE 'Chevron to acquire Hess%' ORDER BY ticker").fetchall()
        self.assertEqual([r[0] for r in both], ["CVX", "HES"])

    def test_gate_counts_add_up_and_samples_are_attached(self):
        r = funnel.report(self.c, 24, NOW)
        self.assertEqual(sum(r["gates"].values()), r["events"])
        self.assertEqual(r["candidates"], 3 + 0 if False else r["candidates"])      # structural: candidates == events whose gate starts with 'candidate'
        self.assertEqual(r["candidates"], self.c.execute("SELECT COUNT(*) FROM events WHERE gate LIKE 'candidate%'").fetchone()[0])
        self.assertTrue(all(len(v) <= 3 for v in r["samples"].values()))
        self.assertIn("duplicate_story", r["samples"])
        text = funnel.text(r)
        for needle in ("WHY events did or did not become candidates", "merger_role_unknown", "Recent merger / acquisition events", "FINDINGS"):
            self.assertIn(needle, text)

    def test_merger_table_shows_gate_and_decision_per_event(self):
        m = {x["headline"][:14]: x for x in funnel.report(self.c, 24, NOW)["mna"]}
        self.assertEqual(m["Chevron to acq"]["decision"] != "-", True)
        self.assertEqual(m["Hess merger ta"]["gate"], "merger_role_unknown")
        self.assertEqual(m["Hess merger ta"]["decision"], "-")


class TestFindings(unittest.TestCase):
    def test_skipped_cycles_are_flagged_with_the_reason(self):
        c = H.fresh_db()
        for i in range(3):
            c.execute("INSERT INTO cycle_runs(started,finished,status,mode,report_json) VALUES(?,?,?,?,?)", (iso(NOW - dt.timedelta(minutes=10 * i)), iso(NOW), "SKIPPED", "full", json.dumps({"skipped": "low memory: 87 MB available"})))
        r = funnel.report(c, 24, NOW)
        f = [x for x in r["findings"] if "SKIPPED" in x["text"]]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["severity"], "HIGH")
        self.assertIn("low memory: 87 MB", f[0]["text"])
        self.assertIn("cleanup-ram", f[0]["fix"])

    def test_stage_errors_are_reported_with_their_actual_messages(self):
        """The funnel used to say '52 cycles had stage errors' and nothing about WHAT failed."""
        c = H.fresh_db()
        for i in range(5):
            rep = {"stages": {"companies": {"seconds": 1.2, "error": "TypeError: unsupported operand type(s) for -: 'str' and 'float'"}, "evidence": {"seconds": 3.0}}, "errors": ["companies"]}
            c.execute("INSERT INTO cycle_runs(started,finished,status,mode,report_json) VALUES(?,?,?,?,?)", (iso(NOW - dt.timedelta(minutes=10 * i)), iso(NOW), "ERROR", "full", json.dumps(rep)))
        c.execute("INSERT INTO cycle_runs(started,finished,status,mode,report_json) VALUES(?,?,?,?,?)", (iso(NOW - dt.timedelta(minutes=70)), iso(NOW), "ERROR", "full", json.dumps({"stages": {"entries": {"error": "KeyError: 'x'"}}})))
        r = funnel.report(c, 24, NOW)
        self.assertEqual(r["stage_errors"][0], ("companies: TypeError: unsupported operand type(s) for -: 'str' and 'float'", 5))
        f = [x for x in r["findings"] if "stage errors" in x["text"]]
        self.assertIn("unsupported operand", f[0]["text"])
        self.assertIn("(x5)", f[0]["text"])
        text = funnel.text(r)
        self.assertIn("STAGE ERRORS (what actually failed)", text)
        self.assertIn("x5", text)
        lines = []
        cfg = H.cfg()
        cfg["runtime"]["bind_port"] = 1
        doctor.run(c, cfg, "/nonexistent", srcs=[], live=False, check_units=False, now=NOW, out=lines.append)
        self.assertIn("stage error x5: companies: TypeError", "\n".join(lines))                  # the doctor shows it right under the [cycles 24h] line

    def test_no_recent_cycle_is_flagged(self):
        c = H.fresh_db()
        c.execute("INSERT INTO cycle_runs(started,finished,status,mode) VALUES(?,?,?,?)", (iso(NOW - dt.timedelta(hours=5)), iso(NOW), "OK", "full"))
        self.assertTrue(any("No cycle has started" in x["text"] for x in funnel.report(c, 24, NOW)["findings"]))
        self.assertTrue(any("never" in x["text"] for x in funnel.report(H.fresh_db(), 24, NOW)["findings"]))

    def test_cycles_running_but_no_events_is_flagged(self):
        c = H.fresh_db()
        c.execute("INSERT INTO cycle_runs(started,finished,status,mode) VALUES(?,?,?,?)", (iso(NOW - dt.timedelta(minutes=5)), iso(NOW), "OK", "full"))
        c.execute("INSERT INTO events(created,event_type,scope,direction,magnitude,gate) VALUES(?,?,?,?,?,?)", (iso(NOW - dt.timedelta(hours=8)), "general_news", "company", 0, .1, "no_company_matched"))
        self.assertTrue(any("NO news item has been turned into an event" in x["text"] for x in funnel.report(c, 24, NOW)["findings"]))

    def test_events_flowing_but_nothing_qualifying_is_informational_with_top_reasons(self):
        c = H.fresh_db()
        c.execute("INSERT INTO cycle_runs(started,finished,status,mode) VALUES(?,?,?,?)", (iso(NOW - dt.timedelta(minutes=5)), iso(NOW), "OK", "full"))
        for i in range(20):
            c.execute("INSERT INTO events(created,event_type,scope,direction,magnitude,gate) VALUES(?,?,?,?,?,?)", (iso(NOW - dt.timedelta(minutes=i)), "general_news", "company", 0, .1, "no_company_matched"))
        f = [x for x in funnel.report(c, 24, NOW)["findings"] if "none qualified" in x["text"]]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["severity"], "INFO")
        self.assertIn("no_company_matched x20", f[0]["text"])

    def test_healthy_pipeline_has_no_findings(self):
        c = run([("Chevron to acquire Hess in $53 billion all-stock deal", "", RF(NOW - dt.timedelta(hours=1)))], prices={"HES": 150.0, "CVX": 160.0})
        self.assertEqual([x for x in funnel.report(c, 24, NOW)["findings"] if x["severity"] in ("HIGH", "MEDIUM")], [])

    def test_a_buy_that_cannot_get_a_price_while_the_market_is_open_is_called_out_with_the_reason(self):
        c = run([("Chevron to acquire Hess in $53 billion all-stock deal", "", RF(NOW - dt.timedelta(hours=1)))])          # no quotes available at all
        f = [x for x in funnel.report(c, 24, NOW)["findings"] if "no verified price" in x["text"]]
        self.assertEqual((len(f), f[0]["severity"]), (1, "MEDIUM"))


class TestSurfaces(unittest.TestCase):
    def setUp(self):
        # The dashboard / doctor / CLI use the REAL clock, so this fixture must too (a fixed fake date silently aged out of their 24h window)
        from paper_agent.timeutil import utcnow
        self.now = utcnow()
        self.c = run([("Chevron to acquire Hess in $53 billion all-stock deal", "", RF(self.now - dt.timedelta(hours=1))), ("Fed cuts interest rates by 25 basis points", "", RF(self.now - dt.timedelta(hours=1)))], now=self.now)

    def test_dashboard_funnel_tab_renders_reasons_and_mergers(self):
        html = dashboard.render(self.c, H.cfg(), "funnel", {})
        for needle in ("Why events did or did not become candidates", "market_wide_event", "Recent merger", "Chevron to acquire Hess", "Trade candidates"):
            self.assertIn(needle, html)
        self.assertIn("Window", dashboard.render(self.c, H.cfg(), "funnel", {"hours": ["72"]}))

    def test_doctor_summarises_the_funnel_and_flags_skipped_cycles(self):
        self.c.execute("INSERT INTO cycle_runs(started,finished,status,mode,report_json) VALUES(?,?,?,?,?)", (iso(self.now - dt.timedelta(minutes=3)), iso(self.now), "SKIPPED", "full", json.dumps({"skipped": "low memory: 90 MB available"})))
        lines = []
        cfg = H.cfg()
        cfg["runtime"]["bind_port"] = 1
        r = doctor.run(self.c, cfg, "/nonexistent", srcs=[], live=False, check_units=False, out=lines.append)
        text = "\n".join(lines)
        self.assertIn("[funnel 24h]", text)
        self.assertIn("why events dropped", text)
        self.assertTrue(any("SKIPPED" in i["message"] for i in r["issues"]))

    def test_cli_funnel_command_prints_the_report(self):
        import io
        import contextlib
        from paper_agent import __main__ as cli
        p = tempfile.mktemp(suffix=".db")
        src = sqlite3.connect(self.c.execute("PRAGMA database_list").fetchone()[2])
        dst = sqlite3.connect(p)
        src.backup(dst)
        src.close()
        dst.close()
        old = cli.DB_PATH
        cli.DB_PATH = p
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(["funnel", "--hours", "48"]), 0)
        finally:
            cli.DB_PATH = old
        self.assertIn("DECISION FUNNEL, last 48h", buf.getvalue())

    def test_old_database_without_the_gate_column_is_upgraded_and_reported_honestly(self):
        c = H.fresh_db()
        c.execute("INSERT INTO events(created,event_type,scope,direction,magnitude) VALUES(?,?,?,?,?)", (iso(NOW), "general_news", "company", 0, .1))      # gate NULL = recorded by an older build
        self.assertIn("not recorded (older build)", funnel.report(c, 24, NOW)["gates"])


if __name__ == "__main__":
    unittest.main()


class TestNoCandidateIsEverSilentlyDropped(unittest.TestCase):
    """REGRESSION: after `max_new_per_cycle` executions the entry loop did `break`: every remaining candidate vanished with no decision row and
    no retry (the news item is already consumed). In a busy burst most candidates were lost."""

    def candidates(self, n):
        return [{"event_id": None, "ticker": "T%d" % i, "direction": 0.8, "magnitude": 0.6 - i * 0.01, "event_type": "earnings_beat", "conf": 0.9, "credibility": 0.9, "age_hours": 1.0,
                 "headline": "Co %d beats estimates" % i, "source": "Wire", "news_uid": None, "amount_usd": None, "horizon": 8} for i in range(n)]

    def run_entries(self, c, cands, now=NOW, results=None):
        from unittest import mock
        from paper_agent import trader
        cfg = H.cfg()
        cfg["risk"]["max_new_per_cycle"] = 3
        calls = []

        def fake(conn, cfg_, universe, cand, regime, now_, quote_fn, md_fetch=None, existing_decision=None, intel=None):
            calls.append(cand["ticker"])
            if existing_decision:                                                 # the real function marks the decision it just executed
                conn.execute("UPDATE decisions SET status='EXECUTED' WHERE id=?", (existing_decision,))
            return {"status": "EXECUTED", "decision_id": existing_decision or 0}
        with mock.patch.object(trader, "process_candidate", fake):
            return trader.run_entries(c, cfg, None, cands, {"regime": "risk_on"}, now, None), calls

    def test_candidates_beyond_the_cap_are_recorded_and_retried_not_discarded(self):
        c = H.fresh_db()
        out, calls = self.run_entries(c, self.candidates(8))
        self.assertEqual(len(calls), 3)                                           # only 3 evaluated/executed this cycle
        self.assertEqual(out["executed"], 3)
        rows = c.execute("SELECT ticker,status FROM decisions ORDER BY id").fetchall()
        self.assertEqual(len(rows), 5)                                            # the other 5 are RECORDED
        self.assertTrue(all(r["status"] == "AWAITING_CAPACITY" for r in rows))
        self.assertEqual(sorted(calls), ["T0", "T1", "T2"])                       # best-ranked first
        # next cycle: the deferred ones are retried (3 more allowed), the rest stay pending
        out2, calls2 = self.run_entries(c, [], NOW + dt.timedelta(minutes=10))
        self.assertEqual((len(calls2), out2["retried"], out2["deferred"]), (3, 3, 2))
        # and the cycle after that finishes them
        out3, calls3 = self.run_entries(c, [], NOW + dt.timedelta(minutes=20))
        self.assertEqual(len(calls3), 2)

    def test_deferred_candidates_expire_with_the_event_instead_of_lingering(self):
        c = H.fresh_db()
        self.run_entries(c, self.candidates(5))
        self.run_entries(c, [], NOW + dt.timedelta(hours=30))                     # older than the 24h trade window
        self.assertEqual(c.execute("SELECT COUNT(*) FROM decisions WHERE status='EXPIRED'").fetchone()[0], 2)

    def test_the_funnel_counts_deferred_decisions(self):
        c = H.fresh_db()
        self.run_entries(c, self.candidates(6))
        self.assertEqual(funnel.report(c, 24, NOW)["decisions"].get("AWAITING_CAPACITY"), 3)
