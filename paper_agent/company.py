"""Company intelligence: 6 dimension scores (0-100) with completeness, plus 'company DNA' classification.

Data: Finnhub profile/metrics/earnings (free key), SEC XBRL company facts (keyless), market series (keyless).
Missing data lowers *completeness* (and pulls scores toward neutral 50) rather than being guessed."""
from __future__ import annotations

import json
import math
import os
import urllib.parse
from typing import Callable, Dict, List, Optional, Tuple

from . import marketdata as md, net
from .timeutil import iso, utcnow

SECTOR_KEYWORDS = [
    ("Semiconductors", ["semiconductor", "chip"]), ("Software", ["software", "saas", "cloud"]), ("Defense", ["aerospace", "defense", "defence"]),
    ("Technology", ["technology", "computer", "electronic", "internet", "communications equipment", "it services"]),
    ("Healthcare", ["pharma", "biotech", "health", "medical", "drug", "life sciences"]), ("Financials", ["bank", "insurance", "financial", "capital markets", "asset management", "credit"]),
    ("Energy", ["oil", "gas", "energy", "coal", "uranium", "petroleum"]), ("Utilities", ["utilit", "electric", "water", "power producer"]),
    ("Materials", ["chemical", "metal", "mining", "steel", "gold", "materials", "paper", "aluminum"]), ("Real Estate", ["reit", "real estate"]),
    ("Consumer Staples", ["food", "beverage", "tobacco", "household", "staples", "grocery"]), ("Consumer Discretionary", ["retail", "auto", "apparel", "restaurant", "leisure", "hotel", "homebuilding", "cruise", "airline"]),
    ("Communication", ["media", "telecom", "entertainment", "communication"]), ("Industrials", ["machinery", "industrial", "construction", "transport", "electrical", "building", "logistics", "railroad", "trucking"]),
]
DEFENSIVE = {"Consumer Staples", "Utilities", "Healthcare"}
CYCLICAL = {"Materials", "Industrials", "Energy", "Consumer Discretionary", "Semiconductors", "Financials"}
COUNTRY_CURRENCY = {"US": "USD", "CA": "CAD", "GB": "GBP", "JP": "JPY", "CN": "CNY", "TW": "TWD", "NL": "EUR", "DE": "EUR", "FR": "EUR", "IE": "EUR", "CH": "CHF", "IL": "ILS", "KR": "KRW", "IN": "INR", "BR": "BRL", "AU": "AUD"}


def sector_of(text: str) -> str:
    t = (text or "").lower()
    for sec, kws in SECTOR_KEYWORDS:
        if any(k in t for k in kws):
            return sec
    return "Unclassified"


_NOT_A_NUMBER = {"", "N/A", "NA", "NULL", "NONE", "-", "--", "NAN", "INF", "-INF", "NULLNULL"}


def num(x) -> Optional[float]:
    """Provider numbers arrive as 28.4, "28.4", "1,234", "N/A", "", null, true, NaN... -> a FINITE float, or None. Every parse boundary uses this."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
    elif isinstance(x, str):
        s = x.strip().replace(",", "").rstrip("%")
        if s.upper() in _NOT_A_NUMBER:
            return None
        try:
            v = float(s)
        except ValueError:
            return None
    else:
        return None
    return v if math.isfinite(v) else None


def lin(x: Optional[float], lo: float, hi: float) -> Optional[float]:
    x = num(x)
    if x is None:
        return None
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) if hi != lo else None


def blend(parts: List[Tuple[Optional[float], float]]) -> Tuple[float, float]:
    """-> (score 0..100, completeness 0..1). Missing parts pull the score toward 50 rather than being invented."""
    tw = sum(w for _, w in parts)
    have = [(v, w) for v, w in parts if v is not None]
    hw = sum(w for _, w in have)
    if not have:
        return 50.0, 0.0
    raw = sum(v * w for v, w in have) / hw
    comp = hw / tw
    return 100.0 * (raw * comp + 0.5 * (1 - comp)), comp


def score_company(f: Dict[str, Optional[float]]) -> dict:
    f = {k: num(v) for k, v in (f if isinstance(f, dict) else {}).items()}          # whatever the caller passes, scoring only ever sees finite floats or None
    mc = f.get("market_cap")
    fs, c1 = blend([(lin(f.get("net_margin"), -.05, .20), .3), (None if f.get("debt_to_equity") is None else 1 - (lin(f.get("debt_to_equity"), .3, 2.5) or 0), .3),
                    (lin(f.get("cash_to_debt"), .1, 1.5), .2), (lin(f.get("op_margin"), 0, .25), .2)])
    mq, c2 = blend([(lin(f.get("roe"), .05, .25), .4), (None if f.get("insider") is None else (f["insider"] + 1) / 2, .35), (lin(f.get("buyback_flag"), 0, 1), .25)])
    mo, c3 = blend([(lin(f.get("gross_margin"), .25, .65), .4), (lin(f.get("op_margin"), .05, .30), .35), (lin(f.get("roe"), .08, .30), .25)])
    iq, c4 = blend([(lin(f.get("rd_ratio"), .02, .20), .5), (lin(f.get("revenue_growth"), 0, .30), .5)])
    inst, c5 = blend([(lin(math.log10(mc) if mc and mc > 0 else None, 8.5, 11.0), .5), (lin(math.log10(f["adv_usd"]) if f.get("adv_usd") else None, 6.5, 9.5), .5)])
    ex, c6 = blend([(f.get("beat_rate"), .5), (lin(f.get("revenue_growth"), -.05, .20), .3), (lin(f.get("op_margin"), -.05, .20), .2)])
    comp = (c1 + c2 + c3 + c4 + c5 + c6) / 6
    return {"financial_strength": fs, "management_quality": mq, "competitive_moat": mo, "innovation_quality": iq, "institutional_support": inst, "execution_quality": ex, "completeness": comp}


def classify_dna(f: dict, s: dict, sector: str) -> str:
    f = {k: num(v) for k, v in (f if isinstance(f, dict) else {}).items()}
    g, nm, mc, vol = f.get("revenue_growth"), f.get("net_margin"), f.get("market_cap"), f.get("vol_daily") or 0.0
    if (nm is None or nm < 0) and ((mc is not None and mc < 2e9) or vol > 0.045):
        return "Speculative"
    if f.get("drawdown_from_high") is not None and f["drawdown_from_high"] < -0.35 and (f.get("mom_3m") or 0) > 0.10:
        return "Turnaround"
    if g is not None and g > 0.30:
        return "Hyper Growth"
    if s["competitive_moat"] >= 60 and s["financial_strength"] >= 58 and (g is None or 0.0 <= g <= 0.30):
        return "Quality Compounder"
    if sector in DEFENSIVE and (f.get("beta") is None or f["beta"] < 0.95):
        return "Defensive"
    if sector in CYCLICAL:
        return "Cyclical"
    return "Quality Compounder" if s["competitive_moat"] >= 55 else ("Defensive" if (f.get("beta") or 1.0) < 0.9 else "Cyclical")


# ---------------------------------------------------------------- parsers (pure, fixture-testable)
def parse_finnhub_profile(j: dict) -> dict:
    j = j if isinstance(j, dict) else {}
    return {"name": j.get("name"), "industry": j.get("finnhubIndustry"), "country": j.get("country"), "currency": j.get("currency"),
            "market_cap": (num((j or {}).get("marketCapitalization")) or 0) * 1e6 or None, "shares": (num((j or {}).get("shareOutstanding")) or 0) * 1e6 or None}


def parse_finnhub_metrics(j: dict) -> dict:
    m = (j or {}).get("metric") if isinstance(j, dict) else None
    m = m if isinstance(m, dict) else {}

    def pc(k):
        v = num(m.get(k))
        return v / 100.0 if v is not None else None
    de = num(m.get("totalDebt/totalEquityAnnual")) or num(m.get("totalDebt/totalEquityQuarterly"))
    return {"net_margin": pc("netProfitMarginTTM"), "op_margin": pc("operatingMarginTTM"), "gross_margin": pc("grossMarginTTM"), "roe": pc("roeTTM"),
            "revenue_growth": pc("revenueGrowthTTMYoy") if m.get("revenueGrowthTTMYoy") is not None else pc("revenueGrowth3Y"),
            "debt_to_equity": (de / 100.0 if de > 20 else de) if de is not None else None, "beta": num(m.get("beta"))}


def parse_finnhub_earnings(j) -> Optional[float]:
    rows = [(num(r.get("actual")), num(r.get("estimate"))) for r in (j if isinstance(j, list) else []) if isinstance(r, dict)]
    rows = [(a, e) for a, e in rows if a is not None and e is not None][:4]
    return sum(1 for a, e in rows if a >= e) / len(rows) if rows else None


_SEC_TAGS = {"revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"], "op_income": ["OperatingIncomeLoss"], "net_income": ["NetIncomeLoss"],
             "gross_profit": ["GrossProfit"], "cash": ["CashAndCashEquivalentsAtCarryingValue"], "debt": ["LongTermDebt", "LongTermDebtNoncurrent"], "equity": ["StockholdersEquity"],
             "rd": ["ResearchAndDevelopmentExpense"]}


def parse_sec_facts(j: dict) -> dict:
    """Latest two fiscal-year values per tag from companyfacts JSON -> ratios."""
    gaap = (((j or {}).get("facts") or {}).get("us-gaap") or {}) if isinstance(j, dict) else {}

    def series(tags):
        for tag in tags:
            units = (gaap.get(tag) or {}).get("units", {}).get("USD") or []
            fy = [u for u in units if isinstance(u, dict) and u.get("fp") == "FY" and u.get("form") in ("10-K", "10-K/A") and num(u.get("val")) is not None and u.get("end")]
            byend = {}
            for u in fy:
                byend[u["end"]] = num(u["val"])
            if byend:
                ks = sorted(byend)
                return [byend[k] for k in ks[-2:]]
        return []
    v = {k: series(t) for k, t in _SEC_TAGS.items()}
    last = lambda k: v[k][-1] if v[k] else None  # noqa: E731
    rev, out = last("revenue"), {}
    out["revenue"] = rev
    if rev:
        out["op_margin"] = last("op_income") / rev if last("op_income") is not None else None
        out["net_margin"] = last("net_income") / rev if last("net_income") is not None else None
        out["gross_margin"] = last("gross_profit") / rev if last("gross_profit") is not None else None
        out["rd_ratio"] = last("rd") / rev if last("rd") is not None else None
    if len(v["revenue"]) == 2 and v["revenue"][0]:
        out["revenue_growth"] = v["revenue"][1] / v["revenue"][0] - 1
    if last("equity") and last("debt") is not None:
        out["debt_to_equity"] = last("debt") / last("equity") if last("equity") > 0 else None
    if last("debt"):
        out["cash_to_debt"] = (last("cash") or 0) / last("debt")
    out["cash"], out["debt"] = last("cash"), last("debt")
    return out


def market_features(conn, ticker: str) -> dict:
    c = md.closes(conn, ticker, 260)
    f = {"vol_daily": md.daily_vol(conn, ticker, 60, default=None) if len(c) > 20 else None, "beta": md.beta(conn, ticker), "adv_usd": md.adv_usd(conn, ticker),
         "mom_3m": md.ret_n(c, 63), "drawdown_from_high": (c[-1] / max(c) - 1) if c else None, "last_close": c[-1] if c else None}
    return f


def refresh_company(conn, ticker: str, meta: dict, cfg: dict, fetch: Callable = net.get_json, now=None, use_network: bool = True) -> dict:
    f: Dict[str, Optional[float]] = {}
    prof: dict = {}
    errors = []
    f.update({k: v for k, v in market_features(conn, ticker).items() if v is not None})
    if use_network:
        key = os.getenv("FINNHUB_API_KEY")
        if key:
            q = urllib.parse.quote(ticker)
            for name, url, fn in (("profile", "https://finnhub.io/api/v1/stock/profile2?symbol=%s&token=%s" % (q, key), "p"),
                                  ("metric", "https://finnhub.io/api/v1/stock/metric?symbol=%s&metric=all&token=%s" % (q, key), "m"),
                                  ("earnings", "https://finnhub.io/api/v1/stock/earnings?symbol=%s&limit=4&token=%s" % (q, key), "e")):
                try:
                    j, _ = fetch(url)
                    if fn == "p":
                        prof = parse_finnhub_profile(j)
                        f["market_cap"] = prof.get("market_cap")
                    elif fn == "m":
                        f.update({k: v for k, v in parse_finnhub_metrics(j).items() if v is not None})
                    else:
                        f["beat_rate"] = parse_finnhub_earnings(j)
                except Exception as e:  # noqa: BLE001
                    errors.append("%s:%s" % (name, type(e).__name__))
        cik = meta.get("cik")
        if cik:
            try:
                j, _ = fetch("https://data.sec.gov/api/xbrl/companyfacts/CIK%010d.json" % int(cik))
                sec = parse_sec_facts(j)
                for k, v in sec.items():
                    if v is not None and (f.get(k) is None):
                        f[k] = v
                if sec.get("revenue"):
                    conn.execute("INSERT INTO company_fundamentals(ticker,cik,revenue,operating_income,cash,debt,shares,market_cap,backlog,as_of,source_url) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                                 "ON CONFLICT(ticker) DO UPDATE SET revenue=excluded.revenue,cash=excluded.cash,debt=excluded.debt,market_cap=COALESCE(excluded.market_cap,company_fundamentals.market_cap),as_of=excluded.as_of",
                                 (ticker, str(cik), sec.get("revenue"), None, sec.get("cash"), sec.get("debt"), prof.get("shares"), f.get("market_cap"), None, iso(now), "sec-companyfacts"))
            except Exception as e:  # noqa: BLE001
                errors.append("sec:%s" % type(e).__name__)
    from . import flows
    ins = flows.insider_signal(conn, ticker)
    if ins is not None:
        f["insider"] = ins
    if conn.execute("SELECT 1 FROM capital_flows WHERE ticker=? AND kind='buyback' LIMIT 1", (ticker,)).fetchone():
        f["buyback_flag"] = 1.0
    sector = sector_of(prof.get("industry") or meta.get("industry") or meta.get("sector") or "")
    if sector == "Unclassified" and meta.get("sector") not in (None, "", "Unclassified"):
        sector = meta["sector"]
    country = prof.get("country") or meta.get("country") or "US"
    f = {k: num(v) for k, v in f.items()}                 # final safety net: scoring only ever sees finite floats or None
    s = score_company(f)
    dna = classify_dna(f, s, sector)
    conn.execute("INSERT INTO companies(ticker,name,sector,industry,country,currency,cik,market_cap,adv_usd,beta,vol_daily,dna,financial_strength,management_quality,competitive_moat,innovation_quality,institutional_support,execution_quality,completeness,profile_json,updated) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET name=excluded.name,sector=excluded.sector,industry=excluded.industry,country=excluded.country,currency=excluded.currency,"
                 "cik=excluded.cik,market_cap=excluded.market_cap,adv_usd=excluded.adv_usd,beta=excluded.beta,vol_daily=excluded.vol_daily,dna=excluded.dna,financial_strength=excluded.financial_strength,management_quality=excluded.management_quality,"
                 "competitive_moat=excluded.competitive_moat,innovation_quality=excluded.innovation_quality,institutional_support=excluded.institutional_support,execution_quality=excluded.execution_quality,completeness=excluded.completeness,profile_json=excluded.profile_json,updated=excluded.updated",
                 (ticker, prof.get("name") or meta.get("name"), sector, prof.get("industry") or meta.get("industry"), country, prof.get("currency") or COUNTRY_CURRENCY.get(country, "USD"), str(meta.get("cik") or ""),
                  f.get("market_cap"), f.get("adv_usd"), f.get("beta"), f.get("vol_daily"), dna, s["financial_strength"], s["management_quality"], s["competitive_moat"], s["innovation_quality"], s["institutional_support"], s["execution_quality"],
                  s["completeness"], json.dumps({"features": f, "errors": errors}, default=str, sort_keys=True), iso(now)))
    return {"ticker": ticker, "sector": sector, "dna": dna, "scores": s, "errors": errors}


_NUMERIC_COLS: Optional[set] = None


def get(conn, ticker: str) -> Optional[dict]:
    global _NUMERIC_COLS
    r = conn.execute("SELECT * FROM companies WHERE ticker=?", (ticker,)).fetchone()
    if not r:
        return None
    d = dict(r)
    if _NUMERIC_COLS is None:
        _NUMERIC_COLS = {x[1] for x in conn.execute("PRAGMA table_info(companies)") if str(x[2]).upper() in ("REAL", "INTEGER", "NUMERIC") and x[1] not in ("cik",)}
    for k in _NUMERIC_COLS:
        if k in d and d[k] is not None and not isinstance(d[k], (int, float)):
            d[k] = num(d[k])                               # a text value stored by an older build ("N/A") can no longer reach arithmetic
    return d
