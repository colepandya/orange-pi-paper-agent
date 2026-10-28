"""Capital-flow engine: insider buying/selling (Finnhub), buybacks (events), sector-ETF dollar-volume flow proxy.

13F / institutional ownership needs paid data; it is NOT faked - a research task is raised instead (see gaps.py)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import urllib.parse
from typing import Callable, List, Optional

from . import marketdata as md, net
from .timeutil import iso, parse_ts, utcnow

SECTOR_ETF = {"Semiconductors": "SMH", "Defense": "ITA", "Energy": "XLE", "Financials": "XLF", "Utilities": "XLU", "Healthcare": "XLV", "Technology": "XLK",
              "Software": "IGV", "Industrials": "XLI", "Materials": "XLB", "Consumer Staples": "XLP", "Consumer Discretionary": "XLY", "Real Estate": "XLRE"}
ETF_LIST = sorted(set(SECTOR_ETF.values()) | {"URA", "CIBR"})


def parse_insiders(j: dict, ticker: str) -> List[dict]:
    out = []
    for r in (j or {}).get("data", []) or []:
        code, chg, px = r.get("transactionCode"), r.get("change"), r.get("transactionPrice") or 0
        if code not in ("P", "S") or not chg:
            continue
        kind = "insider_buy" if code == "P" else "insider_sell"
        d = r.get("transactionDate") or r.get("filingDate")
        out.append({"ticker": ticker, "ts": iso(parse_ts(d)) if parse_ts(d) else iso(), "kind": kind, "value": abs(float(chg)) * float(px or 0), "shares": abs(float(chg)),
                    "detail": {"name": r.get("name"), "price": px}, "dedupe": hashlib.sha1(("%s|%s|%s|%s" % (r.get("name"), d, chg, px)).encode()).hexdigest()})
    return out


def store(conn, rows: List[dict], source: str) -> int:
    n = 0
    for r in rows:
        n += conn.execute("INSERT OR IGNORE INTO capital_flows(ticker,ts,kind,value,shares,detail_json,source,dedupe) VALUES(?,?,?,?,?,?,?,?)",
                          (r["ticker"], r["ts"], r["kind"], r["value"], r.get("shares"), json.dumps(r.get("detail") or {}), source, r["dedupe"])).rowcount
    return n


def fetch_insiders(conn, ticker: str, fetch: Callable = net.get_json) -> int:
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        return 0
    j, _ = fetch("https://finnhub.io/api/v1/stock/insider-transactions?symbol=%s&token=%s" % (urllib.parse.quote(ticker), urllib.parse.quote(key)))
    return store(conn, parse_insiders(j, ticker), "finnhub")


def record_event_flows(conn, since_iso: str) -> int:
    """Buyback / dividend-increase events -> capital_flows(kind='buyback')."""
    n = 0
    for e in conn.execute("SELECT id,published,amount_usd,tickers_json,headline FROM events WHERE event_type='buyback_dividend' AND created>=?", (since_iso,)):
        for t in json.loads(e["tickers_json"] or "[]")[:2]:
            n += conn.execute("INSERT OR IGNORE INTO capital_flows(ticker,ts,kind,value,shares,detail_json,source,dedupe) VALUES(?,?,?,?,?,?,?,?)",
                              (t, e["published"] or iso(), "buyback", e["amount_usd"] or 0, None, json.dumps({"headline": e["headline"]}), "events", "ev%d" % e["id"])).rowcount
    return n


def insider_signal(conn, ticker: str, days: int = 90, now=None) -> Optional[float]:
    since = iso((now or utcnow()) - __import__("datetime").timedelta(days=days))
    rows = conn.execute("SELECT kind,SUM(value) v,COUNT(*) n FROM capital_flows WHERE ticker=? AND kind IN ('insider_buy','insider_sell') AND ts>=? GROUP BY kind", (ticker, since)).fetchall()
    if not rows:
        return None
    d = {r["kind"]: (r["v"] or 0.0, r["n"]) for r in rows}
    b, s = d.get("insider_buy", (0, 0)), d.get("insider_sell", (0, 0))
    sig = (b[0] - s[0]) / (b[0] + s[0] + 2e5)
    if b[1] >= 3:
        sig += 0.25   # cluster buying
    return max(-1.0, min(1.0, sig))


def etf_flow_proxy(conn, etf: str) -> Optional[float]:
    """5-day dollar-volume z-score (vs 60d) signed by 5-day return. A PROXY for fund flows, not reported flows."""
    b = md.bars(conn, etf, 70)
    if len(b) < 40:
        return None
    dv = [x["close"] * (x["volume"] or 0) for x in b]
    base, recent = dv[-65:-5], statistics.mean(dv[-5:])
    sd = statistics.pstdev(base) or 1.0
    z = (recent - statistics.mean(base)) / sd
    r5 = md.ret_n([x["close"] for x in b], 5) or 0.0
    return max(-1.0, min(1.0, math.tanh(z / 2.0) * (1 if r5 >= 0 else -1)))


def flow_score(conn, ticker: str, sector: Optional[str] = None, now=None) -> dict:
    ins = insider_signal(conn, ticker, now=now)
    bb = conn.execute("SELECT COUNT(*) FROM capital_flows WHERE ticker=? AND kind='buyback' AND ts>=?", (ticker, iso((now or utcnow()) - __import__("datetime").timedelta(days=120)))).fetchone()[0]
    etf = etf_flow_proxy(conn, SECTOR_ETF[sector]) if sector in SECTOR_ETF else None
    parts = [(ins, 0.5), (min(1.0, bb * 0.5) if bb else None, 0.2), (etf, 0.3)]
    have = [(v, w) for v, w in parts if v is not None]
    score = sum(v * w for v, w in have) / sum(w for _, w in have) if have else 0.0
    return {"score": score, "insider": ins, "buybacks": bb, "etf_proxy": etf, "completeness": sum(w for v, w in parts if v is not None)}
