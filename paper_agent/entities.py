"""Map free text to listed companies with a confidence, avoiding the legacy false positives.

Signals (highest confidence first): exchange tag "(NASDAQ: NVDA)", cashtag "$NVDA", SEC filer CIK,
full company name, single-word name that is capitalised in the original text, bare upper-case ticker (weak).
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from .nlp import norm

# Tickers that are ordinary English words/abbreviations: only accepted via cashtag or exchange tag.
COMMON_TICKERS = set("""A AA AAL ABC ACE ADP AGO AI AIR ALL ALLY AM AN ANY ARE ARM AS AT ATO AWAY BE BIG BILL BIT BOX BRO BY CAN CAR CARS CASH CAT CC CEO
 CFO CHE COST COIN COO CORE CRM CUBE DAY DE DO DOW EAT ED EGO ELF EPAM ES EVER FAST FIVE FOR FORM FREE FUN GAP GO GOOD HAS HE HER HIT HOME HOPE HUGE
 IDEA IF IN INFO IS IT KEY KIND LIFE LIKE LINE LOW LUCK MAIN MAN MAR MAS MAX ME MORE NEW NEXT NOW OF ON ONE OPEN OR OUT OWN PAY PEAK PLAY PM POST PRO
 PUMP RARE REAL RUN SEE SHIP SO SON STAR SUM SUN TAP TEAM TECH THE TO TOP TRUE TWO UP US USA VALUE VIA VS WAS WAY WELL WHO WIN WORK YOU ZONE""".split())

COMMON_WORD_ALIASES = set("""target block general square mark gap best deal point first national united american global
 universal premier standard advanced applied digital energy power health care capital financial industries international group partners
 resources holdings services systems solutions technologies technology networks communications media entertainment pharmaceuticals therapeutics
 bio sciences""".split())

_EX = re.compile(r"\b(?:NASDAQ|NYSE|NYSE American|NYSEAMERICAN|AMEX|NYSE Arca|OTC|Nasdaq GS|Nasdaq CM|NASDAQ GM)\s*[:\-]\s*([A-Z]{1,5}(?:[.\-][A-Z])?)\b")
_CASH = re.compile(r"(?<![A-Za-z0-9])\$([A-Z]{1,5})(?![A-Za-z0-9])")
_CIK = re.compile(r"\((\d{10})\)")
_BARE = re.compile(r"(?<![A-Za-z0-9$])([A-Z]{3,5})(?![A-Za-z0-9])")
_ACQUIRE_VERBS = re.compile(r"\b(to acquire|acquires|acquired|agrees to buy|to buy|buys|will buy|bids for|offers to buy|to purchase|takes over)\b", re.I)
_TARGET_MARKERS = re.compile(r"\b(to be acquired|to be bought|agrees to be|receives? (a |an )?(takeover |buyout )?offer|takeover (bid|offer) for|buyout (of|offer for)|go.private|to be taken private|accepts? offer)\b", re.I)


class Universe:
    def __init__(self, watch: Dict[str, dict]):
        self.watch = watch or {}
        self.by_cik: Dict[int, str] = {}
        self.index: Dict[str, List[Tuple[str, str, bool]]] = {}   # anchor token -> [(phrase_norm, ticker, single_token)]
        for t, meta in self.watch.items():
            cik = meta.get("cik")
            if cik:
                try:
                    self.by_cik[int(cik)] = t
                except (TypeError, ValueError):
                    pass
            phrases = set()
            for v in [meta.get("name", "")] + list(meta.get("aliases", []) or []):
                p = norm(v)
                if len(p) >= 4:
                    phrases.add(p)
            for p in phrases:
                toks = [x for x in p.split() if len(x) >= 3]
                if not toks:
                    continue
                anchor = max(toks, key=len)
                self.index.setdefault(anchor, []).append((p, t, len(p.split()) == 1))

    def __contains__(self, t: str) -> bool:
        return t in self.watch

    def meta(self, t: str) -> dict:
        return self.watch.get(t, {})

    def match(self, title: str, summary: str = "", max_tickers: int = 8) -> List[Tuple[str, float, str]]:
        """Returns [(ticker, confidence, how)] sorted by confidence."""
        raw = (title or "") + " . " + (summary or "")[:1200]
        found: Dict[str, Tuple[float, str]] = {}

        def add(t, c, how):
            if t in self.watch and (t not in found or c > found[t][0]):
                found[t] = (c, how)

        for m in _EX.finditer(raw):
            add(m.group(1).replace(".", "-"), 1.0, "exchange_tag")
            add(m.group(1), 1.0, "exchange_tag")
        for m in _CASH.finditer(raw):
            add(m.group(1), 0.95, "cashtag")
        for m in _CIK.finditer(raw):
            t = self.by_cik.get(int(m.group(1)))
            if t:
                add(t, 0.95, "cik")
        low = " " + norm(raw) + " "
        toks = set(low.split())
        for tok in toks:
            for phrase, t, single in self.index.get(tok, ()):
                if (" " + phrase + " ") not in low:
                    continue
                if single:
                    m_cap = re.search(r"(?<![A-Za-z0-9])(" + re.escape(phrase[0].upper() + phrase[1:]) + "|" + re.escape(phrase.upper()) + r")(?![A-Za-z0-9])", raw)
                    if not m_cap:
                        continue
                    if phrase in COMMON_WORD_ALIASES:
                        prev = re.findall(r"[A-Za-z']+", raw[:m_cap.start()])[-1:]
                        prev_w = prev[0].lower() if prev else ""
                        if prev_w and prev_w not in {"at", "of", "by", "from", "shares", "stock", "with", "says", "said", "as", "for", "and", "vs", "versus"}:
                            continue          # "price Target", "the Block": common-noun use
                        add(t, 0.55, "name_ambiguous")   # recorded as evidence but below the trading confidence floor
                    else:
                        add(t, 0.65, "name_single")
                else:
                    add(t, 0.9, "name")
        for m in _BARE.finditer(title or ""):
            t = m.group(1)
            if t not in COMMON_TICKERS and t in self.watch:
                add(t, 0.5, "bare_ticker")
        out = sorted(((t, c, h) for t, (c, h) in found.items()), key=lambda x: (-x[1], x[0]))
        return out[:max_tickers]

    def mna_role(self, ticker: str, title: str) -> str:
        """'acquirer' | 'target' | 'unknown' for a ticker named in an M&A headline."""
        meta = self.watch.get(ticker, {})
        names = [norm(meta.get("name", ""))] + [norm(a) for a in meta.get("aliases", []) or []]
        names = [n for n in names if n]
        tl = norm(title)
        if _TARGET_MARKERS.search(title or ""):
            for n in names:
                i = tl.find(n.split()[0]) if n else -1
                m = _TARGET_MARKERS.search(title or "")
                if i >= 0 and m and i < norm(title).find(norm(m.group(0)).split()[0]) + 1:
                    return "target"
        m = _ACQUIRE_VERBS.search(title or "")
        if not m:
            return "unknown"
        verb_pos = norm(title[:m.start()]).count(" ") + (1 if norm(title[:m.start()]) else 0)
        for n in names:
            first = n.split()[0]
            words = tl.split()
            if first in words:
                return "acquirer" if words.index(first) < verb_pos else "target"
        return "unknown"
