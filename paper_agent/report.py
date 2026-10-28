"""Unattended reports: what happened around the open / midday / close, written to a file (and optionally pushed to a phone) so nobody has to be awake to run checks.

A report is the pre-open checklist + the decision funnel + what actually traded, boiled down to a VERDICT line:
  OK         everything that should have happened did
  ATTENTION  something needs a look but trading is functioning (e.g. BUYs waiting on quotes, a paused bot, non-benign warnings)
  FAILED     a blocker or errors since the current build, or BUYs waiting with nothing executed well after the open"""
from __future__ import annotations

import datetime as dt
import os
from typing import Callable, Dict, List, Optional

from . import config, funnel, net, notify, portfolio as pf, preflight
from .timeutil import iso, last_market_open, market_state, next_market_open, parse_ts, to_nz, utcnow

BENIGN_WARNINGS = ("Contact email", "Fresh quote at the open")
KEEP = 80


def reports_dir() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_PATH) or ".", "reports")


def label_for(now) -> str:
    st = market_state(now)
    if not st["execution"]:
        return "preopen" if (next_market_open(now) - now).total_seconds() < 4 * 3600 else "close"      # look FORWARD to the next open
    mins = (now - last_market_open(now)).total_seconds() / 60
    return "open" if mins < 45 else "midday"


def build(conn, cfg: dict, universe, srcs: List[dict], now, live: bool = True, fetch: Callable = net.get, fetch_json: Callable = net.get_json, label: Optional[str] = None, check_units: bool = True) -> dict:
    label = label or label_for(now)
    pre_lines: List[str] = []
    pre = preflight.run(conn, cfg, universe, srcs=srcs, live=live, check_units=check_units, now=now, fetch=fetch, fetch_json=fetch_json, out=pre_lines.append)
    fr = funnel.report(conn, 6, now, samples=1)
    since = iso(last_market_open(now))
    opened = conn.execute("SELECT ticker,shares,entry_price,cost FROM positions WHERE opened>=? ORDER BY opened", (since,)).fetchall()
    exits = conn.execute("SELECT ticker,review_code,pnl FROM positions WHERE closed>=? AND status='CLOSED' ORDER BY closed", (since,)).fetchall()
    holdings = conn.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
    eq = pf.equity_of(conn, now=now)
    waiting = fr["pending"]
    by_status: Dict[str, int] = {}
    for p in waiting:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
    st = market_state(now)
    mins_open = (now - last_market_open(now)).total_seconds() / 60 if st["execution"] else None
    problems: List[str] = []
    failed: List[str] = []
    for c in pre["checks"]:
        if c["status"] == "FAIL":
            failed.append("%s: %s" % (c["name"], c["detail"][:140]))
        elif c["status"] == "WARN" and not any(c["name"].startswith(b) for b in BENIGN_WARNINGS):
            problems.append("%s: %s" % (c["name"], c["detail"][:140]))
    if fr["error_cycles_now"] and (st["execution"] or (mins_open is None and label in ("close", "open", "midday"))):
        top = fr["stage_errors"][0][0] if fr["stage_errors"] else "unknown"
        failed.append("%d cycle(s) with stage errors since this build started; top: %s" % (fr["error_cycles_now"], top[:200]))
    elif fr["error_cycles_now"]:
        problems.append("%d cycle(s) with stage errors since this build started" % fr["error_cycles_now"])
    stuck = by_status.get("AWAITING_QUOTE", 0)
    if st["execution"] and stuck:
        problems.append("%d BUY decision(s) are waiting for a verified price while the market is open. e.g. %s %s" % (stuck, next(p["ticker"] for p in waiting if p["status"] == "AWAITING_QUOTE"), next(p["waiting_for"] for p in waiting if p["status"] == "AWAITING_QUOTE")[:160]))
    if st["execution"] and mins_open is not None and mins_open > 20 and not opened and (by_status.get("AWAITING_MARKET", 0) + stuck) >= 5:
        failed.append("%d BUYs are waiting but NOTHING has executed %d minutes after the open" % (by_status.get("AWAITING_MARKET", 0) + stuck, mins_open))
    verdict = "FAILED" if failed else ("ATTENTION" if problems else "OK")
    head = "VERDICT: %s  |  %s report  |  %s" % (verdict, label, to_nz(now))
    summary = "%d position(s) opened this session, %d closed, %d held now; %d BUY decision(s) waiting; equity $%s, cash $%s" % (
        len(opened), len(exits), holdings, len(waiting), format(round(eq["equity"]), ","), format(round(eq["cash"]), ","))
    L = [head, summary, ""]
    if failed:
        L += ["WHAT FAILED:"] + ["  - " + x for x in failed] + [""]
    if problems:
        L += ["NEEDS A LOOK:"] + ["  - " + x for x in problems] + [""]
    if not failed and not problems:
        L += ["Nothing needs your attention.", ""]
    if opened:
        L += ["OPENED THIS SESSION:"] + ["  %-6s %5d sh @ %9.2f  ($%s)" % (r["ticker"], r["shares"], r["entry_price"], format(round(r["cost"]), ",")) for r in opened[:30]] + [""]
    if exits:
        L += ["CLOSED THIS SESSION:"] + ["  %-6s %-22s P&L %+10.2f" % (r["ticker"], r["review_code"] or "?", r["pnl"] or 0) for r in exits[:30]] + [""]
    if waiting:
        L += ["BUY DECISIONS STILL WAITING (%s):" % ", ".join("%s %d" % kv for kv in by_status.items())] + ["  %-6s %-16s %s" % (p["ticker"], p["status"], p["waiting_for"][:110]) for p in waiting[:8]] + [""]
    if not st["execution"]:
        L += ["Next market open: %s" % to_nz(next_market_open(now)), ""]
    L += ["----- full checklist -----"] + pre_lines + ["", "----- funnel (last 6h) -----", funnel.text(fr)]
    return {"verdict": verdict, "label": label, "text": "\n".join(L), "headline": head, "summary": summary, "failed": failed, "problems": problems}


def write(rep: dict, now) -> str:
    d = reports_dir()
    os.makedirs(d, exist_ok=True)
    name = "%s-%s.txt" % (now.strftime("%Y%m%d-%H%M"), rep["label"])
    path = os.path.join(d, name)
    for p, text in ((path, rep["text"]), (os.path.join(d, "latest.txt"), rep["text"])):
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, p)
    files = sorted(f for f in os.listdir(d) if f.endswith(".txt") and f != "latest.txt")
    for old in files[:-KEEP]:
        os.unlink(os.path.join(d, old))
    return path


def list_reports(limit: int = 12) -> List[dict]:
    d = reports_dir()
    out = []
    try:
        for f in sorted((x for x in os.listdir(d) if x.endswith(".txt") and x != "latest.txt"), reverse=True)[:limit]:
            with open(os.path.join(d, f)) as fh:
                first = fh.readline().strip()
            out.append({"file": f, "headline": first})
    except OSError:
        pass
    return out


def read(name: str = "latest.txt") -> Optional[str]:
    if "/" in name or ".." in name or not name.endswith(".txt"):
        return None
    try:
        with open(os.path.join(reports_dir(), name)) as f:
            return f.read()
    except OSError:
        return None


def push(cfg: dict, rep: dict, post: Callable = net.get) -> bool:
    prio = {"FAILED": "urgent", "ATTENTION": "high", "OK": "default"}[rep["verdict"]]
    tags = {"FAILED": "rotating_light", "ATTENTION": "warning", "OK": "white_check_mark"}[rep["verdict"]]
    body = rep["summary"] + "\n" + "\n".join("- " + x[:150] for x in (rep["failed"] + rep["problems"])[:4])
    return notify.push(cfg, "Paper Agent %s: %s" % (rep["label"], rep["verdict"]), body, prio, tags, post)
