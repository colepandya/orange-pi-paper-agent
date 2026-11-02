"""Deterministic classification data for the company knowledge base. Public SIC definitions mapped to sector and industry, and industry-level traits.
Everything derived from these tables is stored as INFERRED with low confidence and the rule id, never as a verified fact about a company. No model, no network."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

SIC_MAP_VERSION = "sic-2026-11"
TRAITS_VERSION = "traits-2026-11"

# two-digit SIC major group -> (sector, industry). Four-digit codes below override. Public SEC SIC division/major-group structure; the sector labels are GICS-style names chosen for readability.
SIC2: Dict[str, Tuple[str, str]] = {
    "01": ("Materials", "Agriculture"), "02": ("Materials", "Agriculture"), "07": ("Materials", "Agriculture"), "08": ("Materials", "Forestry"), "09": ("Consumer Staples", "Fishing"),
    "10": ("Materials", "Metals & Mining"), "12": ("Energy", "Coal"), "13": ("Energy", "Oil & Gas Exploration & Production"), "14": ("Materials", "Nonmetallic Minerals Mining"),
    "15": ("Industrials", "Construction & Engineering"), "16": ("Industrials", "Construction & Engineering"), "17": ("Industrials", "Construction & Engineering"),
    "20": ("Consumer Staples", "Food Products"), "21": ("Consumer Staples", "Tobacco"), "22": ("Consumer Discretionary", "Textiles"), "23": ("Consumer Discretionary", "Apparel"),
    "24": ("Materials", "Wood Products"), "25": ("Consumer Discretionary", "Household Durables"), "26": ("Materials", "Paper & Packaging"), "27": ("Communication Services", "Publishing"),
    "28": ("Materials", "Chemicals"), "29": ("Energy", "Oil Refining & Marketing"), "30": ("Materials", "Rubber & Plastics"), "31": ("Consumer Discretionary", "Leather Goods"),
    "32": ("Materials", "Building Materials"), "33": ("Materials", "Steel & Primary Metals"), "34": ("Industrials", "Metal Fabrication"), "35": ("Industrials", "Industrial Machinery"),
    "36": ("Industrials", "Electrical Equipment"), "37": ("Industrials", "Transportation Equipment"), "38": ("Health Care", "Instruments & Controls"), "39": ("Consumer Discretionary", "Miscellaneous Manufacturing"),
    "40": ("Industrials", "Railroads"), "41": ("Industrials", "Passenger Transit"), "42": ("Industrials", "Trucking & Warehousing"), "44": ("Industrials", "Marine Transportation"), "45": ("Industrials", "Airlines"),
    "46": ("Energy", "Pipelines"), "47": ("Industrials", "Transportation Services"), "48": ("Communication Services", "Communications"), "49": ("Utilities", "Utilities"),
    "50": ("Industrials", "Wholesale Distribution"), "51": ("Industrials", "Wholesale Distribution"), "52": ("Consumer Discretionary", "Retail"), "53": ("Consumer Discretionary", "Retail"),
    "54": ("Consumer Staples", "Food & Staples Retail"), "55": ("Consumer Discretionary", "Auto Dealers & Gas Stations"), "56": ("Consumer Discretionary", "Apparel Retail"), "57": ("Consumer Discretionary", "Home Furnishing Retail"),
    "58": ("Consumer Discretionary", "Restaurants"), "59": ("Consumer Discretionary", "Specialty Retail"),
    "60": ("Financials", "Banks"), "61": ("Financials", "Consumer & Specialty Finance"), "62": ("Financials", "Capital Markets"), "63": ("Financials", "Insurance"), "64": ("Financials", "Insurance Brokers"),
    "65": ("Real Estate", "Real Estate"), "67": ("Financials", "Holding & Investment Offices"),
    "70": ("Consumer Discretionary", "Hotels & Lodging"), "72": ("Consumer Discretionary", "Personal Services"), "73": ("Industrials", "Business Services"), "75": ("Consumer Discretionary", "Auto Services"),
    "78": ("Communication Services", "Entertainment"), "79": ("Consumer Discretionary", "Leisure & Recreation"), "80": ("Health Care", "Health Care Services"), "81": ("Industrials", "Legal Services"),
    "82": ("Consumer Discretionary", "Education"), "83": ("Health Care", "Social Services"), "87": ("Industrials", "Professional & Engineering Services"), "89": ("Industrials", "Miscellaneous Services"),
    "99": ("Unclassified", "Nonclassifiable"),
}
SIC4: Dict[str, Tuple[str, str]] = {
    "2833": ("Health Care", "Pharmaceuticals"), "2834": ("Health Care", "Pharmaceuticals"), "2835": ("Health Care", "Biotechnology"), "2836": ("Health Care", "Biotechnology"),
    "3570": ("Information Technology", "Computer Hardware"), "3571": ("Information Technology", "Computer Hardware"), "3572": ("Information Technology", "Computer Storage"), "3576": ("Information Technology", "Networking Equipment"),
    "3577": ("Information Technology", "Computer Peripherals"), "3661": ("Information Technology", "Communications Equipment"), "3663": ("Information Technology", "Communications Equipment"),
    "3669": ("Information Technology", "Communications Equipment"), "3672": ("Information Technology", "Electronic Components"), "3674": ("Information Technology", "Semiconductors"),
    "3678": ("Information Technology", "Electronic Components"), "3679": ("Information Technology", "Electronic Components"), "3711": ("Consumer Discretionary", "Automobiles"), "3714": ("Consumer Discretionary", "Auto Parts"),
    "3721": ("Industrials", "Aerospace & Defense"), "3724": ("Industrials", "Aerospace & Defense"), "3728": ("Industrials", "Aerospace & Defense"), "3760": ("Industrials", "Aerospace & Defense"),
    "3812": ("Industrials", "Aerospace & Defense"), "3826": ("Health Care", "Life Sciences Tools"), "3841": ("Health Care", "Medical Devices"), "3842": ("Health Care", "Medical Devices"),
    "3845": ("Health Care", "Medical Devices"), "3851": ("Health Care", "Medical Devices"), "4813": ("Communication Services", "Telecommunications"), "4812": ("Communication Services", "Wireless Telecommunications"),
    "4833": ("Communication Services", "Broadcasting"), "4841": ("Communication Services", "Cable & Satellite"), "4911": ("Utilities", "Electric Utilities"), "4922": ("Utilities", "Gas Utilities"),
    "4923": ("Utilities", "Gas Utilities"), "4931": ("Utilities", "Multi-Utilities"), "4941": ("Utilities", "Water Utilities"), "5122": ("Health Care", "Drug Distribution"),
    "5411": ("Consumer Staples", "Food & Staples Retail"), "5812": ("Consumer Discretionary", "Restaurants"), "5961": ("Consumer Discretionary", "Internet Retail"), "6021": ("Financials", "Banks"),
    "6022": ("Financials", "Banks"), "6035": ("Financials", "Savings Institutions"), "6199": ("Financials", "Consumer & Specialty Finance"), "6211": ("Financials", "Capital Markets"),
    "6311": ("Financials", "Life Insurance"), "6321": ("Financials", "Health Insurance"), "6331": ("Financials", "Property & Casualty Insurance"), "6798": ("Real Estate", "Real Estate Investment Trusts"),
    "7370": ("Information Technology", "IT Services"), "7371": ("Information Technology", "IT Services"), "7372": ("Information Technology", "Software"), "7373": ("Information Technology", "IT Services"),
    "7374": ("Information Technology", "Data Processing Services"), "7389": ("Industrials", "Business Services"), "8731": ("Health Care", "Life Sciences Research"),
}
NON_OPERATING_SIC = {"6770": "blank check company (no operating business yet)", "6726": "investment office / closed-end fund", "6221": "commodity contracts / trust vehicle", "6189": "asset-backed securities vehicle"}

# industry-level traits: (cyclicality, capital intensity, regulatory intensity, government dependency, revenue model). 'unknown' wherever the industry alone does not decide it.
TRAITS: Dict[str, Tuple[str, str, str, str, str]] = {
    "Utilities": ("defensive", "high", "high", "unknown", "regulated tariffs and usage charges"),
    "Banks": ("mixed", "low", "high", "unknown", "net interest income and fees"), "Insurance": ("mixed", "low", "high", "unknown", "premiums and investment income"),
    "Capital Markets": ("cyclical", "low", "high", "unknown", "fees, trading and interest"), "Real Estate Investment Trusts": ("mixed", "high", "medium", "unknown", "rents"),
    "Oil & Gas Exploration & Production": ("cyclical", "high", "medium", "unknown", "sale of produced commodities"), "Coal": ("cyclical", "high", "high", "unknown", "sale of produced commodities"),
    "Pipelines": ("defensive", "high", "high", "unknown", "fees for transporting commodities"), "Oil Refining & Marketing": ("cyclical", "high", "medium", "unknown", "refining margins"),
    "Aerospace & Defense": ("mixed", "high", "high", "likely", "contracts and product sales"), "Pharmaceuticals": ("defensive", "medium", "high", "unknown", "product sales"),
    "Biotechnology": ("defensive", "medium", "high", "unknown", "unknown"), "Medical Devices": ("defensive", "medium", "high", "unknown", "product sales"),
    "Health Care Services": ("defensive", "medium", "high", "likely", "fees for services"), "Semiconductors": ("cyclical", "high", "medium", "unknown", "product sales"),
    "Software": ("mixed", "low", "low", "unknown", "unknown"), "Restaurants": ("cyclical", "medium", "low", "unknown", "food and beverage sales"),
    "Airlines": ("cyclical", "high", "high", "unknown", "passenger and cargo fares"), "Railroads": ("mixed", "high", "high", "unknown", "freight charges"),
    "Food Products": ("defensive", "medium", "medium", "unknown", "product sales"), "Food & Staples Retail": ("defensive", "medium", "low", "unknown", "retail sales"),
    "Automobiles": ("cyclical", "high", "medium", "unknown", "vehicle sales"), "Steel & Primary Metals": ("cyclical", "high", "medium", "unknown", "product sales"),
    "Metals & Mining": ("cyclical", "high", "medium", "unknown", "sale of produced commodities"), "Construction & Engineering": ("cyclical", "medium", "medium", "likely", "contract revenue"),
    "Telecommunications": ("defensive", "high", "high", "unknown", "subscriptions and usage"), "Tobacco": ("defensive", "low", "high", "unknown", "product sales"),
}
SECTOR_TRAITS: Dict[str, Tuple[str, str, str, str, str]] = {
    "Utilities": TRAITS["Utilities"], "Energy": ("cyclical", "high", "medium", "unknown", "unknown"), "Materials": ("cyclical", "high", "medium", "unknown", "unknown"),
    "Financials": ("mixed", "low", "high", "unknown", "unknown"), "Health Care": ("defensive", "medium", "high", "unknown", "unknown"), "Consumer Staples": ("defensive", "medium", "medium", "unknown", "unknown"),
    "Consumer Discretionary": ("cyclical", "medium", "low", "unknown", "unknown"), "Information Technology": ("mixed", "medium", "low", "unknown", "unknown"), "Industrials": ("cyclical", "medium", "medium", "unknown", "unknown"),
    "Communication Services": ("mixed", "medium", "medium", "unknown", "unknown"), "Real Estate": ("mixed", "high", "medium", "unknown", "unknown"),
}


def classify_sic(sic: Optional[str]) -> Optional[dict]:
    """sector / industry for an SEC SIC code, from the tables above, or None. Never guesses a code that is not in the SEC scheme."""
    s = re.sub(r"\D", "", str(sic or ""))
    if len(s) != 4 or s == "0000":
        return None
    hit = SIC4.get(s) or SIC2.get(s[:2])
    if not hit:
        return None
    return {"sic": s, "sector": hit[0], "industry": hit[1], "rule": "sic4:%s" % s if s in SIC4 else "sic2:%s" % s[:2], "non_operating": NON_OPERATING_SIC.get(s)}


def traits_for(sector: Optional[str], industry: Optional[str]) -> Optional[dict]:
    t = TRAITS.get(industry or "") or SECTOR_TRAITS.get(sector or "")
    if not t:
        return None
    return {"cyclicality": t[0], "capital_intensity": t[1], "regulatory_intensity": t[2], "gov_dependency": t[3], "revenue_model": t[4], "rule": "industry:%s" % industry if industry in TRAITS else "sector:%s" % sector}


# ---- instrument kind from the NAME (the universe file carries a name, an exchange and a CIK, nothing more). Every rule is a hint with a stated confidence; the SEC submissions refresh can confirm or overturn it.
_R = lambda p: re.compile(p, re.I)  # noqa: E731
INSTRUMENT_RULES: List[Tuple[str, str, "re.Pattern", float]] = [
    ("warrant", "warrant", _R(r"\bWARRANTS?\b"), 0.9),
    ("right", "right", _R(r"\bRIGHTS?\b"), 0.8),
    ("unit", "unit", _R(r"\bUNITS?\b"), 0.8),
    ("preferred", "preferred", _R(r"\bPREFERRED\b|\bPFD\b|\bDEPOSITARY SHARES?\b"), 0.85),
    ("note", "note", _R(r"\bNOTES? DUE\b|\bSENIOR NOTES\b|\bDEBENTURES?\b|\bSUBORDINATED NOTES\b"), 0.85),
    ("etf", "etf", _R(r"\bETF\b|\bETN\b|\bEXCHANGE[- ]TRADED (?:FUND|NOTE)S?\b|\bINDEX FUND\b|\bISHARES\b|\bSPDR\b|\bPROSHARES\b|\bVANECK\b|\bINVESCO QQQ\b"), 0.85),
    ("fund", "fund", _R(r"\bFUND\b(?! (?:SERVICES|ADMINISTRATION|SOLUTIONS|TECHNOLOG))|\bCLOSED[- ]END\b"), 0.7),
    ("spac", "spac", _R(r"\bACQUISITION\s+(?:(?:[IVX]{1,4}|\d{1,2})\s+)?(?:CORP|CORPORATION|CO|COMPANY|HOLDINGS|LTD|LIMITED)\b(?:\.?\s+(?:[IVX]{1,4}|\d{1,2})\b)?|\bBLANK CHECK\b|\bMERGER (?:CORP|CORPORATION)\b|\bSPAC\b"), 0.7),
]
ADR_RX = _R(r"\bADRs?\b(?!-)|\bADSs?\b(?![-\w])|\bAMERICAN DEPOSITARY\b|\bSPONSORED (?:ADR|ADS|DEPOSITARY)\b")
FOREIGN_HINT_RX = _R(r"\b(?:PLC|N\.?V\.?|S\.?A\.?|A\.?G\.?|S\.?P\.?A\.?|OYJ|A/S|AB|SE|ASA|LTD\.?/ADR|SAB DE CV|BERMUDA|CAYMAN)\b\s*$|\bHOLDINGS? \(?(?:CAYMAN|BERMUDA)")
CLASS_RX = _R(r"\bCLASS\s+([A-Z0-9])\b|\bSERIES\s+([A-Z0-9])\b|\bCL\s+([A-Z])\b")
TICKER_CLASS_RX = re.compile(r"[.\-]([A-Z])$")


def classify_instrument(name: str, ticker: str) -> dict:
    """{security_type, kind, operating (1/0/None), adr, foreign_hint, share_class, flags[], rules[], confidence}. Hints from the name only; unknown stays unknown."""
    nm = name or ""
    flags: List[str] = []
    rules: List[str] = []
    kind, sec, operating, conf = "operating_equity", "equity", None, 0.5
    for rule, stype, rx, c in INSTRUMENT_RULES:
        if rx.search(nm):
            kind, sec, operating, conf = rule, stype, 0, c
            flags.append(rule)
            rules.append("name:%s" % rule)
            break
    adr = 1 if ADR_RX.search(nm) else 0
    if adr:
        flags.append("adr")
        rules.append("name:adr")
    foreign = 1 if FOREIGN_HINT_RX.search(nm.strip()) else 0
    if foreign:
        flags.append("foreign_hint")
        rules.append("name:foreign_suffix")
    m = CLASS_RX.search(nm)
    share_class = (m.group(1) or m.group(2) or m.group(3)).upper() if m else None
    if not share_class:
        tm = TICKER_CLASS_RX.search(ticker or "")
        share_class = tm.group(1) if tm else None
    if kind == "operating_equity":
        operating, conf = 1, 0.5                                             # a company-like name with no instrument cue: assumed operating, at low confidence, until the SEC entity type says otherwise
    return {"security_type": sec, "kind": kind, "operating": operating, "adr": adr, "foreign_hint": foreign, "share_class": share_class, "flags": flags, "rules": rules, "confidence": conf}
