"""Service control without giving the dashboard root.

The unprivileged dashboard writes a tiny JSON request into the control dir. A root systemd path unit runs ``python3 -m paper_agent.control``,
which validates the request against a FIXED whitelist (unit names and integer ranges only - nothing from the request is ever passed to a
shell), performs it with ``systemctl``, and writes result.json. A compromised dashboard can therefore do no more than restart paper-agent
units or change the timer intervals within safe bounds."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from typing import Callable, Optional

from . import config

# Only light, stateless units may be restarted. update / backup / universe are JOBS: they are started on demand ("Run now"), never "restarted",
# because restarting their timers used to fire them all at once (a memory stampede on a 730 MB machine).
RESTARTABLE = ("paper-agent-dashboard.service", "paper-agent-cycle.timer", "paper-agent-guard.timer")
RESTART_ALL_ORDER = ("paper-agent-guard.timer", "paper-agent-cycle.timer", "paper-agent-dashboard.service")      # dashboard LAST: it is usually the requester
STARTABLE = ("paper-agent-cycle.service", "paper-agent-update.service", "paper-agent-backup.service", "paper-agent-universe.service")
HEAVY = {"paper-agent-update.service": 300, "paper-agent-backup.service": 150, "paper-agent-universe.service": 200, "paper-agent-cycle.service": 120}   # MB of free RAM required
DEFAULT_SCHEDULE = {"cycle_minutes": 10, "guard_minutes": 2, "update_hours": 3}                                  # what the shipped unit files say
SCHEDULE = {"cycle_minutes": (5, 240, "paper-agent-cycle.timer", "OnUnitInactiveSec", "min"),
            "guard_minutes": (1, 15, "paper-agent-guard.timer", "OnUnitInactiveSec", "min"),
            "update_hours": (1, 48, "paper-agent-update.timer", "OnUnitActiveSec", "h")}
MAX_REQUEST_BYTES = 4096


# ---------------------------------------------------------------------------------------------- client (dashboard side)
def send_request(action: str, args: Optional[dict] = None, control_dir: Optional[str] = None) -> str:
    d = control_dir or config.CONTROL_DIR
    os.makedirs(d, exist_ok=True)
    rid = uuid.uuid4().hex[:12]
    tmp = os.path.join(d, ".request.%s.tmp" % rid)
    with open(tmp, "w") as f:
        json.dump({"id": rid, "action": action, "args": args or {}, "ts": time.time()}, f)
    os.replace(tmp, os.path.join(d, "request.json"))
    return rid


def last_result(control_dir: Optional[str] = None) -> Optional[dict]:
    try:
        with open(os.path.join(control_dir or config.CONTROL_DIR, "result.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------- worker (root side)
def _read_request(path: str, owner_uid: Optional[int]) -> Optional[dict]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        import stat
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_REQUEST_BYTES or (owner_uid is not None and st.st_uid not in (owner_uid, 0)):
            return None
        data = os.read(fd, MAX_REQUEST_BYTES + 1)
    finally:
        os.close(fd)
    try:
        req = json.loads(data.decode("utf-8"))
        return req if isinstance(req, dict) else None
    except ValueError:
        return None


def _write_result(control_dir: str, result: dict) -> None:
    """Symlink-safe: remove whatever is at result.json (a symlink is removed, never followed), then create it exclusively."""
    path = os.path.join(control_dir, "result.json")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(result, f)


def mem_available_mb() -> Optional[int]:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _systemctl(run: Callable, *args: str) -> tuple:
    p = run(["systemctl"] + list(args), capture_output=True, text=True, timeout=90)
    return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()[:300]


def _dropin(dropin_root: str, unit: str) -> str:
    return os.path.join(dropin_root, unit + ".d", "schedule.conf")


def _dropin_text(directive: str, n: int, suffix: str) -> str:
    return "[Timer]\n%s=\n%s=%d%s\n" % (directive, directive, n, suffix)         # content is built ONLY from a validated integer


def applied_schedule(dropin_root: Optional[str] = None) -> dict:
    """What systemd is actually configured with (drop-in value, else the shipped default)."""
    out = dict(DEFAULT_SCHEDULE)
    dropin_root = dropin_root or config.SYSTEMD_DIR
    for key, (lo, hi, unit, directive, suffix) in SCHEDULE.items():
        try:
            with open(_dropin(dropin_root, unit)) as f:
                m = re.search(r"^%s=(\d+)%s\s*$" % (directive, suffix), f.read(), re.M)
            if m:
                out[key] = int(m.group(1))
        except OSError:
            pass
    return out


def schedule_status(cfg_schedule: dict, dropin_root: Optional[str] = None) -> dict:
    """{'cycle_minutes': {'configured': 15, 'applied': 10, 'in_sync': False}, ...} - the dashboard shows this so a setting can never silently not apply."""
    applied = applied_schedule(dropin_root)
    return {k: {"configured": int(cfg_schedule.get(k, DEFAULT_SCHEDULE[k])), "applied": applied[k], "in_sync": int(cfg_schedule.get(k, DEFAULT_SCHEDULE[k])) == applied[k]} for k in SCHEDULE}


def reconcile(cfg_schedule: dict, run: Callable = subprocess.run, dropin_root: Optional[str] = None) -> dict:
    """Make systemd match the effective configuration. Idempotent. Only timers whose interval actually changed are restarted."""
    changed = []
    dropin_root = dropin_root or config.SYSTEMD_DIR
    for key, (lo, hi, unit, directive, suffix) in SCHEDULE.items():
        try:
            n = int(cfg_schedule.get(key, DEFAULT_SCHEDULE[key]))
        except (TypeError, ValueError):
            continue
        if not lo <= n <= hi:
            continue
        path = _dropin(dropin_root, unit)
        want = None if n == DEFAULT_SCHEDULE[key] else _dropin_text(directive, n, suffix)
        have = None
        try:
            with open(path) as f:
                have = f.read()
        except OSError:
            pass
        if want == have:
            continue
        if want is None:
            os.unlink(path)                                  # back to the shipped default: no drop-in needed
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(want)
        changed.append(unit)
    if changed:
        _systemctl(run, "daemon-reload")
        for u in changed:
            _systemctl(run, "restart", "--no-block", u)
    return {"changed": changed, "ok": True}


def execute(req: dict, run: Callable = subprocess.run, dropin_root: Optional[str] = None, mem_mb: Callable = mem_available_mb) -> dict:
    dropin_root = dropin_root or config.SYSTEMD_DIR
    action, args = req.get("action"), req.get("args") or {}
    out = {"id": str(req.get("id", ""))[:24], "action": str(action)[:30], "ok": False, "output": "", "finished": time.time()}
    if not isinstance(args, dict):
        out["output"] = "bad args"
        return out
    if action in ("restart", "start"):
        unit = args.get("unit")
        allowed = RESTARTABLE if action == "restart" else STARTABLE
        if unit not in allowed:
            out["output"] = "unit not allowed"
            return out
        need, have = HEAVY.get(unit, 0), mem_mb()
        if action == "start" and need and have is not None and have < need:
            out["output"] = "refused: only %d MB RAM free (%s needs %d MB). Try again in a few minutes." % (have, unit, need)
            return out
        out["ok"], out["output"] = _systemctl(run, action if action == "restart" else "start", "--no-block", unit)
    elif action == "restart_all":
        res = []
        for u in RESTART_ALL_ORDER:                          # light units only, in a safe order; jobs (update/backup/universe) are never touched
            res.append(_systemctl(run, "restart", "--no-block", u)[0])
        out["ok"], out["output"] = all(res), "restarted %d/%d units (guard, cycle, dashboard)" % (sum(res), len(res))
    elif action == "set_schedule":
        wanted = {}
        for key, (lo, hi, unit, directive, suffix) in SCHEDULE.items():
            if key not in args:
                continue
            try:
                n = int(args[key])
            except (TypeError, ValueError):
                out["output"] = "bad value for %s" % key
                return out
            if not lo <= n <= hi:
                out["output"] = "%s out of range %d-%d" % (key, lo, hi)
                return out
            wanted[key] = n
        if not wanted:
            out["output"] = "nothing to apply"
            return out
        r = reconcile({**applied_schedule(dropin_root), **wanted}, run, dropin_root)       # validated ints only; only changed timers restart
        out["ok"], out["output"] = r["ok"], ("schedule applied to %d timer(s)" % len(r["changed"])) if r["changed"] else "schedule already in effect"
    else:
        out["output"] = "unknown action"
    return out


def run_once(control_dir: Optional[str] = None, run: Callable = subprocess.run, dropin_root: Optional[str] = None, owner_uid: Optional[int] = None) -> Optional[dict]:
    d = control_dir or config.CONTROL_DIR
    path = os.path.join(d, "request.json")
    req = _read_request(path, owner_uid)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return None
    if req is None:
        res = {"id": "", "action": "?", "ok": False, "output": "invalid request", "finished": time.time()}
    else:
        res = execute(req, run, dropin_root)
    _write_result(d, res)
    return res


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "reconcile":              # run as root by the installer: make systemd match the saved settings
        print(json.dumps(reconcile(config.load_config().get("schedule", {}))))
        sys.exit(0)
    try:
        import pwd
        uid = pwd.getpwnam("paperagent").pw_uid
    except (ImportError, KeyError):
        uid = None
    r = run_once(owner_uid=uid)
    print(json.dumps(r))
    sys.exit(0)
