"""Build watchlist.json (US-listed operating companies) from SEC's keyless ticker file. Refuses to overwrite a good file with a bad one."""
from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Callable, Dict

from . import net

URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|holdings|holding|group|sa|nv|ag|lp|llc|the)\b\.?,?", re.I)
EXCH = {"Nasdaq", "NYSE", "NYSE American", "CBOE", "NYSE Arca", "BATS"}


def aliases(name: str) -> list:
    base = re.sub(r"[.,]", "", name).strip()
    short = re.sub(r"\s+", " ", SUFFIX.sub("", base)).strip()
    out = [base]
    if short and short.lower() != base.lower() and len(short) >= 4:
        out.append(short)
        words = short.split()
        from .entities import GENERIC_FIRST
        if len(words) > 2 and len(" ".join(words[:2])) >= 6 and words[0].lower() not in GENERIC_FIRST and words[1].lower() not in GENERIC_FIRST:
            out.append(" ".join(words[:2]))                      # "Goldman Sachs" yes; "Main Street" / "Market Leader" are ordinary phrases: no
    return list(dict.fromkeys(out))


def build(payload: dict) -> Dict[str, dict]:
    fields = payload["fields"]
    ix = {f: fields.index(f) for f in ("cik", "name", "ticker", "exchange")}
    out: Dict[str, dict] = {}
    for row in payload["data"]:
        t, ex = str(row[ix["ticker"]]).upper(), row[ix["exchange"]]
        if ex not in EXCH or not re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", t):
            continue
        name = row[ix["name"]]
        out.setdefault(t, {"name": name, "aliases": aliases(name), "cik": row[ix["cik"]], "exchange": ex, "sector": "Unclassified"})
    return out


def refresh(path: str, fetch: Callable = net.get_json, min_count: int = 2000) -> dict:
    payload, _ = fetch(URL)
    w = build(payload)
    if len(w) < min_count:
        raise RuntimeError("universe too small (%d < %d); keeping existing file" % (len(w), min_count))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(w, f)
    os.replace(tmp, path)
    return {"count": len(w), "path": path}
