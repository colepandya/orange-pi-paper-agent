"""Judge one news item before it becomes evidence: who published it, who first reported it, how good the text is, and so how far to believe it.

credibility = what the FEED learned for this event type (as before, but starting from the outlet-aware prior)
            x the outlet's measured reputation factor (1.0 until it has enough measured events)
            x a quality factor, CENTRED on a typical story: 1 + w x (quality - 0.58), held between 0.7 and 1.1, w = intelligence.quality_weight. Not applied to primary sources (a filing is as reliable as it is dull).
(Measured on the 11,820 non-filing stories of the 6 Nov export: median quality 0.575. A factor of 1 - w + w x quality would have cut EVERY story's credibility by about 15%, which carries no information; the centred factor averages 1.01, leaves a typical story alone, marks the weakest down and the strongest up.)
Switch it off (intelligence.news_intel = off) and the old single-number-per-feed credibility is used exactly as before; the outlet, origin and quality are still recorded.
"""
from __future__ import annotations

from typing import Callable, Dict, Iterable, List

from . import newsquality, outlets, reputation

NEUTRAL_QUALITY = 0.58                                                                                  # the median quality of real news (11,820 stories, 6 Nov export): a story at this score is neither marked up nor down
QF_LO, QF_HI = 0.7, 1.1


def _get(row, key, default=None):
    try:
        v = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def _num(x, default: float = 0.5) -> float:
    """A credibility is a finite number; anything else is the neutral 0.5 (a feed-level function should never return junk, but nothing downstream should depend on that)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v and abs(v) != float("inf") else default


def subjects_for(universe, tickers: Iterable[str]) -> List[str]:
    """The names a company goes by in a headline: its ticker, its name and a few aliases."""
    out: List[str] = []
    watch = getattr(universe, "watch", None) or {}
    for t in tickers or []:
        if not isinstance(t, str):
            continue
        out.append(t)
        meta = watch.get(t) if isinstance(watch, dict) else None
        if isinstance(meta, dict):
            nm = meta.get("name")
            if isinstance(nm, str) and nm:
                out.append(nm)
                out.append(nm.split(",")[0].replace(" Inc.", "").replace(" Inc", "").replace(" Corporation", "").replace(" Corp.", "").replace(" Corp", "").strip())
            out.extend(a for a in (meta.get("aliases") or [])[:3] if isinstance(a, str))
    return [s for s in dict.fromkeys(out) if len(s) >= 2]


def appraise(conn, cfg: dict, n, event_type: str, tickers: List[str], universe, source_credibility: Callable) -> Dict:
    """Everything the engine now knows about one item's reliability. `source_credibility(conn, source_id, event_type, prior)` is the feed-level learned blend."""
    ic = (cfg or {}).get("intelligence") or {}
    title, summary = _get(n, "title", "") or "", _get(n, "summary", "") or ""
    feed = _get(n, "source", "") or ""
    outlet = _get(n, "outlet", "") or outlets.outlet_of(None, _get(n, "url", ""), feed)
    origin = outlets.origin_of(outlet, title, summary)
    tier, _t = outlets.tier_of(outlet)
    q = newsquality.assess(title, summary, subjects_for(universe, tickers), primary=(tier == "primary"))
    try:
        feed_trust = float(_get(n, "trust", 0.5) or 0.5)
    except (TypeError, ValueError):
        feed_trust = 0.5
    if not ic.get("news_intel", True):
        return {"cred": _num(source_credibility(conn, feed, event_type, feed_trust)), "outlet": outlet, "origin": origin, "quality": q, "tier": tier, "prior": feed_trust, "rep": 1.0, "qf": 1.0}
    prior = outlets.prior_trust(feed_trust, outlet)
    try:
        w = max(0.0, min(1.0, float(ic.get("quality_weight", 0.4))))
    except (TypeError, ValueError):
        w = 0.4
    qf = 1.0 if tier == "primary" else max(QF_LO, min(QF_HI, 1.0 + w * (q["score"] - NEUTRAL_QUALITY)))
    rep = reputation.factor(conn, cfg, outlet or feed)
    cred = max(0.05, min(0.99, _num(source_credibility(conn, feed, event_type, prior)) * qf * rep))
    return {"cred": cred, "outlet": outlet, "origin": origin, "quality": q, "tier": tier, "prior": prior, "rep": rep, "qf": qf}
