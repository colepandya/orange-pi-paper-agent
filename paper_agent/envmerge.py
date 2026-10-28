"""Merge two KEY=VALUE env files without ever clobbering what is already on the Pi.

* A key that is missing/empty locally is FILLED from the repo file.
* A key that already has a value locally is KEPT, even if the repo has a different one.
* The only way a release overrides a local value is explicit and one-time: the repo file says
      FORCE_KEYS=KEY1,KEY2
      FORCE_ID=some-unique-id
  and that FORCE_ID has not been applied on this machine before (recorded in the applied-ids file). Re-deploying the same release
  never re-forces, so a fix you make afterwards is safe.
(Keys entered in the dashboard live in a separate file and always win over this one at runtime.)"""
from __future__ import annotations

import sys
from typing import Dict, List, Optional, Set

CONTROL = ("FORCE_KEYS", "FORCE_ID")


def parse(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def merge(existing: str, repo: str, applied: Optional[Set[str]] = None) -> str:
    applied = applied if applied is not None else set()
    cur, new = parse(existing), parse(repo)
    force_id = new.get("FORCE_ID", "")
    force_keys = {k.strip() for k in new.get("FORCE_KEYS", "").split(",") if k.strip()}
    forcing = bool(force_id and force_keys and force_id not in applied)
    order: List[str] = [k for k in new if k not in CONTROL] + [k for k in cur if k not in new]
    merged = dict(cur)
    forced_any = False
    for k, v in new.items():
        if k in CONTROL:
            continue
        if k not in merged or not merged[k]:
            merged[k] = v                                   # fill what is missing
        elif forcing and k in force_keys and v:
            merged[k] = v                                   # explicit, one-time release override
            forced_any = True
    if forcing:
        applied.add(force_id)                               # decided once, whether or not any key actually changed
    lines = ["# Managed by Paper Agent. Values already here are preserved across updates; a release only fills gaps (or forces a key once, explicitly)."]
    lines += ["%s=%s" % (k, merged[k]) for k in dict.fromkeys(order)]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":      # python3 envmerge.py <existing> <repo> <out> [applied-ids-file]
    import os
    ex = ""
    if os.path.exists(sys.argv[1]):
        with open(sys.argv[1]) as fh:
            ex = fh.read()
    with open(sys.argv[2]) as fh:
        repo = fh.read()
    ids_path = sys.argv[4] if len(sys.argv) > 4 else ""
    ids: Set[str] = set()
    if ids_path and os.path.exists(ids_path):
        with open(ids_path) as fh:
            ids = {line.strip() for line in fh if line.strip()}
    before = set(ids)
    out = merge(ex, repo, ids)
    with open(sys.argv[3], "w") as fh:
        fh.write(out)
    if ids_path and ids != before:
        with open(ids_path, "w") as fh:
            fh.write("\n".join(sorted(ids)) + "\n")
