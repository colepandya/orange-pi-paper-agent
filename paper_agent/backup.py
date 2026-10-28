"""Online SQLite backup (safe while the agent runs) with retention."""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import time


def backup(db_path: str, dest_dir: str, keep: int = 14) -> str:
    os.makedirs(dest_dir, exist_ok=True)
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
