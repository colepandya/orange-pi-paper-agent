"""Is this story an EVENT worth reasoning about? Identifying the company correctly does not make an article tradable: a question, a stock-pick list, a market roundup, an earnings-call recap, a promotional statement,
a routine price recap, a company comparison, or a passing mention reports nothing that has happened. Unless a concrete, current event is stated, the engine abstains. ONE function, used by the analyst and by
candidate creation, so the two cannot disagree. Rules look at the structure of the headline (its form, its subject and verb, whether an event type was matched), not at isolated trigger words."""
from __future__ import annotations

import re
from typing import Dict, Optional

_INTERROGATIVE = re.compile(r"^(?:is|are|was|were|should|will|can|could|does|did|would|why|what|how|where|who|which|when|here'?s (?:why|what|how)|everything you need to know)\b", re.I)
_LIST = re.compile(r"^(?:\d+|two|three|four|five|six|seven|eight|nine|ten)\s+(?:[\w-]+\s+){0,3}?(?:stocks?|reasons?|picks?|companies|etfs?|names)\b|\b(?:best|top|better|cheapest|hottest|safest)\s+(?:[\w-]+\s+){0,4}?(?:stocks?|etfs?|funds?)\b|\bstocks? to (?:buy|watch|own|avoid|sell)\b", re.I)
_ROUNDUP = re.compile(r"\b(?:what'?s moving (?:markets?|stocks)|stocks? (?:making|on) the move|market (?:wrap|recap|update|open|close|snapshot)|top (?:movers|gainers|losers)|\bmovers\b|biggest moves|premarket (?:movers|report|wrap|update|stocks?)|pre-market (?:movers|report|wrap|update|stocks?)|after-?hours movers|wall street (?:today|week)|stocks? to watch|futures (?:point|rise|fall|slip))\b", re.I)
_SOLICITATION = re.compile(r"\b(?:investor|shareholder) alert\b|\battention\b.{0,40}\binvestors?\b|\bclass action\b.{0,60}\b(?:reminds?|announces?|contact|deadline|filing)\b|\b(?:law (?:firm|offices?)|llp|pllc)\b.{0,60}\b(?:reminds?|announces|investigat\w+)\b|\bpomerantz\b|\brosen law\b|\bbragar\b|\bfaruqi\b|\bgainey\b|\blevi (?:&|and) korsinsky\b|\bberger montague\b|\bbronstein\b", re.I)
_PRICE_LEVEL = re.compile(r"\b(?:hits?|hit|reach(?:es|ed)?|touch(?:es|ed)?|set|sets)\s+(?:an?\s+|new\s+)?(?:all-time|record|52-week|multi-year|\d+-\w+)\s+(?:highs?|lows?)\b|\bwip(?:es|ing|ed)\s+(?:a record\s+)?\$?[\d.,]+\s?[bm]\w*\s+from\b", re.I)
_COMMENTARY = re.compile(r"\bahead of (?:\w+\s+){0,1}(?:earnings|results|q[1-4])\b|\b(?:imminent|what to expect|preview|set to report|will report|expected to report|due to report|are imminent|is imminent)\b.{0,50}\b(?:earnings|results)\b|\b(?:earnings|results)\b.{0,30}\b(?:are|is) (?:imminent|due)\b|\beveryone?thing (?:announced|you need to know|we know)\b|\beverything announced\b|\bhere'?s why\b|\bwhy the (?:stock|shares)\b|^we(?:'re| are| think| believe| like| love)\b|\bour (?:price target|view|take|top picks?)\b|\bwants?\b.{0,30}\bhere'?s why\b|\b(?:earnings call|conference call|investor call)\b.{0,30}\b(?:highlights?|transcript|takeaways?|recap|said|says)\b|^what\b.{0,40}\bsaid\b|\bkey takeaways?\b|\btakeaways? from\b|\b(?:highlights|transcript)\b$|\blive blog\b|\blive updates?\b|\bas it happened\b|\bexplained\b|\bwhat to know\b|\banalysis:|\bopinion:|\bcolumn:", re.I)
_PROMO = re.compile(r"\b(?:well[- ]positioned|poised for|poised to (?:benefit|win|lead)|optimistic|excited (?:about|to)|confident (?:in|about|of)|bullish on|a (?:great|strong) buy|set to soar|ready to (?:soar|surge|take off))\b", re.I)
_PRICE_MOVE = re.compile(r"^(?:[\w.&'’-]+\s+){1,6}?(?:stock|shares|value|market (?:cap|value)|valuation)\s+(?:[\w']+\s+){0,3}?(?:jumps?|jumped|surges?|surged|soars?|soared|plunges?|plunged|plummets?|plummeted|tumbles?|tumbled|slides?|slid|slips?|slipped|falls?|fell|drops?|dropped|rises?|rose|climbs?|climbed|gains?|gained|sinks?|sank|rall(?:y|ies|ied)|dives?|dived|slumps?|slumped|craters?)\b", re.I)
_COMPARE = re.compile(r"\bvs\.?\b|\bversus\b|\bbetter (?:buy|stock|bet|investment)\b|\bhead[- ]to[- ]head\b|\bcompared (?:with|to)\b|\bwhich (?:is|one|stock)\b|\bor\b.{0,25}\?$", re.I)
_MOVE_PCT = re.compile(r"\b(?:rises?|gains?|falls?|drops?|jumps?|slips?|surges?|climbs?|sinks?|slides?|holds?|steady)\b\s*\d+(?:\.\d+)?\s?%", re.I)
_FORM = re.compile(r"^(424B\d|FWP)\s+-\s", re.I)
_EQUITY = re.compile(r"\b(?:common (?:stock|shares)|ordinary shares|convertible|warrants?|units|depositary shares|at-the-market|follow-?on)\b", re.I)
_EVENT_NOUN = re.compile(r"\b(?:results?|earnings|quarter(?:ly)?|q[1-4]|revenues?|sales|profits?|loss(?:es)?|guidance|outlook|forecasts?|targets?|warning|contracts?|orders?|awards?|deals?|acquisitions?|mergers?|takeover|buyout|lawsuits?|settlement|recall|approval|investigation|probe|fines?|bankruptcy|layoffs?|job cuts|dividends?|buyback|repurchase|offering|agreements?|partnership|strikes?|outage|breach|hack|shutdown|resign\w*|appoint\w*|ceo|cfo)\b", re.I)
_GENERIC = {"general_news", "other", "opinion_piece", "price_recap"}
KINDS = ("routine_filing", "solicitation", "question", "stock_list", "roundup", "commentary", "promotional", "price_recap", "comparison", "background", "no_concrete_event")


def assess(title: str, summary: str, event_type: str, n_tickers: int, has_event_pattern: bool, grounded: Optional[float] = None) -> Dict[str, object]:
    t = (title or "").strip()
    et = event_type or "general_news"
    mf = _FORM.match(t)
    if mf:
        form = mf.group(1).upper()
        if form in ("424B2", "424B3", "424B1", "FWP", "424B8") or (form in ("424B4", "424B5", "424B7") and not _EQUITY.search((title or "") + " " + (summary or ""))):
            return {"worthy": False, "kind": "routine_filing", "why": "a routine prospectus supplement or free-writing prospectus (structured notes and the like) reports no event about the company"}
    ev = et not in _GENERIC or bool(_EVENT_NOUN.search(t))   # a concrete event: the classifier typed one, or the headline itself names an event (results, loss, guidance, contract, recall, probe ...)
    def no(kind: str, why: str) -> Dict[str, object]:
        return {"worthy": False, "kind": kind, "why": why}
    if t.endswith("?") or _INTERROGATIVE.match(t):
        return no("question", "a question or explainer headline reports nothing that has happened")
    if _SOLICITATION.search(t):
        return no("solicitation", "a law-firm class-action solicitation reports no event about the company's business")
    if _LIST.search(t):
        return no("stock_list", "a list of stock picks is not an event")
    if _ROUNDUP.search(t) or (n_tickers >= 4 and not ev) or len(_MOVE_PCT.findall(t)) >= 2:
        return no("roundup", "a market recap or roundup naming several companies reports no single event")
    if _COMMENTARY.search(t):
        return no("commentary", "commentary or a recap of something already reported, not a new event")
    if et == "price_recap" or _PRICE_LEVEL.search(t) or (_PRICE_MOVE.match(t) and not ev):
        return no("price_recap", "a price move with no event of its own stated")
    if _COMPARE.search(t) and n_tickers >= 2 and not ev:
        return no("comparison", "a comparison of companies is not an event")
    if _PROMO.search(t) and not ev:
        return no("promotional", "a statement of optimism, not an event")
    if grounded is not None and grounded < 0.5 and not ev:
        return no("background", "the company is only mentioned in passing")
    if not ev and not has_event_pattern:
        return no("no_concrete_event", "no concrete current event is stated")
    return {"worthy": True, "kind": "concrete_event", "why": "a concrete event (%s) is stated" % et.replace("_", " ")}
