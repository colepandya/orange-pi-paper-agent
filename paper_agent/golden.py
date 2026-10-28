"""Golden-set evaluation of the event analyst. Run `python3 -m paper_agent llm-eval` with your real key to MEASURE how good the LLM's
reading is on cases with known right answers (direction, key tickers, grounding) before trusting it with capital."""
from __future__ import annotations

from typing import Callable, Dict, List

from . import event_intel
from .llm_client import LLMClient

GOLDEN: List[Dict] = [
    {"title": "NVIDIA beats estimates and raises full-year guidance as data center revenue surges 94%", "summary": "NVIDIA reported record data center revenue and lifted its outlook, citing demand from hyperscalers.", "dir": 1, "tickers": ["NVDA"]},
    {"title": "Lockheed Martin wins $2.4 billion Pentagon contract for missile defense", "summary": "The Navy awarded Lockheed Martin a multi-year contract for missile defense systems.", "dir": 1, "tickers": ["LMT"]},
    {"title": "Target cuts full-year outlook as shoppers pull back on discretionary spending", "summary": "Target lowered its profit forecast and warned of weak consumer demand.", "dir": -1, "tickers": ["TGT"]},
    {"title": "Exxon Mobil faces antitrust probe over pipeline acquisition", "summary": "Regulators opened an investigation into Exxon's proposed purchase.", "dir": -1, "tickers": ["XOM"]},
    {"title": "China conducts live-fire drills around Taiwan, shipping lanes disrupted", "summary": "The drills closed key shipping lanes and raised fears for semiconductor supply chains.", "dir": -1, "macro": True, "narr": "china_taiwan_escalation"},
    {"title": "Fed cuts interest rates by 25 basis points and signals further easing", "summary": "The Federal Reserve lowered its benchmark rate citing cooling inflation.", "dir": 1, "macro": True, "narr": "fed_easing"},
    {"title": "OPEC announces surprise output cut, oil jumps 6%", "summary": "Oil producers agreed to cut output, tightening global supply.", "dir": -1, "macro": True, "narr": "middle_east_oil_shock"},
    {"title": "Microsoft says Azure growth accelerated to 39% as AI demand outstrips capacity", "summary": "Microsoft cloud revenue beat expectations; management said it is capacity constrained.", "dir": 1, "tickers": ["MSFT"], "narr": "cloud_expansion"},
    {"title": "Analyst warns of AI bubble as hyperscaler capex plans are cut", "summary": "A prominent analyst said data center cancellations signal an AI spending slowdown.", "dir": -1, "narr": "ai_infrastructure", "narr_stance": "contradict"},
    {"title": "Local bakery wins regional award for best sourdough", "summary": "The bakery in a small town was recognised at a food festival.", "dir": 0, "tickers": []},
]


def run(client: LLMClient, universe, golden: List[Dict] = GOLDEN, out: Callable = print) -> dict:
    passed = total = 0
    for g in golden:
        intel = event_intel.analyze(client, universe, {"title": g["title"], "summary": g["summary"], "tickers": []}, "analyst")
        checks = []
        if intel is None:
            out("FAIL  (no valid analysis)  %s" % g["title"][:70])
            total += 1
            continue
        first = [e for e in intel["beneficiaries"] + intel["losers"] if e["order"] == 1]
        if g.get("macro"):
            got = 1 if intel["direction_market"] > 0.1 else -1 if intel["direction_market"] < -0.1 else 0
        elif first:
            top = max(first, key=lambda e: e["confidence"])
            got = 1 if top in intel["beneficiaries"] else -1
        else:   # no named company: judge the market-level direction
            got = 1 if intel["direction_market"] > 0.1 else -1 if intel["direction_market"] < -0.1 else 0
        checks.append(("direction", got == g["dir"]))
        if g.get("tickers"):
            names = {e["ticker"] for e in intel["beneficiaries"] + intel["losers"]}
            checks.append(("tickers", all(t in names for t in g["tickers"])))
        if g.get("narr"):
            checks.append(("narrative", any(n["key"] == g["narr"] and n["stance"] == g.get("narr_stance", "support" if g["dir"] > 0 else "support") for n in intel["narratives"]) or any(n["key"] == g["narr"] for n in intel["narratives"])))
        if g["dir"] == 0:
            checks.append(("ignores_noise", intel["magnitude"] <= 0.25 and not intel["beneficiaries"] and not intel["losers"]))
        if g["dir"] != 0:
            checks.append(("grounded", bool(intel.get("grounded"))))
        ok = all(v for _, v in checks)
        passed += ok
        total += 1
        out("%s  %-60s %s" % ("PASS" if ok else "FAIL", g["title"][:60], " ".join("%s=%s" % (k, "ok" if v else "X") for k, v in checks)))
    u = client.usage_summary()
    out("golden set: %d/%d passed | cost today $%.4f" % (passed, total, u["today_usd"]))
    return {"passed": passed, "total": total, "cost": u["today_usd"]}
