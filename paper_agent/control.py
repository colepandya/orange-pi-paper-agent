"""Service control without giving the dashboard root.

The unprivileged dashboard writes a tiny JSON request into the control dir. A root systemd path unit runs ``python3 -m paper_agent.control``,
which validates the request against a FIXED whitelist (unit names and integer ranges only - nothing from the request is ever passed to a
shell), performs it with ``systemctl``, and writes result.json. A compromised dashboard can therefore do no more than restart paper-agent
units or change the timer intervals within safe bounds."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from typing import Callable, Optional

from . import config

RESTARTABLE = ("paper-agent-dashboard.service", "paper-agent-cycle.timer", "paper-agent-guard.timer", "paper-agent-update.timer", "paper-agent-backup.timer", "paper-agent-universe.timer")
STARTABLE = ("paper-agent-cycle.service", "paper-agent-update.service", "paper-agent-backup.service", "paper-agent-universe.service")
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


def _systemctl(run: Callable, *args: str) -> tuple:
    p = run(["systemctl"] + list(args), capture_output=True, text=True, timeout=90)
    return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()[:300]


def execute(req: dict, run: Callable = subprocess.run, dropin_root: str = "/etc/systemd/system") -> dict:
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
        out["ok"], out["output"] = _systemctl(run, action if action == "restart" else "start", "--no-block", unit)
    elif action == "restart_all":
        res = [_systemctl(run, "restart", "--no-block", u)[0] for u in RESTARTABLE]
        out["ok"], out["output"] = all(res), "restarted %d/%d units" % (sum(res), len(res))
    elif action == "set_schedule":
        written = []
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
            d = os.path.join(dropin_root, unit + ".d")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "schedule.conf"), "w") as f:        # content is built ONLY from a validated integer
                f.write("[Timer]\n%s=\n%s=%d%s\n" % (directive, directive, n, suffix))
            written.append(unit)
        if not written:
            out["output"] = "nothing to apply"
            return out
        ok1, _ = _systemctl(run, "daemon-reload")
        oks = [_systemctl(run, "restart", "--no-block", u)[0] for u in written]
        out["ok"], out["output"] = ok1 and all(oks), "schedule applied to %d timer(s)" % len(written)
    else:
        out["output"] = "unknown action"
    return out


def run_once(control_dir: Optional[str] = None, run: Callable = subprocess.run, dropin_root: str = "/etc/systemd/system", owner_uid: Optional[int] = None) -> Optional[dict]:
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
    try:
        import pwd
        uid = pwd.getpwnam("paperagent").pw_uid
    except (ImportError, KeyError):
        uid = None
    r = run_once(owner_uid=uid)
    print(json.dumps(r))
    sys.exit(0)
