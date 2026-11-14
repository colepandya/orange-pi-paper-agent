"""REGRESSION: the Pi's database began as an older install whose `provider_health` table had no `backoff_until` column. `CREATE TABLE IF NOT EXISTS` never adds columns, so every real
quote lookup raised `no such column: backoff_until` - but ONLY while the market was open (the only time quotes are fetched): 64 failed cycles, zero trades.
Every earlier test started from a brand-new database, so it could never see this."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import db, doctor, preflight, quotes  # noqa: E402


def legacy_db():
    """A database in which EVERY table exists but with only its key columns (+ its first column): the worst case of 'an older install'."""
    p = tempfile.mktemp(suffix=".db")
    c = sqlite3.connect(p)
    for t, cols in db.expected_schema().items():
        pk = [x for x in cols if x[4]]
        keep = {cols[0][0]} | {x[0] for x in pk}
        defs = ['"%s" %s' % (x[0], x[1]) for x in cols if x[0] in keep]
        if len(pk) > 1:
            defs.append("PRIMARY KEY(%s)" % ",".join('"%s"' % x[0] for x in pk))
        elif len(pk) == 1:
            defs = [d + " PRIMARY KEY" if d.startswith('"%s"' % pk[0][0]) else d for d in defs]
        c.execute('CREATE TABLE "%s"(%s)' % (t, ",".join(defs)))
    c.commit()
    c.close()
    return p


class TestSchemaRepair(unittest.TestCase):
    def test_the_exact_failure_a_legacy_provider_health_table(self):
        p = tempfile.mktemp(suffix=".db")
        raw = sqlite3.connect(p)
        raw.execute("CREATE TABLE provider_health(provider TEXT PRIMARY KEY, requests INTEGER)")
        raw.execute("INSERT INTO provider_health VALUES('TWELVE_DATA',41)")
        raw.commit()
        raw.close()
        c = db.open_db(p)
        self.assertIn("backoff_until", db.columns(c, "provider_health"))
        self.assertEqual(tuple(c.execute("SELECT provider,requests FROM provider_health").fetchone()), ("TWELVE_DATA", 41))      # data preserved

    def test_every_table_missing_every_column_is_repaired_with_no_data_loss(self):
        p = legacy_db()
        raw = sqlite3.connect(p)
        raw.execute("INSERT INTO meta(key) VALUES('keepme')") if "key" in [x[0] for x in db.expected_schema()["meta"]] else None
        raw.commit()
        raw.close()
        c = db.open_db(p)
        self.assertEqual(db.schema_drift(c), {})
        report = db.migrate(c)
        self.assertEqual((report["repaired_columns"], report["unfixable"]), ([], []))                  # idempotent: nothing left to do the second time
        self.assertEqual(c.execute("SELECT COUNT(*) FROM meta WHERE key='keepme'").fetchone()[0], 1)
        for idx in ("ix_candq_status",):
            self.assertTrue(c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (idx,)).fetchone(), idx)         # indexes created AFTER the repair

    def test_drift_is_reported_read_only_before_any_repair(self):
        p = tempfile.mktemp(suffix=".db")
        raw = sqlite3.connect(p)
        raw.execute("CREATE TABLE provider_health(provider TEXT PRIMARY KEY, requests INTEGER)")
        drift = db.schema_drift(raw)
        self.assertIn("backoff_until", drift["provider_health"])
        self.assertEqual(drift["ledger"], ["<table missing>"])

    def test_a_fresh_database_has_no_drift(self):
        self.assertEqual(db.schema_drift(H.fresh_db()), {})

    def test_doctor_and_preflight_report_drift_instead_of_saying_all_good(self):
        c = H.fresh_db()
        c.execute("DROP TABLE provider_health")
        c.execute("CREATE TABLE provider_health(provider TEXT PRIMARY KEY, requests INTEGER)")
        lines = []
        cfg = H.cfg()
        cfg["runtime"]["bind_port"] = 1
        r = doctor.run(c, cfg, "/nonexistent", srcs=[], live=False, check_units=False, now=H.NOW, out=lines.append)
        self.assertTrue(any("missing columns the code needs" in i["message"] and "backoff_until" in i["message"] for i in r["issues"]))
        self.assertIn("[schema] DRIFT", "\n".join(lines))
        res = preflight.run(c, cfg, H.universe(), now=H.NOW, live=False, check_units=False, mem_mb=lambda: 600, out=lambda s: None)
        row = next(x for x in res["checks"] if x["name"].startswith("Database schema"))
        self.assertEqual(row["status"], "FAIL")
        self.assertFalse(res["ready"])

    def test_preflight_fails_when_recent_cycles_are_erroring(self):
        import datetime as dt
        import json
        from paper_agent.timeutil import iso
        c = H.fresh_db()
        for i in range(6):
            rep = {"stages": {"entries": {"error": "OperationalError: no such column: backoff_until"}}}
            c.execute("INSERT INTO cycle_runs(started,finished,status,mode,report_json) VALUES(?,?,?,?,?)", (iso(H.NOW - dt.timedelta(minutes=5 * i)), iso(H.NOW), "ERROR" if i < 4 else "OK", "full", json.dumps(rep)))
        cfg = H.cfg()
        cfg["runtime"]["min_available_memory_mb"] = 120
        res = preflight.run(c, cfg, H.universe(), now=H.NOW, live=False, check_units=False, mem_mb=lambda: 600, out=lambda s: None)
        row = next(x for x in res["checks"] if x["name"].startswith("Recent cycles free"))
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("backoff_until", row["detail"])


class TestEverythingWorksOnALegacyShapedDatabase(TL.Base):
    """The strongest test: the complete buy -> wait -> open -> sell lifecycle, on a database that started life as an older install."""

    def setUp(self):
        super().setUp()
        self.c = db.open_db(legacy_db())

    def test_real_quote_consensus_runs_against_the_database(self):
        self.mkt.now = TL.T_OPEN
        q = quotes.get_quote(self.c, "NVDA", self.cfg, "entry", now=TL.T_OPEN, fetch=self.mkt)
        self.assertEqual(q["status"], "PASS")                                           # this call raised OperationalError on the Pi

    def test_buy_at_the_open_then_sell(self):
        plan = self.open_position()
        self.assertEqual(self.one("SELECT status FROM decisions WHERE ticker='NVDA'")[0], "EXECUTED")
        self.mkt.price["NVDA"] = plan["stop_price"] - 1
        r = self.guard(TL.T_OPEN + __import__("datetime").timedelta(minutes=30))
        self.assertEqual((r["committed"], r["errors"]), (1, 0))
        self.assertEqual(self.one("SELECT review_code FROM positions")[0], "STOP_REACHED")
        TL.books_balance(self, self.c)

    def test_the_cycle_reports_no_stage_errors(self):
        self.cycle(TL.T_PRE, news=True)
        r = self.cycle(TL.T_OPEN)
        self.assertEqual(r["errors"], [])


if __name__ == "__main__":
    unittest.main()
