"""Deterministic event understanding (no ML dependencies; runs on an Orange Pi).

Fixes the legacy scorer, which matched raw substrings ("ban" in "bank", "order" in "border", "sec" in
"second", "will" everywhere) and ignored negation. Everything here is word-boundary based and negation aware.
"""
from __future__ import annotations

import math
import functools
import re
import unicodedata
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

STOP = set("""the and for with that this from have has had will would could should about after before over under into than then them their there
 these those which while where when what said says say also more most such only other some very just being been were are was its our your
 not but you all can may new news report reports reuters according market stock stocks shares company inc corp ltd plc""".split())

NEGATORS = ("not", "no", "never", "without", "fails", "failed", "fail", "denies", "denied", "unlikely", "won't", "wont",
            "doesn't", "didn't", "isn't", "cannot", "can't", "hasn't", "haven't", "nor", "neither", "lacks", "despite")
HEDGES = ("may", "might", "could", "reportedly", "rumor", "rumour", "rumored", "speculation", "considering", "weighs",
          "explores", "exploring", "talks", "plans to", "expected to", "possible", "potential", "said to", "sources say")
BOOST = ("record", "historic", "largest", "unprecedented", "massive", "landmark", "surge", "soar", "plunge", "collapse", "crisis")
DAMP = ("minor", "slight", "small", "modest", "limited", "routine")


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = t.replace("\u2019", "'").replace("\u2018", "'").replace("-", " ").replace("\u2013", " ").replace("\u2014", " ")   # hyphens are word breaks: "Coca-Cola" == "Coca Cola", "full-year" == "full year"
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9$%.'+/ ]+", " ", t)).strip()


@lru_cache(maxsize=4096)
def _rx(phrase: str):
    p = re.escape(norm(phrase)).replace(r"\ ", r"\s+")
    return re.compile(r"(?<![a-z0-9])" + p + r"(?![a-z0-9])")


def has_phrase(text_norm: str, phrase: str) -> bool:
    return _rx(phrase).search(text_norm) is not None


def find_phrase(text_norm: str, phrase: str):
    return _rx(phrase).search(text_norm)


def negated(text_norm: str, start: int, window: int = 4) -> bool:
    before = text_norm[:start].split()[-window:]
    return any(w in NEGATORS for w in before)


def hedged(text_norm: str, start: int, window: int = 5) -> bool:
    before = " ".join(text_norm[:start].split()[-window:])
    return any(h in before for h in HEDGES)


def tokens(text: str, limit: int = 28) -> List[str]:
    out, seen = [], set()
    for w in norm(text).split():
        w = w.strip(".-'")
        if len(w) >= 4 and w not in STOP and not w.isdigit() and w not in seen:
            seen.add(w)
            out.append(w)
    return sorted(out, key=lambda w: (-len(w), w))[:limit]


def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a and b else 0.0


# ---------------------------------------------------------------------------------------------
# Event taxonomy. (name, scope, direction, base_magnitude, decay_days, predictive_power, patterns)
#   direction: +1 / -1 inherent polarity, 0 = decided by lexicon.   decay_days follows the OMEGA brief:
#   earnings 30, contract 180, product launch 90, regulatory approval 365, structural 730.
# ---------------------------------------------------------------------------------------------
class EventType:
    __slots__ = ("name", "scope", "direction", "magnitude", "decay_days", "power", "patterns", "horizon")

    def __init__(self, name, scope, direction, magnitude, decay_days, power, patterns, horizon):
        self.name, self.scope, self.direction, self.magnitude = name, scope, direction, magnitude
        self.decay_days, self.power, self.patterns, self.horizon = decay_days, power, patterns, horizon


def _t(*a):
    return EventType(*a)


EVENT_TYPES: List[EventType] = [
    # ---- company scope
    _t("bankruptcy_distress", "company", -1, .90, 365, .75, ["bankruptcy", "chapter 11", "going concern", "delisting notice", "defaults on", "debt default", "insolvency"], 15),
    _t("guidance_raise", "company", +1, .60, 30, .70, [r"(?:raises?|raised|raising|lifts?|lifted|boosts?|boosted|hikes?|hiked|increases?|increased|upgrades?)\s+(?:(?!capex|capital|spending|costs?|expenses?)[a-z0-9$%.'+/]+\s+){0,5}?(?:guidance|outlook|forecasts?|targets?|projections?|view)", r"guides?\s+(?:[a-z0-9$%.'+/]+\s+){0,4}?(?:above|higher|ahead)", r"(?:guidance|outlook|forecast)\s+(?:raised|lifted|boosted)", r"raises? (its )?(full.year |annual |quarterly )?(guidance|outlook|forecast)", r"lifts? (its )?(full.year )?(guidance|outlook|forecast)", r"boosts? (its )?(full.year )?(guidance|outlook)", "raised guidance", "increases outlook"], 8),
    _t("guidance_cut", "company", -1, .65, 30, .70, [r"(?:cuts?|lowers?|lowered|slashes?|trims?|reduces?|reduced|downgrades?|withdraws?|suspends?)\s+(?:[a-z0-9$%.'+/]+\s+){0,5}?(?:guidance|outlook|forecasts?|targets?|projections?)", r"warn(?:s|ed|ing)?\b", r"guides?\s+(?:[a-z0-9$%.'+/]+\s+){0,4}?(?:below|lower|weaker)", r"forecasts?\s+(?:[a-z0-9$%.'+/]+\s+){0,5}?below\s+(?:estimates|expectations)", r"cuts? (its )?(full.year |annual )?(guidance|outlook|forecast)", r"lowers? (its )?(full.year )?(guidance|outlook|forecast)", "profit warning", "revenue warning", "withdraws guidance", "slashes outlook"], 8),
    _t("earnings_beat", "company", +1, .55, 30, .70, [r"(?:beats?|beating|tops?|topped|surpass\w*|exceed\w*|crush\w*|smash\w*|outpac\w*)\s+(?:[a-z0-9$%.'+/]+\s+){0,4}?(?:estimates?|expectations?|forecasts?|consensus|views?|projections?|analysts?)", r"(?:above|better than|ahead of|stronger than)\s+(?:[a-z0-9$%.'+/]+\s+){0,3}?(?:estimates?|expectations?|forecasts?|consensus)", r"(?:earnings|profit|revenue|sales)\s+(?:jump\w*|surge\w*|soar\w*)", r"beats? (analyst |wall street )?(estimates|expectations|forecasts|consensus)", r"tops? (analyst |wall street )?(estimates|expectations|forecasts)", r"(earnings|profit|revenue|sales) (beat|beats|topped|surpass\w*|exceed\w*)", "better than expected", "above expectations", "record revenue", "record quarterly"], 8),
    _t("earnings_miss", "company", -1, .55, 30, .70, [r"(?:misses?|missed|missing|lags?|lagged|trails?|disappoints?|disappointed)\s+(?:[a-z0-9$%.'+/]+\s+){0,4}?(?:estimates?|expectations?|forecasts?|consensus|views?|projections?|analysts?)", r"(?:below|worse than|weaker than|short of)\s+(?:[a-z0-9$%.'+/]+\s+){0,3}?(?:estimates?|expectations?|forecasts?|consensus)", r"(?:earnings|profit|revenue|sales)\s+(?:slump\w*|plunge\w*|tumble\w*|sink\w*)", r"misses? (analyst |wall street )?(estimates|expectations|forecasts|consensus)", r"falls? short of (estimates|expectations|forecasts)", r"(earnings|profit|revenue|sales) (miss|missed|fell short|disappoint\w*)", "worse than expected", "below expectations", "weaker than expected"], 8),
    _t("regulatory_approval", "company", +1, .70, 365, .65, [r"fda (approves|approval|clears|clearance|grants|authorization)", "receives approval", "regulatory approval", "marketing authorization", r"(approved|cleared) by (the )?(fda|ema|sec|ftc|doj|cfius)", "approval granted", "510(k) clearance", "breakthrough designation"], 12),
    _t("regulatory_action", "company", -1, .65, 90, .60, [r"recall(?:s|ed|ing)?\b", r"(?:faces?|under|opens?|launches?|hit with|slapped with)\s+(?:[a-z0-9$%.'+/]+\s+){0,3}?(?:probe|investigation|lawsuit|inquiry|subpoena|fine|penalty|charges)", r"(?:fined|sued|charged)\b", "complete response letter", "warning letter", "fda rejects", "recall", "antitrust", "subpoena", "sec charges", "doj probe", "investigation into", "probe into", "fined", "consent decree", "class i recall", "clinical hold", "trial halted", "export ban", "banned from"], 10),
    _t("contract_win", "company", +1, .50, 180, .60, [r"(?:wins?|won|awarded|secures?|lands?|receives?|bags?|signs?|signed|inks?|clinch\w*|nabs?|scores?)\s+(?:[a-z0-9$%.'+/]+\s+){0,6}?(?:contracts?|orders?|awards?|deals?|agreements?|task orders?)", r"(wins?|won|awarded|secures?|lands?|receives?|bags?)\s+(?:[\w$.,%-]+\s+){0,6}?(contract|order|award|task order)", "contract award", "awarded a contract", "task order", "multi-year agreement", "supply agreement", "selected by", "selected to", "purchase order", "framework agreement"], 12),
    _t("contract_loss", "company", -1, .45, 180, .55, ["loses contract", "lost the contract", "contract terminated", "contract termination", "contract cancelled", "contract canceled", "protest sustained", "order cancelled", "order canceled"], 10),
    _t("merger_acquisition", "company", +0.4, .70, 60, .55, [r"(?:to buy|buys|bought|to purchase|purchases|to take over|takes over|bid for|offers to buy|to snap up|completes\s+(?:the\s+)?(?:acquisition|purchase|takeover)|closes\s+(?:the\s+)?(?:acquisition|purchase))\b", "to acquire", "agrees to buy", "acquisition", "acquires", "takeover", "buyout", "tender offer", "merger agreement", "to be acquired", "all-cash deal", "go-private", "merger"], 20),
    _t("capital_raise", "company", -1, .45, 30, .65, ["public offering", "secondary offering", "convertible notes", "convertible senior notes", "at-the-market", "atm program", "dilution", "shelf registration", "private placement", "prices offering", "equity raise"], 5),
    _t("buyback_dividend", "company", +1, .35, 60, .55, ["share repurchase", "buyback", "repurchase program", "dividend increase", "raises dividend", "special dividend", "initiates dividend"], 10),
    _t("cyber_incident", "company", -1, .55, 30, .55, ["data breach", "ransomware", "cyberattack", "cyber attack", "hacked", "security incident", "systems outage", "unauthorized access"], 5),
    _t("litigation", "company", -1, .40, 60, .50, ["lawsuit", "sued", "sues", "class action", "patent infringement", "legal action", "securities fraud", "indicted"], 10),
    _t("production_disruption", "company", -1, .50, 45, .55, ["production halt", "plant shutdown", "factory shutdown", "supply shortage", "plant fire", "strike at", "work stoppage", "production cut", "halts production", "recall of"], 8),
    _t("partnership", "company", +1, .35, 90, .45, ["strategic partnership", "partnership with", "partners with", "collaboration with", "joint venture", "strategic alliance", "teams up with", "memorandum of understanding"], 10),
    _t("product_launch", "company", +1, .35, 90, .45, ["launches", "unveils", "introduces", "announces availability", "rolls out", "debuts", "new product", "general availability", "begins shipping", "starts production"], 10),
    _t("analyst_action", "company", 0, .20, 14, .40, ["upgrades", "upgraded", "downgrades", "downgraded", "price target", "initiates coverage", "raises target", "cuts target", "outperform", "underperform"], 5),
    _t("leadership_change", "company", 0, .35, 60, .40, ["ceo resigns", "ceo steps down", "names new ceo", "appoints ceo", "cfo resigns", "cfo departs", "chief executive to retire", "founder steps down"], 10),
    _t("insider_filing", "company", 0, .15, 30, .40, ["form 4", "insider buying", "insider selling", "schedule 13d", "schedule 13g", "sc 13d", "sc 13g", "beneficial ownership"], 10),
    _t("structural_theme", "sector", +1, .55, 730, .55, ["multi-year build-out", "multiyear buildout", "secular growth", "structural shift", "capex plan", "capital expenditure plan", "data center buildout", "data centre buildout", "gigawatt", "reshoring", "onshoring", "once-in-a-generation"], 30),
    # ---- macro / world scope (direction = effect on broad risk appetite; thesis triggers refine sector effects)
    _t("central_bank", "macro", 0, .60, 60, .55, [r"(the fed|fed chair|fed officials|fed policymakers|federal reserve|fomc|ecb|bank of japan|bank of england|pboc|rbnz|rba)", r"fed (cuts|holds|raises|hikes|lowers|keeps|signals|minutes|decision|meeting|rate|rates|pivot)", "interest rates", "basis points", "rate setters", "powell", "lagarde", "higher for longer", "interest rate decision", "rate decision", "rate cut", "rate hike", "rate cuts", "rate hikes", "quantitative easing", "quantitative tightening", "balance sheet runoff"], 10),
    _t("inflation_data", "macro", 0, .50, 45, .55, ["inflation", "cpi", "consumer price index", "pce price", "producer prices", "inflation rate", "inflation expectations", "core inflation"], 5),
    _t("jobs_data", "macro", 0, .45, 30, .50, ["nonfarm payrolls", "jobs report", "unemployment rate", "jobless claims", "payrolls", "labor market", "layoffs surge"], 5),
    _t("growth_recession", "macro", 0, .55, 60, .50, ["recession", "gdp", "economic contraction", "yield curve inver", "economic slowdown", "soft landing", "hard landing", "pmi"], 10),
    _t("trade_tariff", "macro", 0, .60, 90, .55, ["tariff", "tariffs", "trade war", "export controls", "export restrictions", "trade deal", "trade truce", "trade talks", "import duties", "entity list", "chip curbs", "chip restrictions"], 10),
    _t("sanctions", "macro", -1, .55, 120, .50, ["sanctions", "embargo", "blockade", "asset freeze", "price cap on", "secondary sanctions"], 10),
    _t("geopolitical_conflict", "macro", 0, .75, 120, .50, ["invasion", "missile strike", "airstrike", "air strike", "military escalation", "troops", "armed conflict", "war between", "shelling", "naval drills", "military drills", "blockade of taiwan", "warship", "nuclear threat", "ceasefire", "peace talks", "peace deal", "truce", "hostilities", "drone attack"], 10),
    _t("energy_supply", "macro", 0, .60, 60, .55, ["opec", "oil supply", "output cut", "production cut by", "strait of hormuz", "red sea", "pipeline", "refinery outage", "oil prices", "crude prices", "natural gas prices", "lng supply", "energy crisis"], 10),
    _t("supply_chain_macro", "macro", -1, .50, 60, .45, ["port strike", "shipping disruption", "supply chain disruption", "chip shortage", "rare earth", "container rates", "shipping delays", "canal closure"], 10),
    _t("credit_stress", "macro", -1, .70, 90, .55, ["bank run", "bank failure", "credit crunch", "liquidity crisis", "spreads widen", "sovereign downgrade", "debt crisis", "margin call", "contagion", "private credit", "commercial real estate stress"], 15),
    _t("fiscal_policy", "macro", 0, .45, 60, .45, ["government shutdown", "debt ceiling", "stimulus", "tax cut", "tax hike", "budget deal", "fiscal deficit", "spending bill", "infrastructure bill"], 10),
    _t("election_policy", "macro", 0, .40, 90, .40, ["election", "executive order", "policy shift", "legislation", "senate vote", "congress passes", "new regulation", "regulatory crackdown"], 10),
    _t("disaster_pandemic", "macro", -1, .55, 30, .45, ["hurricane", "earthquake", "pandemic", "outbreak", "wildfire", "typhoon", "flooding", "heatwave", "drought"], 5),
    _t("technology_shift", "sector", +1, .45, 365, .45, ["breakthrough", "new ai model", "foundation model", "next-generation chip", "quantum computing", "fusion", "small modular reactor", "smr", "humanoid robot", "autonomous driving approval"], 20),
]
# ---- MARKET SHOCKS: crashes, war, emergency policy, bank failures, exchange outages. High-precision patterns that OUTRANK company-level wording
# (a war headline containing the word "launches" used to be classified as a bullish product launch). Subjects are market-level only, so "Tesla shares plunge" is NOT a market shock.
_VERB = r"(?:plunge[sd]?|plummet\w*|tumble[sd]?|crash(?:es|ed)?|tank(?:s|ed)?|nosedive[sd]?|rout(?:ed)?|collaps\w+|reel\w*|rattle[sd]?|swoon\w*|sell ?off)"
_MKT = r"(?:global |world |us |u s |asian |european )?(?:stocks|equities|futures|markets?|wall street)"
EVENT_TYPES += [
    _t("market_crash", "macro", -1, .85, 5, .85, [
        r"(?:^|\bas |\band |\bwhile )" + _MKT + r"\s+(?:\w+\s+){0,2}" + _VERB,
        r"(?:wall street|stock market|dow|dow jones|nasdaq|s p ?500|s p)\s+(?:\w+\s+){0,3}" + _VERB,
        r"(?:s p ?500|dow|nasdaq|wall street|stock market|stocks|markets?)\s+(?:\w+\s+){0,6}worst (?:day|week|month|session|selloff|sell off) since",
        "circuit breaker", "circuit breakers", "trading halted", "flash crash", "black monday", "black tuesday", "black thursday", "market turmoil", "market meltdown",
        "market panic", "market rout", "global rout", r"(?:stock|equity|global|market|wall street) sell ?off (?:deepens|worsens|intensifies|accelerates|spreads)", "vix spikes", "vix surges", "vix jumps", "fear gauge spikes"], 5),
    _t("war_escalation", "macro", -1, .85, 14, .85, [
        r"(?:missile|drone|military|rocket) (?:strikes?|attacks?|assault)", r"airstrikes?", r"invad(?:es|ed|ing)\s+(?:ukraine|taiwan|poland|israel|iran|lithuania|latvia|estonia|[a-z]+ country|neighbou?ring \w+)", "invasion of", "declares war", "declared war",
        r"war (?:breaks|broke) out", r"(?:attack|strike|strikes|assault) on (?:a |the )?(?:naval|military|air|army) base", r"nuclear (?:test|strike|weapon|missile|threat|attack|explosion)",
        "military offensive", "terror attack", "terrorist attack", r"attacks? on (?:\w+ ){0,2}(?:oil|energy|gulf|shipping|tanker)", r"rattles? (?:\w+ )?markets?"], 10),
    _t("emergency_policy", "macro", -1, .75, 5, .80, [r"emergency (?:rate (?:cut|hike|move|decision)|fed (?:meeting|action|move)|central bank (?:meeting|action|move)|liquidity|lending|stimulus|policy (?:meeting|move))", "unscheduled rate"], 5),
    _t("financial_stress_event", "macro", -1, .85, 14, .85, [
        r"(?:bank|lender|brokerage)\s+(?:collapses?|collapsed|implodes?|imploded|(?:fails?|failed)(?!\s+(?:to|the|a|an|its)\b|(?:\s+\w+){0,2}\s+(?:stress|tests?|audits?|inspections?|exams?)\b))", r"(?:hedge fund|fund)\s+(?:collapses?|collapsed|implodes?|imploded)",
        "bank run", "run on the bank", "bank failure", "bank collapse", "halts withdrawals", "deposit flight"], 10),
    _t("market_infrastructure_outage", "macro", -1, .75, 3, .75, [
        r"(?:cyber ?attack|hack|outage|glitch|ransomware)\s+(?:\w+\s+){0,5}(?:stock exchange|nyse|nasdaq|clearing house|payments? system|trading platform)",
        r"(?:nyse|nasdaq|stock exchange|clearing house|trading platform)\s+(?:\w+\s+){0,4}(?:outage|hacked|cyber ?attack)"], 3),
]
_MOVE = r"(?:up|down|higher|lower|jump\w*|drop\w*|fall\w*|fell|surg\w*|soar\w*|plung\w*|rall\w*|tumbl\w*|slid\w*|slump\w*|sank|sink\w*|crash\w*|climb\w*|gain\w*|pop\w*|spik\w*|rose|ris\w*|tank\w*|skyrocket\w*|moving|slipp\w*|rocket\w*)"
_DAY = r"(?:today|this week|this morning|yesterday|on (?:monday|tuesday|wednesday|thursday|friday))"
EVENT_TYPES += [   # AFTER-THE-FACT explainers of a price move ("Why Garmin Stock Dropped After Record Results"): the move already happened, so they are never a reason to BUY
    _t("price_recap", "company", 0, .10, 2, .20, [
        r"^why (?:[\w']+\s+){1,6}?(?:stock|shares)\s+(?:(?:is|are|was|were|has|have|just|also)\s+)*" + _MOVE, r"^why (?:[\w']+\s+){1,6}?(?:stock|shares)\s+(?:[\w']+\s+){0,3}" + _DAY,
        r"^why (?:[\w']+\s+){1,5}?" + _MOVE + r"\b.*" + _DAY, r"^why is (?:[\w']+\s+){1,5}?(?:stock|shares) " + _MOVE, r"^here'?s why (?:[\w']+\s+){0,6}?(?:stock|shares)",
        r"^what'?s (?:going on|happening) with (?:[\w']+\s+){1,5}?(?:stock|shares)", r"^(?:[\w']+\s+){1,5}?(?:stock|shares) (?:is|are) " + _MOVE + r" " + _DAY,
        r"^(?:[\w']+\s+){1,5}?(?:stock|shares) " + _MOVE + r" " + _DAY, r"^is (?:[\w']+\s+){1,4}(?:stock|shares) a (?:buy|sell|hold)", r"^should you (?:buy|sell) (?:[\w']+\s+){1,4}(?:stock|shares)"], 1),
]
EVENT_TYPES += [   # COMMENTARY, not news: questions, listicles, "I'd buy", "stocks to buy", earnings-call recaps, fund holdings. ("Is Earnings Beat Altering The Investment Case For Equinix?" scored +0.78.)
    _t("opinion_piece", "company", 0, .10, 2, .20, [
        "qmarkend", r"^(?:is|are|should|will|can|could|does|did|would)", r"^how (?:[\w']+\s+){0,8}?(?:can|could|should|might|to)", r"^what(?:'s| is| are| to| does| happened| happens)",
        r"^(?:\d+|two|three|four|five|six|seven|eight|nine|ten)\s+(?:reasons?|key takeaways?|things|ways|stocks?|top|best|must|great|red flags|big)",
        r"\b(?:i'd|i would|i'm|i am|i was|i just|my)\s+(?:buy|buying|sell|selling|bought|sold|top|favorite|portfolio|picks?|wait)", r"buy,? sell,? (?:or|and) hold",
        r"(?:best|top|better|great|safest|cheapest|hottest)\s+(?:[\w']+\s+){0,4}?(?:stocks?|etfs?|funds?)\s+to\s+(?:buy|own|watch|invest)", r"\bstocks? to (?:buy|watch|own|avoid|sell)",
        r"(?:key )?takeaways? for (?:long[- ]term )?investors", r"earnings call (?:highlights|transcript|takeaways|recap)", "portfolio activity", r"\b13f", "investment case",
        r"is it (?:time|too late) to (?:buy|sell)", r"worth (?:buying|owning|a look)"], 1),
]
RECAP_TYPES = {"price_recap", "opinion_piece"}
SHOCK_TYPES = {"market_crash", "war_escalation", "emergency_policy", "financial_stress_event", "market_infrastructure_outage"}
_RECOVERY = re.compile(r"(?<![a-z0-9])(?:years? after|anniversary|lessons?|looks? back|retrospective|rebound\w*|recover\w*|rall(?:y|ies|ied)|bounc\w+|steadi\w+|calm\w*|eas(?:e|es|ed)|recoup\w*|claw\w* back|ceasefire|truce|averted|de ?escalat\w*)")

GENERAL = EventType("general_news", "company", 0, .15, 14, .30, [], 3)
BY_NAME = {e.name: e for e in EVENT_TYPES}
BY_NAME["general_news"] = GENERAL

POS_WORDS = {"surge": .6, "soar": .7, "jump": .5, "rally": .5, "beat": .6, "beats": .6, "record": .5, "growth": .3, "strong": .4, "upgrade": .5,
             "approval": .6, "approved": .6, "wins": .5, "win": .4, "awarded": .5, "expands": .3, "boost": .4, "gains": .3, "profit": .3, "optimism": .4,
             "easing": .4, "eases": .4, "cools": .3, "cut rates": .5, "rate cut": .5, "dovish": .5, "ceasefire": .5, "truce": .5, "deal": .3, "breakthrough": .6,
             "recovery": .4, "rebound": .4, "resilient": .3, "outperform": .4, "raises": .3, "accelerates": .3, "stimulus": .4, "peace": .4}
NEG_WORDS = {"plunge": .7, "slump": .6, "tumble": .6, "drop": .4, "falls": .4, "miss": .6, "misses": .6, "cuts": .3, "downgrade": .5, "recall": .6, "probe": .5,
             "investigation": .5, "lawsuit": .4, "fraud": .8, "bankruptcy": .9, "default": .8, "warning": .5, "warns": .5, "crisis": .7, "fears": .4, "selloff": .6,
             "sell-off": .6, "escalat": .6, "invasion": .8, "attack": .6, "sanctions": .5, "tariffs": .4, "tariff": .4, "hawkish": .5, "rate hike": .5, "inflation surges": .6,
             "recession": .6, "layoffs": .4, "shutdown": .4, "halts": .5, "delay": .3, "delays": .3, "weak": .4, "disappoint": .5, "breach": .6, "ban": .6, "bans": .6,
             "dilution": .5, "shortage": .4, "collapse": .8, "contagion": .7, "strike": .3}

_AMT = re.compile(r"(?:us\s?)?\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s?(trillion|billion|million|thousand|tn|bn|mm|b|m|k)?\b|([0-9][0-9,]*(?:\.[0-9]+)?)\s?(trillion|billion|million)\s+(?:us\s)?dollars", re.I)
_MULT = {"trillion": 1e12, "tn": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mm": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}


def extract_amount(text: str) -> Optional[float]:
    """Largest USD amount with an explicit unit. Bare "$5" (a price) is ignored."""
    best = None
    for m in _AMT.finditer(text or ""):
        num = m.group(1) or m.group(3)
        unit = (m.group(2) or m.group(4) or "").lower()
        if not unit:
            continue
        try:
            val = float(num.replace(",", "")) * _MULT[unit]
        except (ValueError, KeyError):
            continue
        if val >= 1e5 and (best is None or val > best):
            best = val
    return best


REGIONS = {
    "china": ["china", "chinese", "beijing", "pboc", "xi jinping", "shanghai", "shenzhen", "hong kong"],
    "taiwan": ["taiwan", "taipei", "tsmc", "taiwan strait"],
    "russia": ["russia", "russian", "moscow", "kremlin", "putin"],
    "ukraine": ["ukraine", "ukrainian", "kyiv", "kiev", "donbas", "crimea"],
    "middle_east": ["middle east", "gaza", "hamas", "hezbollah", "yemen", "houthi", "red sea", "strait of hormuz", "saudi", "opec", "lebanon", "syria", "iraq"],
    "israel": ["israel", "israeli", "tel aviv", "idf"],
    "iran": ["iran", "iranian", "tehran"],
    "europe": ["european union", "eurozone", "ecb", "germany", "france", "brussels", "european commission", "uk ", "britain", "bank of england"],
    "japan": ["japan", "japanese", "boj", "tokyo", "yen"],
    "korea": ["south korea", "north korea", "seoul", "pyongyang"],
    "india": ["india", "indian", "new delhi", "mumbai", "modi"],
    "us": ["united states", "u.s.", "washington", "white house", "federal reserve", "congress", "treasury", "pentagon", "senate"],
}


# Direction = effect on broad risk appetite / equities. Evaluated for macro event types only.
MACRO_POLARITY = {
    "central_bank": [("rate cuts?", .6), ("cuts? (interest )?rates", .6), ("cut(s|ting)? (its )?benchmark", .5), ("dovish", .5), ("easing", .4), ("pauses? (rate|hikes?)", .3),
                     ("signals? more (cuts|easing)", .4), ("rate hikes?", -.6), ("raises? (interest )?rates", -.6), ("hawkish", -.5), ("tightening", -.4),
                     ("higher for longer", -.4), ("balance sheet runoff", -.3), ("holds? rates", 0.0), ("quantitative easing", .5), ("surprise hike", -.7)],
    "inflation_data": [("inflation (cools|eases|slows|falls|drops|declines)", .6), ("(cpi|pce|prices?) (cooler|softer|lower|below)", .6), ("inflation (rises|accelerates|surges|jumps|reignites)", -.7),
                       ("(cpi|pce|prices?) (hotter|higher|above|jumps?)", -.6), ("stickier", -.5), ("(remains|stays|still) (sticky|elevated|high)", -.5), ("disinflation", .5)],
    "jobs_data": [("(jobs|payrolls) (beat|rise|surge|strong)", .4), ("unemployment (falls|drops|declines)", .4), ("(jobs|payrolls) (miss|fall|weak|slump)", -.4),
                  ("unemployment (rises|jumps|climbs)", -.5), ("layoffs surge", -.6), ("jobless claims (jump|surge|rise)", -.4)],
    "growth_recession": [("recession", -.6), ("contraction", -.5), ("slowdown", -.4), ("hard landing", -.7), ("inver", -.5), ("soft landing", .5), ("gdp (beats|grows|expands|accelerates)", .5),
                         ("growth (accelerates|rebounds|beats)", .5), ("gdp (shrinks|contracts|misses)", -.6), ("avoid[a-z]* recession", .4)],
    "trade_tariff": [("trade deal", .6), ("trade truce", .6), ("tariff (relief|exemption|pause|cut)", .5), ("lifts? (tariffs?|restrictions)", .5), ("talks (progress|resume)", .3), ("tariffs?", -.3), ("(imposes?|announces?|threatens?|slaps?|hikes?|raises?)\\s+(?:[a-z0-9%-]+\\s+){0,4}tariffs?", -.3), ("(tariffs?|restrictions|controls|curbs) (lifted|removed|eased|scrapped|suspended|delayed)", .7),
                     ("export controls?", -.5), ("trade war", -.7), ("entity list", -.5), ("chip (curbs|restrictions)", -.5), ("import duties", -.4), ("retaliat", -.6)],
    "geopolitical_conflict": [("ceasefire", .6), ("peace (talks|deal|agreement)", .5), ("truce", .6), ("de-?escalat", .6), ("withdraw", .3), ("invasion", -.9), ("missile strike", -.8), ("airstrike|air strike", -.7),
                              ("military (drills|escalation|buildup)", -.6), ("blockade", -.8), ("troops", -.5), ("warship", -.5), ("hostilities", -.7), ("armed conflict", -.7), ("drone attack", -.6),
                              ("shelling", -.7), ("nuclear threat", -.9), ("war between", -.8)],
    "energy_supply": [("output cut", -.4), ("production cut by", -.4), ("supply disruption", -.5), ("strait of hormuz", -.4), ("prices? (jump|surge|spike|soar|rise)", -.4), ("prices? (fall|drop|slump|tumble|slide)", .35),
                      ("output increase", .3), ("supply (rises|increases|glut)", .3), ("energy crisis", -.6), ("refinery outage", -.3)],
    "fiscal_policy": [("stimulus", .5), ("tax cut", .5), ("budget deal", .4), ("government shutdown", -.5), ("debt ceiling", -.4), ("tax hike", -.4), ("infrastructure bill", .3)],
    "election_policy": [("crackdown", -.4), ("deregulat", .4), ("executive order", 0.0), ("new regulation", -.3)],
}
_MP_RX = {k: [(re.compile(r"(?<![a-z0-9])(?:" + p + r")"), v) for p, v in lst] for k, lst in MACRO_POLARITY.items()}


def macro_polarity(event_type: str, text_n: str) -> Optional[float]:
    rules = _MP_RX.get(event_type)
    if not rules:
        return None
    tot, n = 0.0, 0
    for rx, val in rules:
        m = rx.search(text_n)
        if not m:
            continue
        if val == 0.0:
            n += 1
            continue
        v = -val * 0.7 if negated(text_n, m.start()) else val
        if hedged(text_n, m.start()):
            v *= 0.6
        tot += v
        n += 1
    if n == 0:
        return None
    return max(-1.0, min(1.0, tot))


def regions(text_norm: str) -> List[str]:
    return [r for r, kws in REGIONS.items() if any(has_phrase(text_norm, k.strip()) for k in kws)]


@functools.lru_cache(maxsize=None)
def _pattern_rx(pat: str):
    """Compiled once per pattern (this used to be rebuilt and re-normalised for every pattern of every event type for every headline)."""
    return re.compile(r"(?<![a-z0-9])(?:" + (pat if any(ch in pat for ch in "\\()|?+*[") else re.escape(norm(pat)).replace(r"\ ", r"\s+")) + r")(?![a-z0-9])")


def _score_type(et: EventType, title_n: str, body_n: str):
    """Returns (score, first_match_start_in_title_or_body, matched_text)."""
    score, start, hit = 0.0, None, ""
    for pat in et.patterns:
        rx = _pattern_rx(pat)
        m = rx.search(title_n)
        if m:
            score += 2.0
            if start is None:
                start, hit = ("t", m.start()), m.group(0)
        elif body_n and (m := rx.search(body_n)):
            score += 1.0
            if start is None:
                start, hit = ("b", m.start()), m.group(0)
    return score, start, hit


# SEC filing headers ("8-K - ACME Acquisition Corp (0001234567) (Filer)") are HALF of everything ingested and carry no news in the title. They used to be classified from the COMPANY'S NAME:
# "Acquisition Corp" became a merger, "Item 5.07: vote" an election, "Private Credit Fund" credit stress. The 8-K ITEM numbers in the summary are what they actually say.
_FILING = re.compile(r"^(?:8-K|8-K/A|10-K|10-K/A|10-Q|10-Q/A|20-F|6-K|40-F|[345]|[345]/A|SC 13[DG](?:/A)?|SCHEDULE 13[DG](?:/A)?|S-1|S-1/A|S-3|S-3ASR|S-8|424B\d|FWP|DEF 14A|DEFA14A|DEF 14C|PRE 14A|13F-HR|NT 10-[KQ]|SD|11-K|D|144|CORRESP|UPLOAD|EFFECT|25-NSE|15-12[BG]|N-[A-Z0-9-]+|497[A-Z]*|POS AM|ARS|X-17A-5) - ")
# 8-K items that are genuinely adverse: (direction, magnitude, what it says). Everything else (results, agreements, officers, Reg FD, exhibits, votes) is neutral until the actual document is read.
_ADVERSE_ITEMS = {"1.03": (-0.90, 0.85, "bankruptcy or receivership"), "3.01": (-0.60, 0.70, "delisting notice"), "4.02": (-0.70, 0.75, "non-reliance on previously issued financial statements"),
                  "2.04": (-0.50, 0.55, "triggering event that accelerates a debt obligation"), "2.06": (-0.40, 0.50, "material impairment"), "1.02": (-0.30, 0.40, "termination of a material agreement")}


def _analyze_filing(title: str, summary: str) -> Dict:
    a = _analyze_text("", "")                                                           # a neutral baseline in exactly the shape every caller expects
    items = re.findall(r"Item (\d\.\d\d)", summary or "")
    worst = min((_ADVERSE_ITEMS[i] for i in items if i in _ADVERSE_ITEMS), default=None, key=lambda v: v[0])
    a.update({"event_type": "sec_filing", "scope": "company", "direction": 0.0, "magnitude": 0.10, "filing_items": sorted(set(items))})
    if worst and re.match(r"^8-K", title):
        a.update({"event_type": "bankruptcy_distress", "direction": worst[0], "magnitude": worst[1], "filing_note": worst[2]})
    return a


def analyze(title: str, summary: str = "") -> Dict:
    if _FILING.match(title or ""):
        return _analyze_filing(title, summary)
    return _analyze_text(title, summary)


def _analyze_text(title: str, summary: str = "") -> Dict:
    """Classify an item. Returns dict(event_type, scope, direction[-1..1], magnitude[0..1], amount_usd, regions, matched, negated, hedged,
    decay_days, power, horizon)."""
    title_n, body_n = norm(title) + (" qmarkend" if (title or "").rstrip().endswith("?") else ""), norm(summary)[:1500]
    full = (title_n + " . " + body_n).strip()
    best, best_s, best_info = GENERAL, 0.0, (None, "")
    for et in EVENT_TYPES:
        s, st, hit = _score_type(et, title_n, body_n)
        if s > 0 and et.name in RECAP_TYPES:
            s += 4.0                                                    # a recap outranks the catalyst words it quotes ("record results", "raises outlook")
        if s > 0 and et.name in SHOCK_TYPES:
            s = 0.0 if _RECOVERY.search(title_n) else s + 4.0          # a market shock outranks any company-level wording; "rebounds"/"ceasefire" headlines are not shocks
        if s > 0:
            s += 0.15 * et.magnitude + (0.3 if et.scope == "company" else 0)  # specific beats generic on ties
            if s > best_s:
                best, best_s, best_info = et, s, (st, hit)
    st, hit = best_info
    neg = hed = False
    if st is not None:
        text_for_pos = title_n if st[0] == "t" else body_n
        neg = negated(text_for_pos, st[1])
        hed = hedged(text_for_pos, st[1])
    # lexical polarity
    p = n = 0.0
    for w, v in POS_WORDS.items():
        if has_phrase(full, w):
            p += v
    for w, v in NEG_WORDS.items():
        if has_phrase(full, w):
            n += v
    lex = (p - n) / (p + n + 1.0)
    mp = macro_polarity(best.name, full) if best.scope == "macro" else None
    if mp is not None:
        direction = 0.8 * mp + 0.2 * lex
    elif best.direction:
        direction = best.direction * (0.65 + 0.35 * abs(lex) * (1 if lex * best.direction >= 0 else -1.2))
        if neg:
            direction = -direction * 0.7
    else:
        direction = lex
        if neg:
            direction = -direction * 0.7
    if best.name in RECAP_TYPES and best_s > 0:
        direction = 0.0
    if best.name in SHOCK_TYPES and best_s > 0:
        direction = 0.0 if neg else -0.85                              # a shock is risk-off, whatever the surrounding vocabulary ("jumps", "surges"...) says
    if hed and mp is None and best.name not in SHOCK_TYPES:
        direction *= 0.6
    reaction, contradictory, priced_in = price_reaction(title_n), False, False
    if reaction and best.scope in ("company", "sector") and best.name not in SHOCK_TYPES and best.name not in RECAP_TYPES and direction != 0:
        sgn, pct, strong = reaction
        if sgn * direction < 0:                                     # "good news" but the stock FELL (or the reverse): the headline contradicts itself, so it is not a clean signal
            direction, contradictory = 0.0, True
        elif (pct or 0) >= 0.10:                                    # the stock ALREADY moved 10%+ on this news (it says so in the headline): that is a move that happened, not a signal
            direction, priced_in = 0.0, True
        elif strong or (pct or 0) >= 0.05:                          # a big move has already happened: much of the edge is priced in
            direction *= 0.6
    amount = extract_amount(title + " " + (summary or ""))
    mag = best.magnitude
    if amount:
        mag += min(0.30, max(0.0, math.log10(amount / 1e6) / 12.0)) * 1.6
    if any(has_phrase(full, b) for b in BOOST):
        mag += 0.12
    if any(has_phrase(full, d) for d in DAMP):
        mag -= 0.15
    if hed:
        mag *= 0.8
    return {"event_type": best.name, "scope": best.scope, "direction": max(-1.0, min(1.0, direction)),
            "magnitude": max(0.05, min(1.0, mag)), "amount_usd": amount, "regions": regions(full), "matched": hit,
            "negated": neg, "hedged": hed, "decay_days": best.decay_days, "power": best.power, "horizon": best.horizon,
            "type_score": best_s, "reaction": reaction[0] * (reaction[1] or 0.0) if reaction else None, "contradictory": contradictory, "priced_in": priced_in}


_NEG_MOVE = r"(?:slump|plung|plummet|tumbl|sink|sank|drop|fall|fell|crater|tank|dive|slid|slip|sag|crash|retreat|erase|lose|lost|sell ?off|sold off|slashe)\w*"
_POS_MOVE = r"(?:jump|soar|surg|spik|rall|climb|gain|pop|rocket|skyrocket|leap|ris|rose|advanc|rebound)\w*"
_STRONG_MOVE = re.compile(r"(?:plung|plummet|crater|soar|surg|skyrocket|crash|tank|rocket)")
_REACTION = re.compile(r"(?:(?:stock|shares|stocks)\s+(?:[\w']+\s+){0,2}?|(?<![a-z])[a-z][a-z0-9']*\s+)(" + _NEG_MOVE + "|" + _POS_MOVE + r")(?:\s+(?:by|about|nearly|over|more than|as much as))?(?:\s+(\d+(?:\.\d+)?)\s*%)?")


def price_reaction(title_n: str):
    """The stock's own move as reported IN THE HEADLINE ("GOOGL Stock Slumps 11% After-Hours", "shares jump 8%"): (+1 / -1, fraction or None, strong?) or None.
    A move needs the word stock/shares before the verb, or a percentage after it, so "raises guidance" and "price rise in the cost of..." are not reactions."""
    for m in _REACTION.finditer(title_n):
        verb, pct = m.group(1), m.group(2)
        has_subject = ("stock" in m.group(0) or "shares" in m.group(0))
        if not (has_subject or pct):
            continue
        sign = -1 if re.fullmatch(_NEG_MOVE, verb) else (1 if re.fullmatch(_POS_MOVE, verb) else 0)
        if sign:
            return sign, (float(pct) / 100.0 if pct else None), bool(_STRONG_MOVE.search(verb))
    return None


def decay_weight(age_days: float, decay_days: float) -> float:
    """Exponential decay reaching ~5% of its weight at ``decay_days`` and 0 beyond."""
    if age_days < 0:
        age_days = 0.0
    if decay_days <= 0 or age_days >= decay_days:
        return 0.0
    return math.exp(-3.0 * age_days / decay_days)
