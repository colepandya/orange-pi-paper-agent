"""Build 52: no loop may write to the database while it is still reading from it. In WAL mode that fails INSTANTLY ('database is locked', no waiting) when ANOTHER connection commits in between: the guard, the executor
and the dashboard all do. The Pi reported it from the research-gap scan; the same pattern was in flows.record_event_flows."""
import ast
import datetime as dt
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
from paper_agent import config, db, flows, gaps  # noqa: E402
from paper_agent.timeutil import iso, utcnow  # noqa: E402

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "paper_agent")


class Foreign:
    """A connection that behaves like 'the cycle' but lets ANOTHER connection commit just before its first matching write: what the guard or the executor do between our read and our write."""

    def __init__(self, conn, other, trigger):
        self.conn, self.other, self.trigger, self.fired = conn, other, trigger, False

    def execute(self, sql, *a, **k):
        if not self.fired and sql.lstrip().upper().startswith(self.trigger):
            self.fired = True
            self.other.execute("INSERT OR REPLACE INTO meta VALUES('foreign_write', ?)", (iso(utcnow()),))
        return self.conn.execute(sql, *a, **k)

    def __getattr__(self, n):
        return getattr(self.conn, n)


def world():
    d = tempfile.mkdtemp(prefix="pa-lock-", dir="/tmp")
    path = d + "/t.db"
    return db.open_db(path), db.connect(path), config.load_config("/nonexistent", with_overrides=False)


def event(c, etype, headline, now, tickers='["AAA"]'):
    c.execute("INSERT INTO events(created,event_type,headline,scope,direction,magnitude,tickers_json,regions_json,novelty,credibility,stale,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (iso(now), etype, headline, "company", 1.0, 0.5, tickers, "[]", 1.0, 0.9, 0, "x"))


class TestAnotherWriterCommittingMidLoop(unittest.TestCase):
    def test_the_research_gap_scan_survives_a_foreign_commit_between_its_read_and_its_write(self):
        a, b, cfg = world()
        now = utcnow()
        for i in range(3):
            event(a, "contract_win", "Big contract %d" % i, now)
        a.commit()
        f = Foreign(a, b, "INSERT OR IGNORE INTO RESEARCH_TASKS")
        n = gaps.scan(f, cfg, now)
        self.assertTrue(f.fired, "the scenario must really have interleaved a foreign commit")
        self.assertGreaterEqual(n, 3)
        self.assertEqual(a.execute("SELECT COUNT(*) FROM research_tasks WHERE kind='missing_contract_value'").fetchone()[0], 3)

    def test_the_thesis_loop_in_the_same_scan_too(self):
        a, b, cfg = world()
        now = utcnow()
        vals = {}
        for cid, name, typ, notnull, dflt, pk in a.execute("PRAGMA table_info(theses)").fetchall():
            if notnull and dflt is None and not pk:
                vals[name] = "x" if "CHAR" in (typ or "").upper() or "TEXT" in (typ or "").upper() or not typ else 0.5
        vals.update({"key": "t1", "name": "T one", "active": 1, "last_evidence": iso(now - dt.timedelta(days=30))})
        a.execute("INSERT INTO theses(%s) VALUES(%s)" % (",".join(vals), ",".join("?" * len(vals))), tuple(vals.values()))
        a.commit()
        f = Foreign(a, b, "INSERT OR IGNORE INTO RESEARCH_TASKS")
        gaps.scan(f, cfg, now)                                                                          # must not raise, whether or not this thesis raises a task

    def test_buyback_flows_survive_a_foreign_commit_between_the_read_and_the_write(self):
        a, b, _cfg = world()
        now = utcnow()
        for i in range(2):
            event(a, "buyback_dividend", "Big buyback %d" % i, now, tickers='["AAA"]')
        a.commit()
        f = Foreign(a, b, "INSERT OR IGNORE INTO CAPITAL_FLOWS")
        n = flows.record_event_flows(f, iso(now - dt.timedelta(days=1)))
        self.assertTrue(f.fired)
        self.assertEqual(n, 2)
        self.assertEqual(a.execute("SELECT COUNT(*) FROM capital_flows WHERE kind='buyback'").fetchone()[0], 2)

    def test_the_harness_itself_would_have_caught_the_old_pattern(self):
        a, b, _cfg = world()
        now = utcnow()
        for i in range(2):
            event(a, "buyback_dividend", "Big buyback %d" % i, now)
        a.commit()
        f = Foreign(a, b, "INSERT OR IGNORE INTO CAPITAL_FLOWS")
        with self.assertRaises(Exception) as cm:                                                         # the OLD shape: write while the read cursor is still open
            for e in f.execute("SELECT id FROM events WHERE event_type='buyback_dividend'"):
                f.execute("INSERT OR IGNORE INTO capital_flows(ticker,ts,kind,value,shares,detail_json,source,dedupe) VALUES('AAA',?, 'buyback',0,NULL,'{}','events',?)", (iso(now), "x%d" % e["id"]))
        self.assertIn("database is locked", str(cm.exception))


WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)


def writes_while_reading(root):
    """Loops over an un-fetched cursor whose body writes. Same rule as the scanner used to find the bug."""
    def is_write(n):
        if not isinstance(n, ast.Call):
            return False
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr in ("execute", "executemany") and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str) and WRITE.match(n.args[0].value):
            return True
        return (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) in ("_task",)
    hits = []
    for dp, _d, fs in os.walk(root):
        for fn in fs:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dp, fn)
            tree = ast.parse(open(p).read())
            lazy = {tg.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "execute" for tg in n.targets if isinstance(tg, ast.Name)}
            for n in ast.walk(tree):
                if isinstance(n, ast.For) and ((isinstance(n.iter, ast.Call) and isinstance(n.iter.func, ast.Attribute) and n.iter.func.attr == "execute") or (isinstance(n.iter, ast.Name) and n.iter.id in lazy)):
                    if any(is_write(x) for b in n.body for x in ast.walk(b)):
                        hits.append("%s:%d" % (os.path.relpath(p, root), n.lineno))
    return hits


class TestNoLoopWritesWhileItIsStillReading(unittest.TestCase):
    def test_no_module_does_it(self):
        self.assertEqual(writes_while_reading(PKG), [], "read the rows fully (.fetchall()) BEFORE writing: a foreign commit in between makes the write fail with 'database is locked'")

    def test_the_guard_itself_catches_the_pattern(self):
        d = tempfile.mkdtemp(prefix="pa-scan-", dir="/tmp")
        open(d + "/bad.py", "w").write("def f(conn):\n    for r in conn.execute('SELECT id FROM t'):\n        conn.execute('INSERT INTO u VALUES (?)', (r[0],))\n")
        open(d + "/bad2.py", "w").write("def f(conn):\n    cur = conn.execute('SELECT id FROM t')\n    for r in cur:\n        conn.execute('UPDATE u SET a=1')\n")
        open(d + "/ok.py", "w").write("def f(conn):\n    for r in conn.execute('SELECT id FROM t').fetchall():\n        conn.execute('INSERT INTO u VALUES (?)', (r[0],))\n    for r in conn.execute('SELECT id FROM t'):\n        print(r)\n")
        self.assertEqual(sorted(writes_while_reading(d)), ["bad.py:2", "bad2.py:3"])


if __name__ == "__main__":
    unittest.main()
