"""Compatibility shim. The LLM implementation lives in llm_client.py (client), event_intel.py (event analysis), redteam.py (challenge)."""
from __future__ import annotations

from typing import Callable, Optional

from .llm_client import LLMClient  # noqa: F401


def validate(obj: dict, allowed: Optional[Callable] = None) -> Optional[dict]:
    """Legacy light validation used by older callers."""
    import re
    try:
        d, m, c = float(obj["direction"]), float(obj["magnitude"]), float(obj.get("confidence", 0.5))
        if not (-1 <= d <= 1 and 0 <= m <= 1 and 0 <= c <= 1):
            return None
        tick = [str(t).upper() for t in (obj.get("tickers") or [])][:4]
        tick = [t for t in tick if re.fullmatch(r"[A-Z]{1,5}", t)]
        return {"direction": d * c, "magnitude": m, "tickers": tick, "confidence": c, "rationale": str(obj.get("rationale", ""))[:200]}
    except (KeyError, TypeError, ValueError):
        return None


def make_analyst(cfg: dict, fetch: Optional[Callable] = None) -> Optional[Callable]:
    """Disabled unless intelligence.llm_enabled and a provider key exist. Prefer event_intel.make_analyst(conn, cfg, universe)."""
    if not cfg["intelligence"].get("llm_enabled"):
        return None
    import os
    t = cfg["intelligence"]["llm_tiers"]["triage"]
    prov = cfg["intelligence"]["llm_providers"][t["provider"]]
    return None if not os.getenv(prov["key_env"]) else (lambda item: None)
