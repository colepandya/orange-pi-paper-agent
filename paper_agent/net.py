"""Tiny stdlib HTTP layer. All network access in the package goes through ``get`` so tests can inject fakes."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

UA = "PaperAgent/5.0 (virtual-research; contact: %s)"
_CONTACT = "paper-agent@example.invalid"


def set_contact(email: str) -> None:
    global _CONTACT
    _CONTACT = email or _CONTACT


class FetchError(Exception):
    def __init__(self, msg: str, code: Optional[int] = None):
        super().__init__(msg)
        self.code = code


def get(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 15, max_bytes: int = 4_000_000,
        data: Optional[bytes] = None, method: Optional[str] = None) -> Tuple[bytes, int, Dict[str, str]]:
    h = {"User-Agent": UA % _CONTACT, "Accept": "application/json, application/xml, text/xml, */*"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise FetchError("response too large", None)
            return body, getattr(r, "status", 200), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return b"", 304, {k.lower(): v for k, v in (e.headers or {}).items()}
        raise FetchError("HTTP %s" % e.code, e.code)
    except urllib.error.URLError as e:
        raise FetchError("URL error: %s" % e.reason)
    except (TimeoutError, OSError) as e:
        raise FetchError("%s: %s" % (type(e).__name__, e))


def get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 15, **kw):
    body, status, hdrs = get(url, headers, timeout, **kw)
    try:
        return json.loads(body.decode("utf-8", "replace")), status
    except ValueError:
        raise FetchError("invalid JSON", status)
