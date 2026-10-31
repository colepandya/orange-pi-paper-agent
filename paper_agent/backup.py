"""Online SQLite backup (safe while the agent runs) with retention, a disk guard, and a forecast of how long the disk will last."""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import time
from typing import List, Optional

MIN_FREE_MB = 500.0
INSTALL_BACKUPS_KEPT = 3                                      # scripts/install.sh keeps this many full (uncompressed) copies


def free_ok(dest_dir: str, db_path: str) -> tuple:
    """Is there room for a backup? Needs the database's size twice over (the copy, then its compressed form) and at least MIN_FREE_MB spare."""
    os.makedirs(dest_dir, exist_ok=True)
    free_mb = shutil.disk_usage(dest_dir).free / 1048576.0
    need = max(MIN_FREE_MB, 2.0 * os.path.getsize(db_path) / 1048576.0)
    return free_mb >= need, "only %.0f MB free on the disk, a backup needs %.0f MB" % (free_mb, need)


def backup(db_path: str, dest_dir: str, keep: int = 14) -> str:
    ok, why = free_ok(dest_dir, db_path)
    if not ok:
        raise RuntimeError("backup skipped: " + why)           # better no backup than a full disk that stops the agent and the Pi
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tmp = os.path.join(dest_dir, "paper-agent-%s.db" % stamp)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(tmp)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    out = tmp + ".gz"
    with open(tmp, "rb") as a, gzip.open(out, "wb") as b:
        shutil.copyfileobj(a, b)
    os.remove(tmp)
    files = sorted(f for f in os.listdir(dest_dir) if f.startswith("paper-agent-") and f.endswith(".db.gz"))
    for f in files[:-keep]:
        os.remove(os.path.join(dest_dir, f))
    return out


def record_size(conn, now_iso: str) -> None:
    """Remember the database size once a day (called after each daily backup) so growth can be measured, not guessed."""
    from . import db
    h = db.param(conn, "db_size_history")
    h = h if isinstance(h, list) else []
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    if h and h[-1][0][:10] == now_iso[:10]:
        h.pop()
    h.append([now_iso, os.path.getsize(path)])
    db.set_param(conn, "db_size_history", h[-60:], "database size by day, for the disk forecast")


def _dir_mb(path: str) -> float:
    total = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1048576.0


def projection(conn, db_path: str, backup_dirs: List[str], now_iso: str, install_backups: int = INSTALL_BACKUPS_KEPT, free_mb: Optional[float] = None) -> dict:
    """How big the database is, how fast it grows, what the backups cost, and how many days of disk are left. The space the database and its backups can ever need is about 7.5x the database
    (itself + 14 daily backups compressed to a quarter + the installer's full copies), so the forecast asks when that outgrows what the disk can give them."""
    from . import db
    db_mb = os.path.getsize(db_path) / 1048576.0
    backups_mb = sum(_dir_mb(d) for d in backup_dirs if os.path.isdir(d))
    free = free_mb if free_mb is not None else shutil.disk_usage(os.path.dirname(db_path)).free / 1048576.0
    h = db.param(conn, "db_size_history")
    growth = None
    if isinstance(h, list) and len(h) >= 2:
        days = (time.mktime(time.strptime(h[-1][0][:10], "%Y-%m-%d")) - time.mktime(time.strptime(h[0][0][:10], "%Y-%m-%d"))) / 86400.0
        if days >= 1:
            growth = max(0.0, (h[-1][1] - h[0][1]) / 1048576.0 / days)
    factor = 1.0 + 14 / 4.0 + install_backups
    headroom = free + db_mb + backups_mb - 500.0                                   # keep half a gigabyte spare
    days_left = None
    if growth and growth > 0:
        days_left = max(0.0, (headroom - factor * db_mb) / (factor * growth))
    return {"db_mb": db_mb, "backups_mb": backups_mb, "free_mb": free, "growth_mb_day": growth, "days_left": days_left}
