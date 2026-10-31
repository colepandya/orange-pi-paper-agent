"""Company financial facts from SEC's free structured data (XBRL), read deterministically. This replaces the LLM that used to read filings: the numbers a company reports to the SEC are already structured, so
nothing needs to be "understood", only extracted, compared with the same period a year earlier, and checked against plain rules.

For each company we hold or are evaluating (a few per cycle, refreshed weekly) it fetches a handful of concepts from data.sec.gov (revenue, net income, operating income, cash, debt, diluted shares, operating
cash flow, capex), works out growth, margins, net debt, dilution and free cash flow, and raises FLAGS (shrinking revenue, losses, debt above cash, dilution above 5% a year, burning cash). The red team and the
`ask why` answer use them. Everything is traceable to the SEC fact (period end, filing date, accession). SEC asks for a contact in the User-Agent: set runtime.contact_email in Settings."""
from __future__ import annotations

import datetime as dt
import json
from typing import Callable, Dict, List, Optional, Tuple

from . import db
from .timeutil import iso, parse_ts, utcnow

BASE = "https://data.sec.gov/api/xbrl/companyconcept/CIK%010d/%s/%s.json"
CONCEPTS: Dict[str, Tuple[str, List[str], str]] = {                      # key: (taxonomy, [concept names tried in order], kind)
    "revenue": ("us-gaap", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"], "flow"),
    "net_income": ("us-gaap", ["NetIncomeLoss", "ProfitLoss"], "flow"),
    "op_income": ("us-gaap", ["OperatingIncomeLoss"], "flow"),
    "cfo": ("us-gaap", ["NetCashProvidedByUsedInOperatingActivities"], "flow"),
    "capex": ("us-gaap", ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"], "flow"),
    "cash": ("us-gaap", ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsAndShortTermInvestments"], "stock"),
    "debt": ("us-gaap", ["LongTermDebt", "LongTermDebtNoncurrent", "DebtInstrumentCarryingAmount"], "stock"),
    "shares": ("us-gaap", ["WeightedAverageNumberOfDilutedSharesOutstanding"], "flow"),
}
REFRESH_DAYS = 7


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def _points(j: dict, unit_pref=("USD", "shares")) -> List[dict]:
    units = (j or {}).get("units") or {}
    for u in unit_pref:
        if u in units:
            return [p for p in units[u] if isinstance(p, dict) and p.get("end") and p.get("val") is not None]
    return [p for v in units.values() for p in v if isinstance(p, dict) and p.get("end") and p.get("val") is not None][:400]


def _latest_per_period(pts: List[dict]) -> List[dict]:
    """One value per period (the most recently FILED one wins, so restatements replace the original), oldest first."""
    best: Dict[Tuple[str, str], dict] = {}
    for p in pts:
        k = (p.get("start") or "", p["end"])
        if k not in best or (p.get("filed") or "") >= (best[k].get("filed") or ""):
            best[k] = p
    return sorted(best.values(), key=lambda p: p["end"])


def quarters(pts: List[dict]) -> List[dict]:
    """Discrete quarters: durations of 80 to 100 days (a 10-K's fourth quarter is not reported on its own, so ttm() falls back to the annual figure when four quarters are not available)."""
    return [p for p in _latest_per_period(pts) if p.get("start") and 80 <= (_d(p["end"]) - _d(p["start"])).days <= 100]


def annuals(pts: List[dict]) -> List[dict]:
    return [p for p in _latest_per_period(pts) if p.get("start") and 350 <= (_d(p["end"]) - _d(p["start"])).days <= 380]


def ttm(pts: List[dict]) -> Optional[Tuple[float, str, str]]:
    """Trailing twelve months: the latest annual figure, unless four consecutive-looking quarters AFTER it exist (then the last four). -> (value, period end, basis)"""
    a, q = annuals(pts), quarters(pts)
    if len(q) >= 4 and (not a or q[-1]["end"] > a[-1]["end"]):
        last4 = q[-4:]
        if 250 <= (_d(last4[-1]["end"]) - _d(last4[0]["start"])).days <= 400:
            return sum(p["val"] for p in last4), last4[-1]["end"], "last four quarters"
    if a:
        return a[-1]["val"], a[-1]["end"], "latest annual"
    return None


def yoy(pts: List[dict]) -> Optional[Tuple[float, str]]:
    """The latest quarter against the SAME quarter a year earlier (falls back to the last two annuals)."""
    q = quarters(pts)
    if q:
        cur = q[-1]
        prev = [p for p in q if 330 <= (_d(cur["end"]) - _d(p["end"])).days <= 400]
        if prev and prev[-1]["val"]:
            return cur["val"] / prev[-1]["val"] - 1.0, "quarter vs the same quarter a year earlier"
    a = annuals(pts)
    if len(a) >= 2 and a[-2]["val"]:
        return a[-1]["val"] / a[-2]["val"] - 1.0, "year vs the year before"
    return None


def latest_stock(pts: List[dict]) -> Optional[Tuple[float, str]]:
    pts = _latest_per_period(pts)
    return (pts[-1]["val"], pts[-1]["end"]) if pts else None


def summarize(raw: Dict[str, List[dict]], now) -> dict:
    """The facts and the flags, from the parsed SEC points (pure: no network)."""
    out: dict = {"asof": None, "basis": {}, "flags": [], "have": sorted(k for k, v in raw.items() if v)}
    rev, ni, oi, cfo, cap = (ttm(raw.get(k) or []) for k in ("revenue", "net_income", "op_income", "cfo", "capex"))
    if rev:
        out.update(revenue_ttm=rev[0], asof=rev[1])
        out["basis"]["revenue"] = rev[2]
    g = yoy(raw.get("revenue") or [])
    if g:
        out["revenue_growth"], out["basis"]["revenue_growth"] = round(g[0], 4), g[1]
    if rev and rev[0] > 0:
        if ni:
            out["net_margin"] = round(ni[0] / rev[0], 4)
        if oi:
            out["op_margin"] = round(oi[0] / rev[0], 4)
    if ni:
        out["net_income_ttm"] = ni[0]
    if cfo:
        out["cfo_ttm"] = cfo[0]
        if cap:
            out["fcf_ttm"] = cfo[0] - abs(cap[0])
    cash, debt = latest_stock(raw.get("cash") or []), latest_stock(raw.get("debt") or [])
    if cash:
        out["cash"] = cash[0]
    if debt:
        out["debt"] = debt[0]
    if cash and debt:
        out["net_debt"] = debt[0] - cash[0]
    sq = [p for p in quarters(raw.get("shares") or [])]
    if len(sq) >= 5:
        prev = [p for p in sq if 330 <= (_d(sq[-1]["end"]) - _d(p["end"])).days <= 400]
        if prev and prev[-1]["val"]:
            out["share_growth"] = round(sq[-1]["val"] / prev[-1]["val"] - 1.0, 4)
    F = out["flags"]
    if out.get("revenue_growth") is not None and out["revenue_growth"] < -0.05:
        F.append({"flag": "shrinking_revenue", "text": "revenue is down %.0f%% on a year earlier" % (-out["revenue_growth"] * 100), "severity": min(0.6, 0.2 - out["revenue_growth"])})
    if out.get("revenue_growth") is not None and out["revenue_growth"] > 0.15:
        F.append({"flag": "fast_growth", "text": "revenue is up %.0f%% on a year earlier" % (out["revenue_growth"] * 100), "severity": 0.0})
    if out.get("net_income_ttm") is not None and out["net_income_ttm"] < 0:
        F.append({"flag": "loss_making", "text": "it lost money over the last twelve months", "severity": 0.3})
    if out.get("net_debt") is not None and out.get("debt") and out["net_debt"] > 0 and out["debt"] > 2 * max(out.get("cash") or 0, 1):
        F.append({"flag": "debt_above_cash", "text": "debt of $%.1fB is more than twice its cash" % (out["debt"] / 1e9), "severity": 0.35})
    if out.get("share_growth") is not None and out["share_growth"] > 0.05:
        F.append({"flag": "dilution", "text": "the share count grew %.0f%% in a year" % (out["share_growth"] * 100), "severity": min(0.6, 0.2 + out["share_growth"] * 2)})
    if out.get("fcf_ttm") is not None and out["fcf_ttm"] < 0 and (out.get("net_income_ttm") or 0) < 0:
        F.append({"flag": "burning_cash", "text": "it spent more cash than it made over the last twelve months", "severity": 0.4})
    return out


def fetch_concept(fetch_json: Callable, cik: int, tax: str, names: List[str], headers: dict, sleep: Callable) -> List[dict]:
    from . import net
    for n in names:
        try:
            j, _ = fetch_json(BASE % (int(cik), tax, n), headers=headers, timeout=20)
            pts = _points(j)
            if pts:
                return pts
        except net.FetchError as e:
            if getattr(e, "code", None) not in (404, 403):
                raise
        sleep(0.12)                                                                                       # SEC allows about 10 requests a second
    return []


def refresh(conn, cfg: dict, ticker: str, cik, fetch_json: Callable, now, sleep: Callable = lambda s: None) -> dict:
    if not cik:
        return {"skipped": "no CIK for %s" % ticker}
    hdr = {"User-Agent": "paper-agent research %s" % (cfg.get("runtime", {}).get("contact_email") or "contact-not-set@example.com"), "Accept": "application/json"}
    raw = {k: fetch_concept(fetch_json, cik, tax, names, hdr, sleep) for k, (tax, names, _) in CONCEPTS.items()}
    facts = summarize(raw, now)
    facts["ticker"], facts["fetched"] = ticker, iso(now)
    with db.tx(conn):
        conn.execute("INSERT OR REPLACE INTO company_facts(ticker,facts_json,updated) VALUES(?,?,?)", (ticker, json.dumps(facts, sort_keys=True), iso(now)))
    return {"ok": True, "have": facts["have"], "flags": [f["flag"] for f in facts["flags"]]}


def get(conn, ticker: str) -> Optional[dict]:
    r = conn.execute("SELECT facts_json FROM company_facts WHERE ticker=?", (ticker,)).fetchone()
    try:
        return json.loads(r[0]) if r else None
    except ValueError:
        return None


def due_tickers(conn, cfg: dict, held: List[str], candidates: List[str], now, max_n: int = 3) -> List[str]:
    seen, out = set(), []
    for t in list(held) + list(candidates):
        if t in seen:
            continue
        seen.add(t)
        r = conn.execute("SELECT updated FROM company_facts WHERE ticker=?", (t,)).fetchone()
        if not r or (now - parse_ts(r[0])).total_seconds() > REFRESH_DAYS * 86400:
            out.append(t)
        if len(out) >= max_n:
            break
    return out
