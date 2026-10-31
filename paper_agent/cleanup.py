"""Clean-up of everything old builds left behind that the current code no longer uses, beyond what `db-maintain` covers (legacy v4 tables and decisions, expired news).

Three scopes, each a PLAN first (nothing is changed) and then an APPLY:
  db      rows that only accumulate and nothing points at: old re-forecasts no decision/position/prediction references, old provider-call budget logs, finished queue rows, stale option caches
  files   leftovers on the Pi: the old v4 app folder inside /opt/paper-agent, stale systemd unit copies (*.before-*, *.bak, *.orig), install backups beyond the newest 3, old unpacked builds and zips in the
          login user's home, old temporary files. Anything archived-worthy is archived to /var/backups/paper-agent first.
  config  keys in /etc/paper-agent/config.json that the current program does not know (the old v4 feed lists...). The program ignores them already, so removing them changes nothing; the effective configuration
          is checked to be IDENTICAL before and after, and the original is kept.
It never touches: the database file itself, secrets and keys, the current installation, the newest backups, settings.json, or anything outside an explicit allow-list of folders."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import tarfile
from typing import Dict, List, Optional

from . import config as cfgmod, db
from .timeutil import iso, utcnow


class Paths:
    def __init__(self, app="/opt/paper-agent", units="/etc/systemd/system", backups="/var/backups/paper-agent", state="/var/lib/paper-agent", config_dir="/etc/paper-agent", home: Optional[str] = None, tmp=("/tmp", "/var/tmp")):
        self.app, self.units, self.backups, self.state, self.config_dir, self.home, self.tmp = app, units, backups, state, config_dir, home, tuple(tmp)

    def roots(self) -> List[str]:
        return [os.path.realpath(p) for p in [self.app, self.units, self.backups, self.state, self.home, *self.tmp] if p]


def _size(p: str) -> int:
    if os.path.isfile(p) or os.path.islink(p):
        try:
            return os.lstat(p).st_size
        except OSError:
            return 0
    total = 0
    for r, _d, fs in os.walk(p):
        for f in fs:
            try:
                total += os.lstat(os.path.join(r, f)).st_size
            except OSError:
                pass
    return total


def _mb(n: int) -> str:
    return "%.1f MB" % (n / 1048576.0)


# ------------------------------------------------------------------------------------------------ database
def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _forecast_refs(conn) -> List[str]:
    out = []
    for t in sorted(_tables(conn)):
        if t != "forecasts" and "forecast_id" in [r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)]:
            out.append("SELECT forecast_id FROM %s WHERE forecast_id IS NOT NULL" % t)
    return out


def db_items(conn, now) -> List[dict]:
    T = _tables(conn)
    d = lambda days: iso(now - dt.timedelta(days=days))                                                  # noqa: E731
    items = []
    if "forecasts" in T:
        refs = " UNION ".join(_forecast_refs(conn)) or "SELECT -1"
        items.append(("orphan_forecasts", "old re-forecasts that no decision, position, prediction or option points at (every 5-minute retry used to write one)", "forecasts", "created<? AND id NOT IN (%s)" % refs, (d(14),)))
    for tbl, why, where, args in (("provider_calls", "price-provider call log older than a week (the rate-limit budgets only look back a day)", "ts<?", (now.timestamp() - 7 * 86400,)),
                                  ("candidate_queue", "finished candidate-queue rows older than 30 days", "status!='PENDING' AND decided IS NOT NULL AND decided<?", (d(30),)),
                                  ("option_chain_cache", "options chains cached more than 2 days ago", "fetched<?", (d(2),)),
                                  ("option_model_checks", "model-vs-market checks older than 120 days", "created<?", (d(120),)),
                                  ("redteam_reviews", "red-team reviews older than 90 days for trades that are closed", "created<? AND (position_id IS NULL OR position_id NOT IN (SELECT id FROM positions WHERE status='OPEN'))", (d(90),))):
        if tbl in T:
            items.append((tbl, why, tbl, where, args))
    out = []
    for key, why, tbl, where, args in items:
        try:
            n = conn.execute("SELECT COUNT(*) FROM %s WHERE %s" % (tbl, where), args).fetchone()[0]
        except Exception:  # noqa: BLE001 - a table shaped differently on an old database is skipped, not fatal
            continue
        out.append({"key": key, "why": why, "table": tbl, "rows": n, "where": where, "args": args})
    return out


def db_apply(conn, items: List[dict]) -> Dict[str, int]:
    done = {}
    with db.tx(conn):
        for it in items:
            if it["rows"]:
                done[it["key"]] = conn.execute("DELETE FROM %s WHERE %s" % (it["table"], it["where"]), it["args"]).rowcount
    return done


# ------------------------------------------------------------------------------------------------ files
def fs_items(paths: Paths, now) -> List[dict]:
    items: List[dict] = []

    def add(path, why, archive=False):
        if os.path.lexists(path):
            items.append({"path": path, "why": why, "bytes": _size(path), "archive": archive})
    try:
        for n in sorted(os.listdir(paths.app)):
            if re.match(r"^app\.legacy-", n):
                add(os.path.join(paths.app, n), "the old v4 application, kept inside the new install", True)
    except OSError:
        pass
    try:
        for n in sorted(os.listdir(paths.units)):
            if n.startswith("paper-agent") and re.search(r"(\.(before-[\w.-]+|bak|orig|old|rej|dpkg-\w+)|~)$", n):
                add(os.path.join(paths.units, n), "a stale copy of a systemd unit", True)
    except OSError:
        pass
    try:
        bk = sorted((n for n in os.listdir(paths.backups) if re.match(r"^v5-", n) and os.path.isdir(os.path.join(paths.backups, n))), reverse=True)
        for n in bk[3:]:
            add(os.path.join(paths.backups, n), "an install backup older than the newest 3")
    except OSError:
        pass
    if paths.home and os.path.isdir(paths.home):
        dirs = sorted((int(m.group(1)), n) for n in os.listdir(paths.home) for m in [re.match(r"^b(\d+)$", n)] if m and os.path.isdir(os.path.join(paths.home, n)))
        zips = sorted((int(m.group(1)), n) for n in os.listdir(paths.home) for m in [re.match(r"^paper-agent-build(\d+)\.zip$", n)] if m)
        for _, n in dirs[:-1]:
            add(os.path.join(paths.home, n), "an old unpacked build (the newest is kept)")
        for _, n in zips[:-1]:
            add(os.path.join(paths.home, n), "an old build zip (the newest is kept)")
    tmpdir = os.path.join(paths.state, "tmp")
    if os.path.isdir(tmpdir):
        for n in os.listdir(tmpdir):
            p = os.path.join(tmpdir, n)
            if now.timestamp() - os.lstat(p).st_mtime > 2 * 86400:
                add(p, "a temporary file more than 2 days old")
    for root in paths.tmp:
        try:
            for n in os.listdir(root):
                if re.match(r"^(pa-review\.|paper-agent-review|pa-export\.sh|pa-ssh-|pull-from-pi\.|dbexport|paper-agent-build\d+\.zip|pa-tests-)", n):
                    p = os.path.join(root, n)
                    if now.timestamp() - os.lstat(p).st_mtime > 86400:
                        add(p, "a leftover from an export, an upload or a test run")
        except OSError:
            pass
    return items


def _inside(path: str, roots: List[str]) -> bool:
    rp = os.path.realpath(os.path.dirname(path)) + os.sep + os.path.basename(path)
    return any(rp.startswith(r + os.sep) for r in roots)


def fs_apply(items: List[dict], paths: Paths, now) -> dict:
    roots = paths.roots()
    safe = [i for i in items if _inside(i["path"], roots)]                                               # only inside the allow-listed folders (a symlink is removed itself, never followed)
    arch = [i for i in safe if i["archive"]]
    out = {"archived_to": None, "removed": 0, "freed": 0, "refused": len(items) - len(safe)}
    if arch:
        os.makedirs(paths.backups, exist_ok=True)
        tgz = os.path.join(paths.backups, "legacy-files-%s.tar.gz" % now.strftime("%Y%m%dT%H%M%SZ"))
        with tarfile.open(tgz, "w:gz") as t:
            for i in arch:
                t.add(i["path"], arcname=i["path"].lstrip("/"))
        if not tarfile.is_tarfile(tgz) or os.path.getsize(tgz) == 0:
            raise RuntimeError("the archive could not be verified; nothing was removed")
        out["archived_to"] = tgz
    for i in safe:
        try:
            if os.path.isdir(i["path"]) and not os.path.islink(i["path"]):
                shutil.rmtree(i["path"])
            else:
                os.remove(i["path"])
            out["removed"] += 1
            out["freed"] += i["bytes"]
        except OSError:
            out["refused"] += 1
    return out


# ------------------------------------------------------------------------------------------------ config.json
def unknown_keys(user: dict, defaults: dict, prefix: str = "") -> List[str]:
    out = []
    for k, v in user.items():
        if k not in defaults:
            out.append(prefix + k)
        elif isinstance(v, dict) and isinstance(defaults[k], dict):
            out += unknown_keys(v, defaults[k], prefix + k + ".")
    return out


def _prune(user: dict, defaults: dict) -> dict:
    out = {}
    for k, v in user.items():
        if k not in defaults:
            continue
        out[k] = _prune(v, defaults[k]) if isinstance(v, dict) and isinstance(defaults[k], dict) else v
    return out


def _known_view(cfg: dict, defaults: dict) -> str:
    """Everything the program KNOWS about in a merged configuration, as text. Deliberately written differently from _prune (it walks the DEFAULTS, not the user's file) so a bug in the pruner cannot hide
    itself from this check. The merged configuration keeps unknown keys inert; what must not change is everything else."""
    def walk(c, d):
        return {k: (walk((c or {}).get(k), v) if isinstance(v, dict) else (c or {}).get(k)) for k, v in d.items()}
    return json.dumps(walk(cfg, defaults), sort_keys=True, default=str)


def config_item(paths: Paths) -> Optional[dict]:
    p = os.path.join(paths.config_dir, "config.json")
    try:
        with open(p) as fh:
            user = json.load(fh)
    except (OSError, ValueError):
        return None
    bad = unknown_keys(user, cfgmod.DEFAULTS)
    return {"path": p, "unknown": bad, "bytes": len(json.dumps(user))} if bad else None


def config_apply(paths: Paths, backups: str, now) -> dict:
    it = config_item(paths)
    if not it:
        return {"changed": False}
    with open(it["path"]) as fh:
        raw = fh.read()
    user = json.loads(raw)
    before = cfgmod.load_config(it["path"], with_overrides=False)                                       # (load_config takes the FILE path)
    pruned = _prune(user, cfgmod.DEFAULTS)
    os.makedirs(backups, exist_ok=True)
    keep = os.path.join(backups, "config.json.before-cleanup-%s" % now.strftime("%Y%m%dT%H%M%SZ"))
    with open(keep, "w") as fh:
        fh.write(raw)
    st = os.stat(it["path"])
    tmp = it["path"] + ".new"
    with open(tmp, "w") as fh:
        json.dump(pruned, fh, indent=2, sort_keys=True)
    os.replace(tmp, it["path"])
    try:
        os.chmod(it["path"], st.st_mode & 0o777)
        os.chown(it["path"], st.st_uid, st.st_gid)
    except (OSError, PermissionError):
        pass
    after = cfgmod.load_config(it["path"], with_overrides=False)
    if _known_view(before, cfgmod.DEFAULTS) != _known_view(after, cfgmod.DEFAULTS):
        with open(it["path"], "w") as fh:
            fh.write(raw)                                                                                # never leave a change in force that altered the effective settings
        raise RuntimeError("pruning would have changed the effective configuration; restored the original")
    return {"changed": True, "removed_keys": it["unknown"], "original_kept_at": keep}


# ------------------------------------------------------------------------------------------------ the entry points
def plan(conn, paths: Paths, now=None, scope=("db", "files", "config")) -> dict:
    now = now or utcnow()
    return {"db": db_items(conn, now) if "db" in scope and conn is not None else [], "files": fs_items(paths, now) if "files" in scope else [], "config": config_item(paths) if "config" in scope else None}


def format_plan(pl: dict) -> str:
    L = ["=== LEFTOVER CLEAN-UP PLAN (nothing has been changed) ==="]
    n = sum(i["rows"] for i in pl["db"])
    L.append("\nDATABASE (rows nothing uses; regenerable or log-only): %d row(s)" % n)
    L += ["   %-20s %7d rows  %s" % (i["key"], i["rows"], i["why"]) for i in pl["db"]] or ["   (nothing)"]
    tot = sum(i["bytes"] for i in pl["files"])
    L.append("\nFILES ON THE PI: %d item(s), %s" % (len(pl["files"]), _mb(tot)))
    L += ["   %9s  %s%s\n             %s" % (_mb(i["bytes"]), i["path"], "   [archived first]" if i["archive"] else "", i["why"]) for i in pl["files"]] or ["   (nothing)"]
    c = pl["config"]
    L.append("\nCONFIG: " + (("%d key(s) in %s that the current program does not know (it ignores them already): %s" % (len(c["unknown"]), c["path"], ", ".join(c["unknown"][:12]))) if c else "nothing to remove"))
    L.append("\nSafety: archived items go to /var/backups/paper-agent first; the database file, secrets, settings.json, the current install and the newest backups are never touched.")
    return "\n".join(L)


def apply(conn, paths: Paths, now=None, scope=("db", "files", "config")) -> dict:
    now = now or utcnow()
    pl = plan(conn, paths, now, scope)
    rep: dict = {}
    if "db" in scope and conn is not None:
        rep["db"] = db_apply(conn, pl["db"])
    if "files" in scope:
        rep["files"] = fs_apply(pl["files"], paths, now)
    if "config" in scope:
        rep["config"] = config_apply(paths, paths.backups, now)
    return rep
