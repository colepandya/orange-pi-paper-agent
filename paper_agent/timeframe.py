"""ONE timeframe vocabulary for labels, the analyst, expiry, relationships and the red team. Defined in trading SESSIONS: immediate 0-1, short 2-5, medium 6-20, long > 20, unknown. Every other dialect in the
system (days / weeks / 1-3m, hours, holding days) is converted here and nowhere else."""
from __future__ import annotations

import math
from typing import Optional

IMMEDIATE, SHORT, MEDIUM, LONG, UNKNOWN = "immediate", "short", "medium", "long", "unknown"
ALL = (IMMEDIATE, SHORT, MEDIUM, LONG, UNKNOWN)
BOUNDS = {IMMEDIATE: "0-1 sessions", SHORT: "2-5 sessions", MEDIUM: "6-20 sessions", LONG: "more than 20 sessions", UNKNOWN: "not determinable"}
LEGACY = {"immediate": IMMEDIATE, "intraday": IMMEDIATE, "1d": IMMEDIATE, "days": SHORT, "short": SHORT, "weeks": MEDIUM, "medium": MEDIUM, "1-3m": LONG, "months": LONG, "long": LONG, "quarter": LONG, "unknown": UNKNOWN, "": UNKNOWN, None: UNKNOWN}


def from_sessions(n: Optional[float]) -> str:
    if n is None:
        return UNKNOWN
    try:
        n = float(n)
    except (TypeError, ValueError):
        return UNKNOWN
    if n < 0:
        return UNKNOWN
    return IMMEDIATE if n <= 1 else SHORT if n <= 5 else MEDIUM if n <= 20 else LONG


def from_hours(h: Optional[float]) -> str:
    """Calendar hours -> sessions (about 5 sessions per 7 calendar days)."""
    try:
        return from_sessions(float(h) / 24.0 * 5.0 / 7.0)
    except (TypeError, ValueError, OverflowError):
        return UNKNOWN


def from_legacy(label) -> str:
    return LEGACY.get((str(label).lower().strip() if label is not None else None), UNKNOWN)


# The label convention used by the evaluation datasets: the expected duration of the PRICE EFFECT, fixed by what kind of event it is (written down before any engine output was compared).
GOLD_BY_FAMILY = {("results", "confirmed"): SHORT, ("contracts", "confirmed"): SHORT, ("corporate", "confirmed"): SHORT, ("corporate", "rumored"): IMMEDIATE, ("corporate", "proposed"): IMMEDIATE, ("regulatory", "confirmed"): MEDIUM,
                  ("legal", "confirmed"): MEDIUM, ("operations", "confirmed"): MEDIUM, ("capital", "confirmed"): SHORT, ("distress", "confirmed"): MEDIUM, ("people", "confirmed"): SHORT, ("analyst", "confirmed"): IMMEDIATE}


def gold_for(family: Optional[str], state: Optional[str]) -> str:
    if not family or family in ("none", "other"):
        return UNKNOWN
    return GOLD_BY_FAMILY.get((family, state or "confirmed"), GOLD_BY_FAMILY.get((family, "confirmed"), UNKNOWN))
