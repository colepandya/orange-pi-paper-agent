"""Recall / precision benchmark for company recognition and event qualification.
Run on the Pi with the REAL watchlist:  python3 -m paper_agent recall-check
Headlines are written the way wires write them (brand/short names, varied phrasing). Anything missed is listed so it can be fixed."""
from __future__ import annotations

from typing import Callable, Dict, List, Tuple

from . import evidence, nlp

# (headline, expected ticker, expected direction sign)
POSITIVE: List[Tuple[str, str, int]] = [
    ("Alphabet shares jump after Google Cloud revenue beats estimates", "GOOGL", 1), ("AMD forecasts fourth-quarter revenue above estimates on AI chip demand", "AMD", 1),
    ("Disney beats profit estimates as streaming turns profitable", "DIS", 1), ("Boeing wins $4.2 billion contract from U.S. Air Force", "BA", 1), ("Tesla recalls 200,000 vehicles over software glitch", "TSLA", -1),
    ("Nvidia beats estimates, guides above on data center demand", "NVDA", 1), ("Amazon raises full-year outlook as AWS growth accelerates", "AMZN", 1), ("JPMorgan raises net interest income forecast", "JPM", 1),
    ("Johnson & Johnson cuts annual forecast on tariff costs", "JNJ", -1), ("Intel secures $3 billion Pentagon award for secure chips", "INTC", 1), ("Walmart raises full-year outlook after strong holiday sales", "WMT", 1),
    ("Exxon Mobil to buy Pioneer Natural Resources in $59.5 billion deal", "PXD", 1), ("Eli Lilly obesity drug wins FDA approval", "LLY", 1), ("Netflix beats subscriber estimates, shares rise", "NFLX", 1),
    ("Uber announces $5 billion share buyback", "UBER", 1), ("Moderna says FDA grants approval for new RSV vaccine", "MRNA", 1), ("Lockheed Martin wins $2.4 billion Navy contract", "LMT", 1),
    ("Goldman Sachs raises dividend by 33%", "GS", 1), ("Starbucks cuts forecast as US traffic falls", "SBUX", -1), ("Nike warns of weaker holiday-quarter sales", "NKE", -1), ("Ford recalls 150,000 trucks", "F", -1),
    ("General Motors raises profit guidance", "GM", 1), ("Palantir wins $480 million Army contract for Maven AI", "PLTR", 1), ("CrowdStrike beats estimates, raises annual forecast", "CRWD", 1),
    ("Microsoft says Azure revenue grew 39%, beats estimates", "MSFT", 1), ("Oracle signs $30 billion cloud contract", "ORCL", 1), ("Costco beats profit estimates on strong membership fees", "COST", 1),
    ("Coca-Cola beats quarterly estimates, raises sales outlook", "KO", 1), ("Micron forecasts quarterly revenue above estimates on memory demand", "MU", 1), ("IBM raises full-year revenue forecast", "IBM", 1),
    ("Home Depot misses sales estimates", "HD", -1), ("Salesforce to acquire Informatica for $8 billion", "CRM", -1), ("Bank of America beats profit estimates", "BAC", 1),
    ("Apple unveils new iPhone, begins shipping", "AAPL", 1), ("Meta lands multi-year AI chip supply agreement with Broadcom", "META", 1), ("Citigroup cuts annual revenue forecast", "C", -1),
]
# text that must NOT produce a trade candidate for the named ticker (ordinary words / unrelated meanings)
NEGATIVE: List[Tuple[str, str]] = [
    ("A meta-analysis of 20 studies found no link between coffee and heart disease", "META"), ("Researchers shell out for new reef survey as ford crossing washes away", "F"),
    ("Local block party draws thousands as city cuts street funding", "XYZ"), ("Apple pie recipe wins regional baking contest", "AAPL"), ("Target date funds see record inflows as savers cut risk", "TGT"),
    ("Visa applications for foreign students fall as consulates cut staff", "V"), ("Amazon rainforest fires hit record high as drought deepens", "AMZN"), ("City council approves new park near the old oracle building", "ORCL"),
]
BRAND_TEST_UNIVERSE: List[Tuple[str, str]] = []


def evaluate(universe, analyze: Callable = nlp.analyze, icfg: dict = None) -> Dict:
    icfg = icfg or {"trade_event_max_age_hours": 24}
    rows, rec, cand, sign_ok, tested = [], 0, 0, 0, 0
    for h, tk, sign in POSITIVE:
        if tk not in universe:
            rows.append({"headline": h, "want": tk, "status": "n/a (ticker not in this universe)"})
            continue
        tested += 1
        a = analyze(h)
        m = universe.match(h)
        tickers, conf = [t for t, c, _ in m if c >= 0.5], {t: c for t, c, _ in m}
        gate = evidence.candidate_gate(a, tickers, conf, 1.0, 1.0, icfg)
        recognised = tk in tickers
        is_cand = recognised and gate is None
        rec += recognised
        cand += is_cand
        d = a["direction"]
        if a["event_type"] == "merger_acquisition" and recognised:        # same per-company role logic the live pipeline uses
            role = universe.mna_role(tk, h)
            d = 0.8 if role == "target" else evidence.MNA_ACQUIRER_DIRECTION if role == "acquirer" else 0.0
        ok_sign = is_cand and (d > 0) == (sign > 0)
        sign_ok += ok_sign
        rows.append({"headline": h, "want": tk, "matched": tickers, "type": a["event_type"], "direction": round(d, 2), "gate": gate or "candidate",
                     "status": "ok" if ok_sign or (sign < 0 and is_cand) else ("company not recognised" if not recognised else ("no candidate: " + str(gate)))})
    false_pos = []
    for h, tk in NEGATIVE:
        if tk not in universe:
            continue
        a = analyze(h)
        m = universe.match(h)
        tickers, conf = [t for t, c, _ in m if c >= 0.6], {t: c for t, c, _ in m}
        if tk in tickers and evidence.candidate_gate(a, tickers, conf, 1.0, 1.0, icfg) is None:
            false_pos.append(h)
    return {"tested": tested, "recognised": rec, "candidates": cand, "direction_correct": sign_ok, "false_positives": false_pos, "rows": rows,
            "recall": rec / tested if tested else 0.0, "candidate_rate": cand / tested if tested else 0.0}


def text(r: Dict) -> str:
    L = ["=== RECALL CHECK: company recognition + event qualification (%d headlines testable on this universe) ===" % r["tested"],
         "company recognised:       %d/%d  (%.0f%%)" % (r["recognised"], r["tested"], 100 * r["recall"]),
         "became trade candidate:   %d/%d  (%.0f%%)" % (r["candidates"], r["tested"], 100 * r["candidate_rate"]),
         "direction correct:        %d/%d" % (r["direction_correct"], max(1, r["candidates"])), "false positives:         %d  %s" % (len(r["false_positives"]), r["false_positives"] or "")]
    misses = [x for x in r["rows"] if x["status"] not in ("ok",) and not x["status"].startswith("n/a")]
    if misses:
        L.append("\nMISSES (fix these):")
        for x in misses:
            L.append("  [%s] want %-6s matched=%-14s type=%-18s dir=%+.2f | %s" % (x["status"], x["want"], ",".join(x.get("matched", [])) or "-", x.get("type", "?"), x.get("direction", 0), x["headline"][:70]))
    return "\n".join(L)
