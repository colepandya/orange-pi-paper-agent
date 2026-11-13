"""How good is this piece of news as a basis for a trade? Deterministic, explainable, no language model.

The engine used to know only WHICH FEED an item came through. It had no view of the item itself: "Is NVIDIA stock a buy now? 3 stocks that could soar!" and "NVIDIA wins a $2 billion contract, the company said in a statement" scored the same except
for the keywords that classify the event. This scores five things, each with an explicit formula so it can be checked by hand, tested, and (lab study `news_quality`) validated against what the stock did afterwards:

  specificity   (25%)  concrete facts: an amount, a percentage, a count, a date, a deal term.  1 - 0.5**n, n = distinct kinds of fact found (0-6)
  attribution   (20%)  0.6 to start; +0.3 primary-source language (filed, announced, "said in a statement"); -0.35 anonymous sourcing (sources, reportedly, rumour); -0.2 a hedged headline (may, could, in talks)
  tone          (15%)  1.0 minus 0.15 per sensational word (at most 3), an exclamation mark, three or more ALL-CAPS words, a question headline; minus 0.1 for promotional wording
  subject       (25%)  is the company the subject? 1.0 named in the first six words of the headline, 0.7 later in it, 0.35 only in the summary, 0.15 absent; 0.5 when the names are unknown
  type          (15%)  1.0 a primary filing; 0.8 reported news; 0.5 an analyst note (price target, upgrade); 0.25 opinion or a listicle

Each component is 0 to 1; the score is their weighted sum. It is a MEASURE OF THE TEXT, not a forecast: whether it predicts anything is what the lab's news_quality study tests as outcomes accumulate.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

WEIGHTS = {"specificity": 0.25, "attribution": 0.20, "tone": 0.15, "subject": 0.25, "type": 0.15}

_MONEY = re.compile(r"\$\s?\d|\b\d+(?:\.\d+)?\s?(?:million|billion|trillion|bn|mln)\b", re.I)
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s?(?:%|percent\b)", re.I)
_COUNT = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d{5,}\b")
_DATE = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|\bq[1-4]\b|\b(?:19|20)\d\d\b", re.I)
_TERMS = {"eps", "revenue", "guidance", "backlog", "margin", "contract", "order", "acquire", "acquires", "acquisition", "merger", "dividend", "buyback", "offering", "lawsuit", "recall", "approval", "approved",
          "fda", "settlement", "bankruptcy", "layoffs", "outlook", "forecast", "earnings", "profit", "loss", "deal", "agreement"}
_ANON = ("sources", "people familiar", "person familiar", "unnamed", "anonymous", "insiders", "according to reports", "reportedly", "rumor", "rumour", "speculation", "allegedly", "unconfirmed")
_PRIMARY = ("said in a statement", "according to a statement", "according to the company", "press release", "filed", "filing", "8-k", "10-q", "10-k", "announces", "announced", "completes", "completed", "signed")
_HEDGE = re.compile(r"\b(?:may|might|could|would|set to|poised to|eyes|weighs|considering|exploring|in talks)\b")
_SENS = re.compile(r"\b(?:shocking|stunning|massive|explosive|skyrocket(?:s|ed)?|plummet(?:s|ed)?|plunge(?:s|d)?|soar(?:s|ed)?|surge(?:s|d)?|crash(?:es|ed)?|bombshell|jaw-dropping|game-changing|revolutionary|must-buy|secret|urgent|warning)\b"
                   r"|you won't believe", re.I)
_PROMO = ("world-class", "leading provider", "industry-leading", "cutting-edge", "best-in-class", "excited to announce", "thrilled to announce", "proud to announce")
_OPINION = ("opinion:", "analysis:", "commentary", "why i ", "should you", "is it time to", "here's why", "here are", "what to know", "3 stocks", "5 stocks", "7 stocks", "10 stocks", "best stocks", "top stocks",
            "stocks to buy", "buy now", "motley fool", "zacks rank", "what it means", "everything you need", "explained")
_RATING = ("price target", "upgrade", "downgrade", "initiates coverage", "reiterates", "maintains")


def _s(x) -> str:
    return x if isinstance(x, str) else ""


def specificity(text: str) -> float:
    low = text.lower()
    kinds = sum(1 for p in (_MONEY, _PERCENT, _COUNT, _DATE) if p.search(text))
    terms = len({w for w in re.findall(r"[a-z]+", low) if w in _TERMS})
    return 1.0 - 0.5 ** (kinds + min(2, terms))


def attribution(title: str, text: str) -> float:
    low = text.lower()
    a = 0.6
    if any(m in low for m in _PRIMARY):
        a += 0.3
    if any(m in low for m in _ANON):
        a -= 0.35
    if _HEDGE.search(title.lower()):
        a -= 0.2
    return max(0.0, min(1.0, a))


def tone(title: str, text: str) -> float:
    low = text.lower()
    t = 1.0 - 0.15 * min(3, len({m.group(0).lower() for m in _SENS.finditer(title)}))
    if "!" in title:
        t -= 0.15
    if len(re.findall(r"\b[A-Z]{4,}\b", title)) >= 3:
        t -= 0.15
    if title.rstrip().endswith("?"):
        t -= 0.15
    if any(p in low for p in _PROMO):
        t -= 0.1
    return max(0.0, min(1.0, t))


def _names(subjects) -> List[str]:
    if isinstance(subjects, str):
        subjects = [subjects]
    try:
        return [n.strip() for n in subjects if isinstance(n, str) and len(n.strip()) >= 2]
    except TypeError:                                                                                    # not iterable (a number, an object): no names
        return []


def subject_score(title: str, summary: str, subjects: Optional[Iterable[str]]) -> float:
    names = _names(subjects)
    if not names:
        return 0.5
    tl, sl, best = title.lower(), summary.lower(), 0.15
    for n in names:
        pat = re.compile(r"\b" + re.escape(n.lower()) + r"\b")
        m = pat.search(tl)
        if m:
            best = max(best, 1.0 if len(tl[:m.start()].split()) < 6 else 0.7)
        elif pat.search(sl):
            best = max(best, 0.35)
    return best


def type_score(title: str, primary: bool) -> float:
    if primary:
        return 1.0
    low = title.lower()
    if any(m in low for m in _OPINION):
        return 0.25
    if any(m in low for m in _RATING):
        return 0.5
    return 0.8


def assess(title, summary="", subjects: Optional[Iterable[str]] = None, primary: bool = False) -> Dict:
    """The quality of one news item: a score from 0 to 1, its five components, and plain-language flags. Never raises."""
    title, summary = _s(title)[:300], _s(summary)[:600]
    text = title + " " + summary
    comp = {"specificity": specificity(text), "attribution": attribution(title, text), "tone": tone(title, text), "subject": subject_score(title, summary, subjects), "type": type_score(title, primary)}
    score = sum(WEIGHTS[k] * comp[k] for k in WEIGHTS)
    low, flags = text.lower(), []
    if comp["specificity"] >= 0.75:
        flags.append("specific facts")
    elif comp["specificity"] == 0.0:
        flags.append("no concrete facts")
    if any(m in low for m in _PRIMARY):
        flags.append("primary-source language")
    if any(m in low for m in _ANON):
        flags.append("anonymous sourcing")
    if _HEDGE.search(title.lower()):
        flags.append("hedged headline")
    if comp["tone"] < 0.85:
        flags.append("sensational or promotional wording")
    if comp["type"] <= 0.25:
        flags.append("opinion or listicle")
    elif comp["type"] == 0.5:
        flags.append("analyst note")
    if comp["subject"] <= 0.35:
        flags.append("company only mentioned in passing")
    return {"score": score, "components": comp, "flags": flags, "tier": "high" if score >= 0.75 else "medium" if score >= 0.5 else "low"}
