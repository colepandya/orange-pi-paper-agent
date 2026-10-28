"""Map free text to listed companies with a confidence, avoiding the legacy false positives.

Signals (highest confidence first): exchange tag "(NASDAQ: NVDA)", cashtag "$NVDA", SEC filer CIK,
full company name, single-word name that is capitalised in the original text, bare upper-case ticker (weak).
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

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
_ACQUIRE_VERBS = re.compile(r"\b(to acquire|acquires|acquired|agrees to buy|agrees to acquire|to buy|buys|will buy|bids for|offers to buy|to purchase|purchases|takes over|to take over|to snap up|completes (?:the )?(?:acquisition|purchase|takeover) of|closes (?:the )?(?:acquisition|purchase) of|finalizes (?:the )?(?:acquisition|purchase) of)\b", re.I)
_TARGET_MARKERS = re.compile(r"\b(to be acquired|to be bought|agrees to be|receives? (a |an )?(takeover |buyout )?offer|takeover (bid|offer) for|buyout (of|offer for)|go.private|to be taken private|accepts? offer)\b", re.I)


# Well-known brand / short names -> ticker (applied only when the ticker is in the loaded universe). Wire headlines use these, not SEC legal names.
BRANDS = {
    "google": "GOOGL", "alphabet": "GOOGL", "youtube": "GOOGL", "waymo": "GOOGL", "facebook": "META", "instagram": "META", "whatsapp": "META", "meta": "META", "amazon": "AMZN", "aws": "AMZN",
    "apple": "AAPL", "microsoft": "MSFT", "azure": "MSFT", "nvidia": "NVDA", "tesla": "TSLA", "netflix": "NFLX", "disney": "DIS", "walmart": "WMT", "costco": "COST", "target corp": "TGT",
    "jpmorgan": "JPM", "jp morgan": "JPM", "goldman sachs": "GS", "goldman": "GS", "morgan stanley": "MS", "bank of america": "BAC", "wells fargo": "WFC", "citigroup": "C", "citi": "C",
    "berkshire hathaway": "BRK-B", "berkshire": "BRK-B", "exxon": "XOM", "exxon mobil": "XOM", "chevron": "CVX", "conocophillips": "COP", "occidental": "OXY", 
    "coca cola": "KO", "coke": "KO", "pepsico": "PEP", "pepsi": "PEP", "mcdonald's": "MCD", "mcdonalds": "MCD", "starbucks": "SBUX", "nike": "NKE", "home depot": "HD", "lowe's": "LOW", "lowes": "LOW",
    "boeing": "BA", "lockheed": "LMT", "lockheed martin": "LMT", "raytheon": "RTX", "rtx": "RTX", "northrop": "NOC", "northrop grumman": "NOC", "general dynamics": "GD", "palantir": "PLTR",
    "intel": "INTC", "amd": "AMD", "qualcomm": "QCOM", "broadcom": "AVGO", "micron": "MU", "tsmc": "TSM", "taiwan semiconductor": "TSM", "asml": "ASML", "applied materials": "AMAT", "lam research": "LRCX",
    "oracle": "ORCL", "salesforce": "CRM", "adobe": "ADBE", "ibm": "IBM", "cisco": "CSCO", "uber": "UBER", "lyft": "LYFT", "airbnb": "ABNB", "paypal": "PYPL", "visa": "V", "mastercard": "MA",
    "american express": "AXP", "amex": "AXP", "johnson & johnson": "JNJ", "j&j": "JNJ", "pfizer": "PFE", "moderna": "MRNA", "merck": "MRK", "abbvie": "ABBV", "eli lilly": "LLY", "lilly": "LLY",
    "novo nordisk": "NVO", "amgen": "AMGN", "gilead": "GILD", "unitedhealth": "UNH", "cvs": "CVS", "ford": "F", "general motors": "GM", "gm": "GM", "rivian": "RIVN", "lucid": "LCID",
    "snowflake": "SNOW", "crowdstrike": "CRWD", "palo alto": "PANW", "palo alto networks": "PANW", "datadog": "DDOG", "servicenow": "NOW", "shopify": "SHOP", "spotify": "SPOT", "zoom": "ZM",
    "coinbase": "COIN", "robinhood": "HOOD", "block": "XYZ", "caterpillar": "CAT", "deere": "DE", "3m": "MMM", "honeywell": "HON", "ge aerospace": "GE", "general electric": "GE",
    "delta air lines": "DAL", "united airlines": "UAL", "american airlines": "AAL", "southwest airlines": "LUV", "fedex": "FDX", "ups": "UPS", "verizon": "VZ", "at&t": "T", "t-mobile": "TMUS", "comcast": "CMCSA",
}
GENERIC_FIRST = set("""american national united global first general advanced applied digital international pacific atlantic western eastern southern northern central capital financial energy power health medical
 pharma bio tech technology technologies systems solutions services group holdings industries partners resources new old blue red green gold silver star sun bank trust one world universal premier standard royal great
 big little bright clear smart open next future prime alpha omega home city state county valley mountain river lake ocean coast bay harbor park garden house land""".split())
GENERIC_FIRST |= set("""stock stocks here there where when what which while work works working worker aware main core edge pure fresh live lives play plays player game games gamer power smart open next future prime
 alpha beta gamma delta omega sigma zeta apex peak summit point line link links net web cloud data code byte bit pixel digital logic mind brain idea ideas vision visions view views sight site sites
 market markets trade trades trader trading deal deals offer offers price prices cost costs value values worth wealth money cash coin coins gold silver bronze copper iron steel metal metals stone rock rocks
 earth world globe planet space star stars sun moon sky air wind fire water ocean sea river lake bay coast island mountain valley forest field fields farm farms garden gardens park parks road roads street
 bridge gate gates door doors wall walls home house houses building build builder builders block blocks brick bricks tower towers castle city town village county state states nation national people person
 human life living health care cares care medical medicine drug drugs pharma therapy therapies clinic clinics hospital cure heal healing body mind soul spirit heart hearts hand hands head heads face faces
 eye eyes ear ears voice voices word words text story stories news media press paper papers book books page pages letter letters mail post posts note notes message messages talk talks call calls
 phone phones signal signals wave waves sound sounds light lights bright clear clean pure true real right left up down over under above below inside outside high low big small little great good best
 better new old young first last second third early late fast slow quick rapid swift strong weak hard soft heavy light dark black white red green blue yellow orange purple pink brown grey gray silver
 universal global general standard premier royal imperial grand major minor senior junior special super ultra mega micro macro mini max min total full half double single triple multi omni uni bio eco geo
 tech techs technology technologies system systems solution solutions service services support product products brand brands group groups team teams club clubs union unions partners partner
 capital capitals fund funds trust trusts bank banks banc credit loan loans lending mortgage mortgages insurance assurance holdings holding company companies corporation industries industry
 resource resources energy energies oil gas petroleum fuel fuels electric electricity solar wind nuclear coal mining mines mine minerals chemical chemicals plastic plastics paper packaging
 food foods beverage beverages drink drinks coffee tea sugar salt rice grain grains corn wheat meat fish dairy fruit fruits vegetable vegetables restaurant restaurants hotel hotels resort resorts
 travel tour tours cruise cruises airline airlines air flight flights ship ships shipping freight cargo truck trucks rail railway road auto cars car motor motors motion move moves moving drive drives
 ride rides run runs running race races sport sports fitness gym health wellness beauty fashion style styles wear clothing apparel shoe shoes bag bags luxury home homes living lifestyle""".split())
CLEAN_TAIL = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|holdings|holding|group|sa|nv|ag|lp|llc|the|com|dot|class [a-c]|new)\b", re.I)


def derive_aliases(name: str) -> Tuple[str, List[str], str]:
    """-> (cleaned legal name, [multi-word aliases], first word). 'AMAZON COM INC' -> ('AMAZON', [], 'amazon');  'Ford Motor Co' -> ('Ford Motor', [], 'ford')."""
    n = re.sub(r"/[A-Za-z]{1,4}/?", " ", name or "")           # jurisdiction markers: /DE/ /NEW
    n = re.sub(r"[.,&]", " ", n)
    n = re.sub(r"\s+", " ", CLEAN_TAIL.sub(" ", n)).strip()
    words = n.split()
    multi = []
    if len(words) > 2 and words[0].lower() not in GENERIC_FIRST and words[1].lower() not in {"of", "the", "and", "for", "de", "la"} and len(" ".join(words[:2])) >= 7:
        multi = [" ".join(words[:2])]                           # 'Goldman Sachs' yes; 'Bank Of' / 'International Business' no
    return n, multi, (words[0].lower() if words else "")


class Universe:
    def __init__(self, watch: Dict[str, dict], common_words: Optional[set] = None):
        self.watch = watch or {}
        self.common = {w.lower() for w in (common_words or ())}          # words seen in lowercase in real news: ordinary English, never a company name on their own
        self.by_cik: Dict[int, str] = {}
        self.index: Dict[str, List[Tuple[str, str, bool, float]]] = {}   # anchor token -> [(phrase_norm, ticker, single_token, base_confidence)]
        derived: Dict[str, Tuple[str, List[str], str]] = {t: derive_aliases(m.get("name", "")) for t, m in self.watch.items()}
        first_counts = Counter(d[2] for d in derived.values() if d[2])

        def add(phrase: str, ticker: str, conf: float, multi_conf: Optional[float] = None) -> None:
            p = norm(phrase)
            if len(p) < 3:
                return
            toks = [x for x in p.split() if len(x) >= 2]
            if not toks:
                return
            single = len(p.split()) == 1
            c = conf if single else (multi_conf if multi_conf is not None else max(conf, 0.9))
            anchor = max(toks, key=len)
            if not any(e[0] == p and e[1] == ticker for e in self.index.setdefault(anchor, [])):
                self.index[anchor].append((p, ticker, single, c))

        for t, meta in self.watch.items():
            cik = meta.get("cik")
            if cik:
                try:
                    self.by_cik[int(cik)] = t
                except (TypeError, ValueError):
                    pass
            clean, multi, first = derived[t]
            for v in [meta.get("name", "")] + list(meta.get("aliases", []) or []) + [clean] + multi:
                if len(norm(v)) >= 4 and not (len(norm(v).split()) == 1 and (norm(v) in GENERIC_FIRST or norm(v) in self.common)):
                    add(v, t, 0.65)
            if first and len(first) >= 5 and first not in GENERIC_FIRST and first not in self.common and first_counts[first] == 1:
                add(first, t, 0.70)                                   # a distinctive, UNIQUE, non-dictionary first word: "Garmin", "Palantir", "Wendy's"
        for brand, t in BRANDS.items():
            if t in self.watch:
                add(brand, t, 0.85, 0.9)

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
        for tok in set(low.split()):
            for phrase, t, single, base in self.index.get(tok, ()):
                if (" " + phrase + " ") not in low:
                    continue
                if single:
                    # single words must start with a capital in the original text ("Ford" yes, "ford a river" no; "JPMorgan" yes)
                    m_cap = re.search(r"(?<![A-Za-z0-9-])" + re.escape(phrase[0].upper()) + r"(?i:" + re.escape(phrase[1:]) + r")(?![A-Za-z0-9])(?!-[A-Za-z])", raw)      # not "Meta-analysis", "Apple-style"
                    if not m_cap:
                        continue
                    if phrase in COMMON_WORD_ALIASES:
                        prev = re.findall(r"[A-Za-z']+", raw[:m_cap.start()])[-1:]
                        prev_w = prev[0].lower() if prev else ""
                        if prev_w and prev_w not in {"at", "of", "by", "from", "shares", "stock", "with", "says", "said", "as", "for", "and", "vs", "versus"}:
                            continue          # "price Target", "the Block": common-noun use
                        add(t, 0.55, "name_ambiguous")   # recorded as evidence but below the trading confidence floor
                        continue
                add(t, base, "brand" if phrase in BRANDS else ("name" if not single else "name_single"))
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


def build_common_words(texts, min_count: int = 4) -> set:
    """Words that appear in LOWERCASE at least ``min_count`` times in real news are ordinary English ("stock", "here", "work", "aware"...), so a capitalised
    occurrence at a sentence start or in a title must not be mistaken for a company. Company names ("Garmin", "Palantir") are almost never lowercase."""
    counts: Dict[str, int] = {}
    for t in texts:
        for w in re.findall(r"(?<![A-Za-z0-9'-])[a-z][a-z]{3,}(?![A-Za-z0-9'-])", t or ""):
            counts[w] = counts.get(w, 0) + 1
    return {w for w, n in counts.items() if n >= min_count}
