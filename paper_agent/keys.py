"""Key repair helpers.

Finnhub shows TWO 20-character strings (API key + webhook secret). Pasting both together gives a 40-character value that Finnhub rejects (401).
resolve_finnhub() tries each half against the API ONCE, remembers the winner (in the state dir, keyed by a hash of the pasted value), and sets the
environment variable to the working half. No network after the first success; a failure is retried at most hourly."""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Callable, Optional

from . import config, net


def _cache_path() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_PATH) or ".", "resolved-keys.json")


def _load() -> dict:
    try:
        with open(_cache_path()) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    p = _cache_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def resolve_finnhub(fetch: Callable = net.get_json, clock: Callable = time.time, environ=None) -> Optional[str]:
    """Returns the key now in use (or None). Mutates environ['FINNHUB_API_KEY'] only when it resolves a 40-character value."""
    environ = os.environ if environ is None else environ
    raw = environ.get("FINNHUB_API_KEY", "")
    if len(raw) != 40:
        return raw or None
    tag = hashlib.sha256(raw.encode()).hexdigest()[:16]
    cache = _load()
    hit = cache.get(tag)
    if hit and hit.get("key"):
        environ["FINNHUB_API_KEY"] = hit["key"]
        return hit["key"]
    if hit and clock() - float(hit.get("tried", 0)) < 3600 and not hit.get("key"):
        return raw                                           # tried recently and failed: do not hammer the API
    for half in (raw[:20], raw[20:]):
        try:
            j, _ = fetch("https://finnhub.io/api/v1/quote?symbol=AAPL&token=" + half)
            if isinstance(j, dict) and j.get("c") not in (None, "") and "error" not in j:
                cache[tag] = {"key": half, "at": clock()}
                _save(cache)
                environ["FINNHUB_API_KEY"] = half
                return half
        except Exception:  # noqa: BLE001 - 401 for the wrong half, or no network
            continue
    cache[tag] = {"key": None, "tried": clock()}
    _save(cache)
    return raw
