"""A simulated internet: replaces urllib.request.urlopen, the single point ALL real I/O passes through, so the WHOLE system runs unmodified (real net.get, real parsers,
real company refresh, real quote consensus) against realistic responses - and against realistically broken ones.
Earlier tests injected fakes at function level, which silently switched off the production-only code (e.g. Finnhub/SEC company refresh)."""
import datetime as dt
import io
import json
import random
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

UTC = dt.timezone.utc
MODES = ("ok", "empty", "nulls", "strings", "error429", "error500", "error403", "html", "truncated", "list")


class _Resp:
    def __init__(self, body: bytes, status=200, headers=None):
        self._b, self.status, self.headers = io.BytesIO(body), status, _H(headers or {})

    def read(self, n=-1):
        return self._b.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _H(dict):
    def items(self):
        return list(super().items())


class Internet:
    def __init__(self, now, companies, headlines, seed=0, degrade=0.0, degrade_quotes=False, weird=False):
        """companies: {ticker: {"name":..., "cik":..., "price":...}}; headlines: [(title, summary)]"""
        self.now, self.co, self.headlines, self.rng = now, companies, headlines, random.Random(seed)
        self.degrade, self.degrade_quotes, self.weird = degrade, degrade_quotes, weird
        self.requests = []
        self.modes_used = {}

    # ------------------------------------------------------------------ plumbing
    def install(self):
        self._orig = urllib.request.urlopen
        urllib.request.urlopen = self._urlopen
        return self

    def uninstall(self):
        urllib.request.urlopen = self._orig

    def __enter__(self):
        return self.install()

    def __exit__(self, *a):
        self.uninstall()

    def _urlopen(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.requests.append(url)
        host, path, qs = urllib.parse.urlparse(url).netloc, urllib.parse.urlparse(url).path, urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        is_quote = any(h in host for h in ("twelvedata", "alpaca")) or ("finnhub.io" in host and path.endswith("/quote")) or ("finance.yahoo.com" in host and qs.get("range", [""])[0] == "1d")
        mode = "ok"
        if self.degrade and (self.degrade_quotes or not is_quote) and self.rng.random() < self.degrade:
            mode = self.rng.choice(MODES[1:])
        self.modes_used[mode] = self.modes_used.get(mode, 0) + 1
        if mode.startswith("error"):
            code = int(mode[5:])
            raise urllib.error.HTTPError(url, code, "simulated", {}, io.BytesIO(b""))
        body, ctype = self._route(url, host, path, qs, req)
        if mode == "html":
            body = b"<html><body><h1>Service temporarily unavailable</h1></body></html>"
        elif mode == "truncated":
            body = body[: max(1, len(body) // 2)]
        elif mode == "empty":
            body = b"{}" if ctype == "json" else b""
        elif mode == "list":
            body = b"[]"
        elif mode in ("nulls", "strings") and ctype == "json":
            try:
                body = json.dumps(self._mangle(json.loads(body), mode)).encode()
            except ValueError:
                pass
        return _Resp(body, 200, {"Content-Type": "application/json" if ctype == "json" else "application/xml"})

    def _mangle(self, x, mode):
        if isinstance(x, dict):
            return {k: self._mangle(v, mode) for k, v in x.items()}
        if isinstance(x, list):
            return [self._mangle(v, mode) for v in x]
        if self.rng.random() < 0.35:
            if mode == "nulls":
                return None
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                return str(x)
            if isinstance(x, str):
                return self.rng.choice(["N/A", "", "--", x])
        return x

    # ------------------------------------------------------------------ routes
    def _stamp(self, age=5):
        return int(self.now.timestamp() - age)

    ETFS = {"SPY": 450.0, "IWM": 200.0, "HYG": 77.0, "LQD": 108.0}              # real providers quote index ETFs too (the checklist proves freshness on SPY)

    def _sym_price(self, sym):
        if sym in self.ETFS and sym not in self.co:
            return self.ETFS[sym]
        c = self.co.get(sym)
        if not c:
            raise urllib.error.HTTPError("x", 404, "unknown symbol", {}, io.BytesIO(b""))
        return c["price"]

    def _route(self, url, host, path, qs, req):
        J = lambda o: (json.dumps(o).encode(), "json")  # noqa: E731
        sym = (qs.get("symbol") or [""])[0]
        if "twelvedata" in host:
            p = self._sym_price(sym)
            return J({"symbol": sym, "name": self.co.get(sym, {}).get("name", sym), "exchange": "NASDAQ", "currency": "USD", "datetime": self.now.strftime("%Y-%m-%d"), "timestamp": self._stamp(7),
                      "open": "%.2f" % (p * .99), "high": "%.2f" % (p * 1.01), "low": "%.2f" % (p * .98), "close": "%.4f" % p, "volume": "1234567", "previous_close": "%.2f" % (p * .995),
                      "is_market_open": True})
        if "finnhub.io" in host:
            if path.endswith("/quote"):
                if sym not in self.co and sym not in self.ETFS:
                    return J({"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0})
                p = self._sym_price(sym)
                return J({"c": p, "d": 0.5, "dp": 0.4, "h": p * 1.01, "l": p * .98, "o": p * .99, "pc": p * .995, "t": self._stamp(4)})
            if path.endswith("/calendar/earnings"):
                day = (self.now + dt.timedelta(days=1)).strftime("%Y-%m-%d")
                return J({"earningsCalendar": [{"date": day, "hour": "amc", "symbol": list(self.co)[0], "epsEstimate": 1.1}, {"date": day, "hour": "bmo", "symbol": list(self.co)[1]}]})
            if path.endswith("/stock/profile2"):
                if sym not in self.co:
                    return J({})
                c = self.co[sym]
                return J({"country": "US", "currency": "USD", "exchange": "NASDAQ NMS - GLOBAL MARKET", "finnhubIndustry": c.get("industry", "Technology"), "ipo": "1999-01-22",
                          "logo": "https://x/logo.png", "marketCapitalization": c.get("mcap", 25000.5), "name": c["name"], "phone": "1", "shareOutstanding": 410.2, "ticker": sym, "weburl": "https://x"})
            if path.endswith("/stock/metric"):
                if sym not in self.co:
                    return J({})
                return J({"metric": {"10DayAverageTradingVolume": 12.3, "52WeekHigh": 150.2, "52WeekLow": 80.1, "beta": 1.21, "peTTM": 28.4, "psTTM": 6.1, "roeTTM": 21.5, "roaTTM": 9.1,
                                     "grossMarginTTM": 55.2, "operatingMarginTTM": 30.1, "netProfitMarginTTM": 24.2, "currentRatioQuarterly": 1.8, "totalDebt/totalEquityQuarterly": 0.4,
                                     "revenueGrowthTTMYoy": 18.2, "epsGrowthTTMYoy": 22.0, "dividendYieldIndicatedAnnual": None}, "metricType": "all", "series": {"annual": {}, "quarterly": {}}, "symbol": sym})
            if path.endswith("/stock/earnings"):
                return J([{"actual": 1.2, "estimate": 1.1, "period": "2026-09-30", "quarter": 3, "surprise": 0.1, "surprisePercent": 9.1, "symbol": sym, "year": 2026},
                          {"actual": 1.0, "estimate": 1.05, "period": "2026-06-30", "quarter": 2, "surprise": -0.05, "surprisePercent": -4.8, "symbol": sym, "year": 2026}])
            if "insider-transactions" in path:
                return J({"data": [{"name": "J Doe", "share": 1000, "change": -500, "filingDate": "2026-10-20", "transactionDate": "2026-10-18", "transactionCode": "S", "transactionPrice": 100.5}], "symbol": sym})
            if path.endswith("/company-news"):
                return J([{"category": "company", "datetime": self._stamp(900), "headline": "%s shares move on analyst note" % sym, "id": 1, "image": "", "related": sym, "source": "Finnhub", "summary": "x", "url": "https://x/1"}])
            if path.endswith("/news"):
                return J([{"category": "general", "datetime": self._stamp(600 + i * 60), "headline": h, "id": i, "image": "", "related": "", "source": "Wire", "summary": s, "url": "https://x/%d" % i} for i, (h, s) in enumerate(self.headlines[:15])])
        if "alpaca" in host:
            s = path.split("/stocks/")[1].split("/")[0]
            p = self._sym_price(s)
            t = dt.datetime.fromtimestamp(self._stamp(9), UTC).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
            return J({"latestTrade": {"t": t, "x": "V", "p": p, "s": 100, "c": ["@"], "i": 1, "z": "C"}, "latestQuote": {"t": t, "ax": "V", "ap": p + .02, "as": 1, "bx": "V", "bp": p - .02, "bs": 1, "c": ["R"], "z": "C"},
                      "minuteBar": {}, "dailyBar": {}, "prevDailyBar": {}})
        if "alphavantage" in host:
            p = self._sym_price(sym)
            return J({"Global Quote": {"01. symbol": sym, "05. price": "%.4f" % p, "07. latest trading day": self.now.strftime("%Y-%m-%d")}})
        if "polygon" in host or "massive" in host or "tiingo" in host or "eodhd" in host:
            raise urllib.error.HTTPError(url, 403, "plan", {}, io.BytesIO(b""))
        if "finance.yahoo.com" in host:
            s = urllib.parse.unquote(path.split("/chart/")[1])
            base = {"SPY": 450.0, "^VIX": 15.0, "^TNX": 4.2, "^IRX": 4.0, "HYG": 77.0, "LQD": 108.0, "DX-Y.NYB": 100.0, "CL=F": 70.0, "GC=F": 2000.0, "IWM": 200.0}.get(s)
            rng = qs.get("range", ["1y"])[0]
            if rng == "1d" and qs.get("interval", [""])[0] == "1m":                       # extended-hours request: 1-minute bars ending seconds ago, with nulls for quiet minutes
                px = self.co[s]["price"] if s in self.co else self.ETFS.get(s, {"^VIX": 15.0}.get(s))
                if px is None:
                    raise urllib.error.HTTPError(url, 404, "No data found", {}, io.BytesIO(b""))
                ts = [int(self.now.timestamp()) - 60 * (4 - i) - 20 for i in range(5)]
                cl = [px * (1 + .001 * i) for i in range(5)]
                cl[-1] = None
                return J({"chart": {"result": [{"meta": {"symbol": s, "chartPreviousClose": px * .99}, "timestamp": ts, "indicators": {"quote": [{"close": cl}]}}], "error": None}})
            n = 1 if rng == "1d" else (5 if rng == "5d" else 252)
            px = (self.co[s]["price"] if s in self.co else base)
            if px is None:
                raise urllib.error.HTTPError(url, 404, "No data found, symbol may be delisted", {}, io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found"}}}'))
            r = random.Random(sum(map(ord, s)))
            closes, ts, vols = [], [], []
            for i in range(n):
                closes.append(px * (1 + r.gauss(0, .01)) if i < n - 1 else px)
                ts.append(int((self.now - dt.timedelta(days=n - i)).timestamp()))
                vols.append(r.randint(500_000, 9_000_000))
            if n > 20:
                closes[-3] = None                     # real Yahoo data contains nulls for missing sessions
                vols[-3] = None
            return J({"chart": {"result": [{"meta": {"currency": "USD", "symbol": s, "exchangeName": "NMS", "instrumentType": "EQUITY", "regularMarketPrice": px, "regularMarketTime": self._stamp(3),
                                                    "gmtoffset": -14400, "timezone": "EDT", "chartPreviousClose": px * .99},
                                           "timestamp": ts, "indicators": {"quote": [{"open": closes, "high": [c * 1.01 if c else None for c in closes], "low": [c * .99 if c else None for c in closes],
                                                                                         "close": closes, "volume": vols}], "adjclose": [{"adjclose": closes}]}}], "error": None}})
        if "data.sec.gov" in host and "companyfacts" in path:
            cik = int(path.split("CIK")[1].split(".")[0])
            if not any(c.get("cik") == cik for c in self.co.values()):
                raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO(b""))
            unit = lambda v: {"units": {"USD": [{"end": "2026-06-30", "val": v, "accn": "0001-26-000001", "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-08-01", "frame": "CY2026Q2"}]}}  # noqa: E731
            return J({"cik": cik, "entityName": "X", "facts": {"us-gaap": {"Revenues": unit(61_000_000_000), "CashAndCashEquivalentsAtCarryingValue": unit(9_000_000_000), "LongTermDebt": unit(7_000_000_000),
                                                                           "OperatingIncomeLoss": unit(30_000_000_000)}}})
        if "data.sec.gov" in host or "sec.gov" in host and "files/company_tickers" in path:
            return J({"fields": ["cik", "name", "ticker", "exchange"], "data": [[c["cik"], c["name"], t, "Nasdaq"] for t, c in self.co.items()]})
        if "sec.gov" in host and "browse-edgar" in path:
            ents = "".join("<entry><title>8-K - %s (%010d) (Filer)</title><link href=\"https://www.sec.gov/x/%d\"/><updated>%s</updated><summary>Item 2.02 Results of Operations</summary><id>urn:%d</id></entry>"
                           % (escape(c["name"].upper()), c["cik"], i, self.now.strftime("%Y-%m-%dT%H:%M:%S-04:00"), i) for i, c in enumerate(list(self.co.values())[:6]))
            return ("<feed xmlns=\"http://www.w3.org/2005/Atom\">%s</feed>" % ents).encode(), "xml"
        if "gdeltproject" in host:
            return J({"articles": [{"url": "https://x/g%d" % i, "title": h, "seendate": self.now.strftime("%Y%m%dT%H%M%SZ"), "domain": "example.com", "language": "English", "sourcecountry": "United States"} for i, (h, s) in enumerate(self.headlines[:12])]})
        if "api.fda.gov" in host:
            return J({"meta": {}, "results": [{"report_date": self.now.strftime("%Y%m%d"), "recalling_firm": "Acme Pharma", "product_description": "Tablets 10mg", "reason_for_recall": "Mislabeling", "classification": "Class II", "status": "Ongoing"}]})
        if "usaspending" in host:
            return J({"results": [{"Award ID": "W1", "Recipient Name": "LOCKHEED MARTIN CORP", "Start Date": self.now.strftime("%Y-%m-%d"), "Award Amount": 2_400_000_000.0, "Awarding Agency": "Department of Defense", "Description": "missile systems"}], "page_metadata": {}})
        if "federalregister.gov" in host:
            return J({"results": [{"title": "Rule on semiconductors", "abstract": "Export controls", "html_url": "https://x/fr", "publication_date": self.now.strftime("%Y-%m-%d"), "document_type": "Rule", "agencies": [{"name": "Commerce"}]}]})
        # everything else is an RSS feed: a rotating window of headlines, newest first
        k = int(self.now.timestamp() // 300)
        items = [self.headlines[(k + i) % len(self.headlines)] for i in range(10)]
        if self.weird:                                      # malformed items that real feeds really do contain
            items += [("", ""), ("\U0001F600" * 40 + " stock surges", ""), ("A" * 30000, "B" * 30000), ("Fed cuts rates ]]> <b>&nbsp;</b> & more", "<script>x</script>"), ("   ", None),
                      ("NVIDIA " * 200, "beats estimates " * 500), ("Q3 results\x00with nul", ""), ("\u202e reversed text \u202c Apple raises outlook", "")]
        body = "".join("<item><title>%s</title><link>https://x/%d</link><description>%s</description><pubDate>%s</pubDate><guid>%d-%d</guid></item>"
                       % (escape(t), i, escape(s), ("not a date" if (self.weird and i % 7 == 3) else ("" if (self.weird and i % 7 == 5) else (self.now - dt.timedelta(minutes=10 + i * 7)).strftime("%a, %d %b %Y %H:%M:%S +0000"))), k, i) for i, (t, s) in enumerate(items))
        return ("<?xml version=\"1.0\"?><rss version=\"2.0\"><channel><title>f</title>%s</channel></rss>" % body).encode(), "xml"
