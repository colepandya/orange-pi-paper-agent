"""Company financial facts from SEC's free structured data (XBRL), read deterministically. This replaces the LLM that used to read filings: the numbers a company reports to the SEC are already structured, so
nothing needs to be "understood", only extracted, put on a comparable footing, and checked against plain rules.

THE RULE THAT MATTERS: a trailing-twelve-month figure is built only when a RELIABLE construction exists, otherwise the metric is reported as UNAVAILABLE, with the reason. It never quietly falls back to an old annual
figure. Reliable means one of:
  * annual + this year's year-to-date - the same year-to-date a year earlier, where the periods line up (this year's year-to-date starts where the annual ended, last year's starts where the annual started, both
    cover the same length, the gap between them is a year give or take a 52/53-week year), in one unit, with no conflicting duplicates
  * four contiguous quarters (the fourth derived from the annual figure where a 10-K never reports it)
  * the latest annual figure, but ONLY when it is recent (no newer quarterly data exists and it ended within about five months)
Every metric carries its own freshness: the period it covers, the filing date, the retrieval time and its age. The valuation also carries the market-value timestamp and refuses a stale one.
Discontinued operations, restated (recast) comparatives, amendments, duplicate contexts and a change of reporting currency or fiscal year-end are handled, and malformed data is skipped, never fatal.
SEC asks for a contact in the User-Agent: set runtime.contact_email in Settings."""
from __future__ import annotations

import datetime as dt
import json
import math
from typing import Callable, Dict, List, Optional, Tuple

from . import db
from .timeutil import iso, parse_ts, utcnow

BASE = "https://data.sec.gov/api/xbrl/companyconcept/CIK%010d/%s/%s.json"
CONCEPTS: Dict[str, Tuple[str, List[str]]] = {
    "revenue": ("us-gaap", ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet"]),
    "gross_profit": ("us-gaap", ["GrossProfit"]),
    "net_income": ("us-gaap", ["NetIncomeLoss", "ProfitLoss"]),
    "net_income_cont": ("us-gaap", ["IncomeLossFromContinuingOperations"]),
    "disc_ops": ("us-gaap", ["IncomeLossFromDiscontinuedOperationsNetOfTax"]),
    "op_income": ("us-gaap", ["OperatingIncomeLoss"]),
    "cfo": ("us-gaap", ["NetCashProvidedByUsedInOperatingActivities"]),
    "capex": ("us-gaap", ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"]),
    "interest": ("us-gaap", ["InterestExpense", "InterestExpenseDebt"]),
    "cash": ("us-gaap", ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsAndShortTermInvestments"]),
    "debt_lt": ("us-gaap", ["LongTermDebtNoncurrent", "LongTermDebt"]),
    "debt_st": ("us-gaap", ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings"]),
    "assets_current": ("us-gaap", ["AssetsCurrent"]),
    "liab_current": ("us-gaap", ["LiabilitiesCurrent"]),
    "equity": ("us-gaap", ["StockholdersEquity"]),
    "shares": ("us-gaap", ["WeightedAverageNumberOfDilutedSharesOutstanding"]),
}
RECENT_DAYS = 240                  # a tag whose latest figure is older than this is probably one the company has stopped using
STALE_DAYS = 420                   # the whole record older than this is not used for judgement
FRESH_ANNUAL_DAYS = 150            # an annual figure alone is a valid twelve months only this soon after the year ended
MAX_PERIOD_AGE_DAYS = 270          # no metric is reported if the period it covers ended longer ago than this
MAX_PRICE_AGE_DAYS = 5             # a market value older than this is not used for a valuation
REFRESH_DAYS = 7
D = dt.timedelta


def _today(now) -> dt.date:
    return now.date() if hasattr(now, "date") else now


def _date(s) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _points(j) -> List[dict]:
    """The facts of one concept, cleaned: only rows with a real end date and number survive. If the company reports in more than one unit (it changed reporting currency) only the unit that still has the NEWEST data
    is kept and the points are marked `multi_unit`, so a twelve-month figure that would need the old unit is correctly unavailable instead of mixing currencies."""
    units = (j or {}).get("units") if isinstance(j, dict) else None
    if not isinstance(units, dict):
        return []
    by_unit: Dict[str, List[dict]] = {}
    for u, rows in units.items():
        if not isinstance(rows, list):
            continue
        pts = []
        for p in rows[:3000]:
            if not isinstance(p, dict):
                continue
            end, val = _date(p.get("end")), _num(p.get("val"))
            if end is None or val is None:
                continue
            pts.append({"start": _date(p.get("start")), "end": end, "val": val, "filed": str(p.get("filed") or ""), "form": str(p.get("form") or ""), "accn": str(p.get("accn") or ""), "unit": str(u)})
        if pts:
            by_unit[str(u)] = pts
    if not by_unit:
        return []
    best = max(by_unit, key=lambda u: (max(p["end"] for p in by_unit[u]), u == "USD"))
    out = by_unit[best]
    newest = max(p["end"] for p in out)
    others = sorted(u for u in by_unit if u != best and max(p["end"] for p in by_unit[u]) >= newest - D(days=400))     # another currency with data in the SAME window is ambiguous; one that stopped long ago is irrelevant
    if others:
        for p in out:
            p["multi_unit"] = others
    return out


def _latest_per_period(pts: List[dict]) -> List[dict]:
    """One value per period: the most recently FILED wins (a restatement or recast replaces the original). Two DIFFERENT values for the same period filed on the same day are a conflict and are marked as one."""
    best: Dict[Tuple, dict] = {}
    for p in pts:
        k = (p["start"], p["end"])
        cur = best.get(k)
        if cur is None or p["filed"] > cur["filed"]:
            best[k] = dict(p, conflict=False)
        elif p["filed"] == cur["filed"] and abs(p["val"] - cur["val"]) > 1e-9 * max(1.0, abs(cur["val"])):
            cur["conflict"] = True
    return sorted(best.values(), key=lambda p: (p["end"], p["start"] or dt.date.min))


def _days(p: dict) -> Optional[int]:
    return (p["end"] - p["start"]).days if p.get("start") else None


def annuals(pts: List[dict]) -> List[dict]:
    return [p for p in _latest_per_period(pts) if _days(p) is not None and 350 <= _days(p) <= 380]            # 52 and 53 week years are 364 and 371 days


def quarters(pts: List[dict]) -> List[dict]:
    """Discrete quarters (13 or 14 weeks included), plus the FOURTH quarter derived (annual minus the three before it) where all are known, since a 10-K never reports it on its own."""
    q = [p for p in _latest_per_period(pts) if _days(p) is not None and 80 <= _days(p) <= 100]
    for a in annuals(pts):
        inside = sorted([p for p in q if p["start"] >= a["start"] - D(days=4) and p["end"] < a["end"] - D(days=20)], key=lambda p: p["end"])
        ends = [p["end"] for p in inside]
        contiguous = len(inside) == 3 and all(abs((inside[i + 1]["start"] - inside[i]["end"]).days - 1) <= 8 for i in range(2))
        if contiguous and 60 <= (a["end"] - ends[-1]).days <= 125 and not any(p["end"] == a["end"] for p in q) and not any(p.get("conflict") for p in inside + [a]):
            q.append({"start": ends[-1] + D(days=1), "end": a["end"], "val": a["val"] - sum(p["val"] for p in inside), "filed": a["filed"], "form": "derived", "accn": "", "derived": True, "unit": a.get("unit"), "conflict": False})
    return sorted(q, key=lambda p: p["end"])


def _ytd_pair(pts: List[dict]) -> Tuple[Optional[Tuple[dict, dict, dict]], str]:
    """(latest annual, this year's cumulative figure, the same cumulative figure a year earlier) when the periods genuinely line up; else (None, why not)."""
    a = annuals(pts)
    if not a:
        return None, "no annual figure"
    A = a[-1]
    lp = _latest_per_period(pts)
    cur = [p for p in lp if p.get("start") and abs((p["start"] - (A["end"] + D(days=1))).days) <= 12 and p["end"] > A["end"] + D(days=60) and 60 <= _days(p) <= 300]
    if not cur:
        return None, "no year-to-date figure that starts where the last annual ended"
    Y = max(cur, key=lambda p: p["end"])
    prev = [p for p in lp if p.get("start") and abs((p["start"] - A["start"]).days) <= 20 and abs(_days(p) - _days(Y)) <= 12 and 350 <= (Y["end"] - p["end"]).days <= 380]
    if not prev:
        also = sorted({u for x in (A, Y) for u in (x.get("multi_unit") or [])})
        return None, "no prior-year year-to-date figure for the same period" + ((" (the company also reports in %s: the reporting currency may have changed)" % ", ".join(also)) if also else "")
    P = prev[-1]
    for x in (A, Y, P):
        if x.get("conflict"):
            return None, "conflicting values were filed for the same period"
    if len({A.get("unit"), Y.get("unit"), P.get("unit")}) > 1 or any(x.get("multi_unit") for x in (A, Y, P)):
        return None, "the reporting unit or currency changed between the periods needed"
    return (A, Y, P), ""


def _four_quarters(pts: List[dict]) -> Tuple[Optional[List[dict]], str]:
    q = quarters(pts)
    if len(q) < 4:
        return None, "fewer than four usable quarters"
    last4 = q[-4:]
    if any(abs((last4[i + 1]["start"] - last4[i]["end"]).days - 1) > 8 for i in range(3)):
        return None, "the last four quarters are not contiguous (a gap or a change of fiscal year-end)"
    span = (last4[-1]["end"] - last4[0]["start"]).days
    if not 340 <= span <= 385:
        return None, "the last four quarters do not span a year (%d days)" % span
    newer = [p for p in _latest_per_period(pts) if p["end"] > last4[-1]["end"] + D(days=30)]
    if newer:
        return None, "newer data exists than the last full set of quarters"
    if any(x.get("conflict") for x in last4) or len({x.get("unit") for x in last4}) > 1 or any(x.get("multi_unit") for x in last4):
        return None, "conflicting duplicates or a change of unit among the quarters"
    return last4, ""


def ttm_detail(pts: List[dict], now) -> dict:
    """The trailing twelve months, with its provenance, or {unavailable: why}. NEVER an old annual figure standing in for a recent period."""
    today = _today(now)
    if not pts:
        return {"unavailable": "no data"}
    why = []
    pair, w = _ytd_pair(pts)
    if pair:
        A, Y, P = pair
        v, end, basis, filed = A["val"] + Y["val"] - P["val"], Y["end"], "annual + year-to-date - prior year-to-date", max(A["filed"], Y["filed"], P["filed"])
        unit = A.get("unit")
    else:
        why.append(w)
        q4, w2 = _four_quarters(pts)
        if q4:
            v, end, basis, filed, unit = sum(p["val"] for p in q4), q4[-1]["end"], "last four contiguous quarters", max(p["filed"] for p in q4), q4[-1].get("unit")
        else:
            why.append(w2)
            a = annuals(pts)
            if not a:
                return {"unavailable": "; ".join(x for x in why if x)}
            A = a[-1]
            newer = [p for p in _latest_per_period(pts) if p["end"] > A["end"] + D(days=60)]
            if newer:
                return {"unavailable": "newer quarterly data exists but a reliable twelve-month figure cannot be built from it (%s); the latest annual (ended %s) is NOT used in its place" % ("; ".join(x for x in why if x), A["end"].isoformat())}
            if (today - A["end"]).days > FRESH_ANNUAL_DAYS:
                return {"unavailable": "the latest annual figure ended %d days ago and nothing newer is usable (%s)" % ((today - A["end"]).days, "; ".join(x for x in why if x))}
            if A.get("conflict") or A.get("multi_unit"):
                return {"unavailable": "the annual figure has conflicting duplicates or a changed unit"}
            v, end, basis, filed, unit = A["val"], A["end"], "latest annual (recent, nothing newer)", A["filed"], A.get("unit")
    age = (today - end).days
    if age > MAX_PERIOD_AGE_DAYS:
        return {"unavailable": "the period ended %d days ago" % age}
    return {"value": v, "period_end": end.isoformat(), "filed": filed, "basis": basis, "unit": unit, "age_days": age}


def growth_detail(pts: List[dict], now) -> dict:
    """The latest period against the same period a year earlier, on a like-for-like footing, or unavailable."""
    today = _today(now)
    pair, w = _ytd_pair(pts)
    if pair and pair[2]["val"]:
        A, Y, P = pair
        return {"value": Y["val"] / P["val"] - 1.0, "period_end": Y["end"].isoformat(), "filed": max(Y["filed"], P["filed"]), "basis": "year to date against the same period last year", "age_days": (today - Y["end"]).days}
    q = quarters(pts)
    if q:
        cur = q[-1]
        prev = [p for p in q if 345 <= (cur["end"] - p["end"]).days <= 385 and abs(_days(p) - _days(cur)) <= 12]
        if prev and prev[-1]["val"] and not cur.get("conflict") and not prev[-1].get("conflict") and (today - cur["end"]).days <= MAX_PERIOD_AGE_DAYS:
            return {"value": cur["val"] / prev[-1]["val"] - 1.0, "period_end": cur["end"].isoformat(), "filed": max(cur["filed"], prev[-1]["filed"]), "basis": "quarter against the same quarter a year earlier", "age_days": (today - cur["end"]).days}
    a = annuals(pts)
    if len(a) >= 2 and a[-2]["val"] and 350 <= (a[-1]["end"] - a[-2]["end"]).days <= 380 and (today - a[-1]["end"]).days <= FRESH_ANNUAL_DAYS and not [p for p in _latest_per_period(pts) if p["end"] > a[-1]["end"] + D(days=60)]:
        return {"value": a[-1]["val"] / a[-2]["val"] - 1.0, "period_end": a[-1]["end"].isoformat(), "filed": max(a[-1]["filed"], a[-2]["filed"]), "basis": "year against the year before (recent)", "age_days": (today - a[-1]["end"]).days}
    return {"unavailable": "no like-for-like comparison with a year earlier can be built from recent data"}


def stock_detail(pts: List[dict], now) -> dict:
    """The latest balance-sheet (instant) figure, if it is recent enough."""
    inst = [p for p in _latest_per_period(pts) if p.get("start") is None]
    if not inst:
        return {"unavailable": "no balance-sheet figure"}
    p = inst[-1]
    age = (_today(now) - p["end"]).days
    if age > MAX_PERIOD_AGE_DAYS:
        return {"unavailable": "the latest balance-sheet figure is %d days old" % age}
    if p.get("conflict"):
        return {"unavailable": "conflicting values for the same date"}
    return {"value": p["val"], "period_end": p["end"].isoformat(), "filed": p["filed"], "basis": "balance sheet", "age_days": age, "unit": p.get("unit")}


def ttm(pts: List[dict], now=None) -> Optional[Tuple[float, dt.date, str]]:
    """Compatibility: (value, period end, basis) or None."""
    d = ttm_detail(pts, now if now is not None else (max(p["end"] for p in pts) if pts else dt.date.today()))
    return (d["value"], _date(d["period_end"]), d["basis"]) if "value" in d else None


def growth(pts: List[dict], now=None) -> Optional[Tuple[float, str]]:
    d = growth_detail(pts, now if now is not None else (max(p["end"] for p in pts) if pts else dt.date.today()))
    return (d["value"], d["basis"]) if "value" in d else None


def growth_pair_margin(gp_pts: List[dict], rev_pts: List[dict], now) -> Optional[float]:
    """Gross margin now minus gross margin a year earlier, like for like (year to date, else a recent annual)."""
    pg, _ = _ytd_pair(gp_pts)
    pr, _ = _ytd_pair(rev_pts)
    if pg and pr and pr[1]["val"] and pr[2]["val"] and pg[1]["end"] == pr[1]["end"] and pg[2]["end"] == pr[2]["end"]:
        return pg[1]["val"] / pr[1]["val"] - pg[2]["val"] / pr[2]["val"]
    ga, ra = annuals(gp_pts), annuals(rev_pts)
    if len(ga) >= 2 and len(ra) >= 2 and ra[-1]["val"] and ra[-2]["val"] and ga[-1]["end"] == ra[-1]["end"] and ga[-2]["end"] == ra[-2]["end"] and (_today(now) - ra[-1]["end"]).days <= FRESH_ANNUAL_DAYS \
            and not [p for p in _latest_per_period(rev_pts) if p["end"] > ra[-1]["end"] + D(days=60)]:
        return ga[-1]["val"] / ra[-1]["val"] - ga[-2]["val"] / ra[-2]["val"]
    return None


def summarize(raw: Dict[str, List[dict]], now) -> dict:
    """The facts and the flags from parsed SEC points. Pure (no network) and tolerant: a concept that cannot be read is recorded and skipped. `metrics` holds every figure WITH its freshness; `unavailable` says
    which metrics could not be reliably built and why."""
    def norm(v):
        if isinstance(v, list) and v and all(isinstance(p, dict) and isinstance(p.get("end"), dt.date) for p in v):
            return v                                                                                       # already cleaned
        return _points({"units": {"USD": v if isinstance(v, list) else []}})                              # anything else is cleaned, row by row
    raw = {k: norm(v) for k, v in (raw if isinstance(raw, dict) else {}).items()}
    out: dict = {"asof": None, "basis": {}, "flags": [], "have": sorted(k for k, v in raw.items() if v), "errors": [], "metrics": {}, "unavailable": {}, "retrieved": iso(now) if hasattr(now, "isoformat") else None}
    today = _today(now)
    M, U = out["metrics"], out["unavailable"]

    def take(name, detail, flat=None, pct=False):
        if "value" in detail:
            M[name] = {k: detail[k] for k in ("value", "period_end", "filed", "basis", "age_days") if k in detail}
            M[name]["retrieved"] = out["retrieved"]
            if flat:
                out[flat] = detail["value"]
            return detail["value"]
        U[name] = detail.get("unavailable", "unavailable")
        return None

    def safe(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - one unreadable concept must not lose the rest
            out["errors"].append("%s: %s" % (name, type(e).__name__))
            return {"unavailable": "could not be read (%s)" % type(e).__name__}
    rev = take("revenue_ttm", safe("revenue", lambda: ttm_detail(raw.get("revenue", []), now)), "revenue_ttm")
    if "revenue_ttm" in M:
        out["basis"]["revenue"] = M["revenue_ttm"]["basis"]
    ni = take("net_income_ttm", safe("net_income", lambda: ttm_detail(raw.get("net_income", []), now)), "net_income_ttm")
    nic = take("net_income_continuing_ttm", safe("net_income_cont", lambda: ttm_detail(raw.get("net_income_cont", []), now)), "net_income_continuing_ttm") if raw.get("net_income_cont") else None
    disc = take("discontinued_ops_ttm", safe("disc_ops", lambda: ttm_detail(raw.get("disc_ops", []), now)), "discontinued_ops_ttm") if raw.get("disc_ops") else None
    oi = take("op_income_ttm", safe("op_income", lambda: ttm_detail(raw.get("op_income", []), now)), "op_income_ttm")
    gp = take("gross_profit_ttm", safe("gross_profit", lambda: ttm_detail(raw.get("gross_profit", []), now)), "gross_profit_ttm") if raw.get("gross_profit") else None
    cfo = take("cfo_ttm", safe("cfo", lambda: ttm_detail(raw.get("cfo", []), now)), "cfo_ttm")
    cap = take("capex_ttm", safe("capex", lambda: ttm_detail(raw.get("capex", []), now)), "capex_ttm") if raw.get("capex") else None
    intr = take("interest_ttm", safe("interest", lambda: ttm_detail(raw.get("interest", []), now)), "interest_ttm") if raw.get("interest") else None
    g = take("revenue_growth", safe("growth", lambda: growth_detail(raw.get("revenue", []), now)), "revenue_growth")
    if g is not None:
        out["revenue_growth"] = round(g, 4)
        out["basis"]["revenue_growth"] = M["revenue_growth"]["basis"]
    ends = [m["period_end"] for k, m in M.items() if k in ("revenue_ttm", "net_income_ttm", "cfo_ttm")]
    out["asof"] = max(ends) if ends else None
    latest = max([p["end"] for k in ("revenue", "net_income", "cash", "equity", "cfo") for p in raw.get(k, [])] or [None]) if any(raw.get(k) for k in ("revenue", "net_income", "cash", "equity", "cfo")) else None
    out["stale"] = bool((latest and (today - latest).days > STALE_DAYS) or (not M and any(raw.values())))
    if rev and rev > 0:
        for key, v in (("net_margin", ni), ("op_margin", oi), ("gross_margin", gp)):
            if v is not None:
                out[key] = round(v / rev, 4)
    gm = safe("margin", lambda: {"value": growth_pair_margin(raw.get("gross_profit", []), raw.get("revenue", []), now)})
    if gm.get("value") is not None:
        out["gross_margin_change"] = round(gm["value"], 4)
    if cfo is not None and cap is not None:
        out["fcf_ttm"] = cfo - abs(cap)
    cash = take("cash", safe("cash", lambda: stock_detail(raw.get("cash", []), now)), "cash")
    dlt = take("debt_lt", safe("debt_lt", lambda: stock_detail(raw.get("debt_lt", []), now)), None) if raw.get("debt_lt") else None
    dst = take("debt_st", safe("debt_st", lambda: stock_detail(raw.get("debt_st", []), now)), None) if raw.get("debt_st") else None
    if dlt is not None or dst is not None:
        out["debt"] = (dlt or 0.0) + (dst or 0.0)
    if cash is not None and "debt" in out:
        out["net_debt"] = out["debt"] - cash
    ac = take("assets_current", safe("ac", lambda: stock_detail(raw.get("assets_current", []), now)), None) if raw.get("assets_current") else None
    lc = take("liab_current", safe("lc", lambda: stock_detail(raw.get("liab_current", []), now)), None) if raw.get("liab_current") else None
    eq = take("equity", safe("equity", lambda: stock_detail(raw.get("equity", []), now)), "equity") if raw.get("equity") else None
    if ac is not None and lc and lc > 0:
        out["current_ratio"] = round(ac / lc, 2)
    if intr and intr > 0 and oi is not None:
        out["interest_cover"] = round(oi / abs(intr), 2)
    sq = quarters(raw.get("shares", []))
    if sq and (today - sq[-1]["end"]).days <= MAX_PERIOD_AGE_DAYS:
        prev = [p for p in sq if 345 <= (sq[-1]["end"] - p["end"]).days <= 385]
        if prev and prev[-1]["val"] > 0 and not sq[-1].get("multi_unit"):
            out["share_growth"] = round(sq[-1]["val"] / prev[-1]["val"] - 1.0, 4)
            M["share_growth"] = {"value": out["share_growth"], "period_end": sq[-1]["end"].isoformat(), "filed": sq[-1]["filed"], "basis": "diluted shares, quarter against the same quarter a year earlier", "age_days": (today - sq[-1]["end"]).days, "retrieved": out["retrieved"]}
    if "share_growth" not in out and raw.get("shares"):
        U["share_growth"] = "no like-for-like share count a year earlier"
    if disc is not None and ni is not None and abs(disc) > 0.25 * max(abs(ni), 1.0):
        out["discontinued_note"] = "net income includes discontinued operations of $%.0fM; continuing operations are used for the loss test where available" % (disc / 1e6)
    F = out["flags"]

    def src(*keys):
        ms = [M[k] for k in keys if k in M]
        return {"metrics": [k for k in keys if k in M], "period_end": min((m["period_end"] for m in ms), default=None), "filed": max((m["filed"] for m in ms), default=None)}

    def flag(name, text, sev, *keys):
        F.append(dict({"flag": name, "text": text, "severity": round(max(0.0, min(0.7, sev)), 2)}, **src(*keys)))
    rg = out.get("revenue_growth")
    if rg is not None and rg < -0.05:
        flag("shrinking_revenue", "revenue is down %.0f%% (%s)" % (-rg * 100, out["basis"].get("revenue_growth", "")), 0.2 - rg, "revenue_growth")
    if rg is not None and rg > 0.15:
        flag("fast_growth", "revenue is up %.0f%%" % (rg * 100), 0.0, "revenue_growth")
    loss_basis = nic if nic is not None else ni
    if loss_basis is not None and loss_basis < 0:
        flag("loss_making", "it lost money over the last twelve months%s" % (" (continuing operations)" if nic is not None else ""), 0.3, "net_income_continuing_ttm" if nic is not None else "net_income_ttm")
    if out.get("debt") and out.get("net_debt") is not None and out["net_debt"] > 0 and out["debt"] > 2 * max(out.get("cash") or 0, 1):
        flag("debt_above_cash", "debt of $%.1fB is more than twice its cash" % (out["debt"] / 1e9), 0.35, "debt_lt", "debt_st", "cash")
    if out.get("share_growth") is not None and out["share_growth"] > 0.05:
        flag("dilution", "the share count grew %.0f%% in a year" % (out["share_growth"] * 100), 0.2 + out["share_growth"] * 2, "share_growth")
    if out.get("fcf_ttm") is not None and out["fcf_ttm"] < 0 and (loss_basis or 0) < 0:
        flag("burning_cash", "it spent more cash than it made over the last twelve months", 0.4, "cfo_ttm", "capex_ttm")
    if out.get("current_ratio") is not None and out["current_ratio"] < 1.0:
        flag("low_liquidity", "short-term liabilities exceed short-term assets (current ratio %.2f)" % out["current_ratio"], 0.45 if out["current_ratio"] < 0.7 else 0.3, "assets_current", "liab_current")
    if out.get("interest_cover") is not None and out["interest_cover"] < 2.0:
        flag("thin_interest_cover", "operating profit covers interest only %.1f times" % out["interest_cover"], 0.55 if out["interest_cover"] < 1.0 else 0.4, "op_income_ttm", "interest_ttm")
    if out.get("equity") is not None and out["equity"] < 0:
        flag("negative_equity", "liabilities exceed assets (negative shareholders' equity)", 0.35, "equity")
    if out.get("gross_margin_change") is not None and out["gross_margin_change"] < -0.03:
        flag("margin_squeeze", "gross margin fell %.1f points on a year earlier" % (-out["gross_margin_change"] * 100), 0.3, "gross_profit_ttm", "revenue_ttm")
    if cfo is not None and cfo < 0 and (loss_basis or 0) > 0:
        flag("earnings_quality", "it reports a profit but its operations used cash", 0.35, "cfo_ttm", "net_income_ttm")
    if out.get("net_debt") is not None and out["net_debt"] > 0 and (oi or 0) > 0 and out["net_debt"] > 5 * oi:
        flag("high_leverage", "net debt is %.1f times operating profit" % (out["net_debt"] / oi), 0.35, "op_income_ttm", "debt_lt", "cash")
    if out.get("net_debt") is not None and out["net_debt"] < 0 and (loss_basis or 0) > 0:
        flag("strong_balance_sheet", "profitable, with more cash than debt", 0.0, "net_income_ttm", "cash")
    return out


def valuation(facts: Optional[dict], market_cap, price_ts=None, now=None) -> Optional[Tuple[str, str, dict]]:
    """A rough 'cheap / fair / rich' from market value against reported sales and profit, with the timestamps of BOTH sides. Refuses a stale market value, stale facts, or numbers it cannot compare. Informational only."""
    mc = _num(market_cap)
    if not facts or not mc or mc <= 0 or facts.get("stale"):
        return None
    now = now or utcnow()
    pts = parse_ts(price_ts) if price_ts else None
    if pts is None or (now - pts).total_seconds() > MAX_PRICE_AGE_DAYS * 86400:
        return None                                                                                        # no timestamp, or a market value too old to compare with recent results
    M = facts.get("metrics") or {}
    rev, ni = (M.get("revenue_ttm") or {}).get("value"), (M.get("net_income_ttm") or {}).get("value")
    ps = mc / rev if rev else None
    pe = mc / ni if (ni or 0) > 0 else None
    g = facts.get("revenue_growth") or 0.0
    detail = {"market_cap": mc, "market_value_as_of": iso(pts), "market_value_age_days": round((now - pts).total_seconds() / 86400.0, 1), "revenue_period_end": (M.get("revenue_ttm") or {}).get("period_end"),
              "revenue_filed": (M.get("revenue_ttm") or {}).get("filed"), "net_income_period_end": (M.get("net_income_ttm") or {}).get("period_end"), "facts_retrieved": facts.get("retrieved") or facts.get("fetched")}
    if pe is not None and pe > 70 + 150 * max(0.0, g) or (ps is not None and ps > 18 + 40 * max(0.0, g) and pe is None):
        return "rich", "valued at %s" % (("%.0f times earnings" % pe) if pe else ("%.0f times sales" % ps)), detail
    if pe is not None and pe < 12 and g > -0.05:
        return "cheap", "valued at %.0f times earnings" % pe, detail
    if pe is not None or ps is not None:
        return "fair", "valued at %s" % (("%.0f times earnings" % pe) if pe else ("%.1f times sales" % ps)), detail
    return None


def fetch_concept(fetch_json: Callable, cik: int, tax: str, names: List[str], headers: dict, sleep: Callable, now=None) -> List[dict]:
    """The best tag for this concept: the one with the most RECENT data (a company that moved to a new revenue tag leaves old data under the old name). A refusal (403, 429) is raised, not swallowed."""
    from . import net
    today = _today(now) if now is not None else utcnow().date()
    best: List[dict] = []
    best_end: Optional[dt.date] = None
    for n in names:
        try:
            j, _ = fetch_json(BASE % (int(cik), tax, n), headers=headers, timeout=20)
        except net.FetchError as e:
            if getattr(e, "code", None) != 404:
                raise
            sleep(0.12)
            continue
        pts = _points(j)
        if pts:
            e_end = max(p["end"] for p in pts)
            if best_end is None or e_end > best_end:
                best, best_end = pts, e_end
            if (today - best_end).days <= RECENT_DAYS:
                break
        sleep(0.12)
    return best


def refresh(conn, cfg: dict, ticker: str, cik, fetch_json: Callable, now, sleep: Callable = lambda s: None) -> dict:
    if not cik:
        return {"skipped": "no CIK for %s" % ticker}
    contact = cfg.get("runtime", {}).get("contact_email") or ""
    hdr = {"User-Agent": "paper-agent research %s" % (contact or "contact-not-set@example.com"), "Accept": "application/json"}
    raw = {k: fetch_concept(fetch_json, cik, tax, names, hdr, sleep, now) for k, (tax, names) in CONCEPTS.items()}
    facts = summarize(raw, now)
    facts["ticker"], facts["fetched"] = ticker, iso(now)
    with db.tx(conn):
        conn.execute("INSERT OR REPLACE INTO company_facts(ticker,facts_json,updated) VALUES(?,?,?)", (ticker, json.dumps(facts, sort_keys=True, default=str), iso(now)))
    return {"ok": True, "have": facts["have"], "flags": [f["flag"] for f in facts["flags"]], "asof": facts["asof"], "unavailable": sorted(facts["unavailable"]), "errors": facts["errors"]}


def get(conn, ticker: str) -> Optional[dict]:
    try:
        r = conn.execute("SELECT facts_json FROM company_facts WHERE ticker=?", (ticker,)).fetchone()
        return json.loads(r[0]) if r else None
    except (ValueError, TypeError):
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
