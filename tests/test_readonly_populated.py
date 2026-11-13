"""Nothing the dashboard does may ever write. This builds a POPULATED, MIGRATED database (every table the new code writes has rows: reasoning records, red-team reviews, graph relations, SEC facts, outcome paths, caches),
fingerprints the file and every table, runs EVERY Ask command and EVERY dashboard tab through a genuinely read-only connection, and fingerprints again: the database file must be byte-for-byte identical.
By default it uses a synthetic populated database. Set PA_REAL_DB=/path/to/an/export.db to run the same checks on a migrated COPY of a real database (the original is never touched)."""
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from test_build42 import NOW, SEED  # noqa: E402
from test_build42b import ins  # noqa: E402
from paper_agent import dashboard, db, facts as F, graph, nlp, pathways, reasoning as R, skeptic as S  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

REAL = os.environ.get("PA_REAL_DB")
EVENT_PHRASES = ["beats earnings", "misses estimates", "raises guidance", "cuts guidance", "wins a contract", "loses a contract", "recalls its product", "is sued", "suffers an outage", "is acquired", "is hacked", "gets FDA approval", "is fined", "launches a product", "goes bankrupt"]


def fingerprint(path):
    """sha256 of the database file, and (row count, sha256 of every row) for EVERY table."""
    with open(path, "rb") as fh:
        file_hash = hashlib.sha256(fh.read()).hexdigest()
    c = db.connect(path, readonly=True)
    tabs = {}
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        h, n = hashlib.sha256(), 0
        for row in c.execute('SELECT * FROM "%s" ORDER BY rowid' % t):
            h.update(repr(tuple(row)).encode())
            n += 1
        tabs[t] = (n, h.hexdigest())
    c.close()
    return file_hash, tabs


def populate(path, cfg):
    """Migrate (open_db) and fill every table the new code writes."""
    rw = db.open_db(path)
    rw.row_factory = __import__("sqlite3").Row
    graph.seed(rw, NOW, SEED)
    graph.ensure_edges(rw, cfg, NOW)
    from paper_agent.entities import Universe
    wl = "/tmp/rv/config/paper-agent/watchlist.json"
    if REAL and os.path.exists(wl):
        U = Universe(json.load(open(wl)))
    else:
        U = None
    rows = rw.execute("SELECT e.id,e.news_uid,e.headline,e.event_type,e.direction,e.magnitude,e.scope,e.tickers_json,n.summary FROM events e JOIN news n ON n.uid=e.news_uid ORDER BY e.id DESC LIMIT 250").fetchall()
    for r in rows:
        item = {"uid": r["news_uid"], "title": r["headline"], "summary": (r["summary"] or "")[:800], "analysis": {"event_type": r["event_type"], "direction": r["direction"], "magnitude": r["magnitude"], "scope": r["scope"], "confidence": 0.7},
                "tickers": json.loads(r["tickers_json"] or "[]"), "now": NOW}
        out = R.analyze_item(rw, cfg, U, item, NOW)
        R.log_record(rw, cfg, item, out, "guard", NOW)
        rw.execute("UPDATE reasoning_events SET event_ref=? WHERE news_uid=? AND event_ref IS NULL", (r["id"], r["news_uid"]))
    ticks = [r[0] for r in rw.execute("SELECT DISTINCT ticker FROM decisions WHERE ticker!='' ORDER BY id DESC LIMIT 12")] or ["NVDA"]
    ny = lambda a, b, c_: [{"start": "2025-01-01", "end": "2025-12-31", "val": a, "filed": "2026-02-10", "form": "10-K"}, {"start": "2026-01-01", "end": "2026-06-30", "val": b, "filed": "2026-08-01"}, {"start": "2025-01-01", "end": "2025-06-30", "val": c_, "filed": "2026-08-01"}]  # noqa: E731
    for t in ticks[:6]:
        f = F.summarize({"revenue": ny(400, 220, 185), "net_income": ny(-20, -8, -12)}, dt.datetime(2026, 8, 15, tzinfo=dt.timezone.utc))
        rw.execute("INSERT OR REPLACE INTO company_facts(ticker,facts_json,updated) VALUES(?,?,?)", (t, json.dumps(f, default=str), iso(NOW)))
    for d in rw.execute("SELECT d.id,d.ticker,d.event_id,d.forecast_id,f.inputs_json,f.scenarios_json,f.stop_return,f.ev_net,f.confidence,f.horizon_days,f.verdict FROM decisions d JOIN forecasts f ON f.id=d.forecast_id WHERE f.verdict IN ('BUY','SHORT') ORDER BY d.id DESC LIMIT 40").fetchall():
        try:
            fc = {"verdict": d["verdict"], "inputs": json.loads(d["inputs_json"] or "{}"), "stop_return": d["stop_return"], "ev_net": d["ev_net"], "confidence": d["confidence"], "horizon_days": d["horizon_days"], "scenarios": json.loads(d["scenarios_json"] or "{}")}
            rv = S.review(rw, cfg, {"ticker": d["ticker"], "event_id": d["event_id"], "event_type": "earnings_beat", "direction": 0.8, "credibility": 0.8, "headline": "x", "age_hours": 2.0}, fc, {}, {}, {}, NOW, d["id"])
            if rv:
                rw.execute("UPDATE redteam_reviews SET gate=? WHERE decision_id=?", (json.dumps(S.gate(rv, cfg, "guard")), d["id"]))
        except Exception:  # noqa: BLE001 - population is best effort; what matters is what is IN the database afterwards
            pass
    rw.execute("INSERT OR REPLACE INTO option_chain_cache(ticker,fetched,payload) VALUES('NVDA',?,'[]')", (iso(NOW),))
    pathways.compute(rw, cfg, NOW)
    rw.commit()
    counts = {t: rw.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("reasoning_events", "redteam_reviews", "relations", "company_facts", "option_chain_cache", "events", "decisions", "news")}
    rw.close()
    return counts, ticks


def run_everything(path, cfg, ticks):
    ro = db.connect(path, readonly=True)
    qs = ["help", "today", "what could hurt us", "gibberish ???", "", "'; DROP TABLE decisions;--", "why ZZZZ"]
    for t in ticks[:8] + ["NVDA", "AMD", "TSM", "GOOGL", "SPG", "NBHC"]:
        qs += ["why %s" % t, "news %s" % t, "related %s" % t, "what if %s cuts guidance" % t]
    for a, b in ((ticks[0], ticks[-1]), ("NVDA", "AMD")):
        qs.append("compare %s %s" % (a, b))
    for ph in EVENT_PHRASES:
        qs.append("what if NVDA %s" % ph)
    from paper_agent.theses_seed import SEED_THESES
    qs += ["theme %s" % t["key"].replace("_", " ") for t in SEED_THESES]
    n_ask = 0
    for q in qs:
        a = R.ask(ro, cfg, q, None, NOW)
        assert "text" in a, q
        n_ask += 1
    n_pages = 0
    variants = [{}, {"ask": ["why %s" % ticks[0]], "graph": [ticks[0]]}, {"ask": ["what if TSM cuts guidance"]}, {"ask": ["what could hurt us"]}, {"graph": ["NVDA"]}, {"ticker": [ticks[0]]}, {"id": ["1"]}, {"page": ["2"]},
                {"filter": ["related"]}, {"ask": ["'; DROP TABLE decisions;--"], "graph": ["<script>"]}]
    for tab in dashboard.TABS:
        for q in variants:
            h = dashboard.render(ro, cfg, tab, q, {"authed": True})
            assert len(h) > 100, (tab, q)
            n_pages += 1
    ro.close()
    return n_ask, n_pages


class TestNothingTheDashboardDoesEverWrites(unittest.TestCase):
    def test_every_ask_command_and_every_tab_leave_every_table_and_the_file_byte_for_byte_unchanged(self):
        d = tempfile.mkdtemp(prefix="pa-tests-ro-")
        path = os.path.join(d, "populated.db")
        cfg = H.cfg()
        cfg["graph"]["enabled"] = True
        if REAL:
            shutil.copy(REAL, path)
        else:
            rw = db.open_db(path)
            ins(rw, "forecasts", created=iso(NOW), ticker="NVDA", direction="LONG", horizon_days=5, p_up=0.6, ev_net=0.02, confidence=0.7, stop_return=-0.04, target_return=0.05, verdict="BUY",
                scenarios_json=json.dumps({"bull": {"ret": 0.06, "p": 0.3}, "base": {"ret": 0.02, "p": 0.4}, "bear": {"ret": -0.03, "p": 0.3}}), inputs_json=json.dumps({"priced_in": 0.06, "vol_daily": 0.02}), hash="h")
            ins(rw, "decisions", created=iso(NOW), ticker="NVDA", action="BUY", status="EXECUTED", reason="{}", forecast_id=1)
            for i in range(30):
                ins(rw, "news", uid="u%d" % i, source="W", title="NVIDIA beats estimates number %d" % i, summary="Revenue topped estimates.", url="http://x/%d" % i, published=iso(NOW), ingested=iso(NOW), trust=0.8)
                ins(rw, "events", id=i + 1, news_uid="u%d" % i, created=iso(NOW), headline="NVIDIA beats estimates number %d" % i, direction=0.8, magnitude=0.8, credibility=0.9, event_type="earnings_beat", tickers_json='["NVDA"]', scope="company", source="W", stale=0)
            rw.commit()
            rw.close()
        counts, ticks = populate(path, cfg)
        for t in ("reasoning_events", "relations", "company_facts"):
            self.assertGreater(counts[t], 0, "the populated database must have rows in %s" % t)
        before_file, before_tabs = fingerprint(path)
        n_ask, n_pages = run_everything(path, cfg, ticks)
        after_file, after_tabs = fingerprint(path)
        self.assertGreater(n_ask, 60)
        self.assertGreater(n_pages, 90)                                                                  # ten tabs now (the Funnel is part of the Overview): about ten renders each
        self.assertEqual(after_tabs, before_tabs, [t for t in before_tabs if before_tabs[t] != after_tabs.get(t)])      # every table: the same rows
        self.assertEqual(after_file, before_file)                                                                    # and the file itself, byte for byte
        for t in ("redteam_reviews", "reasoning_events", "relations", "company_facts", "option_chain_cache", "events", "decisions", "news", "prediction_paths"):
            self.assertEqual(before_tabs[t][0], after_tabs[t][0], t)
        print("\n[readonly] %s: %d tables fingerprinted, %d Ask questions and %d dashboard renders, file sha256 unchanged (%s...)" % ("REAL DB COPY" if REAL else "synthetic populated DB", len(before_tabs), n_ask, n_pages, before_file[:12]))
        print("[readonly] rows: %s" % json.dumps({t: before_tabs[t][0] for t in ("reasoning_events", "redteam_reviews", "relations", "company_facts", "option_chain_cache", "events", "decisions", "news", "prediction_paths")}))


if __name__ == "__main__":
    unittest.main()
