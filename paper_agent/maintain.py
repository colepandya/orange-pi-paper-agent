"""Database maintenance: archive what the current version no longer uses, expire what only ever grows, keep the file lean.

Principles (it is a paper bot, but the history is still the evidence):
  * the PLAN is read-only and is the default; APPLY takes a verified full backup first and refuses to run during the US session;
  * legacy data (tables the current code does not define, and the old v4 decisions) is COPIED to an archive file and row-counted BEFORE anything is dropped;
  * a table the current code still mentions is never dropped; a row that something live points at is never deleted;
  * retention only touches data that only grows (news / events / run logs): never positions, ledger, decisions of the current app, forecasts, evidence or portfolio snapshots (the drawdown peak lives there)."""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import sqlite3
from typing import Dict, List, Optional, Tuple

from . import db
from .timeutil import iso, market_state, utcnow

# a decision written by the OLD app has none of the columns the current app fills in. Checked against a real database: matches the v4 -> v5 cutover exactly (0 wrong, 0 missed).
LEGACY_DECISIONS = "origin IS NULL AND event_id IS NULL AND forecast_id IS NULL"
TRIM_REPORT_DAYS = 14                                   # successful cycles older than this keep a compact report (errors are always kept in full)


class MaintenanceError(Exception):
    pass


def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _cols(conn, t: str) -> List[str]:
    return [r[1] for r in conn.execute('PRAGMA table_info("%s")' % t)]


def _code_sources() -> Dict[str, str]:
    root = os.path.dirname(os.path.abspath(__file__))
    out = {}
    for f in glob.glob(os.path.join(root, "*.py")):
        if os.path.basename(f) in ("db.py", "maintain.py"):
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            out[os.path.basename(f)] = fh.read()
    return out


def _bytes(conn, t: str) -> Optional[int]:
    try:
        return conn.execute("SELECT SUM(pgsize) FROM dbstat WHERE name=?", (t,)).fetchone()[0]
    except sqlite3.Error:
        return None


def legacy_tables(conn) -> Tuple[List[str], List[Tuple[str, List[str]]]]:
    """-> (tables safe to archive and drop, [(table, code files that still mention it)] left alone)."""
    exp, src = set(db.expected_schema()), _code_sources()
    safe, kept = [], []
    for t in sorted(_tables(conn) - exp):
        refs = sorted(f for f, s in src.items() if re.search(r"\b%s\b" % re.escape(t), s))
        (kept if refs else safe).append((t, refs) if refs else t)
    return safe, kept


def _event_free_clause(conn) -> str:
    tabs, parts = _tables(conn), []
    for t in ("decisions", "candidate_queue", "evidence", "forecasts", "intel_events"):
        if t in tabs and "event_id" in _cols(conn, t):
            parts.append("id NOT IN (SELECT event_id FROM %s WHERE event_id IS NOT NULL)" % t)
    return " AND ".join(parts) or "1=1"


def _retention(conn, news_days: int, log_days: int, now) -> Dict[str, Tuple[str, str, tuple]]:
    """name -> (count SQL, delete SQL, params). Only data that merely accumulates; nothing a live row points at."""
    tabs, out = _tables(conn), {}
    nc, lc = iso(now - dt.timedelta(days=news_days)), iso(now - dt.timedelta(days=log_days))
    if "events" in tabs:
        w = "created<? AND %s" % _event_free_clause(conn)
        out["events"] = ("SELECT COUNT(*) FROM events WHERE " + w, "DELETE FROM events WHERE " + w, (nc,))
    if "news" in tabs and "events" in tabs and "ingested" in _cols(conn, "news"):
        extra = " AND uid NOT IN (SELECT news_uid FROM decisions WHERE news_uid IS NOT NULL)" if "decisions" in tabs and "news_uid" in _cols(conn, "decisions") else ""
        # "no SURVIVING event needs it": the same rule counts (plan) and deletes (apply), whatever order they run in
        w = "ingested<? AND uid NOT IN (SELECT news_uid FROM events WHERE news_uid IS NOT NULL AND NOT (created<? AND %s))%s" % (_event_free_clause(conn), extra)
        out["news"] = ("SELECT COUNT(*) FROM news WHERE " + w, "DELETE FROM news WHERE " + w, (nc, nc))
    if "narrative_states" in tabs:
        w = "ts<? AND id NOT IN (SELECT MAX(id) FROM narrative_states GROUP BY thesis_key)"          # the latest state per thesis is always kept
        out["narrative_states"] = ("SELECT COUNT(*) FROM narrative_states WHERE " + w, "DELETE FROM narrative_states WHERE " + w, (lc,))
    for t in ("executor_runs", "position_monitor_runs"):
        if t in tabs:
            out[t] = ("SELECT COUNT(*) FROM %s WHERE created<?" % t, "DELETE FROM %s WHERE created<?" % t, (lc,))
    return out


def _reports_to_trim(conn, now) -> List[Tuple[int, str]]:
    if "cycle_runs" not in _tables(conn):
        return []
    cut = iso(now - dt.timedelta(days=TRIM_REPORT_DAYS))
    return [(r[0], r[1]) for r in conn.execute("SELECT id,report_json FROM cycle_runs WHERE started<? AND status IN ('OK','SKIPPED') AND length(report_json)>300", (cut,))]


def plan(conn, cfg: dict, now=None, db_path: Optional[str] = None) -> dict:
    now = now or utcnow()
    m = cfg.get("maintenance") or {}
    safe, kept = legacy_tables(conn)
    pl: Dict = {"now": iso(now), "file_mb": (os.path.getsize(db_path) / 1048576.0) if db_path and os.path.exists(db_path) else None,
                "legacy_tables": [], "kept_tables": [{"name": t, "code": refs} for t, refs in kept], "legacy_decisions": {"rows": 0, "mb": 0.0},
                "retention": {}, "news_days": int(m.get("news_days", 30)), "log_days": int(m.get("log_days", 30)), "trim_reports": 0}
    for t in safe:
        b = _bytes(conn, t)
        pl["legacy_tables"].append({"name": t, "rows": conn.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0], "mb": (b or 0) / 1048576.0})
    if "decisions" in _tables(conn) and {"origin", "event_id", "forecast_id"} <= set(_cols(conn, "decisions")):
        r = conn.execute("SELECT COUNT(*),COALESCE(SUM(LENGTH(reason)),0) FROM decisions WHERE " + LEGACY_DECISIONS).fetchone()
        pl["legacy_decisions"] = {"rows": r[0], "mb": r[1] / 1048576.0}
    for name, (csql, _dsql, params) in _retention(conn, pl["news_days"], pl["log_days"], now).items():
        pl["retention"][name] = conn.execute(csql, params).fetchone()[0]
    pl["trim_reports"] = len(_reports_to_trim(conn, now))
    pl["nothing_to_do"] = not (pl["legacy_tables"] or pl["legacy_decisions"]["rows"] or any(pl["retention"].values()) or pl["trim_reports"])
    return pl


def format_plan(pl: dict) -> str:
    L = ["=== DATABASE CLEAN-UP PLAN (nothing has been changed) ==="]
    if pl["file_mb"] is not None:
        L.append("database file: %.1f MB" % pl["file_mb"])
    lt = pl["legacy_tables"]
    L.append("")
    L.append("1. ARCHIVE then remove %d legacy table(s) the current version does not use (%d rows, %.1f MB):" % (len(lt), sum(t["rows"] for t in lt), sum(t["mb"] for t in lt)))
    for t in sorted(lt, key=lambda t: -t["mb"])[:12]:
        L.append("     %-28s %7d rows  %5.1f MB" % (t["name"], t["rows"], t["mb"]))
    if len(lt) > 12:
        L.append("     ... and %d more (all tiny)" % (len(lt) - 12))
    for k in pl["kept_tables"]:
        L.append("     KEPT (the current code still mentions it): %s in %s" % (k["name"], ", ".join(k["code"])))
    ld = pl["legacy_decisions"]
    L.append("2. ARCHIVE then remove %d decisions written by the OLD v4 app (%.1f MB of detail). The current app's own decisions are untouched." % (ld["rows"], ld["mb"]))
    L.append("3. RETENTION (only data that merely accumulates; anything a live decision / forecast / evidence row points at is kept):")
    L.append("     events older than %d days: %d | news older than %d days: %d" % (pl["news_days"], pl["retention"].get("events", 0), pl["news_days"], pl["retention"].get("news", 0)))
    L.append("     narrative states / executor runs / monitor runs older than %d days: %d / %d / %d" % (pl["log_days"], pl["retention"].get("narrative_states", 0), pl["retention"].get("executor_runs", 0), pl["retention"].get("position_monitor_runs", 0)))
    L.append("     successful-cycle reports older than %d days shrunk to a summary (errors are always kept in full): %d" % (TRIM_REPORT_DAYS, pl["trim_reports"]))
    L.append("4. ANALYZE + VACUUM to give the space back.")
    L.append("")
    L.append("Nothing to do." if pl["nothing_to_do"] else "Safety: a verified backup is taken first; legacy data is copied to an archive file and row-counted before anything is dropped; it refuses to run while the US market is open.")
    return "\n".join(L)


def _backup(conn, backup_dir: str, now) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, "pre-maintain-%s.db" % now.strftime("%Y%m%dT%H%M%SZ"))
    dst = sqlite3.connect(path)
    try:
        conn.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
    if ok != "ok":
        os.unlink(path)
        raise MaintenanceError("the backup failed its integrity check (%s): nothing was changed" % ok)
    return path


def _archive(conn, safe: List[str], do_decisions: bool, archive_dir: str, now) -> Tuple[Optional[str], Dict[str, int]]:
    """Copy legacy data into <archive_dir>/legacy-v4-<ts>.db and VERIFY every row count. Raises (and removes the partial file) on any mismatch."""
    os.makedirs(archive_dir, exist_ok=True)
    path = os.path.join(archive_dir, "legacy-v4-%s.db" % now.strftime("%Y%m%dT%H%M%SZ"))
    copied: Dict[str, int] = {}
    conn.execute("ATTACH DATABASE ? AS arch", (path,))
    try:
        conn.execute("CREATE TABLE arch._schema(name TEXT,kind TEXT,sql TEXT,rows INTEGER)")
        jobs = [(t, 'SELECT * FROM main."%s"' % t) for t in safe]
        if do_decisions:
            jobs.append(("legacy_decisions", "SELECT * FROM main.decisions WHERE " + LEGACY_DECISIONS))
        for name, sel in jobs:
            conn.execute('CREATE TABLE arch."%s" AS %s' % (name, sel))
            want = conn.execute("SELECT COUNT(*) FROM (%s)" % sel).fetchone()[0]
            got = conn.execute('SELECT COUNT(*) FROM arch."%s"' % name).fetchone()[0]
            if want != got:
                raise MaintenanceError("archive verification failed for %s (%d rows expected, %d copied): nothing was dropped" % (name, want, got))
            src_sql = conn.execute("SELECT sql FROM main.sqlite_master WHERE type='table' AND name=?", (name if name != "legacy_decisions" else "decisions",)).fetchone()
            conn.execute("INSERT INTO arch._schema VALUES(?,?,?,?)", (name, "table", src_sql[0] if src_sql else "", got))
            copied[name] = got
    except Exception:
        try:
            conn.execute("DETACH DATABASE arch")
        except sqlite3.Error:
            pass
        if os.path.exists(path):
            os.unlink(path)
        raise
    conn.execute("DETACH DATABASE arch")
    # re-open the finished archive read-only and count again: the data on DISK is what counts
    try:
        a = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            for name, n in copied.items():
                if a.execute('SELECT COUNT(*) FROM "%s"' % name).fetchone()[0] != n:
                    raise MaintenanceError("the archive file does not hold what was copied for %s: nothing was dropped" % name)
        finally:
            a.close()
    except Exception:
        if os.path.exists(path):
            os.unlink(path)                                       # never leave an archive we could not verify lying around as if it were good
        raise
    return path, copied


def apply(conn, cfg: dict, db_path: str, now=None, backup_dir: str = "/var/backups/paper-agent", archive_dir: Optional[str] = None, do_archive: bool = True, do_retention: bool = True,
          vacuum: bool = True, force: bool = False, news_days: Optional[int] = None, log_days: Optional[int] = None, log=lambda s: None) -> dict:
    now = now or utcnow()
    if market_state(now)["execution"] and not force:
        raise MaintenanceError("the US market is open: maintenance waits for the evening (override with --force)")
    cfg = dict(cfg)
    cfg["maintenance"] = dict(cfg.get("maintenance") or {})
    if news_days is not None:
        cfg["maintenance"]["news_days"] = news_days
    if log_days is not None:
        cfg["maintenance"]["log_days"] = log_days
    archive_dir = archive_dir or os.path.join(os.path.dirname(os.path.abspath(db_path)), "archive")
    pl = plan(conn, cfg, now, db_path)
    rep: Dict = {"started": iso(now), "before_mb": pl["file_mb"], "dropped_tables": [], "archived": {}, "deleted": {}, "trimmed_reports": 0, "backup": None, "archive": None, "vacuum": "skipped", "integrity": None}
    if pl["nothing_to_do"]:
        rep["after_mb"] = pl["file_mb"]
        return rep
    conn.execute("PRAGMA busy_timeout=60000")
    rep["backup"] = _backup(conn, backup_dir, now)
    log("backup written and verified: %s" % rep["backup"])
    safe = [t["name"] for t in pl["legacy_tables"]] if do_archive else []
    legacy_dec = pl["legacy_decisions"]["rows"] if do_archive else 0
    if safe or legacy_dec:
        rep["archive"], rep["archived"] = _archive(conn, safe, bool(legacy_dec), archive_dir, now)
        log("archived %d table(s) / %d old decisions to %s (verified)" % (len(safe), legacy_dec, rep["archive"]))
        with db.tx(conn):
            for t in safe:
                conn.execute('DROP TABLE main."%s"' % t)
                rep["dropped_tables"].append(t)
            if legacy_dec:
                rep["deleted"]["legacy_decisions"] = conn.execute("DELETE FROM decisions WHERE " + LEGACY_DECISIONS).rowcount
    if do_retention:
        for name, (_c, dsql, params) in _retention(conn, pl["news_days"], pl["log_days"], now).items():
            with db.tx(conn):
                rep["deleted"][name] = conn.execute(dsql, params).rowcount
        trim = _reports_to_trim(conn, now)
        with db.tx(conn):
            for rid, raw in trim:
                try:
                    j = json.loads(raw or "{}")
                except ValueError:
                    continue
                compact = {k: j[k] for k in ("errors", "skipped", "current", "started", "regime") if k in j}
                compact["trimmed"] = True
                conn.execute("UPDATE cycle_runs SET report_json=? WHERE id=?", (json.dumps(compact), rid))
                rep["trimmed_reports"] += 1
    try:
        conn.execute("ANALYZE")
        if vacuum:
            conn.execute("VACUUM")
            rep["vacuum"] = "done"
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError as e:
        rep["vacuum"] = "skipped (%s): the next weekly run will retry" % e
    rep["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
    rep["after_mb"] = (os.path.getsize(db_path) / 1048576.0) if os.path.exists(db_path) else None
    conn.execute("INSERT OR REPLACE INTO meta VALUES('last_maintenance',?)", (iso(now),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES('last_maintenance_report',?)", (json.dumps({k: rep[k] for k in ("before_mb", "after_mb", "dropped_tables", "deleted", "trimmed_reports", "vacuum", "integrity")}, default=str),))
    if rep["integrity"] != "ok":
        raise MaintenanceError("INTEGRITY CHECK FAILED after maintenance (%s). Restore the backup: %s" % (rep["integrity"], rep["backup"]))
    return rep


def format_report(rep: dict) -> str:
    L = ["=== DATABASE CLEAN-UP DONE ==="]
    if rep.get("backup"):
        L.append("backup (restore from this if anything looks wrong): %s" % rep["backup"])
    if rep.get("archive"):
        L.append("legacy data archived to: %s   (%s)" % (rep["archive"], ", ".join("%s=%d" % kv for kv in sorted(rep["archived"].items())[:6]) + (" ..." if len(rep["archived"]) > 6 else "")))
    L.append("dropped %d legacy table(s); deleted: %s; report summaries trimmed: %d" % (len(rep["dropped_tables"]), ", ".join("%s=%d" % kv for kv in rep["deleted"].items()) or "nothing", rep["trimmed_reports"]))
    L.append("vacuum: %s | integrity: %s | size: %s -> %s MB" % (rep["vacuum"], rep["integrity"], "%.1f" % rep["before_mb"] if rep["before_mb"] is not None else "?", "%.1f" % rep["after_mb"] if rep.get("after_mb") is not None else "?"))
    if not rep.get("backup"):
        L.append("Nothing to do.")
    return "\n".join(L)


def summary(conn, db_path: Optional[str] = None) -> dict:
    """Cheap facts for the dashboard / doctor."""
    safe, _kept = legacy_tables(conn)
    ld = conn.execute("SELECT COUNT(*) FROM decisions WHERE " + LEGACY_DECISIONS).fetchone()[0] if "decisions" in _tables(conn) and {"origin", "event_id", "forecast_id"} <= set(_cols(conn, "decisions")) else 0
    last = conn.execute("SELECT value FROM meta WHERE key='last_maintenance'").fetchone() if "meta" in _tables(conn) else None
    return {"file_mb": (os.path.getsize(db_path) / 1048576.0) if db_path and os.path.exists(db_path) else None, "legacy_tables": len(safe), "legacy_decisions": ld, "last": last[0] if last else None}
