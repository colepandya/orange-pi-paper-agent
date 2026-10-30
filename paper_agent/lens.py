"""Article lens: read the BODY of a news article and say what it actually reports, with no LLM.

A headline is a claim; the article holds the numbers. This module turns article text into a small set of auditable FACTS (earnings vs the estimate, guidance changes, deals, contracts, buybacks,
analyst actions, adverse events), each with a polarity and a weight, and combines them into a direction / magnitude / confidence. It understands the things that make keyword reading wrong:
negation ("did not miss"), hedging ("in talks", "could", "reportedly"), attribution ("an analyst said"), historical context, which company a sentence is about, and the difference between a
news report, a press release, an opinion piece, a listicle and a sponsored post. Every fact keeps the sentence it came from, so a person can check it.

Stdlib only; deterministic; fast. It is deliberately narrower than an LLM: it extracts what it can verify and says nothing about the rest."""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Callable, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------------- text helpers

_ABBREV = {"u.s", "u.k", "inc", "corp", "co", "ltd", "plc", "mr", "mrs", "ms", "dr", "st", "no", "vs", "etc", "e.g", "i.e", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "gov", "sen", "rep", "gen"}
_SENT_END = re.compile(r"([.!?])(?=\s+[\"'“‘(\[]?[A-Z0-9$])")


def sentences(text: str) -> List[str]:
    """Split on sentence ends, but not after abbreviations ("Inc.", "U.S.") or inside numbers ("$2.31")."""
    out, buf = [], 0
    text = re.sub(r"\s+", " ", text or "").strip()
    for m in _SENT_END.finditer(text):
        end = m.end(1)
        head = text[buf:end]
        last = re.findall(r"([A-Za-z.]+)\.$", head)
        if last and last[-1].lower().rstrip(".") in _ABBREV:
            continue
        if re.search(r"\b[A-Z]\.$", head):                      # an initial: "J. Smith"
            continue
        out.append(head.strip())
        buf = end
    tail = text[buf:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if len(s) > 2]


_UNIT = {"trillion": 1e12, "tn": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mn": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
_MONEY = re.compile(r"(?:US)?[$€£]\s?(\d[\d,]*(?:\.\d+)?)\s?(trillion|billion|million|thousand|tn|bn|mn|[bBmMkK](?![A-Za-z]))?", re.I)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s?(?:%|percent|per cent)", re.I)


def money_values(s: str) -> List[Tuple[float, int, int]]:
    """[(value in dollars, start, end)] for every money amount in a sentence ($2.31, $1.2 billion, $950M)."""
    out = []
    for m in _MONEY.finditer(s):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        u = (m.group(2) or "").lower()
        out.append((v * _UNIT.get(u, 1.0), m.start(), m.end()))
    return out


def _tail_cents(s: str) -> List[Tuple[float, int, int]]:
    return [(float(m.group(1)) / 100.0, m.start(), m.end()) for m in re.finditer(r"\b(\d+(?:\.\d+)?)\s?cents\b", s, re.I)]


# ----------------------------------------------------------------------------- context: negation, hedging, attribution

_NEG = re.compile(r"\b(?:not|no|never|neither|nor|without|isn'?t|wasn'?t|didn'?t|doesn'?t|don'?t|won'?t|hasn'?t|haven'?t|hadn'?t|fails? to|failed to|no evidence of)\b", re.I)
_DENIAL = re.compile(r"\b(?:denies|denied|deny|denying|rejects?|rejected|refutes?|refuted|disputes?|disputed|contests?|contested|dismiss(?:es|ed)? (?:the )?(?:claims?|allegations?|accusations?))\b", re.I)
_HEDGE = re.compile(r"\b(?:could|may|might|reportedly|rumou?red?s?|allegedly|according to (?:people|sources|a person|two people|three people|the wall street journal|bloomberg)|people familiar|sources? (?:said|say|told)|in (?:advanced )?talks|"
                    r"exploring|considering|weighing|plans? to|planning to|expected to|likely to|potential(?:ly)?|possible|possibly|if |hopes? to|aims? to|seeks? to|may be|is said to|set to|poised to|preparing to|eyeing)\b", re.I)
_ATTRIB = re.compile(r"\b(?:analysts?|strategists?|investors?|economists?|experts?|traders?|brokers?|commentators?|pundits?|observers?)\b[^.]{0,40}\b(?:said|says|say|expect|expects|believe|believes|think|thinks|predict|predicts|note[sd]?|argue[sd]?|warn(?:ed|s)?|see|sees)\b", re.I)
_HIST = re.compile(r"\b(?:last year|a year (?:ago|earlier)|year-ago|previously|in (?:19|20)\d\d\b|earlier this year|last (?:quarter|month|decade)|historically|back in)\b", re.I)
_CONTRAST = re.compile(r"\b(?:but|however|although|though|despite|even as|even though|while|yet|nevertheless|still)\b", re.I)


def _window(s: str, pos: int, width: int = 60) -> str:
    return s[max(0, pos - width):pos]


def _negated(s: str, pos: int) -> bool:
    """A negation word within the three words before the cue ("did NOT miss", "WITHOUT a recall")."""
    return bool(_NEG.search(" ".join(_window(s, pos, 60).split()[-3:])))


def _denied(s: str, pos: int) -> bool:
    return bool(_DENIAL.search(_window(s, pos, 80)))


def _weight(s: str, pos: int, base: float = 1.0) -> float:
    """How much to trust a fact found at ``pos`` in sentence ``s``: hedged, attributed to someone else, or historical statements count for much less."""
    w = base
    if _HEDGE.search(s[max(0, pos - 90):pos + 50]):
        w *= 0.4
    if _ATTRIB.search(s[max(0, pos - 90):pos + 70]):
        w *= 0.55
    if _denied(s, pos):
        w *= 0.7                                                # the company disputes it: still news, less certain
    if _HIST.search(_window(s, pos, 50) + s[pos:pos + 40]):
        w *= 0.3
    if s.count('"') >= 2 or "“" in s:                          # inside a quotation: someone's words
        w *= 0.7
    return w


# ----------------------------------------------------------------------------- the fact extractors

class Fact(dict):
    """kind, pol (-1..1 polarity), w (0..1 weight), mag (0..1 importance), text (the source sentence, trimmed), plus kind-specific numbers."""


def _fact(kind: str, pol: float, w: float, mag: float, sent: str, **kw) -> Fact:
    return Fact(kind=kind, pol=round(max(-1.0, min(1.0, pol)), 3), w=round(max(0.0, min(1.0, w)), 3), mag=round(max(0.0, min(1.0, mag)), 3), text=sent[:220], **kw)


_EST = re.compile(r"\b(?:expected|expectations?|estimates?|estimated|consensus|analysts?|lseg|refinitiv|factset|visible alpha|zacks|polled|predicted|street|anticipated|had (?:expected|forecast|predicted))\b", re.I)    # NOT "forecast": "cut its forecast to $5.0B from $5.4B" is guidance, not a beat
_BEAT = re.compile(r"\b(?:beat|beats|beating|topped|tops|topping|exceeded|exceeds|exceeding|surpassed|surpasses|above|ahead of|outpaced|outperformed|better than|stronger than|higher than|trounced|smashed|cleared)\b", re.I)
_MISS = re.compile(r"\b(?:missed|misses|missing|fell short|falls short|lagged|trailed|below|short of|lower than|worse than|weaker than|disappointed|disappointing|undershot|came in under|shy of)\b", re.I)
_EPS_CUE = re.compile(r"\b(?:eps|earnings per share|profit per share|a share|per share|per diluted share|adjusted earnings|earnings of|earned|net income|profit of|adjusted profit)\b", re.I)
_REV_CUE = re.compile(r"\b(?:revenues?|sales|net sales|total revenue|bookings|turnover)\b", re.I)


def _surprise_pol(s: float) -> float:
    a = abs(s)
    p = 0.12 if a < 0.02 else 0.3 if a < 0.05 else 0.5 if a < 0.10 else 0.7 if a < 0.20 else 0.85
    return p if s >= 0 else -p


def _compare_numbers(sent: str, cue: re.Pattern, scale_ok: Callable[[float], bool]) -> Optional[Tuple[float, float, int]]:
    """Find (actual, estimate, position) in a sentence that states a result next to an expectation. None when the sentence does not hold both."""
    if not cue.search(sent):
        return None
    nums = money_values(sent) + _tail_cents(sent)
    signed = []
    for v, a, b in nums:                                            # "a loss of $0.30 a share" is -0.30: a NARROWER loss than expected is a beat, not a miss
        if re.search(r"\b(?:loss(?:es)?|deficit|red ink)\b[^.$]{0,24}$", sent[max(0, a - 32):a], re.I) or re.search(r"\(\s*$", sent[max(0, a - 2):a]) or re.search(r"-\s?$", sent[max(0, a - 2):a]):
            v = -abs(v)
        signed.append((v, a, b))
    nums = sorted(set((round(v, 6), a, b) for v, a, b in signed), key=lambda x: x[1])
    nums = [n for n in nums if scale_ok(n[0])]
    if len(nums) < 2:
        return None
    marks = [m.start() for m in _EST.finditer(sent)]
    if not marks:
        return None
    best = None
    for i, (v, a, b) in enumerate(nums):
        d = min(abs(a - mk) for mk in marks)
        if best is None or d < best[0]:
            best = (d, i)
    est_i = best[1]
    act_i = 0 if est_i != 0 else 1
    if est_i == act_i:
        return None
    return nums[act_i][0], nums[est_i][0], nums[act_i][1]


def extract_earnings(sent: str) -> List[Fact]:
    out: List[Fact] = []
    low = sent
    # per-share result vs estimate
    cmp_ = _compare_numbers(low, _EPS_CUE, lambda v: 0.0 < abs(v) < 500.0)
    if cmp_ and not re.search(r"\b(?:billion|million)\b", re.sub(r"\$\s?\d[\d,.]*\s?(?:billion|million)", "", low, flags=re.I)[:0] or "x", re.I):
        act, est, pos = cmp_
        s = (act - est) / max(abs(est), 0.01)
        w = _weight(low, pos)
        if _negated(low, pos) and (_BEAT.search(low) or _MISS.search(low)):
            s = -s
        verb = 1 if _BEAT.search(low) and not _MISS.search(low) else -1 if _MISS.search(low) and not _BEAT.search(low) else 0
        if verb and (verb > 0) != (s >= 0) and abs(s) > 0.005:
            w *= 0.5                                            # words and numbers disagree: trust the numbers, trust the sentence less
        out.append(_fact("eps_surprise", _surprise_pol(s), 0.95 * w, min(1.0, 0.35 + abs(s) * 2.2), sent, actual=act, estimate=est, surprise=round(s, 4)))
        return out
    # revenue vs estimate (amounts in the millions or more)
    cmp_ = _compare_numbers(low, _REV_CUE, lambda v: abs(v) >= 1e6)
    if cmp_:
        act, est, pos = cmp_
        s = (act - est) / max(abs(est), 1.0)
        w = _weight(low, pos)
        verb = 1 if _BEAT.search(low) and not _MISS.search(low) else -1 if _MISS.search(low) and not _BEAT.search(low) else 0
        if verb and (verb > 0) != (s >= 0) and abs(s) > 0.003:
            w *= 0.5
        out.append(_fact("revenue_surprise", _surprise_pol(s * 1.5), 0.75 * w, min(1.0, 0.3 + abs(s) * 4.0), sent, actual=act, estimate=est, surprise=round(s, 4)))
        return out
    # beat / miss with no usable numbers ("topped expectations", "missed estimates by 12 cents")
    if _EST.search(low) and (_EPS_CUE.search(low) or _REV_CUE.search(low) or re.search(r"\b(?:results?|quarter|earnings|profit)\b", low, re.I)):
        beat, miss = _BEAT.search(low), _MISS.search(low)
        if beat and not miss:
            pos = beat.start()
            neg = _negated(low, pos)
            out.append(_fact("beat_generic", -0.4 if neg else 0.45, 0.7 * _weight(low, pos), 0.45, sent))
        elif miss and not beat:
            pos = miss.start()
            neg = _negated(low, pos)
            out.append(_fact("miss_generic", 0.4 if neg else -0.45, 0.7 * _weight(low, pos), 0.45, sent))
    return out


_GROWTH_NOUN = r"(?:revenues?|sales|net income|profit|profits|earnings|ebitda|bookings|orders|deliveries|shipments|subscribers|users|operating income|operating profit|free cash flow|same-store sales|comparable sales)"
_UP = r"(?:rose|rises|jumped|jumps|surged|surges|climbed|climbs|grew|grows|increased|increases|gained|gains|up|soared|soars|advanced|expanded|improved|swung to a profit)"
_DOWN = r"(?:fell|falls|dropped|drops|declined|declines|slumped|slumps|plunged|plunges|shrank|shrinks|down|decreased|decreases|tumbled|tumbles|slid|slides|contracted|sank|sinks|swung to a loss)"
_GROWTH = re.compile(r"\b(" + _GROWTH_NOUN + r")\b[^.;]{0,45}?\b(" + _UP + r"|" + _DOWN + r")\b[^.;]{0,30}?(\d+(?:\.\d+)?)\s?(?:%|percent|per cent)", re.I)


def extract_growth(sent: str) -> List[Fact]:
    out = []
    for m in _GROWTH.finditer(sent):
        verb = m.group(2).lower()
        up = re.fullmatch(_UP, verb, re.I) is not None
        pct = float(m.group(3))
        if pct > 1000:
            continue
        sign = 1.0 if up else -1.0
        if _negated(sent, m.start(2)):
            sign = -sign * 0.5
        pol = sign * min(0.75, 0.12 + pct / 100.0 * 0.9)
        out.append(_fact("growth", pol, 0.5 * _weight(sent, m.start()), min(0.8, 0.2 + pct / 100.0), sent, metric=m.group(1).lower(), pct=pct))
        break
    return out


_G_UP = re.compile(r"\b(?:rais(?:e|es|ed|ing)|lift(?:s|ed|ing)?|boost(?:s|ed|ing)?|increas(?:e|es|ed|ing)|hik(?:e|es|ed|ing)|upgrad(?:e|es|ed|ing))\b[^.;]{0,45}?\b(?:guidance|outlook|forecasts?|view|targets?|projections?|expectations|full-year|annual)\b", re.I)
_G_DOWN = re.compile(r"\b(?:lower(?:s|ed|ing)?|cut(?:s|ting)?|slash(?:es|ed|ing)?|reduc(?:e|es|ed|ing)|trim(?:s|med|ming)?|downgrad(?:e|es|ed|ing)|warn(?:s|ed|ing)?|withdr(?:ew|aw|awn|aws|awing)|suspend(?:s|ed|ing)?|scrap(?:s|ped|ping)?)\b[^.;]{0,45}?\b(?:guidance|outlook|forecasts?|view|targets?|projections?|expectations|full-year|annual)\b", re.I)
_G_KEEP = re.compile(r"\b(?:reaffirm(?:s|ed|ing)?|reiterat(?:e|es|ed|ing)|maintain(?:s|ed|ing)?|confirm(?:s|ed|ing)?|kept|keeps|stands by)\b[^.;]{0,45}?\b(?:guidance|outlook|forecasts?|view|targets?)\b", re.I)
_PROFIT_WARNING = re.compile(r"\bprofit warning\b|\bwarns? (?:of|on) (?:profit|earnings|sales|revenue)\b|\bwarned (?:of|on) (?:profit|earnings|sales|revenue)\b", re.I)


def extract_guidance(sent: str) -> List[Fact]:
    out = []
    sent = re.sub(r"\bprice[- ]targets?\b", "PT", sent, flags=re.I)
    down, up, keep = _G_DOWN.search(sent), _G_UP.search(sent), _G_KEEP.search(sent)
    pw = _PROFIT_WARNING.search(sent)
    pct = None
    nums = money_values(sent)
    if len(nums) >= 2 and re.search(r"\bfrom\b", sent, re.I):
        a, b = nums[0][0], nums[1][0]
        if b:
            pct = (a - b) / abs(b)
    if pw or down:
        m = pw or down
        neg = _negated(sent, m.start()) and not pw
        base = 0.62 + (min(0.25, abs(pct)) if pct is not None and pct < 0 else 0.0)
        out.append(_fact("guidance_cut", 0.3 if neg else -base, 0.9 * _weight(sent, m.start()), 0.7, sent, change=pct))
    elif up:
        neg = _negated(sent, up.start())
        base = 0.6 + (min(0.25, abs(pct)) if pct is not None and pct > 0 else 0.0)
        out.append(_fact("guidance_raise", -0.3 if neg else base, 0.9 * _weight(sent, up.start()), 0.7, sent, change=pct))
    elif keep:
        out.append(_fact("guidance_keep", 0.08, 0.5, 0.2, sent))
    return out


_DEAL_VERB = re.compile(r"\b(?:to acquire|will acquire|agreed to (?:buy|acquire)|to buy|will buy|has agreed to (?:buy|acquire)|acquires|acquired|buys|bought|takeover (?:of|bid for)|offer(?:ed|s)? to (?:buy|acquire)|bid for|merge(?:s|d)? with|to merge with|to take private|to be acquired by|to be bought by)\b", re.I)
_PREMIUM = re.compile(r"(\d+(?:\.\d+)?)\s?(?:%|percent)\s+premium|premium of (\d+(?:\.\d+)?)\s?(?:%|percent)", re.I)


def extract_deal(sent: str) -> List[Fact]:
    m = _DEAL_VERB.search(sent)
    if not m:
        return []
    amounts = [v for v, a, b in money_values(sent) if v >= 1e7]
    prem = _PREMIUM.search(sent)
    premium = float(prem.group(1) or prem.group(2)) if prem else None
    definitive = bool(re.search(r"\b(?:agreed|definitive agreement|announced|will acquire|to acquire|completes?|completed|signed)\b", sent, re.I)) and not _HEDGE.search(sent)
    w = 0.9 if definitive else 0.35 * (1.0 if not _NEG.search(sent) else 0.3)
    mag = min(1.0, 0.35 + (math.log10(max(amounts[0], 1e7)) - 7) / 6.0 if amounts else 0.35)
    return [_fact("deal", 0.0, w, mag, sent, amount=(amounts[0] if amounts else None), premium=premium, definitive=definitive, verb=m.group(0).lower(), roles="deal")]


_CONTRACT = re.compile(r"\b(?:awarded|wins?|won|secur(?:es|ed)|land(?:s|ed)|receiv(?:es|ed)|clinch(?:es|ed)|bag(?:s|ged)|signs?|signed|selected for|picked for)\b[^.;]{0,50}?\b(?:contract|order|deal|agreement|award|supply agreement|licen[sc]e)\b", re.I)


def extract_contract(sent: str) -> List[Fact]:
    m = _CONTRACT.search(sent)
    if not m:
        return []
    amounts = [v for v, a, b in money_values(sent) if v >= 1e6]
    neg = _negated(sent, m.start()) or re.search(r"\b(?:lost|loses|losing|cancel(?:s|led|ed)|terminat(?:es|ed))\b", sent, re.I)
    pol = -0.45 if neg else 0.4
    return [_fact("contract", pol, 0.8 * _weight(sent, m.start()), 0.4, sent, amount=(amounts[0] if amounts else None))]


_BUYBACK = re.compile(r"\b(?:authori[sz](?:e|es|ed|ation)|approv(?:e|es|ed)|announc(?:e|es|ed)|launch(?:es|ed)?)\b[^.;]{0,50}?\b(?:share )?(?:repurchase|buyback|buy-back)\b|\b(?:repurchase|buyback)\b[^.;]{0,30}?\b(?:program|plan|authori[sz]ation)\b", re.I)
_DIV_UP = re.compile(r"\b(?:rais(?:e|es|ed|ing)|increas(?:e|es|ed|ing)|hik(?:e|es|ed|ing)|boost(?:s|ed|ing)?|declares? (?:a )?special)\b[^.;]{0,40}?\b(?:dividend|payout)\b", re.I)
_DIV_DOWN = re.compile(r"\b(?:cut(?:s|ting)?|slash(?:es|ed|ing)?|suspend(?:s|ed|ing)?|eliminat(?:e|es|ed|ing)|scrap(?:s|ped|ping)?|reduc(?:e|es|ed|ing))\b[^.;]{0,40}?\b(?:dividend|payout)\b", re.I)


def extract_capital_return(sent: str) -> List[Fact]:
    out = []
    amounts = [v for v, a, b in money_values(sent) if v >= 1e6]
    if _DIV_DOWN.search(sent):
        out.append(_fact("dividend_cut", -0.6, 0.85 * _weight(sent, 0), 0.6, sent))
    elif _DIV_UP.search(sent):
        out.append(_fact("dividend_raise", 0.4, 0.8 * _weight(sent, 0), 0.4, sent))
    if _BUYBACK.search(sent):
        out.append(_fact("buyback", 0.4, 0.8 * _weight(sent, 0), 0.45, sent, amount=(amounts[0] if amounts else None)))
    return out


_UPGRADE = re.compile(r"\b(?:upgrad(?:es|ed|ing)|raised (?:its |the )?(?:rating|recommendation)|initiat(?:es|ed) (?:coverage )?(?:at|with|on)? ?(?:a )?(?:buy|overweight|outperform)|boosts? rating)\b", re.I)
_DOWNGRADE = re.compile(r"\b(?:downgrad(?:es|ed|ing)|cut (?:its |the )?(?:rating|recommendation)|lowered (?:its |the )?(?:rating|recommendation)|initiat(?:es|ed) (?:coverage )?(?:at|with|on)? ?(?:a )?(?:sell|underweight|underperform))\b", re.I)
_PT_UP = re.compile(r"\b(?:rais(?:es|ed)|lift(?:s|ed)|boost(?:s|ed)|hik(?:es|ed))\b[^.;]{0,30}?\bprice target\b|\bprice target (?:raised|lifted|boosted|hiked)\b", re.I)
_PT_DOWN = re.compile(r"\b(?:cut(?:s)?|lower(?:s|ed)|slash(?:es|ed)|trim(?:s|med))\b[^.;]{0,30}?\bprice target\b|\bprice target (?:cut|lowered|slashed|trimmed)\b", re.I)


_RATING_CTX = re.compile(r"\b(?:ratings?|recommendations?|overweight|underweight|outperform|underperform|equal[- ]weight|market[- ]perform|sector[- ]perform|neutral|buy|sell|hold|accumulate|analysts?|brokerage|price targets?|coverage|morgan stanley|goldman|jpmorgan|citi(?:group)?|wells fargo|barclays|ubs|bernstein|jefferies|piper|raymond james|mizuho|keybanc|wedbush|evercore|oppenheimer|bofa|bank of america|deutsche|cowen|needham|stifel|truist|rbc|bmo|cibc|scotiabank|hsbc|nomura|zacks|morningstar)\b", re.I)


def extract_analyst(sent: str) -> List[Fact]:
    out = []
    if not _RATING_CTX.search(sent):
        return out
    if _DOWNGRADE.search(sent):
        out.append(_fact("downgrade", -0.45, 0.6, 0.45, sent))
    elif _UPGRADE.search(sent):
        out.append(_fact("upgrade", 0.45, 0.6, 0.45, sent))
    if _PT_DOWN.search(sent):
        out.append(_fact("target_cut", -0.3, 0.45, 0.3, sent))
    elif _PT_UP.search(sent):
        out.append(_fact("target_raise", 0.3, 0.45, 0.3, sent))
    return out


# adverse / favourable corporate events: (kind, pattern, polarity, weight, importance)
_EVENTS: List[Tuple[str, re.Pattern, float, float, float]] = [
    ("bankruptcy", re.compile(r"\b(?:files? for|filed for|seeks?|seeking|enters?|entered) (?:chapter (?:7|11)|bankruptcy|insolvency|administration)\b|\bgoing[- ]concern (?:doubt|warning)\b|\bchapter 11\b", re.I), -0.9, 0.95, 0.95),
    ("delisting", re.compile(r"\bdelist(?:ing|ed)?\b|\bnon-?compliance with (?:nasdaq|nyse)\b|\bminimum bid price\b", re.I), -0.6, 0.85, 0.7),
    ("restatement", re.compile(r"\brestate(?:s|d|ment)?\b|\bnon-?reliance on previously issued\b|\baccounting (?:irregularit|fraud|error)", re.I), -0.7, 0.9, 0.8),
    ("investigation", re.compile(r"\b(?:sec|doj|ftc|fcc|fda|justice department|department of justice|regulators?|prosecutors?)\b[^.;]{0,50}?\b(?:investigat(?:ion|ing|es|ed)|probe|subpoena|inquiry|charges?|sues?|sued|lawsuit)\b|\b(?:subpoena|criminal probe|formal investigation|wells notice)\b", re.I), -0.5, 0.8, 0.65),
    ("lawsuit", re.compile(r"\b(?:class[- ]action|lawsuit|sued by|sues|filed suit|litigation)\b", re.I), -0.3, 0.6, 0.4),
    ("recall", re.compile(r"\brecall(?:s|ed|ing)?\b[^.;]{0,40}?\b(?:vehicles?|units?|products?|devices?|batter|cars|trucks|lots?)\b|\bsafety recall\b", re.I), -0.5, 0.8, 0.55),
    ("fda_reject", re.compile(r"\bcomplete response letter\b|\bfda (?:rejects?|rejected|declines?|declined|refuses?|refused)\b|\bclinical hold\b|\bfailed to (?:meet|achieve|reach) (?:its |the )?primary endpoint\b|\btrial (?:failed|failure)\b|\bmissed (?:its |the )?primary endpoint\b", re.I), -0.8, 0.9, 0.85),
    ("fda_approve", re.compile(r"\bfda (?:approv(?:es|ed|al)|clears?|cleared|grants?|granted)\b|\bmet (?:its |the )?primary endpoint\b|\bmeets? (?:its |the )?primary endpoint\b|\bpositive (?:topline|phase [123]|trial) (?:results|data)\b", re.I), 0.7, 0.9, 0.8),
    ("short_report", re.compile(r"\bshort[- ]seller\b[^.;]{0,50}?\b(?:report|alleg|claims?|accus)\w*|\b(?:hindenburg|muddy waters|citron|viceroy|spruce point)\b", re.I), -0.5, 0.7, 0.55),
    ("cyber", re.compile(r"\b(?:data breach|cyber-?attack|ransomware|hack(?:ed|ers)?|security incident)\b", re.I), -0.4, 0.7, 0.5),
    ("exec_exit", re.compile(r"\b(?:ceo|cfo|chief executive|chief financial officer|president)\b[^.;]{0,35}?\b(?:resign(?:s|ed)?|steps? down|stepping down|ousted|fired|abruptly (?:leaves|departs)|to depart|departure)\b", re.I), -0.35, 0.7, 0.5),
    ("layoffs", re.compile(r"\b(?:lay(?:s|ing)? off|layoffs?|job cuts?|cut(?:s|ting)? (?:\d[\d,]*|thousands of|hundreds of) (?:jobs|positions|employees|workers)|restructuring plan)\b", re.I), -0.1, 0.5, 0.3),
    ("strike_outage", re.compile(r"\b(?:(?:workers|union|employees|staff|pilots|drivers|dockworkers|machinists|nurses|teachers|labou?r)\b[^.;]{0,40}?\b(?:strike|strikes|walk(?:s|ed)? out|walkout)|on strike|go(?:es)? on strike|strike (?:action|ballot|vote)|walkout|plant (?:fire|explosion|shutdown)|production halt(?:ed)?|power outage|service outage|factory fire|supply disruption|halt(?:s|ed|ing)? production|production (?:was |were )?halt(?:ed)?|shut(?:s|ting)? down (?:its |the )?(?:plant|factory|refinery|mine|line))\b", re.I), -0.35, 0.6, 0.45),
    ("record", re.compile(r"\brecord (?:revenue|sales|profit|earnings|quarter|results|net income|bookings|deliveries|backlog)\b", re.I), 0.4, 0.7, 0.4),
    ("partnership", re.compile(r"\b(?:partner(?:s|ed|ship)? with|strategic (?:partnership|alliance|collaboration)|teams? up with|collaborat(?:es|ion) with)\b", re.I), 0.25, 0.5, 0.3),
    ("insider_buy", re.compile(r"\b(?:insider|director|ceo|cfo|chairman)\b[^.;]{0,40}?\b(?:bought|buys|purchased|purchases|acquired)\b[^.;]{0,30}?\bshares\b", re.I), 0.4, 0.6, 0.4),
    ("insider_sell", re.compile(r"\b(?:insider|director|ceo|cfo|chairman)\b[^.;]{0,40}?\b(?:sold|sells|dumped|unloaded)\b[^.;]{0,30}?\bshares\b", re.I), -0.2, 0.4, 0.25),
    ("product_launch", re.compile(r"\b(?:launch(?:es|ed)|unveil(?:s|ed)|introduc(?:es|ed)|rolls? out)\b[^.;]{0,30}?\b(?:new|latest|first)\b", re.I), 0.12, 0.35, 0.2),
]


def extract_events(sent: str) -> List[Fact]:
    out = []
    for kind, rx, pol, w, mag in _EVENTS:
        m = rx.search(sent)
        if not m:
            continue
        neg = _negated(sent, m.start())
        ww = w * _weight(sent, m.start())
        p = pol
        if neg:                                                       # "denies wrongdoing", "no evidence of fraud", "did not recall": the opposite or nothing
            p, ww = (-pol * 0.3), ww * 0.5
        if kind in ("investigation", "lawsuit") and re.search(r"\b(?:dismiss(?:ed|es)|cleared of|closes? (?:its )?(?:investigation|probe)|drops?)\b", sent, re.I):
            p, ww = abs(pol) * 0.5, ww * 0.8                          # a resolved legal cloud is mildly good news
        elif kind in ("investigation", "lawsuit") and re.search(r"\b(?:settl(?:e|es|ed|ing|ement)|agrees? to pay)\b", sent, re.I):
            p, ww = 0.0, ww * 0.5                                     # paying to settle ends the uncertainty but costs money: not a NEW negative, not a positive
        out.append(_fact(kind, p, ww, mag, sent))
    return out


_EXTRACTORS = (extract_earnings, extract_guidance, extract_growth, extract_deal, extract_contract, extract_capital_return, extract_analyst, extract_events)


# ----------------------------------------------------------------------------- article type, staleness, subject

_OPINION = re.compile(r"\b(?:i think|i believe|in my view|my view|my take|we believe|i'd buy|i would buy|i'm (?:buying|bullish|bearish)|our (?:view|take|pick)|opinion|commentary|column|editorial|op-ed|analysis:|here's why|should you buy|is it a buy|buy, sell,? or hold|stocks? to (?:buy|watch|avoid)|motley fool|zacks rank|reasons to)\b", re.I)
_SPONSORED = re.compile(r"\b(?:sponsored (?:content|post)|advertisement|paid (?:content|post|partnership)|partner content|promoted|presented by|affiliate links?|this article contains affiliate|not investment advice)\b", re.I)
_RELEASE = re.compile(r"\b(?:prnewswire|pr newswire|business wire|businesswire|globenewswire|globe newswire|accesswire|today announced|is pleased to announce|announced today|forward-looking statements)\b", re.I)
_TRANSCRIPT = re.compile(r"\b(?:operator:|question-and-answer session|earnings call transcript|prepared remarks|good (?:morning|afternoon), (?:everyone|and welcome)|thank you,? operator)\b", re.I)
_LISTICLE = re.compile(r"^\s*(?:\d+|two|three|four|five|six|seven|eight|nine|ten)\s+(?:reasons|things|stocks|ways|signs|takeaways)\b", re.I)


def classify_type(title: str, text: str) -> str:
    head = (title or "") + " " + (text or "")[:2500]
    if _SPONSORED.search(head):
        return "promo"
    if _TRANSCRIPT.search(head):
        return "transcript"
    if _LISTICLE.search(title or "") or (_OPINION.search(head) and not _RELEASE.search(head)):
        return "opinion"
    if _RELEASE.search(head):
        return "release"
    return "report"


_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_PAST_ANN = re.compile(r"\b(?:said|says|reported|announced|disclosed|filed|posted|issued|confirmed|revealed|unveiled|agreed|signed|released|published)\b[^.;]{0,40}?\b(?:on|last)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
_PAST_REL = re.compile(r"\b(?:last week|earlier this week|earlier this month|a week ago|two days ago|three days ago|days ago|weeks ago|last month|on the weekend|over the weekend)\b", re.I)


def stale_cues(paragraphs: List[str], published: Optional[dt.datetime]) -> Tuple[bool, str]:
    """True when the LEAD says the news happened days before the article was published (so the market has had time to react)."""
    lead = " ".join(paragraphs[:3])
    if not lead:
        return False, ""
    m = _PAST_ANN.search(lead)
    if m and published is not None:
        want = _DAYS.index(m.group(1).lower())
        back = (published.weekday() - want) % 7
        if m.group(0).lower().find("last ") >= 0 and back == 0:
            back = 7
        if back >= 2:
            return True, "the article says it happened on %s (%d days before it was published)" % (m.group(1).capitalize(), back)
    r = _PAST_REL.search(lead)
    if r and re.search(r"\b(?:said|reported|announced|disclosed|filed|posted|issued|confirmed|revealed|unveiled|agreed|signed)\b[^.;]{0,50}" + re.escape(r.group(0)), lead, re.I):
        return True, "the lead refers to it as already announced (%s)" % r.group(0)
    return False, ""


def _name_hits(text: str, names: List[str]) -> int:
    n = 0
    low = text.lower()
    for nm in names:
        if len(nm) >= 3:
            n += len(re.findall(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(nm.lower()), low))
    return n


def subject_scores(title: str, paragraphs: List[str], names_by_ticker: Dict[str, List[str]]) -> Dict[str, float]:
    """How much each company is what the article is ABOUT: mentions in the headline and the lead count most, mentions deep in the body least."""
    out: Dict[str, float] = {}
    lead = " ".join(paragraphs[:2])
    rest = " ".join(paragraphs[2:])
    for t, names in names_by_ticker.items():
        out[t] = 3.0 * _name_hits(title, names) + 2.0 * _name_hits(lead, names) + 0.5 * min(8, _name_hits(rest, names))
    return out


# ----------------------------------------------------------------------------- the analysis

def analyze(title: str, paragraphs: List[str], published: Optional[dt.datetime] = None, names_by_ticker: Optional[Dict[str, List[str]]] = None,
            market_cap: Optional[float] = None) -> Dict:
    """-> {type, direction, magnitude, confidence, facts[], stale, stale_why, mixed, subject{ticker: score}, chars, n_sentences}.
    ``market_cap`` (dollars) scales the importance of deal / contract / buyback sizes: $2B matters differently to a $20B company and a $2T one."""
    paragraphs = [p for p in (paragraphs or []) if p and p.strip()]
    body = " ".join(paragraphs)
    typ = classify_type(title, body)
    facts: List[Fact] = []
    for pi, para in enumerate(paragraphs[:40]):
        lead_boost = 1.0 if pi < 3 else 0.65                                  # the inverted pyramid: what matters is said first
        for s in sentences(para)[:12]:
            for ex in _EXTRACTORS:
                try:
                    for f in ex(s):
                        f["w"] = round(f["w"] * lead_boost, 3)
                        f["para"] = pi
                        facts.append(f)
                except Exception:  # noqa: BLE001 - one odd sentence must never lose the whole article
                    continue
    if title:
        for ex in _EXTRACTORS:                                               # the headline's own claims count too (at full weight: it is the lead of the lead)
            for f in ex(title):
                f["para"] = -1
                facts.append(f)
    # size matters: a deal / contract / buyback is judged against the company
    for f in facts:
        amt = f.get("amount")
        if amt and market_cap and market_cap > 0 and f["kind"] in ("deal", "contract", "buyback"):
            share = amt / market_cap
            f["pct_of_cap"] = round(share * 100, 3)
            f["mag"] = round(min(1.0, 0.15 + share * 8.0), 3)
            if f["kind"] in ("contract", "buyback"):
                f["pol"] = round(math.copysign(min(0.85, 0.12 + share * 6.0), f["pol"] or 1.0), 3)
    # drop exact repeats of the same fact (the same sentence echoed in the headline and the lead)
    seen, uniq = set(), []
    for f in sorted(facts, key=lambda f: -f["w"] * f["mag"]):
        key = (f["kind"], round(f.get("surprise", f.get("pct", f.get("amount", 0))) or 0, 3), f["text"][:60])
        if (f["kind"], f["text"][:50]) in seen:
            continue
        seen.add((f["kind"], f["text"][:50]))
        uniq.append(f)
    facts = [f for f in uniq if f["kind"] != "deal"] + [f for f in uniq if f["kind"] == "deal"]
    scoring = [f for f in facts if f["kind"] != "deal"]                      # deals are role-dependent: left to the caller (who is the target?)
    if typ in ("opinion", "promo", "transcript"):
        for f in scoring:
            f["w"] = round(f["w"] * 0.4, 3)
    seen_kind: Dict[str, int] = {}
    for f in sorted(scoring, key=lambda f: -abs(f["pol"]) * f["w"]):
        n_prev = seen_kind.get(f["kind"], 0)
        seen_kind[f["kind"]] = n_prev + 1
        f["w"] = round(f["w"] * (1.0, 0.35, 0.15, 0.08)[min(n_prev, 3)], 3)                 # 1st mention full, then 35%, 15%, 8%
    pos = sum(f["pol"] * f["w"] for f in scoring if f["pol"] > 0)
    neg = sum(-f["pol"] * f["w"] for f in scoring if f["pol"] < 0)
    net = pos - neg
    direction = math.tanh(net / 1.1)
    mixed = bool(pos > 0.25 and neg > 0.25 and min(pos, neg) / max(pos, neg) > 0.45)
    if mixed:
        direction *= 0.6
    top = max([abs(f["pol"]) * f["w"] for f in scoring] or [0.0])
    magnitude = min(1.0, top * 1.1 + 0.08 * max(0, len(scoring) - 1))
    hedged = sum(1 for p in paragraphs for s in sentences(p)[:6] if _HEDGE.search(s))
    total_s = max(1, sum(min(6, len(sentences(p))) for p in paragraphs))
    conf = 0.25 + 0.14 * min(3, len(scoring)) + (0.1 if len(paragraphs) >= 4 else 0.0) - 0.35 * (hedged / total_s) - (0.2 if mixed else 0.0)
    if typ in ("opinion", "promo", "transcript"):
        conf -= 0.25
    if not scoring:
        conf = min(conf, 0.2)
    stale, why = stale_cues(paragraphs, published)
    res = {"type": typ, "direction": round(direction, 3), "magnitude": round(magnitude, 3), "confidence": round(max(0.0, min(0.95, conf)), 3), "mixed": mixed,
           "facts": [dict(f) for f in sorted(facts, key=lambda f: -abs(f["pol"]) * f["w"])[:12]], "stale": stale, "stale_why": why,
           "chars": len(body), "paragraphs": len(paragraphs), "hedged_share": round(hedged / total_s, 3)}
    if names_by_ticker:
        res["subject"] = {t: round(v, 2) for t, v in subject_scores(title, paragraphs, names_by_ticker).items()}
    return res
