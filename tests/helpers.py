"""Offline fixtures: synthetic charts, feeds, quotes, universe."""
import datetime as dt
import json
import os
import random
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from paper_agent import db, net  # noqa: E402
from paper_agent.config import load_config  # noqa: E402
from paper_agent.entities import Universe  # noqa: E402
from paper_agent.pipeline import Deps  # noqa: E402

NOW = dt.datetime(2026, 10, 27, 15, 0, tzinfo=dt.timezone.utc)   # Tue 11:00 ET, regular session

WATCH = {"NVDA": {"name": "NVIDIA Corp", "aliases": ["NVIDIA"], "cik": 1045810}, "LMT": {"name": "Lockheed Martin Corp", "aliases": ["Lockheed Martin", "Lockheed"], "cik": 936468},
         "MSFT": {"name": "Microsoft Corp", "aliases": ["Microsoft"], "cik": 789019}, "TGT": {"name": "Target Corp", "aliases": ["Target"], "cik": 27419},
         "XOM": {"name": "Exxon Mobil Corp", "aliases": ["Exxon Mobil", "Exxon"], "cik": 34088}}


def chart_payload(symbol: str, n: int = 260, start: float = 100.0, drift: float = 0.0005, vol: float = 0.012, seed=None, last_price=None):
    r = random.Random(seed if seed is not None else sum(map(ord, symbol)))
    p, ts, closes, vols = start, [], [], []
    d0 = NOW - dt.timedelta(days=n)
    for i in range(n):
        p *= 1 + drift + r.gauss(0, vol)
        ts.append(int((d0 + dt.timedelta(days=i)).timestamp()))
        closes.append(p)
        vols.append(5_000_000)
    if last_price:
        closes[-1] = last_price
    return {"chart": {"result": [{"meta": {"symbol": symbol, "regularMarketPrice": closes[-1], "regularMarketTime": int(NOW.timestamp())}, "timestamp": ts,
                                  "indicators": {"quote": [{"open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes], "close": closes, "volume": vols}]}}], "error": None}}


def series_fetch(url, headers=None, **kw):
    sym = url.split("/chart/")[1].split("?")[0]
    sym = sym.replace("%5E", "^").replace("%3D", "=")
    base = {"SPY": 450, "^VIX": 15, "^TNX": 4.2, "^IRX": 4.0, "HYG": 77, "LQD": 108, "DX-Y.NYB": 100, "CL=F": 70, "GC=F": 2000, "IWM": 200}.get(sym, 100)
    return chart_payload(sym, start=base, vol=0.004 if sym in ("^TNX", "^IRX") else 0.012), 200


def rss(items):
    body = "".join("<item><title>%s</title><link>http://x/%d</link><description>%s</description><pubDate>%s</pubDate></item>" % (t, i, s, p) for i, (t, s, p) in enumerate(items))
    return ("<rss><channel>%s</channel></rss>" % body).encode()


def make_fetch(items):
    def fetch(url, headers=None, timeout=15, max_bytes=0, data=None, method=None):
        if "gdeltproject" in url:
            return json.dumps({"articles": []}).encode(), 200, {}
        return rss(items), 200, {}
    return fetch


class Quotes:
    def __init__(self, prices):
        self.prices = prices

    def __call__(self, conn, ticker, purpose="exit"):
        p = self.prices.get(ticker)
        if p is None:
            return {"status": "FAILED", "price": None, "execution_eligible": False}
        return {"ticker": ticker, "status": "PASS", "price": p, "execution_eligible": True, "age_seconds": 4.0, "quoted_at": "x", "providers_used": ["A", "B"], "provider_count": 2}


_TEMPLATE = None


def fresh_db():
    """Migrated empty DB. The schema is built ONCE per process and copied (the Pi spends most of its test time creating tables otherwise)."""
    global _TEMPLATE
    if _TEMPLATE is None:
        _TEMPLATE = tempfile.mktemp(suffix=".db")
        c = db.open_db(_TEMPLATE)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
    p = tempfile.mktemp(suffix=".db")
    shutil.copyfile(_TEMPLATE, p)
    return db.connect(p)


def cfg():
    c = load_config("/nonexistent", with_overrides=False)      # never read the host's dashboard settings
    c["runtime"]["pause_file"] = "/nonexistent/pause"
    c["runtime"]["min_available_memory_mb"] = 0
    return c


def universe():
    return Universe(WATCH)


def deps(items, prices, llm_transport=None):
    return Deps(fetch=make_fetch(items), fetch_json=lambda *a, **k: (_ for _ in ()).throw(net.FetchError("offline")), quote_fn=Quotes(prices), sleep=lambda s: None, series_fetch=series_fetch, llm_transport=llm_transport)
