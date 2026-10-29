"""Last look before money moves. A BUY decided overnight is re-checked at the open against everything that has happened SINCE it was decided:

  1. adverse news about THAT company (a later event that points the other way)            -> VETOED  (the thesis behind the BUY is contradicted)
  2. a market-wide shock (war, crash, emergency rate move, ...)                            -> HELD    (re-checked every cycle until the shock has aged out)
  3. an overnight price GAP against the idea (down: the market disagrees / bad news we cannot see;
     up: the move has already happened, the edge is gone)                                  -> VETOED
Nothing here predicts anything: it only refuses to act on a stale idea."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from .timeutil import iso, parse_ts


def adverse_company_news(conn, ticker: str, since: str, cfg: dict, exclude_event: Optional[int] = None) -> Optional[dict]:
    r = cfg["risk"]
    row = conn.execute("SELECT id,headline,direction,magnitude,credibility,created,event_type FROM events WHERE created>? AND tickers_json LIKE ? AND id!=? AND scope IN ('company','sector') "
                       "AND direction<=? AND magnitude>=? AND credibility>=0.5 AND gate IS NOT 'stale' ORDER BY direction ASC, magnitude DESC LIMIT 1",
                       (since, '%"' + ticker + '"%', exclude_event or -1, float(r["veto_news_direction"]), float(r["veto_news_magnitude"]))).fetchone()
    return dict(row) if row else None


def market_shock(conn, since: str, now, cfg: dict) -> Optional[dict]:
    r = cfg["risk"]
    hours = float(r["shock_hold_hours"])
    if hours <= 0:
        return None
    row = conn.execute("SELECT id,headline,direction,magnitude,credibility,created,event_type FROM events WHERE created>? AND created>? AND scope NOT IN ('company','sector') "
                       "AND direction<=? AND magnitude>=0.6 AND credibility>=0.6 ORDER BY direction ASC, magnitude DESC LIMIT 1",
                       (since, iso(now - dt.timedelta(hours=hours)), float(r["shock_direction"]))).fetchone()
    return dict(row) if row else None


def gap(price: float, ref: Optional[float], cfg: dict) -> Optional[float]:
    if not ref or ref <= 0 or not price:
        return None
    return price / ref - 1.0


def still_about(c: dict, universe) -> Optional[str]:
    """A BUY queued by an OLDER build may rest on a looser company match (a company named only in the body of someone else's story). Re-check against the HEADLINE: if the headline names companies and
    this ticker is not one of them, the BUY is about the wrong company. Thesis / supply-chain candidates (which carry a mechanism) and headlines naming nobody are left alone."""
    if universe is None or not c.get("headline") or c.get("mechanism") or c.get("chain_order") or c.get("origin") not in (None, "event"):
        return None
    named = {t for t, conf, _h in universe.match(c["headline"]) if conf >= 0.6}
    if named and c["ticker"] not in named:
        return "VETO: the headline is about %s, not %s: this BUY came from an older, looser company match" % (", ".join(sorted(named)), c["ticker"])
    return None


def review(conn, cfg: dict, c: dict, decided_at: str, now, price: Optional[float] = None, universe=None) -> dict:
    """-> {'action': 'go' | 'veto' | 'hold', 'reason': str}. ``price`` is the verified entry price (None = skip the gap test)."""
    r = cfg["risk"]
    wrong = still_about(c, universe)
    if wrong:
        return {"action": "veto", "reason": wrong}
    if r.get("veto_adverse_news", True):
        n = adverse_company_news(conn, c["ticker"], decided_at, cfg, c.get("event_id"))
        if n:
            return {"action": "veto", "reason": "VETO: news about %s arrived after this BUY was decided and points the other way (%s, direction %+.2f): \"%s\"" % (c["ticker"], n["event_type"], n["direction"], (n["headline"] or "")[:110])}
        s = market_shock(conn, decided_at, now, cfg)
        if s:
            until = parse_ts(s["created"]) + dt.timedelta(hours=float(r["shock_hold_hours"]))
            return {"action": "hold", "reason": "HELD: market-wide shock since this BUY was decided (%s, direction %+.2f): \"%s\". Re-checked every cycle; released after %s UTC if nothing worse happens." % (s["event_type"], s["direction"], (s["headline"] or "")[:90], until.strftime("%d %b %H:%M"))}
    g = gap(price, c.get("ref_price"), cfg) if price else None
    if g is not None:
        if g <= -float(r["max_gap_down_pct"]):
            return {"action": "veto", "reason": "VETO: %s opened %.1f%% below its previous close (%.2f -> %.2f): the market disagrees with this BUY, or there is bad news we cannot see" % (c["ticker"], -g * 100, c["ref_price"], price)}
        if g >= float(r["max_gap_up_pct"]):
            return {"action": "veto", "reason": "VETO: %s opened %.1f%% above its previous close (%.2f -> %.2f): the move has already happened, the edge is gone" % (c["ticker"], g * 100, c["ref_price"], price)}
    return {"action": "go", "reason": ""}
