"""Which strategy is in force, and since when.

A trial only measures something if the strategy stays the same while it runs. This module records, in `model_params`, every moment the decision-affecting
settings or the strategy version change (with exactly which settings moved), so any result can later be attributed to the strategy that produced it.
Operational settings (cycle times, memory limits, feed switches, spend caps...) are deliberately not part of the strategy and never create an entry.
STRATEGY_VERSION is bumped by hand whenever decision LOGIC changes (new gate, new signal, changed maths), together with a re-recorded tools/golden.py baseline.
"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Optional

from . import db
from .timeutil import iso, utcnow

STRATEGY_VERSION = "S5"                    # S1 = builds 25-36: news + thesis ideas, swing exits, article reader, EV-based sizing. S2 = build 37: a shock hold needs a headline that names the shock, from distinct publishers. S3 = build 38: an article is only "opinion" on strong evidence (a Zacks "Rank" or a stray word no longer cancels a trade); FFO is read as REIT earnings; "a quarter ago" is history. S4 = build 39: bearish ideas can be SHORTED and strong short-horizon ideas bought as CALLS / PUTS (model-priced); the edge is what the exit plan can collect (not the old, larger claim); a relationship graph spreads events to suppliers / customers / competitors / peers; correlated holdings are one bet; entries wait 10 minutes after the open. S5 = build 40: options use REAL contracts and quotes from four free feeds (liquidity, spread and IV-rank gates; priced at the live stock price with the market implied volatility); the model is only a labelled fallback
HISTORY_KEY = "strategy_history"
SNAPSHOT_KEY = "strategy_snapshot"
MAX_HISTORY = 200
NOT_STRATEGY_SECTIONS = {"runtime", "schedule", "maintenance", "notifications", "prices", "source_overrides", "whatif"}      # whatif = measurement only
NOT_STRATEGY_KEYS = {
    "quotes": {"probe_minutes"},
    "intelligence": {"company_refresh_hours", "contradiction_hours", "regime_refresh_hours", "finnhub_company_news_max", "max_company_refresh_per_cycle", "max_knowledge_refresh_per_cycle",
                     "llm_daily_budget_usd", "llm_monthly_budget_usd", "llm_daily_max_calls", "llm_max_per_cycle", "llm_prices", "llm_providers", "llm_tiers"},
}


def decision_settings(cfg: dict) -> Dict[str, object]:
    """Flat {path: value} of every setting that can change what the bot decides."""
    out: Dict[str, object] = {}
    for section, body in (cfg or {}).items():
        if section in NOT_STRATEGY_SECTIONS:
            continue
        if not isinstance(body, dict):
            out[section] = body
            continue
        for k, v in body.items():
            if k not in NOT_STRATEGY_KEYS.get(section, ()):
                out["%s.%s" % (section, k)] = v
    return out


def fingerprint(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(decision_settings(cfg), sort_keys=True, default=str).encode()).hexdigest()[:12]


def history(conn) -> List[dict]:
    h = db.param(conn, HISTORY_KEY)
    return h if isinstance(h, list) else []


def current(conn) -> Optional[dict]:
    h = history(conn)
    return h[-1] if h else None


def version_at(conn, when_iso: str) -> Optional[dict]:
    """The strategy entry in force at a given time (ISO string); None if it predates the first record."""
    found = None
    for e in history(conn):
        if e["at"] <= when_iso:
            found = e
    return found


def record(conn, cfg: dict, now=None, note: str = "") -> dict:
    """Append an entry if the strategy version or any decision-affecting setting changed (or a note is given). Cheap: called every cycle."""
    now = now or utcnow()
    fp, flat = fingerprint(cfg), decision_settings(cfg)
    h = history(conn)
    last = h[-1] if h else None
    if last and last.get("version") == STRATEGY_VERSION and last.get("fingerprint") == fp and not note:
        return {"version": STRATEGY_VERSION, "fingerprint": fp, "changed": False}
    prev = db.param(conn, SNAPSHOT_KEY)
    moved = {} if not isinstance(prev, dict) else {k: [prev.get(k), flat.get(k)] for k in sorted(set(prev) | set(flat)) if prev.get(k) != flat.get(k)}
    entry = {"at": iso(now), "version": STRATEGY_VERSION, "fingerprint": fp, "build": db.package_build(), "learning_mode": (cfg.get("intelligence") or {}).get("learning_mode", "observe"),
             "note": note or ("first record" if not last else ("strategy version changed" if last.get("version") != STRATEGY_VERSION else "decision settings changed")), "moved": dict(list(moved.items())[:40])}
    h.append(entry)
    db.set_param(conn, HISTORY_KEY, h[-MAX_HISTORY:], "strategy history: what was in force, and when")
    db.set_param(conn, SNAPSHOT_KEY, flat, "decision-affecting settings at the last strategy record")
    return {"version": STRATEGY_VERSION, "fingerprint": fp, "changed": True}
