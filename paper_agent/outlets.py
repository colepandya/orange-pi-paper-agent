"""Who actually published (and who originally reported) a story.

Until Build 57 every news item carried only the name of the FEED it arrived through, and credibility was one hand-set number per feed. Two problems followed:
  * An aggregator (Finnhub, Techmeme, Hacker News, Yahoo) was trusted as one thing although it carries Reuters, a press release and an anonymous blog alike; the publisher the feed itself reported (Finnhub's `source`, a link's domain) was thrown away.
  * Ten copies of one Reuters wire story looked like ten independent confirmations.

This module gives every item an OUTLET (the publisher, normalised) and an ORIGIN (the wire that first reported it, when the text says so, else the outlet). Two items with the same origin are one report, not two.
Tiers here are PRIORS, not verdicts: they set where an outlet starts, and the measured follow-through of its stories (reputation.py) moves it from there. They are deliberately coarse and capped (0.40 to 0.97).
"""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

# a link to one of these says nothing about who wrote the story
AGGREGATOR_DOMAINS = {"finnhub.io", "news.google.com", "techmeme.com", "news.ycombinator.com", "feedburner.com", "feeds.feedburner.com", "flipboard.com", "msn.com", "feedproxy.google.com", "t.co", "bit.ly"}

DOMAIN_NAMES: Dict[str, str] = {
    "reuters.com": "reuters", "apnews.com": "ap", "bloomberg.com": "bloomberg", "wsj.com": "wsj", "ft.com": "ft", "cnbc.com": "cnbc", "marketwatch.com": "marketwatch", "barrons.com": "barrons",
    "seekingalpha.com": "seeking alpha", "fool.com": "motley fool", "zacks.com": "zacks", "benzinga.com": "benzinga", "finance.yahoo.com": "yahoo finance", "yahoo.com": "yahoo finance",
    "prnewswire.com": "pr newswire", "businesswire.com": "business wire", "globenewswire.com": "globenewswire", "accesswire.com": "accesswire", "nytimes.com": "nyt", "washingtonpost.com": "washington post",
    "bbc.com": "bbc", "bbc.co.uk": "bbc", "theguardian.com": "guardian", "npr.org": "npr", "aljazeera.com": "al jazeera", "economist.com": "economist", "fortune.com": "fortune", "forbes.com": "forbes",
    "businessinsider.com": "business insider", "cnn.com": "cnn", "techcrunch.com": "techcrunch", "theverge.com": "the verge", "arstechnica.com": "ars technica", "geekwire.com": "geekwire",
    "investorplace.com": "investorplace", "thestreet.com": "thestreet", "tipranks.com": "tipranks", "investopedia.com": "investopedia", "sec.gov": "sec", "federalregister.gov": "federal register",
    "usaspending.gov": "usaspending", "fda.gov": "fda", "ftc.gov": "ftc", "federalreserve.gov": "federal reserve", "bls.gov": "bls", "eia.gov": "eia", "treasury.gov": "us treasury", "cisa.gov": "cisa",
    "substack.com": "substack", "medium.com": "medium", "reddit.com": "reddit", "stocktwits.com": "stocktwits", "x.com": "x", "twitter.com": "x",
}
ALIASES: Dict[str, str] = {"yahoo": "yahoo finance", "yahoo!": "yahoo finance", "yahoo finance": "yahoo finance", "seekingalpha": "seeking alpha", "the wall street journal": "wsj", "wall street journal": "wsj",
                           "associated press": "ap", "the associated press": "ap", "new york times": "nyt", "the new york times": "nyt", "the motley fool": "motley fool", "marketwatch": "marketwatch",
                           "prnewswire": "pr newswire", "businesswire": "business wire", "the guardian": "guardian", "financial times": "ft", "the economist": "economist"}

# (tier, prior trust). Coarse on purpose: a prior is where an outlet STARTS, not where it ends up.
TIERS: Dict[str, Tuple[str, float]] = {}
for _names, _tier, _trust in (
        (("sec", "federal register", "usaspending", "fda", "ftc", "federal reserve", "bls", "eia", "us treasury", "cisa"), "primary", 0.97),
        (("reuters", "ap", "bloomberg", "afp"), "wire", 0.90),
        (("wsj", "ft"), "wire", 0.88),
        (("cnbc", "marketwatch", "barrons", "nyt", "washington post", "bbc", "guardian", "npr", "economist", "fortune"), "major", 0.80),
        (("cnn", "al jazeera"), "major", 0.74),
        (("forbes", "business insider"), "major", 0.66),
        (("pr newswire", "business wire", "globenewswire", "accesswire"), "press release", 0.72),
        (("techcrunch", "the verge", "ars technica", "geekwire"), "trade", 0.70),
        (("benzinga", "thestreet"), "commentary", 0.55),
        (("seeking alpha", "motley fool", "zacks", "investorplace", "tipranks", "investopedia"), "commentary", 0.48),
        (("yahoo finance",), "aggregator", 0.60),
        (("substack", "medium", "reddit", "stocktwits", "x", "hacker news"), "social", 0.40)):
    for _n in _names:
        TIERS[_n] = (_tier, _trust)

_WIRE_PATTERNS = (
    (re.compile(r"\((reuters)\)", re.I), "reuters"), (re.compile(r"^\s*(reuters)\s*[-\u2013\u2014:]", re.I), "reuters"), (re.compile(r"\bby (reuters)\b", re.I), "reuters"), (re.compile(r"\bsource: (reuters)\b", re.I), "reuters"),
    (re.compile(r"\((ap|associated press)\)", re.I), "ap"), (re.compile(r"^\s*(associated press|ap)\s*[-\u2013\u2014:]", re.I), "ap"), (re.compile(r"\bby (the )?associated press\b", re.I), "ap"),
    (re.compile(r"\((afp)\)", re.I), "afp"), (re.compile(r"^\s*(afp)\s*[-\u2013\u2014:]", re.I), "afp"),
    (re.compile(r"\((bloomberg)\)", re.I), "bloomberg"), (re.compile(r"^\s*(bloomberg)\s*[-\u2013\u2014:]", re.I), "bloomberg"), (re.compile(r"\bby (bloomberg)\b", re.I), "bloomberg"),
    (re.compile(r"\b(pr newswire|prnewswire)\b", re.I), "pr newswire"), (re.compile(r"\b(business wire|businesswire)\b", re.I), "business wire"), (re.compile(r"\b(globenewswire|globe newswire)\b", re.I), "globenewswire"),
)


def domain_of(url) -> str:
    """The host of a link without 'www.', or ''. Never raises."""
    try:
        h = (urlparse(str(url or "").strip()).netloc or "").lower().split("@")[-1].split(":")[0]
    except (ValueError, TypeError, AttributeError):
        return ""
    return h[4:] if h.startswith("www.") else h


def normalize(name) -> str:
    """A publisher's name as one canonical lowercase key: 'Yahoo' -> 'yahoo finance', 'The Wall Street Journal' -> 'wsj'."""
    if not isinstance(name, str):
        return ""
    s = re.sub(r"\s+", " ", re.sub(r"[^\w\s.!&-]", " ", name.strip().lower())).strip()
    return ALIASES.get(s, s)[:60]


def outlet_of(explicit=None, url=None, feed_name=None) -> str:
    """The publisher: what the feed itself says it is (Finnhub's `source`), else the link's domain unless that is an aggregator, else the feed's own name."""
    e = normalize(explicit)
    if e:
        return e
    d = domain_of(url)
    if d and d not in AGGREGATOR_DOMAINS and not any(d.endswith("." + a) for a in AGGREGATOR_DOMAINS):
        name = DOMAIN_NAMES.get(d) or DOMAIN_NAMES.get(".".join(d.split(".")[-2:]))
        return name or d
    return normalize(feed_name)


def wire_origin(title, summary="") -> Optional[str]:
    """The wire service that first reported a story, when the text says so ('(Reuters) - ...', 'By Associated Press', a press-release distributor), else None."""
    for text in (title, summary):
        if not isinstance(text, str):
            continue
        t = text[:400]
        for pat, name in _WIRE_PATTERNS:
            if pat.search(t):
                return name
    return None


def origin_of(outlet: str, title="", summary="") -> str:
    """Two items with the same origin are ONE report: a syndicated copy of a Reuters story is not a second confirmation."""
    return wire_origin(title, summary) or (outlet or "")


def tier_of(outlet) -> Tuple[Optional[str], Optional[float]]:
    t = TIERS.get(normalize(outlet) if isinstance(outlet, str) else "")
    return t if t else (None, None)


def prior_trust(feed_trust, outlet) -> float:
    """Where an item's credibility STARTS: the feed's hand-set trust, blended 40/60 with the outlet's tier when the outlet is known (an aggregator's own number says little about the publisher inside it)."""
    try:
        base = float(feed_trust)
        if base != base:
            base = 0.5
    except (TypeError, ValueError):
        base = 0.5
    _tier, trust = tier_of(outlet)
    v = base if trust is None else 0.4 * base + 0.6 * trust
    return max(0.05, min(0.99, v))
