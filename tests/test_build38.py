"""Build 38 (strategy S3): backups that cannot fill the disk, a forecast of how long the disk lasts, quieter forecast rows, and the installer keeping fewer full copies."""
import datetime as dt
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import backup, db, doctor, ev, stamp  # noqa: E402


def file_db(n_rows=2000):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.db")
    c = db.open_db(path)
    c.executemany("INSERT INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES(?,?,?,?,?,?,?,?)", [("u%d" % i, "s", "t%d" % i, "x" * 400, "u", "2026-10-30T00:00:00", "2026-10-30T00:00:00", 0.5) for i in range(n_rows)])
    c.commit()
    return d, path, c


class TestBackupsCannotFillTheDisk(unittest.TestCase):
    def test_a_backup_is_compressed_verified_and_old_ones_are_pruned(self):
        d, path, c = file_db()
        dest = os.path.join(d, "bk")
        os.makedirs(dest)
        for i in range(20):
            with open(os.path.join(dest, "paper-agent-2026010%02d-000000.db.gz" % i if i < 10 else "paper-agent-202601%02d-000000.db.gz" % i), "wb") as fh:
                fh.write(b"x")
        out = backup.backup(path, dest, keep=5)
        self.assertTrue(out.endswith(".db.gz"))
        self.assertEqual(len([f for f in os.listdir(dest) if f.endswith(".db.gz")]), 5)
        with gzip.open(out) as gz:
            raw = gz.read()
        t = os.path.join(d, "restored.db")
        with open(t, "wb") as fh:
            fh.write(raw)
        self.assertEqual(sqlite3.connect(t).execute("select count(*) from news").fetchone()[0], 2000)            # the backup restores to the same data
        self.assertLess(os.path.getsize(out), os.path.getsize(path))

    def test_it_refuses_to_back_up_when_the_disk_is_short_and_leaves_nothing_behind(self):
        d, path, c = file_db()
        dest = os.path.join(d, "bk")
        os.makedirs(dest)
        short = shutil._ntuple_diskusage(10 * 2 ** 30, 10 * 2 ** 30 - 100 * 2 ** 20, 100 * 2 ** 20)              # 100 MB free
        with mock.patch.object(shutil, "disk_usage", return_value=short):
            with self.assertRaises(RuntimeError) as cm:
                backup.backup(path, dest)
        self.assertIn("backup skipped", str(cm.exception))
        self.assertEqual(os.listdir(dest), [])
        ok, why = backup.free_ok(dest, path)
        self.assertTrue(ok or "free" in why)

    def test_the_space_it_needs_scales_with_the_database(self):
        d, path, c = file_db()
        big = shutil._ntuple_diskusage(100 * 2 ** 30, 0, 100 * 2 ** 30)
        with mock.patch.object(shutil, "disk_usage", return_value=big), mock.patch.object(os.path, "getsize", return_value=800 * 2 ** 20):
            ok, why = backup.free_ok(d, path)
        self.assertTrue(ok)
        tight = shutil._ntuple_diskusage(100 * 2 ** 30, 0, 1000 * 2 ** 20)                                       # 1000 MB free, an 800 MB database needs 1600 MB
        with mock.patch.object(shutil, "disk_usage", return_value=tight), mock.patch.object(os.path, "getsize", return_value=800 * 2 ** 20):
            ok, why = backup.free_ok(d, path)
        self.assertFalse(ok)
        self.assertIn("1600 MB", why)

    def test_the_installer_keeps_three_full_copies_not_five(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "install.sh")) as fh:
            s = fh.read()
        self.assertIn("tail -n +4 | xargs -r rm -rf", s)
        self.assertNotIn("tail -n +6 | xargs -r rm -rf", s)
        self.assertEqual(backup.INSTALL_BACKUPS_KEPT, 3)


class TestTheDiskForecast(unittest.TestCase):
    def test_growth_is_measured_from_recorded_sizes_and_the_days_left_follow(self):
        d, path, c = file_db()
        db.set_param(c, "db_size_history", [["2026-10-20T03:30:00", 40 * 2 ** 20], ["2026-10-30T03:30:00", 100 * 2 ** 20]], "test")
        pr = backup.projection(c, path, [], "2026-10-30T12:00:00", free_mb=2000.0)
        self.assertAlmostEqual(pr["growth_mb_day"], 6.0, places=2)
        db_mb = os.path.getsize(path) / 1048576.0
        expect = (2000 + db_mb - 500 - 7.5 * db_mb) / (7.5 * 6.0)
        self.assertAlmostEqual(pr["days_left"], expect, places=2)
        self.assertTrue(30 < pr["days_left"] < 36)

    def test_no_forecast_until_there_are_two_days_of_measurements(self):
        d, path, c = file_db()
        self.assertIsNone(backup.projection(c, path, [], "2026-10-30T12:00:00", free_mb=5000.0)["growth_mb_day"])
        backup.record_size(c, "2026-10-30T03:30:00")
        backup.record_size(c, "2026-10-30T18:00:00")                                                              # the same day replaces, it does not add
        self.assertEqual(len(db.param(c, "db_size_history")), 1)
        self.assertIsNone(backup.projection(c, path, [], "2026-10-30T12:00:00", free_mb=5000.0)["days_left"])
        backup.record_size(c, "2026-10-31T03:30:00")
        self.assertEqual(len(db.param(c, "db_size_history")), 2)

    def test_a_shrinking_or_flat_database_never_runs_out(self):
        d, path, c = file_db()
        db.set_param(c, "db_size_history", [["2026-10-20T03:30:00", 100 * 2 ** 20], ["2026-10-30T03:30:00", 90 * 2 ** 20]], "test")
        pr = backup.projection(c, path, [], "2026-10-30T12:00:00", free_mb=100.0)
        self.assertEqual(pr["growth_mb_day"], 0.0)
        self.assertIsNone(pr["days_left"])

    def test_the_backup_folders_are_counted(self):
        d, path, c = file_db()
        bd = os.path.join(d, "bk")
        os.makedirs(os.path.join(bd, "v5-1"))
        with open(os.path.join(bd, "v5-1", "a.db"), "wb") as fh:
            fh.write(b"x" * 3 * 2 ** 20)
        self.assertAlmostEqual(backup.projection(c, path, [bd, os.path.join(d, "missing")], "2026-10-30T12:00:00", free_mb=1.0)["backups_mb"], 3.0, places=1)

    def test_the_doctor_says_how_long_the_disk_lasts_and_what_to_do(self):
        d, path, c = file_db()
        db.set_param(c, "db_size_history", [["2026-10-20T03:30:00", 1 * 2 ** 20], ["2026-10-30T03:30:00", 400 * 2 ** 20]], "test")
        lines = []
        with mock.patch.object(shutil, "disk_usage", return_value=shutil._ntuple_diskusage(10 * 2 ** 30, 0, 3 * 2 ** 30)):
            doctor.run(c, H.cfg(), d, live=False, check_units=False, out=lines.append)
        disk = [l for l in lines if l.startswith("[disk]")]
        self.assertEqual(len(disk), 1, lines)
        self.assertIn("growing 39.9 MB/day", disk[0])
        self.assertIn("the disk lasts about", disk[0])
        self.assertIn("LOWER the news / log retention", disk[0])                                                  # under 120 days left: it says what to do


class TestQuieterForecasts(TL.Base):
    def test_a_waiting_buy_rewrites_its_forecast_instead_of_adding_one_every_cycle(self):
        self.cycle(TL.T_PRE, news=True)
        did = self.one("SELECT id FROM decisions WHERE ticker='NVDA'")[0]
        for i in range(1, 11):
            self.cycle(TL.T_PRE + dt.timedelta(minutes=5 * i))
        n = self.one("SELECT COUNT(*) FROM forecasts WHERE decision_id=?", did)[0]
        self.assertLessEqual(n, 2)                                                                                # ten retries used to leave eleven rows
        self.assertEqual(self.one("SELECT forecast_id FROM decisions WHERE id=?", did)[0], self.one("SELECT MAX(id) FROM forecasts WHERE decision_id=?", did)[0])

    def test_the_plan_still_matches_the_forecast_row_after_the_open(self):
        self.open_position()
        plan = self.one("SELECT target_return,stop_return FROM position_forecast_plans WHERE active=1")
        f = self.one("SELECT target_return,stop_return FROM forecasts WHERE ticker='NVDA' ORDER BY id DESC LIMIT 1")
        self.assertAlmostEqual(plan[0], f[0], places=9)
        self.assertAlmostEqual(plan[1], f[1], places=9)

    def test_what_counts_as_the_same_forecast(self):
        prev = {"verdict": "BUY", "horizon_days": 3, "ev_net": 0.040, "target_return": 0.040, "stop_return": -0.03}
        same = {"verdict": "BUY", "horizon_days": 3, "ev_net": 0.0415, "target_return": 0.0402, "stop_return": -0.0301}
        self.assertTrue(ev.materially_same(prev, same))
        for change in ({"ev_net": 0.046}, {"verdict": "REJECT_NEGATIVE_EV"}, {"target_return": 0.045}, {"stop_return": -0.036}, {"horizon_days": 2}):
            self.assertFalse(ev.materially_same(prev, dict(same, **change)), change)
        self.assertFalse(ev.materially_same(None, same))


class TestTheVersion(unittest.TestCase):
    def test_the_version_has_moved_on_from_s3_and_the_s3_note_is_kept(self):
        self.assertIn(stamp.STRATEGY_VERSION, ("S3", "S4", "S5"))
        with open(stamp.__file__) as fh:
            self.assertIn("opinion", fh.read())


if __name__ == "__main__":
    unittest.main()
