"""Time helpers. Everything is stored in UTC ISO-8601; display conversion happens in the dashboard.

Python 3.9 compatible (the Orange Pi ships Bullseye / Python 3.9): no ``X | Y`` unions, no ``match``.
"""
from __future__ import annotations

import datetime as dt
import re
from email.utils import parsedate_to_datetime
from typing import Optional
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")
NZ = ZoneInfo("Pacific/Auckland")


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC).replace(microsecond=0)


def iso(x: Optional[dt.datetime] = None) -> str:
    return (x or utcnow()).astimezone(UTC).replace(microsecond=0).isoformat()


_FRAC = re.compile(r"(\.\d{1,9})")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})T?(\d{2})(\d{2})(\d{2})Z?$")


def parse_ts(v) -> Optional[dt.datetime]:
    """Parse almost any timestamp a feed can produce into an aware UTC datetime.

    Handles RFC-822 ("Tue, 27 Oct 2026 14:00:00 -0400", "... GMT", "... +0000"), ISO-8601 with ``Z``,
    ISO with 7-9 fractional digits (Alpaca / Python 3.9 cannot parse those natively), GDELT compact
    ``20261027T140000Z``, plain dates, and epoch seconds/milliseconds. Returns ``None`` when unparseable.
    Naive timestamps are treated as UTC. Never raises.
    """
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.astimezone(UTC) if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        x = float(v)
        if x != x or x <= 0:
            return None
        if x > 1e17:      # nanoseconds
            x /= 1e9
        elif x > 1e11:    # milliseconds
            x /= 1e3
        try:
            return dt.datetime.fromtimestamp(x, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{9,13}(\.\d+)?", s):
        return parse_ts(float(s))
    m = _COMPACT.match(s)
    if m:
        try:
            return dt.datetime(*[int(g) for g in m.groups()], tzinfo=UTC)
        except ValueError:
            return None
    try:  # RFC-822 family
        if re.match(r"^[A-Za-z]{3},?\s+\d", s) or re.match(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}", s):
            x = parsedate_to_datetime(s)
            return x.astimezone(UTC) if x.tzinfo else x.replace(tzinfo=UTC)
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    t = s.replace("Z", "+00:00").replace("z", "+00:00")
    if t.endswith(" UTC"):
        t = t[:-4] + "+00:00"
    t = _FRAC.sub(lambda m: (m.group(1) + "000000")[:7], t, count=1)  # clamp to microseconds
    t = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", t)                    # +0000 -> +00:00
    try:
        x = dt.datetime.fromisoformat(t)
    except ValueError:
        try:
            x = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return x.astimezone(UTC) if x.tzinfo else x.replace(tzinfo=UTC)


def age_seconds(v, now: Optional[dt.datetime] = None) -> Optional[float]:
    x = parse_ts(v)
    if x is None:
        return None
    return ((now or utcnow()) - x).total_seconds()


def to_nz(v, fmt: str = "%d %b %Y, %I:%M %p %Z") -> str:
    x = parse_ts(v)
    return x.astimezone(NZ).strftime(fmt).lstrip("0") if x else "n/a"


# ---------------------------------------------------------------------------------------------
# NYSE calendar, computed by rule so it is correct in every year (the old table was 2026-only).
# ---------------------------------------------------------------------------------------------
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    d = dt.date(year + (month == 12), (month % 12) + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> dt.date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _observed(d: dt.date, saturday_to_friday: bool = True) -> Optional[dt.date]:
    if d.weekday() == 5:
        return d - dt.timedelta(days=1) if saturday_to_friday else None
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


_HOLIDAY_CACHE = {}


def nyse_holidays(year: int) -> set:
    if year in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[year]
    h = set()
    ny = _observed(dt.date(year, 1, 1), saturday_to_friday=False)  # NYSE does not close the prior Friday
    if ny:
        h.add(ny)
    h.add(_nth_weekday(year, 1, 0, 3))                      # MLK
    h.add(_nth_weekday(year, 2, 0, 3))                      # Presidents
    h.add(_easter(year) - dt.timedelta(days=2))             # Good Friday
    h.add(_last_weekday(year, 5, 0))                        # Memorial
    if year >= 2022:
        h.add(_observed(dt.date(year, 6, 19)))              # Juneteenth
    h.add(_observed(dt.date(year, 7, 4)))                   # Independence
    h.add(_nth_weekday(year, 9, 0, 1))                      # Labor
    thanks = _nth_weekday(year, 11, 3, 4)
    h.add(thanks)                                           # Thanksgiving
    h.add(_observed(dt.date(year, 12, 25)))                 # Christmas
    h.discard(None)
    _HOLIDAY_CACHE[year] = h
    return h


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def early_close(d: dt.date) -> bool:
    if not is_trading_day(d):
        return False
    thanks = _nth_weekday(d.year, 11, 3, 4)
    if d == thanks + dt.timedelta(days=1):
        return True
    if d.month == 12 and d.day == 24:
        return True
    if d.month == 7 and d.day == 3 and is_trading_day(dt.date(d.year, 7, 3)) and dt.date(d.year, 7, 4).weekday() < 5:
        return True
    return False


SESSION_LABEL = {"PRE_MARKET": "pre-market", "PRE": "pre-market", "REGULAR": "live", "OPEN": "live", "AFTER_HOURS": "after-hours", "CLOSED": "last trade"}


def minutes_to_close(now: Optional[dt.datetime] = None) -> Optional[float]:
    """Minutes until the regular session closes (None when it is not open)."""
    n = (now or utcnow()).astimezone(NY)
    if not market_state(n)["execution"]:
        return None
    close = 13 * 60 if early_close(n.date()) else 16 * 60
    return close - (n.hour * 60 + n.minute + n.second / 60.0)


def market_state(now: Optional[dt.datetime] = None) -> dict:
    """Return the US equity session and whether virtual execution is allowed."""
    n = (now or utcnow()).astimezone(NY)
    if not is_trading_day(n.date()):
        return {"session": "CLOSED", "execution": False}
    m = n.hour * 60 + n.minute
    close = 13 * 60 if early_close(n.date()) else 16 * 60
    if 4 * 60 <= m < 9 * 60 + 30:
        return {"session": "PRE_MARKET", "execution": False}
    if 9 * 60 + 30 <= m < close:
        return {"session": "REGULAR", "execution": True}
    if close <= m < 20 * 60:
        return {"session": "AFTER_HOURS", "execution": False}
    return {"session": "CLOSED", "execution": False}


def last_market_open(now: Optional[dt.datetime] = None) -> dt.datetime:
    """UTC time of the most recent regular-session open (09:30 New York) at or before ``now``."""
    n = (now or utcnow()).astimezone(NY)
    d = n.date()
    for _ in range(12):
        if is_trading_day(d):
            o = dt.datetime.combine(d, dt.time(9, 30), NY)
            if o <= n:
                return o.astimezone(UTC)
        d -= dt.timedelta(days=1)
    return (now or utcnow()) - dt.timedelta(days=7)


def next_market_open(now: Optional[dt.datetime] = None) -> dt.datetime:
    """UTC time of the next regular-session open (09:30 New York) strictly after ``now``."""
    n = (now or utcnow()).astimezone(NY)
    d = n.date()
    for _ in range(12):
        if is_trading_day(d):
            o = dt.datetime.combine(d, dt.time(9, 30), NY)
            if o > n:
                return o.astimezone(UTC)
        d += dt.timedelta(days=1)
    return (now or utcnow()) + dt.timedelta(days=7)


def trading_days_between(a, b) -> int:
    """Whole trading sessions elapsed after date(a) up to and including date(b)."""
    x, y = parse_ts(a), parse_ts(b)
    if not x or not y:
        return 0
    d, n = x.astimezone(NY).date(), 0
    end = y.astimezone(NY).date()
    while d < end:
        d += dt.timedelta(days=1)
        n += is_trading_day(d)
    return n


def add_trading_days(start, days: int) -> dt.datetime:
    x = parse_ts(start) or utcnow()
    d = x.astimezone(NY).date()
    left = int(days)
    while left > 0:
        d += dt.timedelta(days=1)
        if is_trading_day(d):
            left -= 1
    return dt.datetime.combine(d, x.astimezone(NY).timetz().replace(tzinfo=None), NY).astimezone(UTC).replace(microsecond=0)
