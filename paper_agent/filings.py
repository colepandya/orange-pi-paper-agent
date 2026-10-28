"""Company knowledge base from SEC filings (authoritative, keyless). The pipeline finds new 10-K / 10-Q / earnings 8-K filings for companies
we hold or are evaluating, extracts the relevant sections, and has the LLM turn them into STRUCTURED, GROUNDED knowledge:
revenue/cost drivers, customers, suppliers, competitors, risks, guidance, results vs expectations, tone, and what changed.
Every claim must carry a verbatim quote from the filing text or the whole extraction is rejected."""
from __future__ import annotations

import json
import re
import time
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional

from . import db, net
from .llm_client import LLMClient, ground_quotes, wrap
from .timeutil import iso, parse_ts, utcnow

SUBMISSIONS = "https://data.sec.gov/submissions/CIK%010d.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/%d/%s/%s"
FORMS = ("10-K", "10-Q", "8-K", "20-F", "6-K", "10-K/A")


class _Text(HTMLParser):
    SKIP = {"script", "style", "head", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str, limit: int = 3_000_000) -> str:
    p = _Text()
    try:
        p.feed(html[:limit])
    except Exception:  # noqa: BLE001 - malformed HTML must not break the cycle
        pass
    t = "".join(p.parts).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()


def section(text: str, start_rx: str, end_rx: str, maxlen: int = 7000) -> str:
    """Pick the real section (not the table-of-contents line): the candidate start with the most text before the next end marker."""
    best = ""
    ends = [m.start() for m in re.finditer(end_rx, text, re.I)]
    for m in re.finditer(start_rx, text, re.I):
        nxt = next((e for e in ends if e > m.end() + 200), len(text))
        chunk = text[m.start(): nxt]
        if len(chunk) > len(best):
            best = chunk
    return best[:maxlen]


def extract_sections(text: str, form: str) -> str:
    if form.startswith("10-K") or form == "20-F":
        parts = [("BUSINESS", section(text, r"item\s*1\.?\s*business", r"item\s*1a\.?\s*risk", 6000)),
                 ("RISK FACTORS", section(text, r"item\s*1a\.?\s*risk\s*factors", r"item\s*1b|item\s*2\.?\s*properties", 5000)),
                 ("MD&A", section(text, r"item\s*7\.?\s*management", r"item\s*7a|item\s*8\.?\s*financial", 7000))]
    elif form == "10-Q":
        parts = [("MD&A", section(text, r"item\s*2\.?\s*management", r"item\s*3\.?\s*quantitative|item\s*4\.?\s*controls", 8000)),
                 ("RISK FACTORS (changes)", section(text, r"item\s*1a\.?\s*risk\s*factors", r"item\s*2\.?\s*unregistered|item\s*6\.?\s*exhibits", 2500))]
    else:
        parts = [("PRESS RELEASE / REPORT", text[:9000])]
    return "\n\n".join("== %s ==\n%s" % (k, v) for k, v in parts if v)


def recent_filings(sub: dict, max_age_days: int = 400, now=None) -> List[dict]:
    r = ((sub or {}).get("filings") or {}).get("recent") or {}
    out, now = [], now or utcnow()
    for i, form in enumerate(r.get("form", [])):
        if form not in FORMS:
            continue
        items = (r.get("items") or [""] * len(r["form"]))[i] or ""
        if form in ("8-K", "6-K") and not re.search(r"2\.02|7\.01|8\.01|1\.01|2\.01", items or "2.02" if form == "6-K" else items):
            continue
        filed = parse_ts(r["filingDate"][i])
        if not filed or (now - filed).days > max_age_days:
            continue
        out.append({"form": form, "accession": r["accessionNumber"][i], "primary": r["primaryDocument"][i], "filed": r["filingDate"][i], "items": items})
    # newest first; keep the latest 10-K, latest 10-Q, and up to 2 earnings-type 8-Ks
    seen: Dict[str, int] = {}
    keep = []
    for f in out:
        k = "8-K" if f["form"] in ("8-K", "6-K") else f["form"][:4]
        cap = 2 if k == "8-K" else 1
        if seen.get(k, 0) < cap:
            seen[k] = seen.get(k, 0) + 1
            keep.append(f)
    return keep


SYSTEM = """You are an equity research analyst reading an SEC filing. Extract facts into ONE JSON object:
{
 "revenue_drivers": ["..."], "cost_drivers": ["..."], "customers": ["named customers or segments"], "suppliers": ["..."], "competitors": ["..."],
 "benefits": ["structural advantages / tailwinds named in the text"],
 "risks": [{"risk":"...","severity":0..1}],
 "guidance": {"revenue":"text or null","eps":"text or null","commentary":"<=200 chars"},
 "results": {"revenue_growth_yoy": number or null (decimal, e.g. 0.12), "margin_trend":"expanding"|"stable"|"contracting"|"unknown", "vs_expectations":"beat"|"inline"|"miss"|"unknown"},
 "backlog_usd": number or null, "mgmt_tone": -1..1,
 "changes_vs_prior": ["what is NEW or CHANGED in this filing"],
 "evidence_quotes": [{"claim":"...","quote":"EXACT verbatim text copied from the document, >=12 chars"}]
}
Use ONLY what the document states; use null/[] when absent. Do not guess numbers. Every material claim needs a verbatim quote. Max 8 items per list."""


def _lst(x, n=8):
    return [str(i)[:200] for i in (x or [])[:n] if isinstance(i, (str, int, float))]


def make_validator(source_text: str) -> Callable[[dict], Optional[dict]]:
    def v(o: dict) -> Optional[dict]:
        quotes, ratio = ground_quotes(o.get("evidence_quotes"), source_text)
        if ratio is None or ratio < 0.5:
            return None        # filings are long; an extraction with no verifiable quotes is not trusted
        g = o.get("guidance") if isinstance(o.get("guidance"), dict) else {}
        r = o.get("results") if isinstance(o.get("results"), dict) else {}
        try:
            growth = float(r["revenue_growth_yoy"]) if r.get("revenue_growth_yoy") is not None else None
        except (TypeError, ValueError):
            growth = None
        risks = [{"risk": str(x.get("risk", ""))[:200], "severity": max(0.0, min(1.0, float(x.get("severity", 0.3) or 0.3)))} for x in (o.get("risks") or [])[:8] if isinstance(x, dict)]
        try:
            backlog = float(o["backlog_usd"]) if o.get("backlog_usd") is not None else None
        except (TypeError, ValueError):
            backlog = None
        try:
            tone = max(-1.0, min(1.0, float(o.get("mgmt_tone", 0))))
        except (TypeError, ValueError):
            tone = 0.0
        return {"revenue_drivers": _lst(o.get("revenue_drivers")), "cost_drivers": _lst(o.get("cost_drivers")), "customers": _lst(o.get("customers")), "suppliers": _lst(o.get("suppliers")),
                "competitors": _lst(o.get("competitors")), "benefits": _lst(o.get("benefits")), "risks": risks,
                "guidance": {"revenue": g.get("revenue"), "eps": g.get("eps"), "commentary": str(g.get("commentary", ""))[:240]},
                "results": {"revenue_growth_yoy": growth, "margin_trend": r.get("margin_trend") if r.get("margin_trend") in ("expanding", "stable", "contracting") else "unknown",
                            "vs_expectations": r.get("vs_expectations") if r.get("vs_expectations") in ("beat", "inline", "miss") else "unknown"},
                "backlog_usd": backlog, "mgmt_tone": tone, "changes_vs_prior": _lst(o.get("changes_vs_prior")), "quotes": quotes}
    return v


def merge(old: Optional[dict], new: dict, form: str, filed: str) -> dict:
    out = dict(old or {})
    for k in ("revenue_drivers", "cost_drivers", "customers", "suppliers", "competitors", "benefits"):
        out[k] = list(dict.fromkeys(new.get(k, []) + (out.get(k) or [])))[:12]
    seen, risks = set(), []
    for r in new.get("risks", []) + (out.get("risks") or []):
        if r["risk"].lower() not in seen:
            seen.add(r["risk"].lower())
            risks.append(r)
    out["risks"] = risks[:12]
    out["latest"] = {"form": form, "filed": filed, "guidance": new["guidance"], "results": new["results"], "backlog_usd": new["backlog_usd"], "mgmt_tone": new["mgmt_tone"], "changes_vs_prior": new["changes_vs_prior"]}
    return out


def valuation(conn, ticker: str) -> dict:
    """Simple, honest valuation snapshot from data we actually hold (price x shares vs reported revenue / margin)."""
    c = conn.execute("SELECT market_cap,profile_json FROM companies WHERE ticker=?", (ticker,)).fetchone()
    f = conn.execute("SELECT revenue FROM company_fundamentals WHERE ticker=?", (ticker,)).fetchone()
    out: Dict = {}
    if c and c["market_cap"] and f and f["revenue"]:
        out["price_to_sales"] = round(c["market_cap"] / f["revenue"], 2)
        try:
            nm = (json.loads(c["profile_json"] or "{}").get("features") or {}).get("net_margin")
        except ValueError:
            nm = None
        if nm and nm > 0:
            out["pe_estimate"] = round(c["market_cap"] / (f["revenue"] * nm), 1)
    return out


def refresh(conn, cfg: dict, client: Optional[LLMClient], ticker: str, cik, fetch: Callable = net.get, now=None, max_filings: int = 2, sleep: Callable = time.sleep) -> dict:
    """Process new filings for one company. Returns {'new':n,'ok':n,'skipped':reason}."""
    now = now or utcnow()
    if client is None or not client.enabled("analyst"):
        return {"skipped": "llm disabled"}
    if not cik:
        return {"skipped": "no CIK"}
    body, status, _ = fetch(SUBMISSIONS % int(cik), timeout=20)
    todo = [f for f in recent_filings(json.loads(body), now=now) if not conn.execute("SELECT 1 FROM company_filings WHERE accession=?", (f["accession"],)).fetchone()][:max_filings]
    res = {"new": len(todo), "ok": 0}
    for f in todo:
        acc_nodash = f["accession"].replace("-", "")
        doc = f["primary"]
        try:
            if f["form"] in ("8-K", "6-K"):        # the earnings release lives in an exhibit, not the cover document
                sleep(0.2)
                idx = json.loads(fetch(ARCHIVE % (int(cik), acc_nodash, "index.json"), timeout=20)[0])
                names = [i["name"] for i in idx.get("directory", {}).get("item", [])]
                ex = [n for n in names if re.search(r"ex-?99", n, re.I) and n.lower().endswith((".htm", ".html"))]
                doc = ex[0] if ex else doc
            sleep(0.2)
            raw, st, _ = fetch(ARCHIVE % (int(cik), acc_nodash, doc), timeout=45, max_bytes=6_000_000)
            text = extract_sections(html_to_text(raw.decode("utf-8", "replace")), f["form"])
            del raw
            if len(text) < 400:
                conn.execute("INSERT OR REPLACE INTO company_filings(accession,ticker,form,filed,items,processed,ok) VALUES(?,?,?,?,?,?,0)", (f["accession"], ticker, f["form"], f["filed"], f["items"], iso(now)))
                continue
            user = "Company %s, form %s filed %s. Extract the facts.\n%s" % (ticker, f["form"], f["filed"], wrap(text, 24000))
            out = client.complete_json("analyst", "filing_extract", SYSTEM, user, make_validator(text), max_tokens=1800)
        except Exception as e:  # noqa: BLE001
            conn.execute("INSERT OR REPLACE INTO company_filings(accession,ticker,form,filed,items,processed,ok,extraction_json) VALUES(?,?,?,?,?,?,0,?)", (f["accession"], ticker, f["form"], f["filed"], f["items"], iso(now), json.dumps({"error": str(e)[:150]})))
            continue
        with db.tx(conn):
            conn.execute("INSERT OR REPLACE INTO company_filings(accession,ticker,form,filed,items,processed,ok,extraction_json) VALUES(?,?,?,?,?,?,?,?)",
                         (f["accession"], ticker, f["form"], f["filed"], f["items"], iso(now), int(out is not None), json.dumps(out) if out else None))
            if out:
                old = conn.execute("SELECT summary_json FROM company_knowledge WHERE ticker=?", (ticker,)).fetchone()
                merged = merge(json.loads(old[0]) if old else None, out, f["form"], f["filed"])
                merged["valuation"] = valuation(conn, ticker)
                srcs = [dict(r) for r in conn.execute("SELECT form,filed,accession FROM company_filings WHERE ticker=? AND ok=1 ORDER BY filed DESC LIMIT 6", (ticker,))]
                conn.execute("INSERT INTO company_knowledge(ticker,summary_json,sources_json,updated) VALUES(?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET summary_json=excluded.summary_json,sources_json=excluded.sources_json,updated=excluded.updated",
                             (ticker, json.dumps(merged, sort_keys=True), json.dumps(srcs), iso(now)))
                res["ok"] += 1
    return res


def due_tickers(conn, cfg: dict, held: List[str], candidates: List[str], now) -> List[str]:
    """Which companies to check for new filings this cycle: held first, then candidates; each at most once per 12 h; bounded per cycle."""
    cap = int(cfg["intelligence"].get("max_knowledge_refresh_per_cycle", 2))
    out = []
    for t in list(dict.fromkeys(held + candidates)):
        if len(out) >= cap:
            break
        if db.due(conn, "kb:" + t, 12, now):
            out.append(t)
    return out
