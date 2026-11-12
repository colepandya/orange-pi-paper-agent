"""The research lab: tests the engine's own assumptions against stored price history BEFORE a rule is changed to use them.

Five studies, each a pure function of stored prices (or of the bot's own resolved forecasts):
  post_move    what stocks do after a 5-day move   (tests the forecast's "priced in" assumption)
  earnings     what stocks do after an earnings reaction
  big_days     what stocks do after a large move on heavy volume with no earnings release
  calibration  whether the forecast probabilities come true  (the bot's own resolved predictions)
  exit_plans   what a stop-and-target plan earns with NO signal at all (the bar a signal must clear)

How the numbers are made honest:
  * MARKET-ADJUSTED: every return is the stock's minus SPY's over the same days.
  * DAY-CLUSTERED: stocks on one day move together, so the observations of one day are averaged first and the t-statistic is taken across DAYS, not across stock-days.
  * OUT OF SAMPLE: the final 365 days are held out; the earlier days are the 'train' period. A result that holds in only one of them is not a result.
  * MANY TESTS: every table counts how many comparisons it contains; about one in twenty will look significant by chance.
  * COSTED where money is involved (shadow.cost_bps round trip).
A study never changes a trade. A result is a reason to test an idea going forward (the shadow book), not a reason to trade it.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import math
import time
from array import array
from typing import Callable, Dict, List, Optional, Tuple

from .timeutil import iso, market_state, parse_ts, utcnow

STUDY_ORDER = ("post_move", "earnings", "big_days", "calibration", "exit_plans")
TITLES = {"post_move": "What stocks do after a 5-day move (market-adjusted)", "earnings": "What stocks do after an earnings reaction", "big_days": "What stocks do after a big move on heavy volume (no earnings)",
          "calibration": "Do the forecast probabilities come true?", "exit_plans": "What a stop-and-target plan earns with NO signal"}
CAVEAT = ("Prices are daily bars, split-adjusted. Every return is against SPY. The t-statistic is taken across DAYS, not stock-days. The last 365 days are held out: a result that holds in only one period is not a result. "
          "Each table contains many comparisons, so some will look significant by chance. A finding here is a reason to test an idea going forward, never a reason to trade it.")
BUCKETS = ((-9.0, -0.15, "fell more than 15%", -1), (-0.15, -0.08, "fell 8% to 15%", -1), (-0.08, -0.03, "fell 3% to 8%", -1), (-0.03, 0.03, "flat (within 3%)", 0),
           (0.03, 0.08, "rose 3% to 8%", 1), (0.08, 0.15, "rose 8% to 15%", 1), (0.15, 9.0, "rose more than 15%", 1))
PRICED_IN_SHARE, PRICED_IN_CAP = 0.8, 0.20                                                           # what the forecast assumes (ev.py): share of the prior move treated as already priced in, and the cap on that move
OOS_DAYS = 365


class StudyTimeout(Exception):
    pass


# ------------------------------------------------------------------------------------------------------------------ data
class Series:
    """One stock's daily bars as parallel arrays (compact: a few hundred stocks over several years is tens of megabytes, not hundreds)."""
    __slots__ = ("sym", "d", "o", "h", "l", "c", "v", "_idx")

    def __init__(self, sym: str):
        self.sym, self.d, self.o, self.h, self.l, self.c, self.v, self._idx = sym, array("i"), array("d"), array("d"), array("d"), array("d"), array("d"), None

    def __len__(self) -> int:
        return len(self.c)

    def index(self) -> Dict[int, int]:
        if self._idx is None:
            self._idx = {d: i for i, d in enumerate(self.d)}
        return self._idx


def _pos(x, default: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if (math.isfinite(v) and v > 0) else default


def load_series(conn, check: Optional[Callable] = None) -> Dict[str, Series]:
    """Every symbol's bars. A bar with no usable close is skipped; a missing open/high/low is replaced by the close (never invented)."""
    out: Dict[str, Series] = {}
    cur, s = None, None
    for sym, day, o, h, l, c, v in conn.execute("SELECT symbol,day,open,high,low,close,volume FROM market_series ORDER BY symbol,day"):
        cl = _pos(c, 0.0)
        if cl <= 0:
            continue
        try:
            dord = dt.date.fromisoformat(str(day)[:10]).toordinal()
        except ValueError:
            continue
        if sym != cur:
            if check:
                check()
            cur, s = sym, Series(sym)
            out[sym] = s
        if s.d and dord <= s.d[-1]:
            continue                                                                                   # keep strictly increasing days
        s.d.append(dord)
        s.o.append(_pos(o, cl))
        s.h.append(_pos(h, cl))
        s.l.append(_pos(l, cl))
        s.c.append(cl)
        s.v.append(_pos(v, 0.0) if v is not None else 0.0)
    return out


def align(s: Series, spy: Series) -> array:
    """SPY's close on each of this stock's days (NaN where SPY has no bar), so a return and the market's over the same days can be taken by index."""
    idx = spy.index()
    nan = float("nan")
    return array("d", [spy.c[idx[d]] if d in idx else nan for d in s.d])


# ------------------------------------------------------------------------------------------------------------------ statistics
class DailyAcc:
    """Observations pooled by DAY. The statistic is the mean of the daily means and its t is taken across days."""

    def __init__(self):
        self.cells: Dict[int, List[float]] = {}

    def add(self, day: int, x: float) -> None:
        if not isinstance(day, int) or isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            return                                                                                     # a value that is not a finite number is never averaged in
        c = self.cells.get(day)
        if c is None:
            self.cells[day] = [x, 1]
        else:
            c[0] += x
            c[1] += 1

    def stats(self, lo: Optional[int] = None, hi: Optional[int] = None) -> Optional[dict]:
        days = [d for d in self.cells if (lo is None or d >= lo) and (hi is None or d < hi)]
        if not days:
            return None
        means = [self.cells[d][0] / self.cells[d][1] for d in days]
        k, nobs = len(means), sum(self.cells[d][1] for d in days)
        m = sum(means) / k
        t = se = None
        if k >= 3:
            sd = math.sqrt(sum((x - m) ** 2 for x in means) / (k - 1))
            se = sd / math.sqrt(k)
            t = (m / se) if se > 0 else None
        return {"obs": nobs, "days": k, "mean": m, "se": se, "t": t}

    def summary(self, cut: Optional[int], min_days: int = 30) -> Optional[dict]:
        """Overall, plus the train period (before the cut) and the held-out test period (from the cut). The split is shown only when BOTH halves have at least `min_days` days: a train period of two days is noise that looks like evidence."""
        a = self.stats()
        if a is None:
            return None
        cut = cut if isinstance(cut, int) and not isinstance(cut, bool) else None
        min_days = min_days if isinstance(min_days, int) and not isinstance(min_days, bool) else 30

        def pc(s):
            return None if s is None else {"mean_pct": round(100 * s["mean"], 3), "t": None if s["t"] is None else round(s["t"], 2), "obs": s["obs"], "days": s["days"]}
        out = pc(a)
        out["train"] = out["test"] = None
        if cut is not None:
            tr, te = self.stats(hi=cut), self.stats(lo=cut)
            if tr is not None and te is not None and tr["days"] >= min_days and te["days"] >= min_days:
                out["train"], out["test"] = pc(tr), pc(te)
        return out


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """The Wilson score interval for a proportion: honest at small n, where 'k/n' alone is not."""
    try:
        n, k, z = int(n), int(k), float(z)
    except (TypeError, ValueError, OverflowError):
        return 0.0, 1.0
    if n <= 0 or not math.isfinite(z):
        return 0.0, 1.0
    k = max(0, min(k, n))
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def _cut(series: Dict[str, Series]) -> Optional[int]:
    last = max((s.d[-1] for s in series.values() if len(s)), default=None)
    return None if last is None else last - OOS_DAYS


def _window(series: Dict[str, Series]) -> dict:
    ds = [s for s in series.values() if len(s)]
    if not ds:
        return {"from": None, "to": None, "symbols": 0, "stock_days": 0}
    lo, hi = min(s.d[0] for s in ds), max(s.d[-1] for s in ds)
    return {"from": dt.date.fromordinal(lo).isoformat(), "to": dt.date.fromordinal(hi).isoformat(), "symbols": len(ds), "stock_days": sum(len(s) for s in ds)}


def _ab(c, m, i: int, j: int) -> Optional[float]:
    """The stock's return from i to j minus SPY's over the same days, or None if SPY has no bar on either."""
    mi, mj = m[i], m[j]
    if mi != mi or mj != mj or mi <= 0:
        return None
    return (c[j] / c[i] - 1.0) - (mj / mi - 1.0)


# ------------------------------------------------------------------------------------------------------------------ studies
def study_post_move(series: Dict[str, Series], spy: Series, cfg: dict, conn=None, check=None) -> dict:
    hz = (1, 3, 5)
    acc = {(b[2], h): DailyAcc() for b in BUCKETS for h in hz}
    for sym, s in series.items():
        if sym == spy.sym or len(s) < 60:
            continue
        if check:
            check()
        m, c, d, n = align(s, spy), s.c, s.d, len(s)
        for i in range(5, n - 5):
            if m[i] != m[i]:
                continue
            r5 = c[i] / c[i - 5] - 1.0
            name = None
            for lo, hi, nm, _sg in BUCKETS:
                if lo <= r5 < hi:
                    name = nm
                    break
            if name is None:
                continue
            for h in hz:
                x = _ab(c, m, i, i + h)
                if x is not None:
                    acc[(name, h)].add(d[i], x)
    cut = _cut(series)
    rows = []
    for lo, hi, nm, sg in BUCKETS:
        mid = (max(lo, -0.30) + min(hi, 0.30)) / 2.0                                                      # the open-ended buckets are represented by a large move of about 22-30%, where the forecast cap binds
        rows.append({"label": nm, "sign": sg, "model_subtracts_pct": round(100 * PRICED_IN_SHARE * min(PRICED_IN_CAP, abs(mid)), 2) if sg else 0.0,
                     "h": {str(h): acc[(nm, h)].summary(cut) for h in hz}})
    return {"rows": rows, "notes": ["'model subtracts' is what the forecast takes off the expected move in the direction of the prior move (80% of it, capped at 16%), at the bucket's midpoint. Compare it with what actually happened, in the direction of the prior move.",
                                    "A positive number means the stock rose against the market; for the 'fell' buckets a positive number is a rebound."], "tests": len(BUCKETS) * len(hz)}


def _earnings_days(conn) -> Dict[str, List[int]]:
    out: Dict[str, List[int]] = {}
    if conn is None:
        return out
    try:
        for t, f in conn.execute("SELECT ticker,filed FROM kb_filing_events WHERE event_type='results_announcement' AND ticker IS NOT NULL AND filed IS NOT NULL").fetchall():
            try:
                out.setdefault(t, []).append(dt.date.fromisoformat(str(f)[:10]).toordinal())
            except ValueError:
                continue
    except Exception:  # noqa: BLE001 - the table may not exist yet
        return {}
    return out


EARN_BUCKETS = ((0.08, 9.0, "reaction +8% or more", 1), (0.03, 0.08, "reaction +3% to +8%", 1), (-0.08, -0.03, "reaction -3% to -8%", -1), (-9.0, -0.08, "reaction -8% or worse", -1))


def study_earnings(series: Dict[str, Series], spy: Series, cfg: dict, conn=None, check=None) -> dict:
    hz = (1, 3, 5, 10)
    acc = {(b[2], h): DailyAcc() for b in EARN_BUCKETS for h in hz}
    earn = _earnings_days(conn)
    first = min((x for v in earn.values() for x in v), default=None)
    n_events = 0
    for sym, days in earn.items():
        s = series.get(sym)
        if s is None or sym == spy.sym or len(s) < 60:
            continue
        if check:
            check()
        m, c, d, n = align(s, spy), s.c, s.d, len(s)
        for fd in set(days):
            i = bisect.bisect_left(d, fd)                                                              # the first session on or after the release date
            if i < 1 or i + 1 + max(hz) >= n:
                continue
            rx = _ab(c, m, i - 1, i + 1)                                                               # before-open and after-close releases are both inside close(i-1) -> close(i+1)
            if rx is None:
                continue
            name = next((nm for lo, hi, nm, _sg in EARN_BUCKETS if (lo < rx <= hi if lo >= 0 else lo <= rx < hi)), None)
            if name is None:
                continue
            n_events += 1
            sign = 1.0 if rx > 0 else -1.0
            for h in hz:
                x = _ab(c, m, i + 1, i + 1 + h)
                if x is not None:
                    acc[(name, h)].add(d[i + 1], sign * x)
    cut = _cut(series)
    rows = [{"label": nm, "sign": sg, "h": {str(h): acc[(nm, h)].summary(cut) for h in hz}} for _lo, _hi, nm, sg in EARN_BUCKETS]
    return {"rows": rows, "notes": ["Drift AFTER the reaction, in the direction of the reaction: positive = it kept going, negative = it reversed.",
                                    "Earnings dates come from stored 8-K filings%s: %d usable events. More history needs more filings, not more prices." % ((" since " + dt.date.fromordinal(first).isoformat()) if first else "", n_events)], "tests": len(EARN_BUCKETS) * len(hz), "events": n_events}


def study_big_days(series: Dict[str, Series], spy: Series, cfg: dict, conn=None, check=None) -> dict:
    hz = (1, 3, 5, 10)
    names = ("UP 5%+ on 2x volume", "DOWN 5%+ on 2x volume")
    acc = {(nm, h): DailyAcc() for nm in names for h in hz}
    earn = _earnings_days(conn)
    for sym, s in series.items():
        if sym == spy.sym or len(s) < 80:
            continue
        if check:
            check()
        m, c, d, v, n = align(s, spy), s.c, s.d, s.v, len(s)
        near = set()
        for fd in earn.get(sym, ()):
            i = bisect.bisect_left(d, fd)
            near.update(range(i - 2, i + 3))                                                           # a move inside an earnings window is the earnings study's business
        pv = [0.0]
        for x in v:
            pv.append(pv[-1] + x)
        for i in range(21, n - max(hz) - 1):
            if i in near:
                continue
            a = _ab(c, m, i - 1, i)
            if a is None or abs(a) < 0.05:
                continue
            avg = (pv[i] - pv[i - 20]) / 20.0
            if avg <= 0 or v[i] < 2 * avg:
                continue
            nm, sign = (names[0], 1.0) if a > 0 else (names[1], -1.0)
            for h in hz:
                x = _ab(c, m, i, i + h)
                if x is not None:
                    acc[(nm, h)].add(d[i], sign * x)
    cut = _cut(series)
    return {"rows": [{"label": nm, "sign": 1 if k == 0 else -1, "h": {str(h): acc[(nm, h)].summary(cut) for h in hz}} for k, nm in enumerate(names)],
            "notes": ["A move of 5% or more against the market, on at least twice the 20-day average volume, outside an earnings window. Drift is in the direction of the move: negative = it reversed."], "tests": len(names) * len(hz)}


def study_calibration(series: Dict[str, Series], spy: Series, cfg: dict, conn=None, check=None) -> dict:
    rows_db = []
    if conn is not None:
        rows_db = conn.execute("SELECT p.probability pr, p.event_type et, o.outcome oc FROM predictions p JOIN outcomes o ON o.prediction_id=p.id "
                               "WHERE p.kind='forecast' AND p.probability IS NOT NULL AND o.outcome IS NOT NULL").fetchall()
    data = []
    for r in rows_db:
        try:
            pr, oc = float(r["pr"]), float(r["oc"])
        except (TypeError, ValueError):
            continue
        if math.isfinite(pr) and math.isfinite(oc) and 0.0 <= pr <= 1.0:
            data.append((pr, 1.0 if oc >= 0.5 else 0.0, r["et"] or "(none)"))
    n = len(data)
    base = sum(x[1] for x in data) / n if n else None
    bins = []
    for lo, hi in ((0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0001)):
        xs = [x for x in data if lo <= x[0] < hi]
        if xs:
            k = int(sum(x[1] for x in xs))
            wl, wh = wilson(k, len(xs))
            bins.append({"kind": "bin", "label": "%d%% to %d%%" % (round(100 * lo), min(100, round(100 * hi))), "n": len(xs), "said_pct": round(100 * sum(x[0] for x in xs) / len(xs), 1), "came_true_pct": round(100 * k / len(xs), 1),
                         "lo_pct": round(100 * wl, 1), "hi_pct": round(100 * wh, 1), "flag": "worse than a coin" if wh < 0.5 else "better than a coin" if wl > 0.5 else "no evidence either way"})
    by: Dict[str, List[float]] = {}
    for pr, hit, et in data:
        by.setdefault(et, []).append(hit)
    types = []
    for et, hits in sorted(by.items(), key=lambda kv: -len(kv[1])):
        if len(hits) >= 10:
            k = int(sum(hits))
            wl, wh = wilson(k, len(hits))
            types.append({"kind": "event_type", "label": et, "n": len(hits), "came_true_pct": round(100 * k / len(hits), 1), "lo_pct": round(100 * wl, 1), "hi_pct": round(100 * wh, 1),
                          "flag": "worse than a coin" if wh < 0.5 else "better than a coin" if wl > 0.5 else "no evidence either way"})
    brier = (sum((x[0] - x[1]) ** 2 for x in data) / n) if n else None
    return {"rows": bins + types, "summary": {"n": n, "base_rate_pct": None if base is None else round(100 * base, 1), "brier": None if brier is None else round(brier, 4), "brier_always_half": 0.25,
                                               "brier_always_base_rate": None if base is None else round(base * (1 - base), 4), "event_types_under_10": sum(1 for h in by.values() if len(h) < 10)},
            "notes": ["'Came true' is the share of resolved forecasts whose claim (a move against the market) happened. A calibrated forecast says what it means: 80% should come true about 80% of the time. The interval is Wilson 95%.",
                      "A Brier score above 0.25 is worse than always saying 50%."], "tests": len(bins) + len(types)}


EXIT_PLANS = ((0.02, 0.03, 3), (0.02, 0.02, 3), (0.03, 0.03, 3), (0.04, 0.02, 3), (0.015, 0.012, 3), (0.06, 0.03, 10), (9.0, 9.0, 3))


def simulate_long(o, h, l, c, i: int, T: float, S: float, H: int) -> Optional[float]:
    """Long at the open of day i with a target T, a stop S and a time stop after H sessions. With daily bars the order inside a day is unknown: if the stop and the target are both reachable the STOP is assumed first, and a gap through
    either fills at the open (a stop gapped through loses more than S; a target gapped over wins more than T)."""
    if i < 0 or H < 1 or i + H - 1 >= len(c) or not (len(o) == len(h) == len(l) == len(c)):
        return None
    e = o[i]
    if not e > 0:
        return None
    for k in range(H):
        j = i + k
        if k > 0:
            if o[j] <= e * (1 - S):
                return o[j] / e - 1
            if o[j] >= e * (1 + T):
                return o[j] / e - 1
        if l[j] <= e * (1 - S):
            return -S
        if h[j] >= e * (1 + T):
            return T
    return c[i + H - 1] / e - 1


def study_exit_plans(series: Dict[str, Series], spy: Series, cfg: dict, conn=None, check=None) -> dict:
    sw = (cfg.get("strategy") or {})
    own = (0.02, float(sw.get("stop_max_pct", 0.03)), int(sw.get("max_hold_days", 3)))
    plans = [own] + [p for p in EXIT_PLANS if p != own]
    cost = float((cfg.get("shadow") or {}).get("cost_bps", 30.0)) / 1e4
    st = {p: [0, 0, 0.0, 0.0, 0.0] for p in plans}                                                     # trades, wins, sum win, sum loss, sum all
    used = 0
    for sym, s in series.items():
        if sym == spy.sym or len(s) < 120:
            continue
        if sum(s.c[i] * s.v[i] for i in range(len(s))) / len(s) < 5e7:
            continue                                                                                   # liquid names only
        if check:
            check()
        used += 1
        for i in range(1, len(s) - 11, 3):
            for p in plans:
                r = simulate_long(s.o, s.h, s.l, s.c, i, p[0], p[1], p[2])
                if r is None:
                    continue
                a = st[p]
                a[0] += 1
                a[4] += r
                if r > 0:
                    a[1] += 1
                    a[2] += r
                else:
                    a[3] += r
    rows = []
    for p in plans:
        n, w, sw_, sl, sa = st[p]
        if n:
            net = sa / n - cost
            rows.append({"label": ("your plan: " if p == own else "") + ("hold %d sessions, no stop, no target" % p[2] if p[0] >= 9 else "target %.1f%% / stop %.1f%% / %d sessions" % (100 * p[0], 100 * p[1], p[2])),
                         "trades": n, "win_pct": round(100 * w / n, 1), "avg_win_pct": round(100 * sw_ / w, 2) if w else None, "avg_loss_pct": round(100 * sl / (n - w), 2) if n > w else None,
                         "net_per_trade_pct": round(100 * net, 3), "signal_must_add_pct": round(max(0.0, -100 * net), 3)})
    return {"rows": rows, "notes": ["Entries are every third day on EVERY liquid stock: no signal at all. 'Net per trade' is after %.1f bps round trip (the market's drift over the period is included, so long-only plans are slightly flattered). "
                                    "'Signal must add' is the gross edge per trade a signal has to supply just to break even with that exit plan." % (1e4 * cost)], "tests": len(plans), "stocks": used}


STUDIES: Dict[str, Callable] = {"post_move": study_post_move, "earnings": study_earnings, "big_days": study_big_days, "calibration": study_calibration, "exit_plans": study_exit_plans}


def run_study(conn, cfg: dict, name: str, series: Dict[str, Series], now=None, check=None) -> dict:
    """One study with its metadata. Needs SPY in the series: without a market there is no market-adjustment."""
    now = now or utcnow()
    spy = series.get("SPY")
    if spy is None or len(spy) < 30:
        return {"study": name, "title": TITLES[name], "status": "no_market", "rows": [], "notes": ["SPY history is missing, so returns cannot be market-adjusted. The history backfill fetches it first."], "tests": 0,
                "window": _window(series), "computed": iso(now)}
    res = STUDIES[name](series, spy, cfg, conn, check)
    res.update({"study": name, "title": TITLES[name], "status": "ok", "window": _window(series), "computed": iso(now)})
    return res


# ------------------------------------------------------------------------------------------------------------------ persistence and running
def latest_run(conn) -> Optional[dict]:
    try:
        r = conn.execute("SELECT * FROM research_runs ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not r:
        return None
    d = dict(r)
    try:
        d["studies"] = json.loads(d.get("studies_json") or "{}")
    except (ValueError, TypeError):
        d["studies"] = {}
    if not isinstance(d["studies"], dict):
        d["studies"] = {}                                                                              # a corrupted record is "no studies", never an error
    return d


def _save(conn, run_id: int, studies: dict, **f) -> None:
    sets = ["studies_json=?"] + ["%s=?" % k for k in f]
    conn.execute("UPDATE research_runs SET %s WHERE id=?" % ",".join(sets), (json.dumps(studies, default=str),) + tuple(f.values()) + (run_id,))


def _new_run(conn, now) -> int:
    return conn.execute("INSERT INTO research_runs(started,status,studies_json) VALUES(?,?,?)", (iso(now), "running", "{}")).lastrowid


def _finish(conn, run_id: int, studies: dict, now) -> None:
    wins = [s.get("window") or {} for s in studies.values() if isinstance(s, dict) and s.get("window")]
    _save(conn, run_id, studies, finished=iso(now), status="complete", data_from=min((w["from"] for w in wins if w.get("from")), default=None), data_to=max((w["to"] for w in wins if w.get("to")), default=None),
          symbols=max((w.get("symbols", 0) for w in wins), default=0), rows=max((w.get("stock_days", 0) for w in wins), default=0))


def run_all(conn, cfg: dict, now=None, only: Optional[List[str]] = None, check=None, progress: Optional[Callable] = None) -> dict:
    """Every study, from the command line: no time limit. Returns the saved run."""
    now = now or utcnow()
    names = [n for n in STUDY_ORDER if not only or n in only]
    rid = _new_run(conn, now)
    studies: dict = {}
    series = load_series(conn, check)
    for nm in names:
        if progress:
            progress(nm)
        studies[nm] = run_study(conn, cfg, nm, series, now, check)
        _save(conn, rid, studies)
    _finish(conn, rid, studies, utcnow() if now is None else now)
    conn.commit()
    return latest_run(conn)


def _settled(e) -> bool:
    """A study that has an answer, or has been given up on (three timeouts): either way it no longer holds the run open."""
    return isinstance(e, dict) and (e.get("status") in ("ok", "no_market") or (e.get("status") == "timeout" and e.get("tries", 0) >= 3))


def maybe_run(conn, cfg: dict, now=None, clock: Optional[Callable] = None, session: Optional[Callable] = None) -> dict:
    """The per-cycle step: while the market is closed and a weekly run is due, do ONE study with a time limit. A study that is too slow for this machine is retried later and, after three timeouts, left to the command line."""
    rc = cfg.get("research") or {}
    if not rc.get("enabled", True):
        return {"status": "disabled"}
    now = now or utcnow()
    clock = clock or time.monotonic
    try:
        if (session or (lambda n: market_state(n).get("session")))(now) == "REGULAR":
            return {"status": "waiting", "why": "the market is open"}
        run = latest_run(conn)
        if run and run["status"] == "complete" and parse_ts(run["finished"]) > now - dt.timedelta(days=int(rc.get("rerun_days", 7))):
            return {"status": "current"}
        rid = run["id"] if run and run["status"] == "running" else _new_run(conn, now)
        studies = (run["studies"] if run and run["status"] == "running" else {}) or {}
        name = None
        for nm in STUDY_ORDER:
            e = studies.get(nm)
            if e is None or (e.get("status") == "timeout" and e.get("tries", 0) < 3 and parse_ts(e.get("retry_after")) <= now):
                name = nm
                break
        if name is None:
            if not all(_settled(studies.get(n)) for n in STUDY_ORDER):
                return {"status": "waiting", "why": "a study is waiting to be retried"}
            _finish(conn, rid, studies, now)
            conn.commit()
            return {"status": "complete"}
        t0, budget = clock(), float(rc.get("max_seconds", 60.0))

        def check():
            if clock() - t0 > budget:
                raise StudyTimeout(name)
        try:
            studies[name] = run_study(conn, cfg, name, load_series(conn, check), now, check)
            out = {"status": "ran", "study": name}
        except StudyTimeout:
            tries = (studies.get(name) or {}).get("tries", 0) + 1
            studies[name] = {"study": name, "title": TITLES[name], "status": "timeout", "tries": tries, "retry_after": iso(now + dt.timedelta(hours=6)), "rows": [],
                             "notes": ["Too slow for this machine inside a cycle%s. Run `research run` from the command line." % (": given up" if tries >= 3 else "; will retry")], "tests": 0}
            out = {"status": "timeout", "study": name}
        _save(conn, rid, studies)
        if all(_settled(studies.get(n)) for n in STUDY_ORDER):
            _finish(conn, rid, studies, now)
        conn.commit()
        return out
    except Exception as ex:  # noqa: BLE001 - research never raises into a trade
        return {"status": "error", "error": "%s: %s" % (type(ex).__name__, str(ex)[:120])}


# ------------------------------------------------------------------------------------------------------------------ reporting
def _cell(x: Optional[dict]) -> str:
    try:
        t = x.get("t")
        return "n/a" if not x else "%+.2f%% (t=%s, %dd)" % (x["mean_pct"], "n/a" if t is None else "%+.1f" % t, x["days"])
    except Exception:  # noqa: BLE001 - a malformed saved result is "n/a", never an error in a report or a page
        return "n/a"


def _row_lines(r: dict) -> List[str]:
    L: List[str] = []
    if "h" in r:
        L.append("   %-26s %s" % (r["label"], " | ".join("%ss %s" % (h, _cell(v)) for h, v in r["h"].items())) + ("   model subtracts %.1f%%" % r["model_subtracts_pct"] if r.get("model_subtracts_pct") else ""))
        for h, v in r["h"].items():
            if v and v.get("train") and v.get("test"):
                L.append("      %ss  train %s | held-out year %s" % (h, _cell(v["train"]), _cell(v["test"])))
    elif r.get("kind") in ("bin", "event_type"):
        L.append("   %-26s n=%-4d came true %5.1f%% (95%% %4.1f-%4.1f)  %s" % (r["label"], r["n"], r["came_true_pct"], r["lo_pct"], r["hi_pct"], r["flag"]) + ("  said %.0f%%" % r["said_pct"] if "said_pct" in r else ""))
    else:
        L.append("   %-52s trades %-6d win %5.1f%%  avg win %s  avg loss %s  net/trade %+.2f%%  signal must add %.2f%%" % (r["label"], r["trades"], r["win_pct"], r["avg_win_pct"], r["avg_loss_pct"], r["net_per_trade_pct"], r["signal_must_add_pct"]))
    return L


def format_report(run: Optional[dict]) -> str:
    if not run:
        return "No research run yet. It runs once a week while the market is closed, or now with: python3 -m paper_agent research run"
    L = ["RESEARCH LAB (observation only: nothing here changes a trade)", "run %s | %s | data %s to %s | %s symbols" % (run.get("started"), run.get("status"), run.get("data_from"), run.get("data_to"), run.get("symbols")), ""]
    for nm in STUDY_ORDER:
        st = (run.get("studies") or {}).get(nm)
        if not st or not isinstance(st, dict):
            L.append("%s: not run yet\n" % TITLES[nm])
            continue
        L.append("== %s  [%s, %d comparisons]" % (st.get("title", nm), st.get("status"), st.get("tests", 0)))
        for r in (st.get("rows") if isinstance(st.get("rows"), list) else []):
            try:
                L.extend(_row_lines(r))
            except Exception:  # noqa: BLE001
                L.append("   (a malformed result row was skipped)")
            continue
        if st.get("summary"):
            L.append("   summary: %s" % json.dumps(st["summary"]))
        for n in st.get("notes", []):
            L.append("   note: " + n)
        L.append("")
    L.append("READ THIS FIRST: " + CAVEAT)
    return "\n".join(L)


def run_cli(args, conn, cfg: dict) -> int:
    from . import history
    what = getattr(args, "what", None) or "report"
    if what == "status":
        run = latest_run(conn)
        print(json.dumps({"history": history.coverage(conn, cfg), "last_run": None if not run else {k: run.get(k) for k in ("id", "started", "finished", "status", "data_from", "data_to", "symbols")},
                          "studies_done": [] if not run else sorted(run["studies"])}, indent=1, default=str))
        return 0
    if what == "history":
        lim = getattr(args, "limit", None)
        print(json.dumps(history.backfill_now(conn, cfg, 50 if lim is None else history._int(lim, 50)), indent=1))                  # 0 means fetch NOTHING, not "use the default"
        return 0
    if what == "run":
        only = [args.study] if getattr(args, "study", None) else None
        print(format_report(run_all(conn, cfg, only=only, progress=lambda n: print("running %s ..." % n, flush=True))))
        return 0
    print(format_report(latest_run(conn)))
    return 0
