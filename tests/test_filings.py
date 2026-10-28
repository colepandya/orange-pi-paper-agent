import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_intel as TI  # noqa: E402
from paper_agent import dashboard, dossier, filings, llm_client, net, pipeline, redteam  # noqa: E402

NOW = H.NOW
BIZ = "Lockheed Martin designs and manufactures advanced defense systems for the U.S. government. " * 6
RISK = "Our business depends heavily on U.S. government contracts and budget appropriations which may change. " * 5
MDA = "Net sales increased 7% driven by higher volume on the F-35 program and missile defense growth. Backlog was a record $176 billion. " * 4
TEN_K = ("<html><head><style>.x{color:red}</style><script>var secret='SHOULD NOT APPEAR';</script></head><body>"
         "<p>TABLE OF CONTENTS</p><p>Item 1. Business 3</p><p>Item 1A. Risk Factors 9</p><p>Item 1B. Unresolved 20</p><p>Item 7. Management's Discussion 30</p><p>Item 7A. Quantitative 40</p>"
         "<p>Item 1. Business</p><p>%s</p><p>Item 1A. Risk Factors</p><p>%s</p><p>Item 1B. Unresolved Staff Comments</p><p>none</p>"
         "<p>Item 7. Management's Discussion and Analysis</p><p>%s</p><p>Item 7A. Quantitative and Qualitative</p><p>x</p></body></html>") % (BIZ, RISK, MDA)
TEN_Q = "<html><body><p>Item 2. Management's Discussion and Analysis</p><p>" + ("Second quarter sales rose 5% on F-35 deliveries and demand for munitions remained strong across customers. " * 6) + "</p><p>Item 3. Quantitative</p></body></html>"
EX99 = "<html><body><p>Lockheed Martin reports third quarter 2026 results. Net sales of $18.6 billion increased 8 percent. The company raised its full-year sales outlook to $74.0 billion.</p>" + "<p>Segment detail follows here with more explanatory text about the quarter. </p>" * 12 + "</body></html>"
SUB = {"filings": {"recent": {
    "accessionNumber": ["0000936468-26-000030", "0000936468-26-000028", "0000936468-26-000020", "0000936468-26-000010"],
    "filingDate": ["2026-10-20", "2026-10-01", "2026-08-01", "2026-02-10"],
    "form": ["8-K", "8-K", "10-Q", "10-K"], "items": ["2.02,9.01", "5.07", "", ""],
    "primaryDocument": ["lmt-20261020.htm", "lmt-20261001.htm", "lmt-20260801.htm", "lmt-20260210.htm"]}}}


def sec_fetch(url, headers=None, timeout=15, max_bytes=0, data=None, method=None):
    if "submissions" in url:
        return json.dumps(SUB).encode(), 200, {}
    if url.endswith("index.json"):
        return json.dumps({"directory": {"item": [{"name": "lmt-20261020.htm"}, {"name": "ex99-1.htm"}]}}).encode(), 200, {}
    if "ex99-1.htm" in url:
        return EX99.encode(), 200, {}
    if "lmt-20260210" in url:
        return TEN_K.encode(), 200, {}
    if "lmt-20260801" in url:
        return TEN_Q.encode(), 200, {}
    raise net.FetchError("404", 404)


def extraction_handler(task, doc, body):
    if task != "filing_extract":
        return TI.handler_default()(task, doc, body)
    q = []
    if "F-35" in doc:
        q.append({"claim": "F-35 volume", "quote": "driven by higher volume on the F-35 program"} if "driven by higher volume on the F-35 program" in doc else {"claim": "F-35 deliveries", "quote": "Second quarter sales rose 5% on F-35 deliveries"})
    if "raised its full-year sales outlook" in doc:
        q.append({"claim": "guidance", "quote": "The company raised its full-year sales outlook to $74.0 billion"})
    if "depends heavily on U.S. government contracts" in doc:
        q.append({"claim": "risk", "quote": "Our business depends heavily on U.S. government contracts and budget appropriations"})
    return {"revenue_drivers": ["F-35 program volume", "missile defense"], "cost_drivers": ["labor"], "customers": ["U.S. government"], "suppliers": [], "competitors": ["RTX", "Northrop"],
            "benefits": ["record backlog"], "risks": [{"risk": "dependence on U.S. government budgets", "severity": 0.5}], "guidance": {"revenue": "$74.0B", "eps": None, "commentary": "raised"},
            "results": {"revenue_growth_yoy": 0.08, "margin_trend": "stable", "vs_expectations": "unknown"}, "backlog_usd": 176e9, "mgmt_tone": 0.4, "changes_vs_prior": ["raised outlook"], "evidence_quotes": q}


class TestFilings(TI.Base):
    def client(self, handler=extraction_handler):
        self.fake = TI.FakeLLM(handler)
        return llm_client.LLMClient(self.c, self.cfg, self.fake)

    def test_html_cleaning_removes_scripts_and_styles(self):
        t = filings.html_to_text(TEN_K)
        self.assertNotIn("SHOULD NOT APPEAR", t)
        self.assertNotIn("color:red", t)
        self.assertIn("Net sales increased 7%", t)

    def test_section_finder_skips_table_of_contents(self):
        t = filings.html_to_text(TEN_K)
        s = filings.extract_sections(t, "10-K")
        self.assertIn("RISK FACTORS", s)
        self.assertIn("depends heavily on U.S. government contracts", s)
        self.assertGreater(len(s), 800)

    def test_filing_selection_takes_latest_per_type_and_skips_irrelevant_8k(self):
        picked = {(f["form"], f["accession"][-2:]) for f in filings.recent_filings(SUB, now=NOW)}
        self.assertEqual(picked, {("8-K", "30"), ("10-Q", "20"), ("10-K", "10")})     # the 5.07 vote 8-K is ignored

    def test_refresh_builds_knowledge_base_and_never_reprocesses(self):
        cl = self.client()
        r = filings.refresh(self.c, self.cfg, cl, "LMT", 936468, sec_fetch, NOW, max_filings=3, sleep=lambda s: None)
        self.assertEqual((r["new"], r["ok"]), (3, 3))
        kb = json.loads(self.c.execute("SELECT summary_json FROM company_knowledge WHERE ticker='LMT'").fetchone()[0])
        self.assertIn("F-35 program volume", kb["revenue_drivers"])
        self.assertEqual(kb["customers"], ["U.S. government"])
        self.assertEqual(kb["latest"]["backlog_usd"], 176e9)
        n_calls = len(self.fake.calls)
        r2 = filings.refresh(self.c, self.cfg, cl, "LMT", 936468, sec_fetch, NOW, sleep=lambda s: None)
        self.assertEqual(r2["new"], 0)
        self.assertEqual(len(self.fake.calls), n_calls)             # nothing re-read, nothing re-billed
        # the earnings 8-K was read from the EXHIBIT, not the empty cover document
        self.assertTrue(any("third quarter 2026 results" in c["doc"] for c in self.fake.calls))

    def test_hallucinated_extraction_is_rejected_and_not_retried_forever(self):
        def liar(task, doc, body):
            o = extraction_handler(task, doc, body)
            o["evidence_quotes"] = [{"claim": "x", "quote": "the company secretly doubled revenue overnight"}]
            return o
        cl = self.client(liar)
        r = filings.refresh(self.c, self.cfg, cl, "LMT", 936468, sec_fetch, NOW, max_filings=3, sleep=lambda s: None)
        self.assertEqual(r["ok"], 0)
        self.assertIsNone(self.c.execute("SELECT 1 FROM company_knowledge").fetchone())
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM company_filings WHERE ok=0").fetchone()[0], 3)   # remembered => no repeat spend

    def test_skips_cleanly_without_llm_or_cik(self):
        self.cfg["intelligence"]["llm_enabled"] = False
        self.assertEqual(filings.refresh(self.c, self.cfg, self.client(), "LMT", 936468, sec_fetch, NOW)["skipped"], "llm disabled")
        self.cfg["intelligence"]["llm_enabled"] = True
        self.assertEqual(filings.refresh(self.c, self.cfg, self.client(), "LMT", None, sec_fetch, NOW)["skipped"], "no CIK")

    def test_due_tickers_is_bounded_and_rate_limited_per_ticker(self):
        from paper_agent import db
        self.cfg["intelligence"]["max_knowledge_refresh_per_cycle"] = 2
        self.assertEqual(filings.due_tickers(self.c, self.cfg, ["A", "B", "C"], ["D"], NOW), ["A", "B"])
        db.mark_run(self.c, "kb:A", "", NOW)                      # same clock as the due-check below
        self.assertEqual(filings.due_tickers(self.c, self.cfg, ["A", "B", "C"], [], NOW), ["B", "C"])

    def test_pipeline_reads_filings_then_redteam_and_dossier_use_the_knowledge(self):
        deps = H.deps([(TI.LMT_TITLE, "Navy awards contract", TI.RF(NOW - dt.timedelta(hours=1)))], {"LMT": 480.0}, llm_transport=TI.FakeLLM(extraction_handler))
        self.fake = deps.llm_transport

        def routed(url, **k):
            if "sec.gov" in url:
                return sec_fetch(url, **k)
            return H.make_fetch([(TI.LMT_TITLE, "Navy awards contract", TI.RF(NOW - dt.timedelta(hours=1)))])(url, **k)
        deps.fetch = routed
        r = pipeline.run_cycle(self.c, self.cfg, self.U, TI.SRC, deps, NOW)
        self.assertEqual(r["errors"], [], r["stages"])
        self.assertGreaterEqual(r["knowledge"]["LMT"]["ok"], 1)
        rt_call = next(c for c in self.fake.calls if c["task"] == "redteam")
        self.assertIn("Company knowledge base", rt_call["doc"])           # the skeptic sees what the filings say
        self.assertIn("F-35", rt_call["doc"])
        d = dossier.build(self.c, self.cfg, "LMT", NOW)
        self.assertIn("F-35 program volume", d["knowledge"]["revenue_drivers"])
        self.assertIn("Company knowledge", dashboard.render(self.c, self.cfg, "dossiers", {"ticker": ["LMT"]}))


if __name__ == "__main__":
    unittest.main()
