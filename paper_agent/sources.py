"""Collectors: RSS/Atom, SEC EDGAR, Federal Register, USAspending, openFDA, GDELT (world events), Finnhub news.

Each source is independent: one failure never blocks another (exponential backoff persisted in ``source_health``).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Callable, Dict, List, Optional, Tuple

from . import db, net
from .timeutil import iso, parse_ts, utcnow

SEC_FORMS = {"8-K", "8-K/A", "6-K", "6-K/A", "S-1", "S-1/A", "F-1", "F-1/A", "10-Q", "10-K", "4", "SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A",
             "424B2", "424B3", "424B4", "424B5", "NT 10-Q", "NT 10-K", "DEF 14A", "SC TO-I", "SC TO-T", "13F-HR"}


def strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(data: bytes) -> List[dict]:
    if b"<!ENTITY" in data[:4096] or b"<!DOCTYPE" in data[:512] and b"ENTITY" in data[:8192]:
        raise net.FetchError("DTD entities rejected")
    root = ET.fromstring(data)
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        v: Dict[str, str] = {}
        for c in list(el):
            k = _local(c.tag)
            val = "".join(c.itertext()).strip()
            if k == "link" and not val:
                val = c.attrib.get("href", "")
            v.setdefault(k, val)
        title = strip_html(v.get("title", ""))
        if title:
            out.append({"title": title, "summary": strip_html(v.get("summary") or v.get("description") or v.get("content", ""))[:1500],
                        "url": v.get("link", ""), "published": v.get("updated") or v.get("published") or v.get("pubDate") or v.get("date") or ""})
    return out


def _gdelt_url(query: str, timespan: str = "6h", maxrecords: int = 50) -> str:
    return "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(
        {"query": query, "mode": "artlist", "maxrecords": maxrecords, "format": "json", "sort": "datedesc", "timespan": timespan})


def host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""


def request_for(src: dict, conn=None) -> dict:
    """WHAT to ask the network for this source: {"args": (...), "kw": {...}} exactly as it will be passed to fetch(). No network and no writes (it only reads the saved ETag),
    so it is safe to do for every source first and fetch them all side by side afterwards."""
    typ = src["type"]
    if typ in ("rss", "atom", "sec_atom"):
        hdrs: Dict[str, str] = {}
        if conn is not None:
            r = conn.execute("SELECT etag,last_modified FROM source_state WHERE source_id=?", (src["id"],)).fetchone()
            if r:
                if r["etag"]:
                    hdrs["If-None-Match"] = r["etag"]
                if r["last_modified"]:
                    hdrs["If-Modified-Since"] = r["last_modified"]
        return {"args": (src["url"],), "kw": {"headers": hdrs, "timeout": 20, "max_bytes": 8_000_000}}
    if typ in ("federal_register", "openfda"):
        return {"args": (src["url"],), "kw": {"timeout": 20}}
    if typ == "usaspending":
        today = utcnow().date()
        payload = {"filters": {"time_period": [{"start_date": (today - dt.timedelta(days=5)).isoformat(), "end_date": today.isoformat()}], "award_type_codes": ["A", "B", "C", "D"]},
                   "fields": ["Award ID", "Recipient Name", "Start Date", "Award Amount", "Awarding Agency", "Description"], "page": 1, "limit": 100, "subawards": False}
        return {"args": (src["url"],), "kw": {"headers": {"Content-Type": "application/json"}, "data": json.dumps(payload).encode(), "timeout": 25}}
    if typ == "gdelt":
        return {"args": (_gdelt_url(src["query"], src.get("timespan", "6h"), src.get("max", 50)),), "kw": {"timeout": 25}}
    if typ == "finnhub_general":
        key = os.getenv("FINNHUB_API_KEY", "")
        if not key:
            raise net.FetchError("DISABLED_NO_KEY")
        return {"args": ("https://finnhub.io/api/v1/news?category=general&token=" + urllib.parse.quote(key),), "kw": {"timeout": 15}}
    raise ValueError("unknown source type " + typ)


def parse_response(src: dict, resp: tuple, conn=None) -> Tuple[List[dict], int]:
    """Turn what the network returned (body, status, headers) into items. This is where the saved ETag is written, so it belongs with the database work, in the caller's order."""
    typ = src["type"]
    body, status, rh = resp
    if typ in ("rss", "atom", "sec_atom"):
        if status == 304:
            return [], 304     # healthy and unchanged - NOT a failure
        if conn is not None and (rh.get("etag") or rh.get("last-modified")):
            conn.execute("INSERT INTO source_state(source_id,etag,last_modified,updated) VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET etag=excluded.etag,last_modified=excluded.last_modified,updated=excluded.updated",
                         (src["id"], rh.get("etag"), rh.get("last-modified"), iso()))
        items = parse_feed(body)
        if typ == "sec_atom":
            keep = []
            for x in items:
                m = re.match(r"\s*(.+?)\s+-\s+(.+)", x["title"])
                form = m.group(1).strip() if m else ""
                if form in SEC_FORMS:
                    x["form"] = form
                    keep.append(x)
            items = keep
        return items, status
    if typ == "federal_register":
        j = json.loads(body)
        out = []
        for x in j.get("results", j.get("documents", [])):
            agency = ", ".join(a.get("name", "") for a in x.get("agencies", []) if isinstance(a, dict))
            out.append({"title": x.get("title", ""), "summary": " ".join(filter(None, [agency, x.get("abstract", ""), x.get("document_type", "")])),
                        "url": x.get("html_url") or x.get("pdf_url", ""), "published": x.get("filed_at") or x.get("publication_date") or ""})
        return out, status
    if typ == "openfda":
        out = []
        for x in json.loads(body).get("results", []):
            out.append({"title": "%s recall: %s" % (x.get("recalling_firm", ""), (x.get("product_description", "") or "")[:140]),
                        "summary": " ".join(filter(None, [x.get("reason_for_recall", ""), x.get("classification", ""), x.get("status", "")])),
                        "url": "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/enforcement-reports", "published": x.get("report_date") or ""})
        return out, status
    if typ == "usaspending":
        out = []
        for x in json.loads(body).get("results", []):
            amt = x.get("Award Amount")
            amt_s = ("$%.1f million" % (float(amt) / 1e6)) if isinstance(amt, (int, float)) and amt >= 1e5 else ""
            out.append({"title": "%s federal contract award %s" % (x.get("Recipient Name", ""), amt_s), "summary": " ".join(filter(None, [x.get("Awarding Agency", ""), x.get("Description") or ""])),
                        "url": "https://www.usaspending.gov/award/" + str(x.get("Award ID", "")), "published": x.get("Start Date") or ""})
        return out, status
    if typ == "gdelt":
        try:
            j = json.loads(body.decode("utf-8", "replace") or "{}")
        except ValueError:
            raise net.FetchError("GDELT returned non-JSON (rate limited?)")
        out = []
        for a in j.get("articles", []):
            out.append({"title": a.get("title", ""), "summary": "%s %s" % (a.get("domain", ""), a.get("sourcecountry", "")), "url": a.get("url", ""), "published": a.get("seendate") or ""})
        return out, status
    if typ == "finnhub_general":
        return [{"title": a.get("headline", ""), "summary": strip_html(a.get("summary", "")), "url": a.get("url", ""), "published": a.get("datetime") or ""} for a in json.loads(body)[:60]], status
    raise ValueError("unknown source type " + typ)


def fetch_source(src: dict, fetch: Callable = net.get, conn=None) -> Tuple[List[dict], int]:
    """One source, start to finish, one after another: plan, fetch, parse. (collect() does the same in three phases so the fetching can run side by side.)"""
    req = request_for(src, conn if src["type"] in ("rss", "atom", "sec_atom") else None)
    return parse_response(src, fetch(*req["args"], **req["kw"]), conn)


def finnhub_company_news(ticker: str, fetch: Callable = net.get, days: int = 2) -> List[dict]:
    key = os.getenv("FINNHUB_API_KEY", "")
    if not key:
        return []
    today = utcnow().date()
    url = "https://finnhub.io/api/v1/company-news?" + urllib.parse.urlencode({"symbol": ticker, "from": (today - dt.timedelta(days=days)).isoformat(), "to": today.isoformat(), "token": key})
    body, _, _ = fetch(url, timeout=15)
    return [{"title": a.get("headline", ""), "summary": strip_html(a.get("summary", "")), "url": a.get("url", ""), "published": a.get("datetime") or "", "ticker_hint": ticker} for a in json.loads(body)[:15]]


# ------------------------------------------------------------------------------------------ health / scheduling
def record_health(conn, src: dict, status, error=None, count: int = 0, clock: Callable = time.time, ms: Optional[int] = None) -> None:
    """Stamp health with the SAME clock source_due() reads (a real-clock stamp under an injected clock made sources look permanently 'not due')."""
    t = iso(dt.datetime.fromtimestamp(clock(), dt.timezone.utc))
    r = conn.execute("SELECT failures FROM source_health WHERE source_id=?", (src["id"],)).fetchone()
    failures = r[0] if r else 0
    if error:
        failures += 1
        back, success = clock() + min(21600, 300 * (2 ** min(failures, 6))), None
    else:
        failures, back, success = 0, 0, t
    conn.execute("INSERT INTO source_health(source_id,name,last_attempt,last_success,last_status,failures,backoff_until,items_total,last_error,last_ms) VALUES(?,?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(source_id) DO UPDATE SET name=excluded.name,last_attempt=excluded.last_attempt,last_success=COALESCE(excluded.last_success,source_health.last_success),"
                 "last_status=excluded.last_status,failures=excluded.failures,backoff_until=excluded.backoff_until,items_total=source_health.items_total+excluded.items_total,last_error=excluded.last_error,last_ms=excluded.last_ms",
                 (src["id"], src["name"], t, success, str(status), failures, back, count, (str(error)[:300] if error else None), ms))


def disabled_ids(cfg: Optional[dict], srcs: Optional[List[dict]] = None) -> set:
    """Ids of the feeds that are switched off: by the owner in Settings (source_overrides) or shipped off in sources.json. The ONE definition, so the collector, the Sources table and the research-gap scan agree."""
    off = {sid for sid, o in ((cfg or {}).get("source_overrides") or {}).items() if isinstance(o, dict) and o.get("enabled") is False}
    if srcs is None:
        try:
            from . import config as _cfg
            d = _cfg.load_json(os.path.join(_cfg.CONFIG_DIR, "sources.json"), {"sources": []})
            srcs = d["sources"] if isinstance(d, dict) else d
        except Exception:  # noqa: BLE001
            srcs = []
    for s in srcs:
        o = ((cfg or {}).get("source_overrides") or {}).get(s.get("id"), {})
        if (s.get("enabled", True) is False and o.get("enabled") is not True) or o.get("enabled") is False:
            off.add(s.get("id"))
    return off


def source_due(conn, src: dict, clock: Callable = time.time) -> bool:
    r = conn.execute("SELECT last_attempt,backoff_until FROM source_health WHERE source_id=?", (src["id"],)).fetchone()
    if not r:
        return True
    if float(r["backoff_until"] or 0) > clock():
        return False
    last = parse_ts(r["last_attempt"])
    return last is None or clock() - last.timestamp() >= int(src.get("interval", 600))


def store_items(conn, src: dict, items: List[dict], now: Optional[dt.datetime] = None) -> int:
    new = 0
    now = now or utcnow()
    with db.tx(conn):
        for it in items:
            title = (it.get("title") or "").strip()
            if not title:
                continue
            url = (it.get("url") or "").strip()
            uid = hashlib.sha256((src["id"] + "|" + url + "|" + title).encode()).hexdigest()
            pub = parse_ts(it.get("published"))
            cur = conn.execute("INSERT OR IGNORE INTO news(uid,source,title,summary,url,published,ingested,trust) VALUES(?,?,?,?,?,?,?,?)",
                               (uid, src["name"], title, (it.get("summary") or "")[:1500], url, iso(pub) if pub else "", iso(now), float(src.get("trust", 0.5))))
            new += cur.rowcount
    return new


GDELT_GAP_SECONDS, GDELT_MAX_PER_CYCLE = 5.5, 3        # GDELT asks for <= 1 request per 5 s; extra feeds simply wait for the next cycle


def collect(conn, sources: List[dict], fetch: Callable = net.get, clock: Callable = time.time, max_items: int = 100, only_ids: Optional[set] = None,
            sleep: Callable = time.sleep, workers: int = 1, deadline_s: float = 90.0) -> dict:
    """Poll every source that is due. Three phases, so the slow part (waiting for servers) can run side by side while everything that touches the database stays in ONE place, in a FIXED order:
    1. PLAN (database reads): which sources are due, and exactly what to ask each.  2. FETCH (network only, `workers` at a time, at most 2 per server, GDELT one per 5.5 s).
    3. PARSE + STORE (database writes) one source at a time in the original order, so the result is the same whichever server answered first. workers=1 is the old one-after-another behaviour."""
    from . import parallel
    tot = {"sources_ok": 0, "fetch_failures": 0, "skipped": 0, "unchanged": 0, "new": 0, "errors": [], "slowest": []}
    gdelt_done = 0

    def last_attempt(src: dict) -> str:
        r = conn.execute("SELECT last_attempt FROM source_health WHERE source_id=?", (src["id"],)).fetchone()
        return (r[0] if r and r[0] else "")            # never polled sorts first

    def failed(src: dict, e: BaseException, ms: Optional[int] = None) -> None:
        code = getattr(e, "code", None) or type(e).__name__
        record_health(conn, src, code, e, clock=clock, ms=ms)
        tot["fetch_failures"] += 1
        tot["errors"].append("%s:%s" % (src["id"], code))

    # fair rotation: with a per-cycle cap, the least-recently-polled GDELT feeds go first so none can be starved
    ordered = [s for s in sources if s["type"] != "gdelt"] + sorted((s for s in sources if s["type"] == "gdelt"), key=last_attempt)
    plan: List[Tuple[dict, dict]] = []
    for src in ordered:
        if only_ids is not None and src["id"] not in only_ids:
            continue
        if src.get("enabled", True) is False or not source_due(conn, src, clock):
            tot["skipped"] += 1
            continue
        if src["type"] == "gdelt":
            if gdelt_done >= GDELT_MAX_PER_CYCLE:
                tot["skipped"] += 1
                continue
            gdelt_done += 1
        try:
            plan.append((src, request_for(src, conn)))
        except Exception as e:  # noqa: BLE001 - every failure mode must be isolated per source
            failed(src, e)
    jobs = [{"key": src["id"], "host": host_of(req["args"][0]), "fn": (lambda req=req: fetch(*req["args"], **req["kw"]))} for src, req in plan]
    gaps = {host_of(req["args"][0]): GDELT_GAP_SECONDS for src, req in plan if src["type"] == "gdelt"}
    t_fetch = time.monotonic()
    outcomes = parallel.run_jobs(jobs, workers=workers, per_host=2, min_gap=gaps, deadline_s=deadline_s, sleep=sleep)
    tot["fetch_seconds"] = round(time.monotonic() - t_fetch, 1)
    for (src, req), out in zip(plan, outcomes):
        ms = int(out.seconds * 1000)
        if not out.ok:
            failed(src, out.error, ms)
            continue
        try:
            items, status = parse_response(src, out.value, conn)
        except Exception as e:  # noqa: BLE001
            failed(src, e, ms)
            continue
        record_health(conn, src, status, count=len(items), clock=clock, ms=ms)
        if status == 304:
            tot["unchanged"] += 1
            continue
        tot["sources_ok"] += 1
        tot["new"] += store_items(conn, src, items[:max_items], dt.datetime.fromtimestamp(clock(), dt.timezone.utc))      # ingestion time on the SAME clock as "is it due"
    tot["slowest"] = ["%s %.1fs" % (o.key, o.seconds) for o in sorted(outcomes, key=lambda o: -o.seconds)[:3]]
    return tot