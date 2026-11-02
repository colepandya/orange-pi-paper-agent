"""Deterministic filing intelligence: only what a filing STATES in a structured or unambiguous way. No attempt at free-text understanding. A form type, an 8-K item number, a Form 4 transaction code, an XBRL value,
or a sentence of an unmistakable fixed shape is evidence; anything else is not extracted. Every record keeps its provenance (CIK, accession, form, dates, item, XBRL tag, unit, value, source), and carries TWO times:
`filed` (when it became public) and `first_known` (when the bot retrieved it). A replay of what the bot knew uses first_known; an evaluation of what the market could have known uses filed.
CAVEAT: the XML parsers follow the published EDGAR structures but have not been run against live EDGAR from the build environment."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from . import db
from .timeutil import iso, utcnow

ARCHIVE = "https://www.sec.gov/Archives/edgar/data/%d/%s/%s"
ITEM_EVENTS = {"1.01": "material_agreement", "1.02": "agreement_termination", "1.03": "bankruptcy_or_restructuring", "1.04": "mine_safety", "1.05": "cybersecurity_incident", "2.01": "acquisition_or_disposition_completed",
               "2.02": "results_announcement", "2.03": "debt_obligation_created", "2.04": "debt_acceleration", "2.05": "exit_or_restructuring_costs", "2.06": "material_impairment", "3.01": "delisting_notice",
               "3.02": "unregistered_equity_sale", "3.03": "rights_of_holders_modified", "4.01": "auditor_change", "4.02": "non_reliance_on_financials", "5.01": "change_in_control", "5.02": "officer_or_director_change",
               "5.07": "shareholder_vote", "7.01": "regulation_fd", "8.01": "other_events"}
FORM_EVENTS = {"NT 10-K": "late_annual_report", "NT 10-Q": "late_quarterly_report", "NT 10-K/A": "late_annual_report", "NT 10-Q/A": "late_quarterly_report", "3": "insider_initial_holdings", "4": "insider_transaction", "5": "insider_annual_statement",
               "SC 13D": "activist_or_control_stake", "SC 13D/A": "activist_or_control_stake_amended", "SC 13G": "passive_5pct_stake", "SC 13G/A": "passive_5pct_stake_amended", "SCHEDULE 13D": "activist_or_control_stake",
               "SCHEDULE 13D/A": "activist_or_control_stake_amended", "SCHEDULE 13G": "passive_5pct_stake", "SCHEDULE 13G/A": "passive_5pct_stake_amended", "S-3": "shelf_registration", "S-3ASR": "shelf_registration", "S-3/A": "shelf_registration",
               "424B1": "securities_offering", "424B2": "securities_offering", "424B3": "securities_offering", "424B4": "securities_offering", "424B5": "securities_offering", "424B7": "securities_offering",
               "10-K": "annual_report", "10-K/A": "annual_report", "10-Q": "quarterly_report", "10-Q/A": "quarterly_report", "6-K": "foreign_current_report", "20-F": "foreign_annual_report", "8-K": "current_report", "8-K/A": "current_report"}
TRACKED = set(FORM_EVENTS)
INSIDER_CODES = {"P": ("insider_purchase", "open-market or private purchase", 1), "S": ("insider_sale", "open-market or private sale", -1), "A": ("insider_grant", "grant or award (not a market trade)", 0), "M": ("option_exercise", "exercise of a derivative", 0),
                 "F": ("tax_withholding", "shares withheld to pay tax (not a market sale)", 0), "G": ("gift", "gift", 0), "D": ("disposition_to_issuer", "disposition to the issuer", 0), "C": ("conversion", "conversion of a derivative", 0)}


def source_url(cik: int, accession: str, doc: Optional[str]) -> str:
    return ARCHIVE % (int(cik), (accession or "").replace("-", ""), doc or "")


def _insert(conn, **r) -> bool:
    try:
        conn.execute("INSERT INTO kb_filing_events(entity_id,cik,ticker,accession,form,filed,period,amendment,item,event_type,fact_json,xbrl_taxonomy,xbrl_tag,unit,value,source_url,first_known,retrieved,reliability) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (r.get("entity_id"), r.get("cik"), r.get("ticker"), r["accession"], r.get("form"), r.get("filed"), r.get("period"), int(bool(r.get("amendment"))), r.get("item"), r["event_type"], json.dumps(r.get("fact") or {}, sort_keys=True, default=str),
                      r.get("taxonomy"), r.get("tag"), r.get("unit"), r.get("value"), r.get("url"), r.get("first_known"), r.get("retrieved"), r.get("reliability") or "high"))
        return True
    except Exception as ex:  # noqa: BLE001
        if "UNIQUE" in str(ex):
            return False
        raise


def ingest_submissions(conn, eid: str, ticker: str, sub: dict, now=None, max_age_days: int = 120) -> dict:
    """The structured events a submissions record already states: form types and 8-K item numbers. Nothing is read from a document here."""
    now = now or utcnow()
    now_s = iso(now)
    cutoff = iso(now - __import__("datetime").timedelta(days=max_age_days))[:10]
    n = 0
    for f in sub.get("filings") or []:
        form = (f.get("form") or "").strip().upper()
        if form not in TRACKED or (f.get("filed") or "") < cutoff:
            continue
        base = dict(entity_id=eid, cik=sub["cik"], ticker=ticker, accession=f["accession"], form=form, filed=f.get("filed"), period=f.get("period"), amendment=form.endswith("/A"), first_known=now_s, retrieved=now_s,
                    url=source_url(sub["cik"], f["accession"], f.get("doc")), reliability="high")
        if form.startswith("8-K"):
            items = [i.strip() for i in (f.get("items") or "").split(",") if i.strip()]
            for it in items:
                if it in ITEM_EVENTS and it != "9.01":
                    n += _insert(conn, event_type=ITEM_EVENTS[it], item=it, fact={"basis": "8-K item number %s as stated in the filing index" % it}, **base)
            if not items:
                n += _insert(conn, event_type="current_report", item=None, fact={"basis": "8-K with no item list in the index"}, **dict(base, reliability="low"))
        else:
            n += _insert(conn, event_type=FORM_EVENTS[form], item=None, fact={"basis": "form type %s" % form}, **base)
    return {"ingested": n}


def known_events(conn, ticker: str, when, public_time: bool = False, kinds: Optional[List[str]] = None, limit: int = 100) -> List[dict]:
    """Filing events available at `when`. By default what the BOT knew (first_known); with public_time=True what was PUBLIC (filed date). Never anything later."""
    when_s = iso(when) if hasattr(when, "isoformat") else when
    col = "filed" if public_time else "first_known"
    cmp = when_s[:10] if public_time else when_s
    q = "SELECT * FROM kb_filing_events WHERE ticker=? AND %s<=? %s ORDER BY %s DESC LIMIT ?" % (col, "AND event_type IN (%s)" % ",".join("?" * len(kinds)) if kinds else "", col)
    args = [ticker, cmp] + (list(kinds) if kinds else []) + [limit]
    return [dict(r) for r in conn.execute(q, args)]


# ------------------------------------------------------------------------------------------------ Form 4
def _t(el, path: str) -> Optional[str]:
    x = el.find(path)
    return x.text.strip() if x is not None and x.text else None


def _f(s) -> Optional[float]:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_form4(xml_text: str) -> dict:
    """Transactions in a Form 4 ownershipDocument. Open-market purchases (P) and sales (S) are the insider signals; grants, option exercises and tax withholding are recorded as what they are and carry no signal.
    Returns {issuer_cik, issuer_ticker, owner, relationship, planned (10b5-1), transactions: [...], reliability} or {} if the XML cannot be read."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    if root.tag.lower().split("}")[-1] != "ownershipdocument":
        return {}
    owner = _t(root, "reportingOwner/reportingOwnerId/rptOwnerName")
    rel = root.find("reportingOwner/reportingOwnerRelationship")
    roles = [name for name, tag in (("director", "isDirector"), ("officer", "isOfficer"), ("10% owner", "isTenPercentOwner"), ("other", "isOther")) if rel is not None and (_t(rel, tag) or "").lower() in ("1", "true")]
    title = _t(rel, "officerTitle") if rel is not None else None
    planned = (_t(root, "aff10b5One") or "").lower() in ("1", "true")
    out = {"issuer_cik": _t(root, "issuer/issuerCik"), "issuer_ticker": _t(root, "issuer/issuerTradingSymbol"), "owner": owner, "relationship": roles, "officer_title": title, "planned_10b5_1": planned, "transactions": [], "reliability": "high"}
    for tbl, deriv in (("nonDerivativeTable/nonDerivativeTransaction", False), ("derivativeTable/derivativeTransaction", True)):
        for tx in root.findall(tbl):
            code = _t(tx, "transactionCoding/transactionCode")
            sh, px = _f(_t(tx, "transactionAmounts/transactionShares/value")), _f(_t(tx, "transactionAmounts/transactionPricePerShare/value"))
            ad = _t(tx, "transactionAmounts/transactionAcquiredDisposedCode/value")
            after = _f(_t(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"))
            kind = INSIDER_CODES.get(code or "", ("other_transaction", "transaction code %s" % code, 0))
            out["transactions"].append({"code": code, "kind": kind[0], "meaning": kind[1], "signal": kind[2], "derivative": deriv, "shares": sh, "price": px, "value_usd": (sh * px) if sh is not None and px is not None else None,
                                        "acquired_disposed": ad, "shares_after": after, "date": _t(tx, "transactionDate/value"), "security": _t(tx, "securityTitle/value"), "ownership": _t(tx, "ownershipNature/directOrIndirectOwnership/value"),
                                        "pct_of_holding": (sh / (after + (sh if ad == "D" else 0.0))) if sh and after is not None and (after + (sh if ad == "D" else 0.0)) > 0 else None})
    return out


def insider_events(parsed: dict) -> List[dict]:
    """The open-market insider signals in a parsed Form 4, aggregated per direction, with the size relative to the insider's own holding."""
    out = []
    for kind in ("insider_purchase", "insider_sale"):
        tx = [t for t in parsed.get("transactions", []) if t["kind"] == kind and not t["derivative"] and t["shares"]]
        if not tx:
            continue
        val = sum(t["value_usd"] or 0.0 for t in tx)
        sh = sum(t["shares"] for t in tx)
        pct = max((t["pct_of_holding"] or 0.0) for t in tx)
        out.append({"event_type": kind, "value_usd": val if val else None, "shares": sh, "pct_of_holding": pct or None, "owner": parsed.get("owner"), "relationship": parsed.get("relationship"), "planned_10b5_1": parsed.get("planned_10b5_1"),
                    "weight_note": "a planned (10b5-1) sale is a weak signal" if parsed.get("planned_10b5_1") and kind == "insider_sale" else "open-market trade by a company insider"})
    return out


# ------------------------------------------------------------------------------------------------ Schedule 13D / 13G
def parse_13dg(xml_text: str) -> dict:
    """Beneficial ownership from a Schedule 13D/G XML submission. Tag names are matched by local name wherever they sit, so a layout difference costs reliability, not correctness: reliability is 'medium'."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    local = lambda e: e.tag.split("}")[-1].lower()  # noqa: E731
    vals: Dict[str, List[str]] = {}
    for e in root.iter():
        if e.text and e.text.strip() and len(list(e)) == 0:
            vals.setdefault(local(e), []).append(e.text.strip())
    pick = lambda *names: next((vals[n][0] for n in names if n in vals), None)  # noqa: E731
    pct = _f(pick("percentofclass", "percentclass", "percentofclassrepresentedbyamountinrow11"))
    shares = _f(pick("aggregateamountowned", "aggregateamountbeneficiallyowned", "amountbeneficiallyowned"))
    if pct is None and shares is None:
        return {}
    return {"reporting_person": pick("reportingpersonname", "nameofreportingperson"), "issuer": pick("issuername", "nameofissuer"), "percent_of_class": pct, "shares": shares, "purpose_stated": pick("itemfourtext", "purposeoftransaction"),
            "reliability": "medium"}


# ------------------------------------------------------------------------------------------------ offerings and guidance (only fixed-shape sentences)
_OFFER_USD = re.compile(r"aggregate offering price of (?:up to )?\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion)?", re.I)
_OFFER_SH = re.compile(r"offering\s+([\d,]+(?:\.\d+)?)\s*(million)?\s+shares of (?:our )?(?:common|ordinary) (?:stock|shares)", re.I)
_GUIDE = re.compile(r"(?:expects?|reaffirms?|raises?|lowers?)\s+(?:full[- ]year |fiscal \d{4} |first[- ]quarter |second[- ]quarter |third[- ]quarter |fourth[- ]quarter )?(revenue|sales|net revenue)\s+(?:of|to be|in the range of|between)\s+\$\s?([\d.,]+)\s*(million|billion)?\s*(?:to|and|-)\s*\$?\s?([\d.,]+)\s*(million|billion)?", re.I)
_M = {"million": 1e6, "billion": 1e9, None: 1.0}


def parse_offering_text(text: str) -> dict:
    """Offering size from a prospectus (supplement) sentence of the fixed shape 'aggregate offering price of up to $X' or 'offering N shares of common stock'. Nothing else is inferred."""
    out = {}
    m = _OFFER_USD.search(text or "")
    if m:
        out["usd"] = float(m.group(1).replace(",", "")) * _M[(m.group(2) or "").lower() or None]
        out["usd_text"] = m.group(0)
    m = _OFFER_SH.search(text or "")
    if m:
        out["shares"] = float(m.group(1).replace(",", "")) * (1e6 if m.group(2) else 1.0)
        out["shares_text"] = m.group(0)
    return dict(out, reliability="medium") if out else {}


def parse_guidance(text: str) -> dict:
    """An explicit revenue range, e.g. 'expects full-year revenue of $4.1 billion to $4.3 billion'. A guidance statement without a stated range is not extracted."""
    m = _GUIDE.search(text or "")
    if not m:
        return {}
    try:
        lo = float(m.group(2).replace(",", "")) * _M[(m.group(3) or m.group(5) or "").lower() or None]
        hi = float(m.group(4).replace(",", "")) * _M[(m.group(5) or m.group(3) or "").lower() or None]
    except (ValueError, KeyError):
        return {}
    if hi < lo:
        lo, hi = hi, lo
    lo, hi = round(lo, 2), round(hi, 2)
    return {"metric": m.group(1).lower(), "low": lo, "high": hi, "midpoint": round((lo + hi) / 2.0, 2), "text": m.group(0)[:160], "reliability": "medium"}


# ------------------------------------------------------------------------------------------------ XBRL-derived fact changes (from consecutive stored snapshots)
FACT_CHANGES = [("revenue_ttm", "revenue_change", 0.10), ("net_income_ttm", "earnings_change", 0.25), ("op_income_ttm", "operating_income_change", 0.25), ("cash", "cash_change", 0.30), ("cfo_ttm", "operating_cash_flow_change", 0.25), ("capex_ttm", "capex_change", 0.25)]


def derive_fact_events(conn, eid: str, ticker: str, now=None) -> int:
    """Changes between the latest two financial snapshots with different periods, as filing-derived events with the accession and tag of BOTH figures. Only when both values are present and the move is large."""
    now = now or utcnow()
    snaps = conn.execute("SELECT * FROM kb_financial_snapshots WHERE entity_id=? ORDER BY first_known DESC,id DESC LIMIT 12", (eid,)).fetchall()
    if len(snaps) < 2:
        return 0
    new = snaps[0]
    old = next((s for s in snaps[1:] if s["period_end"] and s["period_end"] != new["period_end"]), None)
    if not old:
        return 0
    a, b = json.loads(new["metrics_json"] or "{}").get("reported", {}), json.loads(old["metrics_json"] or "{}").get("reported", {})
    n = 0
    for key, ev, thr in FACT_CHANGES:
        x, y = (a.get(key) or {}), (b.get(key) or {})
        if not x.get("value") or not y.get("value") or y["value"] == 0 or x.get("unit") != y.get("unit"):
            continue
        ch = (x["value"] - y["value"]) / abs(y["value"])
        if abs(ch) >= thr and (x.get("accn") or [None])[0]:
            n += _insert(conn, entity_id=eid, cik=int(eid[1:]) if eid[1:].isdigit() else None, ticker=ticker, accession=x["accn"][0], form=(x.get("form") or [None])[0], filed=x.get("filed"), period=x.get("period_end"), amendment=False,
                         item=None, event_type=ev, fact={"change": round(ch, 4), "from": y["value"], "from_period": y.get("period_end"), "to": x["value"], "to_period": x.get("period_end"), "from_accessions": y.get("accn"), "basis": x.get("basis")},
                         taxonomy=x.get("taxonomy"), tag=x.get("tag"), unit=x.get("unit"), value=x["value"], url=None, first_known=iso(now), retrieved=iso(now), reliability="high")
    sg = (json.loads(new["metrics_json"] or "{}").get("reported", {}).get("share_growth") or {})
    if sg.get("value") is not None and abs(sg["value"]) >= 0.02 and (sg.get("accn") or [None])[0]:
        et = "dilution_or_share_issuance" if sg["value"] > 0 else "net_buyback"
        n += _insert(conn, entity_id=eid, cik=None, ticker=ticker, accession=sg["accn"][0], form=(sg.get("form") or [None])[0], filed=sg.get("filed"), period=sg.get("period_end"), amendment=False, item=None, event_type=et,
                     fact={"share_count_change": round(sg["value"], 4), "basis": sg.get("basis")}, taxonomy=sg.get("taxonomy"), tag=sg.get("tag"), unit="shares", value=sg["value"], url=None, first_known=iso(now), retrieved=iso(now), reliability="high")
    return n
