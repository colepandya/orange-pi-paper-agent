"""Merge two KEY=VALUE env files: values already on the Pi are kept unless the repo supplies a NON-EMPTY value for the same key.
Prevents auto-updates from wiping keys entered locally (e.g. via enable-llm.sh)."""
from __future__ import annotations

import sys
from typing import Dict, List


def parse(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def merge(existing: str, repo: str) -> str:
    cur, new = parse(existing), parse(repo)
    order: List[str] = [k for k in parse(repo)] + [k for k in cur if k not in new]
    merged = dict(cur)
    for k, v in new.items():
        if v or k not in merged:          # empty repo values never erase a real local value
            merged[k] = v
    lines = ["# Managed by Paper Agent. Local values are preserved across updates; the repo only fills in or overrides with NON-empty values."]
    lines += ["%s=%s" % (k, merged[k]) for k in dict.fromkeys(order)]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":      # python3 -m paper_agent.envmerge <existing> <repo> <out>
    ex = open(sys.argv[1]).read() if len(sys.argv) > 1 and __import__("os").path.exists(sys.argv[1]) else ""
    open(sys.argv[3], "w").write(merge(ex, open(sys.argv[2]).read()))
