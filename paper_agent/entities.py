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

# first names that are also company brand words ("Simon" Property, "Jack" in the Box, "Warren"...): "Simon Sharwood" is a person. Only suppressed when a capitalised surname follows.
_PERSON_FIRST_NAMES = set("""simon david john james michael robert william richard thomas charles peter paul brian kevin gary jeff steve scott andrew anthony eric jason matthew daniel joseph jonathan nicholas adam alex ben sam tom jim bob bill dan chris jack
 jay roy ray ross carl dale dean glen grant hunter wesley walker warren jordan henry harry george edward frank larry terry jerry barry kenneth donald ronald timothy patrick stephen gregory raymond""".split())
_BYLINE = re.compile(r"^\s*[A-Z][a-z]+(?: [A-Z][a-z'\-]+){1,2} / [^:]{2,60} :\s*")                  # "Simon Sharwood / The Register : ..."  (Techmeme-style author and publisher prefix)

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
    "instacart": "CART", "stmicro": "STM", "stmicroelectronics": "STM",
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
# Words that are NOT a company name by themselves. Real failures (Oct 2026): "Helen Markovic" -> Helen of Troy (HELE), "Jordan Valley" -> JORD, "Fed Chair Powell" -> Powell Industries,
# "Google Gemini" -> Gemini Space Station, "NBT Bancorp Inc." -> "Bancorp, Inc." (TBBK): the SEC's legal name for The Bancorp is just "Bancorp, Inc.".
GIVEN_NAMES = set("""james john robert michael william david richard joseph thomas charles christopher daniel matthew anthony mark donald steven paul andrew joshua kenneth kevin brian george timothy ronald edward jason jeffrey ryan jacob gary
 nicholas eric jonathan stephen larry justin scott brandon benjamin samuel gregory alexander frank patrick raymond jack dennis jerry tyler aaron jose adam nathan henry douglas zachary peter kyle walter ethan jeremy harold keith christian roger noah gerald carl
 terry sean austin arthur lawrence jesse dylan bryan joe jordan billy bruce albert willie gabriel logan alan juan wayne roy ralph randy eugene vincent russell elijah louis bobby philip johnny mary patricia jennifer linda elizabeth barbara susan jessica sarah karen
 nancy lisa margaret betty sandra ashley dorothy kimberly emily donna michelle carol amanda melissa deborah stephanie rebecca sharon laura cynthia kathleen amy angela shirley anna brenda pamela emma nicole helen samantha katherine christine debra rachel carolyn
 janet catherine maria heather diane ruth julie olivia joyce virginia victoria kelly lauren christina joan evelyn judith megan andrea cheryl hannah jacqueline martha gloria teresa ann sara madison frances kathryn janice jean abigail alice julia judy sophia grace
 denise amber doris marilyn danielle beverly isabella theresa diana natalie brittany charlotte marie kayla alexis lori vladimir angela ursula marcus gustavo mohammed ahmed ali omar hassan carlos miguel pedro luis diego sergio andre pierre jean francois hans klaus""".split())
GIVEN_NAMES |= _PERSON_FIRST_NAMES                                                      # the index never makes a company's FIRST WORD a brand when it is also a first name ("Simon" Property, "Jack" in the Box): "Simon Sharwood" is a person
SURNAMES = set("""smith johnson williams brown jones garcia miller davis rodriguez martinez hernandez lopez gonzalez wilson anderson thomas taylor moore jackson martin lee perez thompson white harris sanchez clark ramirez lewis robinson walker young allen
 king wright scott torres nguyen hill flores green adams nelson baker hall rivera campbell mitchell carter roberts gomez phillips evans turner diaz parker cruz edwards collins reyes stewart morris morales murphy cook rogers gutierrez ortiz morgan cooper
 peterson bailey reed kelly howard ramos kim cox ward richardson watson brooks chavez wood james bennett gray mendoza ruiz hughes price alvarez castillo sanders patel myers long ross foster powell jenkins perry russell sullivan bell coleman butler henderson
 barnes fisher vasquez simmons romero patterson hamilton graham reynolds griffin wallace moreno west cole hayes bryant herrera gibson ellis tran medina stevens murray castro marshall owens harrison fernandez mcdonald woods washington kennedy wells vargas
 henry chen freeman webb tucker burns crawford olson simpson porter hunter gordon mendez silva shaw snyder mason dixon munoz hunt hicks holmes palmer wagner black robertson boyd rose stone salazar fox warren mills meyer rice schmidt garza daniels ferguson
 nichols stephens soto weaver gardner payne grant dunn kelley spencer hawkins arnold pierce hansen peters santos hart bradley knight elliott cunningham duncan armstrong hudson carroll lane riley andrews ray delgado berry perkins hoffman johnston matthews
 richards willis carpenter lawrence sandoval guerrero chapman ortega watkins greene wheeler valdez harper burke larson santiago maldonado morrison franklin carlson townsend trump biden musk bezos zuckerberg buffett dimon yellen lagarde bessent""".split())
PLACE_NAMES = set("""alabama alaska arizona arkansas california colorado connecticut delaware florida georgia hawaii idaho illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan minnesota mississippi
 missouri montana nebraska nevada ohio oklahoma oregon pennsylvania tennessee texas utah vermont virginia washington wisconsin wyoming carolina dakota england scotland ireland france germany italy spain portugal poland ukraine russia china japan korea india brazil
 canada mexico israel iran iraq syria turkey egypt africa europe asia australia zealand london paris berlin tokyo beijing shanghai moscow kyiv kiev taiwan singapore dubai texan alaskan""".split())
PRODUCT_WORDS = set("gemini copilot bard claude llama alexa siri cortana chatgpt grok sora".split())
DESCRIPTOR_WORDS = set("""bancorp bancshares bankshares financial financials finance industries industrial enterprises international national united american federal savings mutual community citizens peoples security heritage
 pioneer liberty patriot independence frontier premier southern northern eastern western central pacific atlantic midwest regional""".split())
TITLE_WORDS = set("""chair chairman chairwoman chairperson president ceo cfo coo mr mrs ms dr sen senator rep representative governor gov secretary judge justice minister prime fed treasury speaker mayor commissioner director professor prof
 sir dame lord lady pope king queen prince princess sheikh emir general gen analyst economist strategist""".split())
TAIL_WORDS = set("inc incorporated corp corporation co company ltd limited plc holdings holding group the llc lp sa nv ag".split())
_EXCH_PHRASES = re.compile(r"(?i)\b(?:nasdaq global select market|nasdaq global market|nasdaq capital market|nasdaq stock market|nasdaq composite|nasdaq[- ]100|nasdaq[- ]listed|nyse[- ]listed|nyse american|nyse arca|"
                           r"(?:on|in|listed on|trades on|traded on|trading on)\s+(?:the\s+)?(?:nasdaq|nyse))\b")
BODY_ONLY = 0.6            # a company named ONLY in the body (while the headline names another) is context, not the subject
COUNTERPARTY = 0.6         # "... with Microsoft", "... on Google Gemini": the counterparty, not the subject
COUNTERPARTY_WORDS = {"with", "from", "via", "using", "on", "atop", "through", "alongside"}

NATIONALITY_WORDS = set("""british canadian chinese japanese german french indian korean swiss australian european asian african latin western eastern northern southern central royal imperial irish scottish
 welsh english dutch spanish italian russian brazilian mexican israeli turkish saudi korean taiwanese singaporean norwegian swedish danish finnish polish""".split())    # nationality and region adjectives start many headlines and name no company: they may not stand ALONE as a company's brand (but "British American Tobacco" still matches by its full or two-word name)
GENERIC_FIRST |= DESCRIPTOR_WORDS | PRODUCT_WORDS

CLEAN_TAIL = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|holdings|holding|group|sa|nv|ag|lp|llc|the|com|dot|class [a-c]|new)\b", re.I)


def derive_aliases(name: str) -> Tuple[str, List[str], str]:
    """-> (cleaned legal name, [multi-word aliases], first word). 'AMAZON COM INC' -> ('AMAZON', [], 'amazon');  'Ford Motor Co' -> ('Ford Motor', [], 'ford')."""
    partnership = bool(re.match(r"\s*[A-Za-z'-]+\s+(?:&|and)\s+[A-Za-z]", name or ""))     # "Cohen & Steers", "Smith & Wesson": the first word is a partner's surname, not the company
    n = re.sub(r"/[A-Za-z]{1,4}/?", " ", name or "")           # jurisdiction markers: /DE/ /NEW
    n = re.sub(r"[.,&]", " ", n)
    n = re.sub(r"\s+", " ", CLEAN_TAIL.sub(" ", n)).strip()
    words = n.split()
    multi = []
    if len(words) > 2 and words[0].lower() not in GENERIC_FIRST and words[1].lower() not in {"of", "the", "and", "for", "de", "la"} and len(" ".join(words[:2])) >= 7:
        multi = [" ".join(words[:2])]                           # 'Goldman Sachs' yes; 'Bank Of' / 'International Business' no
    return n, multi, ("" if partnership else (words[0].lower() if words else ""))


_CORP = re.compile(r"\b(incorporated|inc|corporation|corp|company|co|ltd|limited|llc|lp|plc|trust)\b")


def _core_name(name: str) -> str:
    """The distinctive part of a legal name, for the is-the-full-name-in-the-story test."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9& ]+", " ", _CORP.sub(" ", norm(name or "")))).strip()


_LEGAL_FORMS = {"llc", "l.l.c", "lp", "l.p", "llp", "inc", "corp", "corporation", "ltd", "limited", "co", "company", "gmbh", "plc", "ag", "sa", "nv", "pllc"}
_VENUES = {"nasdaq", "nyse", "cboe", "cme", "dow", "russell", "s&p"}                       # exchanges and indices: the words appear as VENUES far more often than as the company that runs them
_VENUE_CONTEXT = re.compile(r"\b(?:on|in|at|from|via|listed on|trading on)\s+(?:the\s+)?(?:nasdaq|nyse|cboe|cme)\b|\b(?:nasdaq|nyse|dow|russell|s&p)[- ](?:listed|composite|100|500|2000|stock|exchange|market|index|futures|jones)\b|\b(?:the\s+)(?:nasdaq|nyse|dow|s&p)\b|\b(?:nasdaq|nyse|dow|russell|s&p)(?:\s+\d+)?\s+(?:drops?|falls?|rises?|gains?|slides?|jumps?|climbs?|sinks?|closes?|ends?|opens?|hits?|tumbles?|surges?|slips?|rall(?:y|ies)|fell|rose|dropped|gained|sank|slid)\b", re.I)


def _words_norm(s: str) -> List[str]:
    return [("and" if w == "&" else w) for w in re.findall(r"[a-z0-9&]+", (s or "").lower().replace("&", " & ")) if w not in {"inc", "corp", "corporation", "co", "company", "ltd", "limited", "llc", "lp", "plc", "trust"}]


def legal_name_spans(title: str) -> List[List[str]]:
    """Full names a headline STATES, up to a legal-form word: 'Noble Supply & Logistics, LLC' -> ['noble', 'supply', 'and', 'logistics'] (two or more name words only)."""
    toks = re.findall(r"[A-Za-z0-9&.'’,-]+", title or "")
    out: List[List[str]] = []
    for i, tk in enumerate(toks):
        if i and tk.strip(".,").lower() in _LEGAL_FORMS:
            j = i
            while j > 0 and i - j < 6 and (toks[j - 1][:1].isupper() or toks[j - 1].strip(",") in ("&", "and", "of")):
                j -= 1
            words = _words_norm(" ".join(toks[j:i]))
            if len(words) >= 2:
                out.append(words)
    return out


_MEDIA = re.compile(r"\b(?:times|news|media|journal|post|press|reuters|bloomberg|broadcasting|publishing|gannett|comcast|nbc|cbs|cnn|fox|dispatch|tribune|herald|gazette|daily)\b")
_NONCOMMON_LAST = set("GHIMNOPQRTUVWX")        # Nasdaq's fifth-letter codes: convertible bonds G/H/I, preferred M/N/O/P, bankrupt Q, rights R, warrants T/W, units U, when-issued V, mutual-fund X
_MARKET_CUE = re.compile(r"\b(?:stock|stocks|shares?|rises?|rose|falls?|fell|drops?|dropped|jumps?|jumped|gains?|gained|slips?|slipped|surges?|surged|soars?|plunges?|plunged|sinks?|slides?|tumbles?|climbs?|rall(?:y|ies|ied)|earnings|results|guidance|outlook|upgrades?|upgraded|downgrades?|downgraded|price target|options?|premarket|after[- ]hours|calls|puts|buyback|dividend|beats?|misses|misses?)\b|[%$]", re.I)
_ATTR_BEFORE = re.compile(r"(?:according to|reported by|reports? from|reporting by|told|per|via|citing|cited by|a report in|an article in|writing in|wrote in|published in|interview with|spoke to|speaking to|the)\s+$")
_ATTR_AFTER = re.compile(r"^\s*(?:reported|reports|said|says|wrote|cited|quoted|found|noted|reviewed|obtained|first reported|has reported|has said)\b")
_RATING_VERB = r"(?i:maintains|reiterates|upgrades|downgrades|initiates|assigns|raises|lowers|cuts|reaffirms|resumes|rates|keeps|lifts|boosts|sets|starts|reinstates)"
_RATING_FIRM = re.compile(r"^\s*((?:[A-Z][\w&.'’-]*\s+){1,4})" + _RATING_VERB + r"\b.*\b(?i:price target|rating|overweight|underweight|outperform|underperform|neutral|buy|sell|hold|coverage|target)\b")


def derivative_security(t: str, meta: dict, watch: dict, issuer: dict) -> bool:
    """A warrant, unit, right or preferred series of an issuer whose common ticker is also in the universe (Nasdaq fifth-letter convention): it is the issuer's SECURITY, not the issuer, so it matches only on its own ticker or tag, never on the company's name."""
    if meta.get("exchange") != "Nasdaq" or len(t) != 5 or t[-1] not in _NONCOMMON_LAST:
        return False
    root = t[:-1]
    return root in watch and issuer.get(root, root) == issuer.get(t, t)


def _attributed(ph: str, text: str) -> bool:
    """Every occurrence of the phrase in the text is a SOURCE ('according to the New York Times', 'Reuters reported'), never the subject."""
    idx = [m.start() for m in re.finditer(re.escape(" " + ph + " "), text)]
    if not idx:
        return False
    for i in idx:
        before, after = text[max(0, i - 40):i + 1], text[i + len(ph) + 1:i + len(ph) + 40]
        if not (_ATTR_BEFORE.search(before) or _ATTR_AFTER.search(after)):
            return False
    return True


def _byline(title: str, ph: str) -> bool:
    """The headline ends with its own publisher: 'Story text - Reuters', 'Story text (The New York Times)'."""
    words = [re.escape(w) for w in ph.split()]
    return bool(words) and bool(re.search(r"(?:\s[-–—|]\s|\()\s*(?:the\s+)?" + r"\W*".join(words) + r"\W*(?:com)?\)?\s*$", title or "", re.I))


_DANGLING = {"&", "and", "of", "for", "the", "de", "del", "la", "le", "von", "van", "y", "e", "-"}
_FILING_HEADER = re.compile(r"^(?:[0-9A-Z/\-]{1,10})\s+-\s+(.+?)\s+\((\d{10})\)\s+\((?:Filer|Issuer|Reporting|Subject|Filed by|Filed for)\)")
_FINAL_DOT = re.compile(r"([a-z]{3,})\.(?=\s|$)")
_POSSESSIVE = re.compile(r"(\w)'s\b")


class Universe:
    def __init__(self, watch: Dict[str, dict], common_words: Optional[set] = None):
        self.watch = watch or {}
        self.common = {w.lower() for w in (common_words or ())}
        self.guard = True                                                    # kb.entity_guard: a fund, SPAC or other non-operating instrument matches only on its own tag or full name (set from the settings by the pipeline)
        from . import kbdata
        self.nonop: Dict[str, str] = {}
        for t_, v_ in (watch or {}).items():
            c_ = kbdata.classify_instrument((v_ or {}).get("name") or "", t_)
            if c_["kind"] != "operating_equity":
                self.nonop[t_] = _core_name((v_ or {}).get("name") or "")          # words seen in lowercase in real news: ordinary English, never a company name on their own
        self.by_cik: Dict[int, str] = {}
        self.index: Dict[str, List[Tuple[str, str, bool, float]]] = {}   # anchor token -> [(phrase_norm, ticker, single_token, base_confidence)]
        derived: Dict[str, Tuple[str, List[str], str]] = {t: derive_aliases(m.get("name", "")) for t, m in self.watch.items()}
        first_counts = Counter(d[2] for d in derived.values() if d[2])

        def add(phrase: str, ticker: str, conf: float, multi_conf: Optional[float] = None) -> None:
            raw_tokens = (phrase or "").strip().lower().split()
            if raw_tokens and (raw_tokens[-1] in _DANGLING or raw_tokens[0] in _DANGLING):
                return                                              # a cut-off name ("Cohen &", "Bank of") names no company: judged on the RAW phrase, before "&" is stripped
            p = norm(phrase)
            if len(p) < 3:
                return
            toks = [x for x in p.split() if len(x) >= 2]
            if not toks:
                return
            single = len(p.split()) == 1
            if p.split()[-1] in _DANGLING or p.split()[0] in _DANGLING:
                return                                              # a cut-off name ("cohen &", "bank of"): it names no company
            core = [w.strip(".,'") for w in p.split() if w.strip(".,'") not in TAIL_WORDS]
            if len(core) <= 1 and (not core or core[0] in GENERIC_FIRST):
                return                                              # ONE generic word plus legal suffixes ("Bancorp, Inc.") can only be matched by ticker / exchange tag / CIK; multi-word names like "Main Street Capital" are fine
            c = conf if single else (multi_conf if multi_conf is not None else max(conf, 0.9))
            anchor = max(toks, key=len)
            if not any(e[0] == p and e[1] == ticker for e in self.index.setdefault(anchor, [])):
                self.index[anchor].append((p, ticker, single, c))

        self.issuer: Dict[str, object] = {}
        self.core: Dict[str, str] = {}
        self.derivative: set = set()
        for t, meta in self.watch.items():
            try:
                self.issuer[t] = int(meta.get("cik")) if meta.get("cik") and int(meta.get("cik")) > 0 else t
            except (TypeError, ValueError, OverflowError):
                self.issuer[t] = t
            self.core[t] = _core_name(meta.get("name", ""))
        self.derivative = {t for t, m in self.watch.items() if derivative_security(t, m, self.watch, self.issuer)}
        for t, meta in self.watch.items():
            cik = meta.get("cik")
            if cik:
                try:
                    ci = int(cik)
                    cur = self.by_cik.get(ci)
                    if t not in self.derivative and (cur is None or cur in self.derivative or (len(t), t) < (len(cur), cur)):
                        self.by_cik[ci] = t                          # the issuer's own (shortest, non-derivative) ticker: not whichever happened to be listed last
                except (TypeError, ValueError, OverflowError):
                    pass
            if t in self.derivative:
                continue                                              # a security of the issuer is matched only by its own ticker or tag
            clean, multi, first = derived[t]
            for v in [meta.get("name", "")] + list(meta.get("aliases", []) or []) + [clean] + multi:
                if len(norm(v)) >= 4 and not (len(norm(v).split()) == 1 and (norm(v) in GENERIC_FIRST or norm(v) in self.common or norm(v) in GIVEN_NAMES or norm(v) in SURNAMES or norm(v) in PLACE_NAMES)):
                    add(v, t, 0.65)
            if first and len(first) >= 5 and first not in GENERIC_FIRST and first not in NATIONALITY_WORDS and first not in self.common and first not in GIVEN_NAMES and first not in SURNAMES and first not in PLACE_NAMES and first_counts[first] == 1:
                add(first, t, 0.70)                                   # a distinctive, UNIQUE, non-dictionary first word: "Garmin", "Palantir", "Wendy's"
        for brand, t in BRANDS.items():
            if t in self.watch:
                add(brand, t, 0.85, 0.9)

    def __contains__(self, t: str) -> bool:
        return t in self.watch

    def meta(self, t: str) -> dict:
        return self.watch.get(t, {})

    def subjects(self, title: str) -> List[str]:
        """The companies a HEADLINE names, in the order it names them (the first is the subject: "Alphabet slides, Microsoft firms" is about Alphabet)."""
        hits = [t for t, c, _h in self.match(title, "") if c >= 0.6]
        if not hasattr(self, "_by_ticker"):
            bt: Dict[str, List[str]] = {}
            for entries in self.index.values():
                for ph, tk, _s, _c in entries:
                    bt.setdefault(tk, []).append(ph)
            self._by_ticker = bt
        low, up = " " + norm(title) + " ", (title or "").upper()

        def pos(t: str) -> int:
            best = 10 ** 6
            for ph in self._by_ticker.get(t, ()):
                i = low.find(" " + ph + " ")
                if i >= 0:
                    best = min(best, i)
            m = re.search(r"(?<![A-Z0-9])%s(?![A-Z0-9])" % re.escape(t), up)
            return min(best, m.start()) if m else best
        return sorted(hits, key=pos)

    def match(self, title: str, summary: str = "", max_tickers: int = 8) -> List[Tuple[str, float, str]]:
        """Returns [(ticker, confidence, how)] sorted by confidence."""
        fh = _FILING_HEADER.match(title or "")
        if fh:                                                                  # an EDGAR index entry names its FILER and gives the filer's CIK: that is authoritative, a guess from words in the name is not
            t_cik = self.by_cik.get(int(fh.group(2)))
            if t_cik and t_cik in self.watch:
                return [(t_cik, 0.95, "cik")]
            out_f = [m for m in self._match_text(title, summary, max_tickers) if m[2] in ("name", "exchange_tag", "cashtag", "cik") and (m[2] != "name" or len(self.nonop.get(m[0], "x y").split()) >= 2 or m[0] not in self.nonop)]
            return [m for m in out_f if len(norm(self.watch[m[0]].get("name", "")).split()) <= 1 or _core_name(self.watch[m[0]].get("name", "")) in _core_name(fh.group(1)) or _core_name(fh.group(1)) in _core_name(self.watch[m[0]].get("name", ""))]
        return self._match_text(title, summary, max_tickers)

    def _match_text(self, title: str, summary: str = "", max_tickers: int = 8) -> List[Tuple[str, float, str]]:
        raw = (title or "") + " . " + _BYLINE.sub("", (summary or ""))[:1200]
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
        names_raw = raw
        for m in _EX.finditer(raw):                                      # "(NASDAQ: NBTB)": the ticker counts (above), the word NASDAQ is not the company Nasdaq, Inc.
            names_raw = names_raw[:m.start()] + " " * (m.end() - m.start()) + names_raw[m.end():]
        names_raw = _EXCH_PHRASES.sub(lambda m: " " * len(m.group(0)), names_raw)       # "trades on the Nasdaq Global Select Market"
        raw_for_names = raw
        raw = names_raw
        low = " " + norm(raw) + " "
        title_n = " " + norm(title or "") + " "
        low = low + "| " + _POSSESSIVE.sub(r"\1", low)                        # "Google's clearing": the company is Google. A second view without the possessive 's, joined by a separator no phrase contains (so names that ARE possessive, McDonald's, still match above)
        title_n = title_n + "| " + _POSSESSIVE.sub(r"\1", title_n)
        low = low + "| " + _FINAL_DOT.sub(r"\1", low)                       # "...buying Lumentum. Analysts say": the full stop ends the sentence, it is not part of the name
        title_n = title_n + "| " + _FINAL_DOT.sub(r"\1", title_n)
        named: List[Tuple[str, float, str, str]] = []
        for tok in set(low.split()):
            for phrase, t, single, base in self.index.get(tok, ()):
                if (" " + phrase + " ") not in low:
                    continue
                if single:
                    # single words must start with a capital in the original text ("Ford" yes, "ford a river" no; "JPMorgan" yes)
                    m_cap = re.search(r"(?<![A-Za-z0-9-])" + re.escape(phrase[0].upper()) + r"(?i:" + re.escape(phrase[1:]) + r")(?![A-Za-z0-9])(?!-[A-Za-z])", raw)      # not "Meta-analysis", "Apple-style"
                    if not m_cap:
                        continue
                    if phrase not in BRANDS and set(re.findall(r"[a-z']+", " ".join(re.findall(r"[A-Za-z']+", raw[:m_cap.start()])[-2:]).lower())) & TITLE_WORDS:
                        continue                                       # "Fed Chair Powell", "Sen. Warren": a person, not a company
                    if phrase in COMMON_WORD_ALIASES:
                        prev = re.findall(r"[A-Za-z']+", raw[:m_cap.start()])[-1:]
                        prev_w = prev[0].lower() if prev else ""
                        if prev_w and prev_w not in {"at", "of", "by", "from", "shares", "stock", "with", "says", "said", "as", "for", "and", "vs", "versus"}:
                            continue          # "price Target", "the Block": common-noun use
                        add(t, 0.55, "name_ambiguous")   # recorded as evidence but below the trading confidence floor
                        continue
                named.append((t, base, "brand" if phrase in BRANDS else ("name" if not single else "name_single"), phrase))
        if len(named) > 1:                                             # one phrase, several DIFFERENT companies ("Brookfield Renewable": two issuers; "Banco Santander": several): only an issuer whose own legal name IS the phrase keeps it
            by_ph: Dict[str, set] = {}
            for (t_, _c, _h, ph_) in named:
                by_ph.setdefault(ph_, set()).add(t_)
            drop = set()
            for ph_, ts_ in by_ph.items():
                if len({self.issuer.get(x, x) for x in ts_}) <= 1:
                    continue                                          # share classes of one issuer are one company
                own = {x for x in ts_ if self.core.get(x) == ph_}
                keep = own if len({self.issuer.get(x, x) for x in own}) == 1 else set()
                drop |= {(x, ph_) for x in ts_ if x not in keep}
            if drop:
                kept = []
                for item in named:
                    if (item[0], item[3]) in drop:
                        add(item[0], 0.45, "name_ambiguous")          # recorded as evidence, below the trading floor
                    else:
                        kept.append(item)
                named = kept
        spans = legal_name_spans(title or "")
        if named and (spans or any(ph in _VENUES for (_t, _c, _h, ph) in named)):
            venue_ctx = bool(_VENUE_CONTEXT.search(title or ""))
            kept = []
            for item in named:
                t_, c_, h_, ph_ = item
                if h_ == "name_single" and spans:
                    ew = _words_norm(self.core.get(t_, ""))
                    pw = ph_.split()
                    if any(set(pw) <= set(sp) and sp != ew and sp[-len(ew):] != ew for sp in spans if ew):
                        add(t_, 0.45, "other_legal_entity")                  # the headline STATES a different full name ("Noble Supply & Logistics, LLC" is not Noble Corp)
                        continue
                if ph_ in _VENUES and (venue_ctx or (" " + ph_ + " ") not in title_n):
                    continue                                                  # "fell on the Nasdaq": a venue, not the company that runs it
                kept.append(item)
            named = kept
        in_title = [(t, c, h, ph) for (t, c, h, ph) in named if (" " + ph + " ") in title_n]
        first_pos = min((title_n.find(" " + ph + " ") for (_t, _c, _h, ph) in in_title), default=0)
        firm = set()
        mf = _RATING_FIRM.match(title or "")
        if mf and len({t_ for (t_, _c, _h, ph_) in named if not set(ph_.split()) <= {w.lower().strip(".,'’") for w in mf.group(1).split()}}) >= 1:
            firm = {w.lower().strip(".,'’") for w in mf.group(1).split()}      # "JP Morgan Maintains Underweight on Cboe": JP Morgan is the rater, Cboe is the company
        body_view = low.split("|", 1)[0]
        longest: Dict[str, str] = {}
        in_title_any: Dict[str, bool] = {}
        for (t_, _c, _h, ph_) in named:
            if len(ph_.split()) > len(longest.get(t_, "").split()):
                longest[t_] = ph_
            in_title_any[t_] = in_title_any.get(t_, False) or ((" " + ph_ + " ") in title_n)
        source_only = {t_ for t_, ph_ in longest.items() if _MEDIA.search(self.core.get(t_, "")) and ((not in_title_any[t_] and _attributed(ph_, body_view)) or (in_title_any[t_] and _byline(title or "", ph_)))}      # only a NEWS ORGANISATION can be a mere source; a company that "said" something is the subject      # decided per COMPANY, on its longest phrase ("york times" alone would slip past)
        for (t, c, h, ph) in named:
            pos = title_n.find(" " + ph + " ")
            if firm and set(ph.split()) <= firm:
                add(t, 0.45, "rating_firm")
                continue
            if t in source_only:
                add(t, 0.45, "source_not_subject")                       # named only as a source or a byline
                continue
            if in_title and pos < 0:
                c *= BODY_ONLY                                         # named only in the body while the headline is about someone else
            elif not in_title and h == "name_single" and low.split("|", 1)[0].count(" " + ph + " ") <= 1:
                c *= BODY_ONLY                                         # the headline names NO company and a one-word name appears once in the body: a passing mention, not the subject
            elif in_title and pos > first_pos and (re.findall(r"[a-z0-9']+", title_n[:pos]) or [""])[-1] in COUNTERPARTY_WORDS:
                c *= COUNTERPARTY                                      # "deal WITH Microsoft": the counterparty is not the subject
            add(t, c, h)
        raw = raw_for_names
        letters = [ch for ch in (title or "") if ch.isalpha()]
        shouting = bool(letters) and sum(ch.isupper() for ch in letters) / len(letters) > 0.6          # an ALL-CAPS headline turns every word (NET, FOR, ONE) into a "ticker"
        bare = [m.group(1) for m in ([] if shouting else _BARE.finditer(title or "")) if m.group(1) not in COMMON_TICKERS and m.group(1) in self.watch]
        if bare:
            corroborated = bool(_MARKET_CUE.search(title or "")) or len(set(bare)) >= 2
            for t in dict.fromkeys(bare):
                if corroborated or t in found:
                    add(t, 0.5, "bare_ticker")
                else:
                    add(t, 0.4, "bare_ticker_unconfirmed")                  # "ICE demands", "warns IFS": an acronym that is also a ticker; recorded, below the floor
        out = sorted(((t, c, h) for t, (c, h) in found.items()), key=lambda x: (-x[1], x[0]))
        if self.guard and self.nonop:
            text_n = re.sub(r"\s+", " ", " " + re.sub(r"[^a-z0-9& ]+", " ", _CORP.sub(" ", norm(title or "") + " " + norm((summary or "")[:600]))) + " ")
            out = [(t, c, h) for (t, c, h) in out if t not in self.nonop or h in ("exchange_tag", "cashtag", "cik") or (len(self.nonop[t]) >= 6 and (" " + self.nonop[t] + " ") in text_n)]
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
