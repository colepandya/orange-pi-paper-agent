"""Daily bars cache (Yahoo chart endpoint, keyless) used for regime, volatility, extension, stress betas and outcome scoring."""
from __future__ import annotations

import datetime as dt
import math
import statistics
import urllib.parse
from typing import Callable, Dict, List, Optional, Sequence

from . import net
from .timeutil import UTC, iso, parse_ts, utcnow

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d&includePrePost=false"


def parse_chart(payload: dict) -> List[dict]:
    chart = (payload or {}).get("chart") or {}
    res = (chart.get("result") or [None])[0]
    if not res or chart.get("error"):
        raise net.FetchError("chart error: %s" % chart.get("error"))
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    out = []
    for i, t in enumerate(ts):
        try:
            c = q["close"][i]
        except (KeyError, IndexError):
            continue
        if c is None or not isinstance(c, (int, float)) or not math.isfinite(c) or c <= 0:
            continue

        def g(k):
            try:
                v = q[k][i]
                return float(v) if v is not None else None
            except (KeyError, IndexError):
                return None
        day = dt.datetime.fromtimestamp(t, UTC).date().isoformat()
        out.append({"day": day, "open": g("open"), "high": g("high"), "low": g("low"), "close": float(c), "volume": g("volume")})
    return out


def parse_meta(payload: dict) -> dict:
    res = (((payload or {}).get("chart") or {}).get("result") or [None])[0] or {}
    m = res.get("meta") or {}
    return {"symbol": m.get("symbol"), "price": m.get("regularMarketPrice"), "time": m.get("regularMarketTime"), "prev_close": m.get("chartPreviousClose")}


def fetch_chart(symbol: str, range_: str = "1y", fetch: Optional[Callable] = None) -> dict:
    url = CHART % (urllib.parse.quote(symbol), range_)
    payload, _ = (fetch or net.get_json)(url, headers={"Accept": "application/json"})
    return payload


def store_series(conn, symbol: str, bars: Sequence[dict]) -> int:
    n = 0
    conn.execute("BEGIN")
    try:
        for b in bars:
            conn.execute("INSERT INTO market_series(symbol,day,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?) "
                         "ON CONFLICT(symbol,day) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume",
                         (symbol, b["day"], b.get("open"), b.get("high"), b.get("low"), b["close"], b.get("volume")))
            n += 1
        conn.execute("INSERT INTO series_state(symbol,fetched_at,last_day) VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET fetched_at=excluded.fetched_at,last_day=excluded.last_day",
                     (symbol, iso(), bars[-1]["day"] if bars else None))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return n


def refresh_symbol(conn, symbol: str, max_age_hours: float = 12, range_: str = "1y", fetch: Optional[Callable] = None, now=None) -> str:
    """Fetch & store if the cache is older than ``max_age_hours``. Returns 'fresh' | 'fetched' | 'error:<msg>'."""
    r = conn.execute("SELECT fetched_at FROM series_state WHERE symbol=?", (symbol,)).fetchone()
    if r:
        t = parse_ts(r[0])
        if t and ((now or utcnow()) - t).total_seconds() < max_age_hours * 3600:
            return "fresh"
    try:
        bars = parse_chart(fetch_chart(symbol, range_, fetch))
        if not bars:
            return "error:no bars"
        store_series(conn, symbol, bars)
        return "fetched"
    except net.FetchError as e:
        return "error:%s" % e
    except (KeyError, TypeError, ValueError) as e:
        return "error:parse %s" % e


def closes(conn, symbol: str, n: int = 260) -> List[float]:
    rows = conn.execute("SELECT close FROM market_series WHERE symbol=? ORDER BY day DESC LIMIT ?", (symbol, n)).fetchall()
    return [float(r[0]) for r in reversed(rows)]


def bars(conn, symbol: str, n: int = 260) -> List[dict]:
    rows = conn.execute("SELECT day,open,high,low,close,volume FROM market_series WHERE symbol=? ORDER BY day DESC LIMIT ?", (symbol, n)).fetchall()
    return [dict(r) for r in reversed(rows)]


def returns(xs: Sequence[float]) -> List[float]:
    return [xs[i] / xs[i - 1] - 1 for i in range(1, len(xs)) if xs[i - 1] > 0]


def stdev(xs: Sequence[float]) -> float:
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def ret_n(xs: Sequence[float], n: int) -> Optional[float]:
    return xs[-1] / xs[-1 - n] - 1 if len(xs) > n and xs[-1 - n] > 0 else None


def daily_vol(conn, symbol: str, n: int = 60, default: float = 0.025) -> float:
    c = closes(conn, symbol, n + 1)
    r = returns(c)
    return stdev(r) if len(r) >= 15 else default


def beta(conn, symbol: str, bench: str = "SPY", n: int = 120) -> Optional[float]:
    a, b = bars(conn, symbol, n + 1), bars(conn, bench, n + 1)
    da = {x["day"]: x["close"] for x in a}
    db = {x["day"]: x["close"] for x in b}
    days = sorted(set(da) & set(db))
    if len(days) < 30:
        return None
    ra = returns([da[d] for d in days])
    rb = returns([db[d] for d in days])
    vb = statistics.pvariance(rb)
    if vb <= 0:
        return None
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / len(ra)
    return max(-1.0, min(3.0, cov / vb))


def adv_usd(conn, symbol: str, n: int = 20) -> Optional[float]:
    b = bars(conn, symbol, n)
    vals = [x["close"] * x["volume"] for x in b if x.get("volume")]
    return statistics.mean(vals) if len(vals) >= 5 else None


def close_on_or_after(conn, symbol: str, day: str) -> Optional[dict]:
    r = conn.execute("SELECT day,close,high,low FROM market_series WHERE symbol=? AND day>=? ORDER BY day LIMIT 1", (symbol, day)).fetchone()
    return dict(r) if r else None


def close_on_or_before(conn, symbol: str, day: str) -> Optional[dict]:
    r = conn.execute("SELECT day,close,high,low FROM market_series WHERE symbol=? AND day<=? ORDER BY day DESC LIMIT 1", (symbol, day)).fetchone()
    return dict(r) if r else None
