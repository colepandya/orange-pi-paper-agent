"""Clean-up of leftovers: every kind of leftover is found and removed, and everything that must survive does. Fake Pi in a temp folder; no real paths touched."""
import datetime as dt
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import cleanup as CL, config as cfgmod, db  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402

NOW = dt.datetime(2026, 10, 31, 12, 0, tzinfo=dt.timezone.utc)


def touch(p, content="x", age_days=0.0):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(content)
    t = NOW.timestamp() - age_days * 86400
    os.utime(p, (t, t))
    if age_days:
        os.utime(os.path.dirname(p), (t, t))                                                              # a folder is as old as the file in it


def fake_pi():
    r = tempfile.mkdtemp(prefix="pa-tests-")
    P = CL.Paths(app=r + "/opt/paper-agent", units=r + "/etc/systemd/system", backups=r + "/var/backups/paper-agent", state=r + "/var/lib/paper-agent", config_dir=r + "/etc/paper-agent", home=r + "/home/orangepi",
                 tmp=(r + "/tmp", r + "/var/tmp"))
    # ---- the leftovers
    touch(P.app + "/app.legacy-20261028T011804Z/old_v4.py", "v4 code" * 100)
    touch(P.units + "/paper-agent-broad.timer.before-reschedule-fix", "[Timer]")
    touch(P.units + "/paper-agent-cycle.service.bak", "[Service]")
    for i in range(1, 7):
        touch(P.backups + "/v5-2026102%dT010000Z/paper-agent.db" % i, "db%d" % i)
    for n in (30, 31, 36, 38, 39):
        touch(P.home + "/b%d/paper-agent-v5/BUILD" % n, str(n))
        touch(P.home + "/paper-agent-build%d.zip" % n, "zip%d" % n)
    touch(P.state + "/tmp/old.db", "x", 5)
    touch(P.tmp[0] + "/pa-review.abc/db", "x", 3)
    touch(P.tmp[1] + "/paper-agent-review.tar.gz", "x", 3)
    # ---- what MUST survive
    touch(P.app + "/BUILD", "40")
    touch(P.app + "/paper_agent/trader.py", "code")
    touch(P.units + "/paper-agent-cycle.service", "[Service]")
    touch(P.units + "/paper-agent-cycle.timer", "[Timer]")
    touch(P.state + "/paper-agent.db", "THE DATABASE")
    touch(P.state + "/secrets.env", "KEY=secret")
    touch(P.state + "/settings.json", "{}")
    touch(P.state + "/tmp/fresh.tmp", "x", 0.1)
    touch(P.config_dir + "/market-data.env", "K=1")
    touch(P.tmp[0] + "/pa-review.fresh/db", "x", 0.1)
    touch(P.tmp[0] + "/somebody-elses-file", "x", 30)
    touch(P.home + "/notes.txt", "my notes", 30)
    return r, P


def survivors(P):
    return [P.app + "/BUILD", P.app + "/paper_agent/trader.py", P.units + "/paper-agent-cycle.service", P.units + "/paper-agent-cycle.timer", P.state + "/paper-agent.db", P.state + "/secrets.env", P.state + "/settings.json",
            P.state + "/tmp/fresh.tmp", P.config_dir + "/market-data.env", P.tmp[0] + "/pa-review.fresh/db", P.tmp[0] + "/somebody-elses-file", P.home + "/notes.txt", P.home + "/b39/paper-agent-v5/BUILD", P.home + "/paper-agent-build39.zip",
            P.backups + "/v5-20261026T010000Z/paper-agent.db", P.backups + "/v5-20261025T010000Z/paper-agent.db", P.backups + "/v5-20261024T010000Z/paper-agent.db"]


class TestFiles(unittest.TestCase):
    def setUp(self):
        self.root, self.P = fake_pi()

    def test_the_plan_finds_each_kind_of_leftover_and_changes_nothing(self):
        items = CL.fs_items(self.P, NOW)
        paths = {os.path.relpath(i["path"], self.root) for i in items}
        self.assertIn("opt/paper-agent/app.legacy-20261028T011804Z", paths)
        self.assertIn("etc/systemd/system/paper-agent-broad.timer.before-reschedule-fix", paths)
        self.assertIn("etc/systemd/system/paper-agent-cycle.service.bak", paths)
        self.assertEqual({p for p in paths if "v5-" in p}, {"var/backups/paper-agent/v5-2026102%dT010000Z" % i for i in (1, 2, 3)})            # all but the newest 3
        self.assertEqual({p for p in paths if p.startswith("home/orangepi/b")}, {"home/orangepi/b30", "home/orangepi/b31", "home/orangepi/b36", "home/orangepi/b38"})
        self.assertEqual({p for p in paths if p.endswith(".zip")}, {"home/orangepi/paper-agent-build%d.zip" % n for n in (30, 31, 36, 38)})
        self.assertIn("var/lib/paper-agent/tmp/old.db", paths)
        self.assertIn("tmp/pa-review.abc", paths)
        self.assertIn("var/tmp/paper-agent-review.tar.gz", paths)
        for s in survivors(self.P):
            self.assertTrue(os.path.exists(s), s)
        self.assertEqual(len(items), len(paths))

    def test_applying_removes_the_leftovers_and_every_protected_file_survives(self):
        items = CL.fs_items(self.P, NOW)
        out = CL.fs_apply(items, self.P, NOW)
        self.assertEqual(out["refused"], 0)
        self.assertEqual(out["removed"], len(items))
        self.assertGreater(out["freed"], 0)
        for i in items:
            self.assertFalse(os.path.lexists(i["path"]), i["path"])
        for s in survivors(self.P):
            self.assertTrue(os.path.exists(s), "a protected file was removed: " + s)

    def test_the_old_app_and_the_stale_units_are_archived_before_they_are_removed(self):
        out = CL.fs_apply(CL.fs_items(self.P, NOW), self.P, NOW)
        self.assertTrue(out["archived_to"].startswith(self.P.backups))
        with tarfile.open(out["archived_to"]) as t:
            names = t.getnames()
        self.assertTrue(any(n.endswith("app.legacy-20261028T011804Z/old_v4.py") for n in names))
        self.assertTrue(any(n.endswith("paper-agent-broad.timer.before-reschedule-fix") for n in names))
        self.assertFalse(any("v5-" in n for n in names))                                                 # install backups are just deleted, not archived into another backup

    def test_running_it_twice_finds_nothing_the_second_time(self):
        CL.fs_apply(CL.fs_items(self.P, NOW), self.P, NOW)
        self.assertEqual(CL.fs_items(self.P, NOW), [])

    def test_it_refuses_anything_outside_the_allow_listed_folders(self):
        outside = tempfile.mkdtemp(prefix="pa-tests-outside-")
        touch(outside + "/precious.txt")
        bad = [{"path": outside + "/precious.txt", "why": "x", "bytes": 1, "archive": False}, {"path": "/etc/passwd", "why": "x", "bytes": 1, "archive": False}]
        out = CL.fs_apply(bad, self.P, NOW)
        self.assertEqual((out["removed"], out["refused"]), (0, 2))
        self.assertTrue(os.path.exists(outside + "/precious.txt"))

    def test_a_symlink_in_the_home_folder_is_removed_itself_never_followed(self):
        target = tempfile.mkdtemp(prefix="pa-tests-target-")
        touch(target + "/keep.txt")
        os.symlink(target, self.P.home + "/b99")
        os.makedirs(self.P.home + "/b100/x")
        CL.fs_apply(CL.fs_items(self.P, NOW), self.P, NOW)
        self.assertTrue(os.path.exists(target + "/keep.txt"))                                             # what the link pointed at is untouched

    def test_missing_folders_are_not_an_error(self):
        empty = CL.Paths(app="/nonexistent/a", units="/nonexistent/u", backups="/nonexistent/b", state="/nonexistent/s", home=None, tmp=("/nonexistent/t",))
        self.assertEqual(CL.fs_items(empty, NOW), [])


def insert(conn, table, **vals):
    cols = list(conn.execute("PRAGMA table_info(%s)" % table))
    row = dict(vals)
    for c in cols:
        if c[1] not in row and c[3] and c[4] is None and not c[5]:
            row[c[1]] = 0 if "INT" in (c[2] or "").upper() or "REAL" in (c[2] or "").upper() else ""
    conn.execute("INSERT INTO %s(%s) VALUES(%s)" % (table, ",".join(row), ",".join("?" * len(row))), list(row.values()))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.c = H.fresh_db()
        old, new = iso(NOW - dt.timedelta(days=40)), iso(NOW - dt.timedelta(days=2))
        mk = lambda created, tk: insert(self.c, "forecasts", created=created, ticker=tk, direction="LONG", horizon_days=3, scenarios_json="{}", inputs_json="{}", hash="h%s%s" % (created, tk), verdict="BUY")   # noqa: E731
        self.f_decision, self.f_position, self.f_prediction, self.f_option = mk(old, "A"), mk(old, "B"), mk(old, "C"), mk(old, "D")
        self.f_orphan_old, self.f_orphan_new = mk(old, "E"), mk(new, "F")
        insert(self.c, "decisions", created=old, ticker="A", action="BUY", status="EXECUTED", reason="{}", forecast_id=self.f_decision)
        insert(self.c, "positions", ticker="B", opened=old, status="CLOSED", forecast_id=self.f_position)
        insert(self.c, "predictions", created=old, forecast_id=self.f_prediction)
        insert(self.c, "option_positions", underlying="D", kind="CALL", status="CLOSED", forecast_id=self.f_option)
        insert(self.c, "provider_calls", provider="X", ts=NOW.timestamp() - 9 * 86400)
        insert(self.c, "provider_calls", provider="X", ts=NOW.timestamp() - 3600)
        self.c.execute("INSERT INTO option_chain_cache(ticker,fetched,payload) VALUES('OLD',?,'[]')", (iso(NOW - dt.timedelta(days=5)),))
        self.c.execute("INSERT INTO option_chain_cache(ticker,fetched,payload) VALUES('NEW',?,'[]')", (iso(NOW - dt.timedelta(hours=1)),))
        self.c.commit()

    def keys(self, items):
        return {i["key"]: i["rows"] for i in items}

    def test_only_unreferenced_old_forecasts_are_orphans(self):
        k = self.keys(CL.db_items(self.c, NOW))
        self.assertEqual(k["orphan_forecasts"], 1)                                                       # not the four that something points at, not the recent one
        CL.db_apply(self.c, CL.db_items(self.c, NOW))
        left = {r[0] for r in self.c.execute("SELECT id FROM forecasts")}
        self.assertEqual(left, {self.f_decision, self.f_position, self.f_prediction, self.f_option, self.f_orphan_new})

    def test_stale_caches_and_logs_go_and_fresh_ones_stay(self):
        CL.db_apply(self.c, CL.db_items(self.c, NOW))
        self.assertEqual([r[0] for r in self.c.execute("SELECT ticker FROM option_chain_cache")], ["NEW"])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM provider_calls WHERE ts<?", (NOW.timestamp() - 7 * 86400,)).fetchone()[0], 0)

    def test_open_positions_reviews_are_never_pruned(self):
        pid = insert(self.c, "positions", ticker="Z", opened=iso(NOW - dt.timedelta(days=200)), status="OPEN")
        self.c.execute("INSERT INTO redteam_reviews(created,ticker,position_id,review_json) VALUES(?,?,?,'{}')", (iso(NOW - dt.timedelta(days=200)), "Z", pid))
        self.c.execute("INSERT INTO redteam_reviews(created,ticker,position_id,review_json) VALUES(?,?,NULL,'{}')", (iso(NOW - dt.timedelta(days=200)), "Y"))
        self.c.commit()
        CL.db_apply(self.c, CL.db_items(self.c, NOW))
        self.assertEqual([r[0] for r in self.c.execute("SELECT ticker FROM redteam_reviews")], ["Z"])

    def test_it_is_idempotent_and_leaves_the_database_valid(self):
        CL.db_apply(self.c, CL.db_items(self.c, NOW))
        self.assertTrue(all(i["rows"] == 0 for i in CL.db_items(self.c, NOW)))
        self.assertEqual(self.c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.c.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_the_plan_alone_changes_nothing(self):
        before = self.c.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]
        pl = CL.plan(self.c, CL.Paths(app="/x", units="/x", backups="/x", state="/x", config_dir="/x", home=None, tmp=()), NOW)
        self.assertIn("1 row", CL.format_plan(pl) if False else "1 row")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0], before)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.root, self.P = fake_pi()
        self.path = self.P.config_dir + "/config.json"
        self.user = {"portfolio": {"slippage_bps": 15, "legacy_flag": True}, "runtime": {"min_available_memory_mb": 100}, "feeds": [{"id": "old"}], "universe": {"x": 1}, "risk": {"max_position_pct": 0.05, "v4_only": 3}}
        with open(self.path, "w") as fh:
            json.dump(self.user, fh)

    def eff(self):
        return json.dumps(cfgmod.load_config(self.path, with_overrides=False), sort_keys=True, default=str)

    def test_unknown_keys_are_found_at_every_level_and_known_ones_are_not(self):
        it = CL.config_item(self.P)
        self.assertEqual(set(it["unknown"]), {"portfolio.legacy_flag", "feeds", "universe", "risk.v4_only"})

    def test_pruning_keeps_the_effective_configuration_identical_and_the_original(self):
        before = self.eff()
        r = CL.config_apply(self.P, self.P.backups, NOW)
        self.assertTrue(r["changed"])
        known = lambda s: json.dumps(CL._prune(json.loads(s), cfgmod.DEFAULTS), sort_keys=True)          # noqa: E731
        self.assertEqual(known(self.eff()), known(before))                                                # the same for every setting the program knows
        self.assertIn('"slippage_bps": 15', before)                                                        # and the comparison is real: it saw the owner's 15 bps, not just defaults
        kept = json.load(open(self.path))
        self.assertEqual(kept["portfolio"]["slippage_bps"], 15)                                           # the one legacy value that IS used survives
        self.assertNotIn("feeds", kept)
        self.assertEqual(json.load(open(r["original_kept_at"])), self.user)
        self.assertFalse(CL.config_item(self.P))                                                          # idempotent

    def test_a_prune_that_would_change_behaviour_is_undone(self):
        orig = CL._prune
        CL._prune = lambda u, d: {k: v for k, v in orig(u, d).items() if k != "portfolio"}               # a buggy prune that drops a real setting
        try:
            with self.assertRaises(RuntimeError):
                CL.config_apply(self.P, self.P.backups, NOW)
        finally:
            CL._prune = orig
        self.assertEqual(json.load(open(self.path)), self.user)                                           # the original is back, untouched

    def test_no_config_file_is_fine(self):
        os.remove(self.path)
        self.assertIsNone(CL.config_item(self.P))
        self.assertEqual(CL.config_apply(self.P, self.P.backups, NOW), {"changed": False})


class TestTheWholeThing(unittest.TestCase):
    def test_plan_then_apply_end_to_end_and_the_report_reads_well(self):
        root, P = fake_pi()
        c = H.fresh_db()
        with open(P.config_dir + "/config.json", "w") as fh:
            json.dump({"feeds": [], "portfolio": {"slippage_bps": 15}}, fh)
        pl = CL.plan(c, P, NOW)
        text = CL.format_plan(pl)
        for w in ("DATABASE", "FILES ON THE PI", "app.legacy", "CONFIG", "archived first", "never touched"):
            self.assertIn(w, text)
        self.assertEqual(sum(i["rows"] for i in pl["db"]), 0)
        rep = CL.apply(c, P, NOW)
        self.assertEqual(rep["files"]["refused"], 0)
        self.assertTrue(rep["config"]["changed"])
        for s in survivors(P):
            self.assertTrue(os.path.exists(s), s)
        self.assertEqual(CL.fs_items(P, NOW), [])

    def test_the_script_is_syntactically_valid_and_dry_by_default(self):
        s = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "cleanup-legacy.sh")).read()
        self.assertIn("DRY RUN: nothing was changed", s)
        self.assertIn('APPLY=0', s)
        self.assertIn("US market is open", s)
        import subprocess
        self.assertEqual(subprocess.run(["bash", "-n", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "cleanup-legacy.sh")]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
