"""Article reader: get the BODY of a news article, understand it with ``lens`` (no LLM), and fold what it says into the headline's reading.

Safety first, because this fetches URLs that came out of news feeds:
  * only http(s) on ports 80/443; the host must resolve to PUBLIC addresses only (no loopback / private / link-local), checked again at EVERY redirect hop;
  * at most 1.5 MB, a short timeout, no cookies, an honest User-Agent with your contact address;
  * a site that keeps refusing us (403 / paywall / timeouts) is left alone for a day: we never hammer anyone;
  * a hard budget per cycle (articles and seconds): reading can never slow trading.
Nothing is stored except the extracted FACTS (each with one short source sentence), never the article text.

Modes (Settings -> Article reader): off | shadow (record what the article says, change nothing) | guard (the article may CANCEL or WEAKEN a candidate: it can never create or enlarge one) |
full (guard + the article may also confirm, strengthen, or rescue a candidate whose headline was too vague to act on)."""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Tuple

from . import db, lens, net
from .timeutil import iso, parse_ts

MAX_BYTES = 1_500_000
MIN_CHARS = 300                       # shorter than this is a paywall stub / error page, not an article
NEVER_FETCH = ("youtube.com", "youtu.be", "x.com", "twitter.com", "facebook.com", "instagram.com", "tiktok.com", "linkedin.com", "t.me", "reddit.com", "github.com", "news.ycombinator.com", "sec.gov", "federalregister.gov", "usaspending.gov")
BLOCK_AFTER_FAILURES = 3
BLOCK_HOURS = 24


class Blocked(Exception):
    pass


# ----------------------------------------------------------------------------- safe fetching

def check_url(url: str, resolver: Callable = socket.getaddrinfo) -> str:
    """Raise Blocked unless the URL is a plain public web address. Returns the lower-cased host."""
    u = urllib.parse.urlsplit(url or "")
    if u.scheme not in ("http", "https"):
        raise Blocked("not an http(s) URL")
    if u.username or u.password:
        raise Blocked("credentials in URL")
    host = (u.hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".lan", ".home", ".localdomain")):
        raise Blocked("not a public host")
    try:
        port = u.port
    except ValueError:
        raise Blocked("bad port")
    if port not in (None, 80, 443):
        raise Blocked("unusual port")
    try:
        ipaddress.ip_address(host)
        raise Blocked("IP-address hosts are not fetched")
    except ValueError:
        pass
    try:
        infos = resolver(host, port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        raise Blocked("host does not resolve")
    addrs = {i[4][0] for i in infos}
    if not addrs:
        raise Blocked("host does not resolve")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global:
            raise Blocked("host resolves to a non-public address")
    return host


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 4

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)                                         # every hop is checked, not just the first
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_html(url: str, timeout: float = 8.0, max_bytes: int = MAX_BYTES, opener: Optional[urllib.request.OpenerDirector] = None) -> Tuple[str, str]:
    """-> (html text, final URL). Raises Blocked / net.FetchError."""
    check_url(url)
    op = opener or urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": net.UA % net._CONTACT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", "Accept-Language": "en"})
    try:
        with op.open(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype and not any(x in ctype for x in ("text/html", "application/xhtml", "text/plain")):
                raise net.FetchError("not a web page (%s)" % ctype.split(";")[0], None)
            body = r.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise net.FetchError("page too large", None)
            m = re.search(r"charset=([\w-]+)", ctype)
            enc = m.group(1) if m else (re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I) or [None, b"utf-8"])[1]
            enc = enc.decode("ascii", "ignore") if isinstance(enc, bytes) else enc
            try:
                text = body.decode(enc or "utf-8", "replace")
            except LookupError:
                text = body.decode("utf-8", "replace")
            return text, r.geturl()
    except urllib.error.HTTPError as e:
        raise net.FetchError("HTTP %s" % e.code, e.code)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), Blocked):
            raise e.reason
        raise net.FetchError("URL error: %s" % e.reason)
    except (TimeoutError, OSError) as e:
        raise net.FetchError("%s: %s" % (type(e).__name__, e))


# ----------------------------------------------------------------------------- robots.txt: the publisher's own rules for bots

_ROBOTS: Dict[str, Tuple[float, urllib.robotparser.RobotFileParser, float]] = {}
ROBOTS_TTL = 24 * 3600


def robots_allows(url: str, fetch: Optional[Callable] = None, clock: Callable[[], float] = time.time) -> bool:
    """May a bot called PaperAgent fetch this URL? Reads the site's robots.txt (remembered for 24 h). No robots.txt (a 4xx) means everything is allowed; if the file cannot be read for any other reason
    we stay away from that site for an hour rather than guess."""
    u = urllib.parse.urlsplit(url)
    origin = "%s://%s" % (u.scheme, u.netloc)
    ent = _ROBOTS.get(origin)
    if not ent or clock() - ent[0] > ent[2]:
        rp, ttl = urllib.robotparser.RobotFileParser(), ROBOTS_TTL
        try:
            txt, _ = (fetch or fetch_html)(origin + "/robots.txt", 5.0)
            rp.parse(txt.splitlines())
        except net.FetchError as e:
            if e.code is not None and 400 <= e.code < 500:
                rp.parse([])
            else:
                rp.parse(["User-agent: *", "Disallow: /"])
                ttl = 3600
        except Exception:  # noqa: BLE001 - Blocked, DNS, anything else: stay away
            rp.parse(["User-agent: *", "Disallow: /"])
            ttl = 3600
        ent = _ROBOTS[origin] = (clock(), rp, ttl)
    return ent[1].can_fetch("PaperAgent", url)


# ----------------------------------------------------------------------------- readable-text extraction (stdlib only)

_SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "form", "nav", "footer", "aside", "button", "select", "template", "figcaption", "canvas", "dialog"}
_SKIP_HINT = re.compile(r"comment|related|sidebar|promo|newsletter|footer|share|social|recirc|subscribe|cookie|advert|outbrain|taboola|breadcrumb|\bmenu\b|modal|popup|author-?bio|trending|most-?read|read-?more|paywall|signup|sign-up|disclosure|banner|toolbar|ticker-?bar|stock-?quote|widget", re.I)
_VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}
_BOILER = re.compile(r"^(?:subscribe|sign up|read more|related:|related coverage|advertisement|click here|follow us|copyright|©|all rights reserved|image caption|photo:|getty images|reuters file photo|"
                     r"\(reuters\) - $|story continues|this article was|our standards|want more|get the latest|download the app|share this|listen to this|more from|sponsored)", re.I)
_PAYWALL = re.compile(r"subscribe to (?:continue|read|unlock)|to read the full (?:article|story)|enable javascript|access denied|captcha|are you a robot|verify you are human|create a free account to|sign in to (?:read|continue)|already a subscriber|this content is (?:for|available to) subscribers", re.I)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: List[Tuple[str, bool, str]] = []              # (tag, skipping, container-key)
        self.cur_p: Optional[List[str]] = None
        self.cur_tag = ""
        self.cur_links = 0
        self.cur_total = 0
        self.paras: List[Tuple[str, str, bool]] = []              # (text, container key, inside <article>)
        self.heads: List[str] = []
        self.ld: List[str] = []
        self._in_ld = False
        self._ld_buf: List[str] = []
        self.meta: Dict[str, str] = {}
        self.title_text = ""
        self._in_title = False
        self._in_a = False

    @property
    def skipping(self) -> bool:
        return any(s for _, s, _ in self.stack)

    def _key(self) -> str:
        return ">".join(k for _, _, k in self.stack[-3:] if k)

    _BLOCKS = {"p", "li", "blockquote", "h1", "h2", "h3", "h4", "div", "section", "article", "ul", "ol", "table", "tr", "td", "main", "header", "footer", "aside", "nav", "figure"}

    def _finish_paragraph(self):
        """HTML often leaves <p> unclosed (the next block implicitly ends it): finish the paragraph being read so its text is not lost."""
        if self.cur_p is None:
            return
        tag = self.cur_tag
        text = re.sub(r"\s+", " ", "".join(self.cur_p)).strip()
        link_share = (self.cur_links / self.cur_total) if self.cur_total else 0.0
        if text and not self.skipping:
            if tag in ("h1", "h2", "h3"):
                self.heads.append(text)
            elif len(text) >= (60 if tag == "li" else 40) and link_share < 0.5 and not _BOILER.match(text):
                self.paras.append((text, self._key(), any(t == "article" for t, _, _ in self.stack)))
        self.cur_p = None

    def handle_starttag(self, tag, attrs):
        if tag in self._BLOCKS:
            self._finish_paragraph()
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            k = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if k and a.get("content"):
                self.meta.setdefault(k, a["content"])
            return
        if tag in _VOID:
            return
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_ld, self._ld_buf = True, []
        hint = (a.get("class", "") + " " + a.get("id", "")).strip()
        skip = tag in _SKIP_TAGS or bool(hint and _SKIP_HINT.search(hint)) or a.get("hidden") is not None or a.get("aria-hidden") == "true"
        key = ("%s.%s" % (tag, re.sub(r"\s+", ".", hint)[:40])) if tag in ("article", "main", "section", "div") else ""
        self.stack.append((tag, skip, key))
        if tag == "title":
            self._in_title = True
        if tag == "a":
            self._in_a = True
        if tag in ("p", "li", "blockquote", "h1", "h2", "h3") and not self.skipping:
            self.cur_p, self.cur_tag, self.cur_links, self.cur_total = [], tag, 0, 0

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.ld.append("".join(self._ld_buf))
        if tag == "title":
            self._in_title = False
        if tag == "a":
            self._in_a = False
        if tag in self._BLOCKS:
            self._finish_paragraph()
        for i in range(len(self.stack) - 1, -1, -1):             # close back to the matching open tag (tolerates sloppy HTML)
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._in_ld:
            self._ld_buf.append(data)
            return
        if self._in_title:
            self.title_text += data
        if self.cur_p is not None and not self.skipping:
            self.cur_p.append(data)
            self.cur_total += len(data.strip())
            if self._in_a:
                self.cur_links += len(data.strip())


def _ld_article(blobs: List[str]) -> Tuple[str, Optional[str]]:
    body, pub = "", None
    def walk(o):
        nonlocal body, pub
        if isinstance(o, dict):
            t = o.get("@type")
            ts = t if isinstance(t, list) else [t]
            if any(x in ("NewsArticle", "Article", "ReportageNewsArticle", "BlogPosting") for x in ts):
                ab = o.get("articleBody")
                if isinstance(ab, str) and len(ab) > len(body):
                    body = ab
                if not pub and isinstance(o.get("datePublished"), str):
                    pub = o["datePublished"]
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for b in blobs:
        try:
            walk(json.loads(b))
        except ValueError:
            continue
    return body, pub


def extract(html: str) -> Dict:
    """-> {paragraphs, title, published, site, via, chars}. ``via`` says where the text came from: 'article' (the main content block) or 'ld+json' (the page's own structured copy)."""
    p = _Page()
    try:
        p.feed(html or "")
        p.close()
        p._finish_paragraph()
    except Exception:  # noqa: BLE001 - badly broken HTML still yields what was read so far
        pass
    paras = p.paras
    chosen: List[str] = []
    via = "article"
    inside = [t for t, k, ia in paras if ia]
    if len(inside) >= 2:
        chosen = inside
    elif paras:
        by: Dict[str, int] = {}
        for t, k, _ in paras:
            by[k] = by.get(k, 0) + len(t)
        best = max(by, key=by.get)
        chosen = [t for t, k, _ in paras if k == best]
        if sum(len(t) for t in chosen) < 0.5 * sum(len(t) for t, _, _ in paras):
            chosen = [t for t, _, _ in paras]
    ld_body, ld_pub = _ld_article(p.ld)
    if ld_body and len(ld_body) > 1.4 * sum(len(t) for t in chosen):
        chosen = [x.strip() for x in re.split(r"\n+|(?<=[.!?])\s{2,}", ld_body) if len(x.strip()) >= 40] or [ld_body]
        via = "ld+json"
    seen, out = set(), []
    for t in chosen:
        k = t[:80].lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return {"paragraphs": out[:60], "title": (p.meta.get("og:title") or (p.heads[0] if p.heads else "") or p.title_text).strip(), "published": p.meta.get("article:published_time") or p.meta.get("datepublished") or ld_pub,
            "site": p.meta.get("og:site_name", ""), "via": via, "chars": sum(len(t) for t in out)}


# ----------------------------------------------------------------------------- budgeted reading with memory

def _domain(url: str) -> str:
    h = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def skip_reason(conn, url: str, now) -> Optional[str]:
    if not url:
        return "no URL"
    d = _domain(url)
    if not d:
        return "no host"
    if any(d == x or d.endswith("." + x) for x in NEVER_FETCH):
        return "site not worth fetching"
    row = conn.execute("SELECT blocked_until FROM article_domains WHERE domain=?", (d,)).fetchone()
    if row and row["blocked_until"] and (parse_ts(row["blocked_until"]) or now) > now:
        return "site is being left alone until %s" % row["blocked_until"][:16].replace("T", " ")
    return None


def _note(conn, domain: str, ok: bool, status: str, now) -> None:
    conn.execute("INSERT OR IGNORE INTO article_domains(domain,ok,fails,updated) VALUES(?,0,0,?)", (domain, iso(now)))
    if ok:
        conn.execute("UPDATE article_domains SET ok=ok+1,fails=0,blocked_until=NULL,last_status=?,updated=? WHERE domain=?", (status, iso(now), domain))
    else:
        conn.execute("UPDATE article_domains SET fails=fails+1,last_status=?,updated=?,blocked_until=CASE WHEN fails+1>=? THEN ? ELSE blocked_until END WHERE domain=?",
                     (status, iso(now), BLOCK_AFTER_FAILURES, iso(now + dt.timedelta(hours=BLOCK_HOURS)), domain))


def read_one(conn, uid: str, title: str, summary: str, url: str, published: Optional[dt.datetime], now, fetcher: Optional[Callable], cfg: dict,
             names_by_ticker: Optional[Dict[str, List[str]]] = None, market_cap: Optional[float] = None, fetch_page: bool = True, robots: Optional[Callable] = None) -> Dict:
    """Read one article (the page if allowed and reachable, otherwise the feed's own snippet), analyse it, remember the result. Never raises."""
    rc = cfg.get("reader") or {}
    paras: List[str] = []
    via, status, http = "snippet", "SNIPPET", None
    d = _domain(url)
    why = skip_reason(conn, url, now) if fetch_page else "page fetch not requested"
    check_robots = robots if robots is not None else (robots_allows if fetcher is None else None)         # a test's fake fetcher never touches the network, so it skips this
    if why is None and check_robots is not None:
        try:
            if not check_robots(url):
                why = "the site's robots.txt asks bots not to fetch this page"
        except Exception:  # noqa: BLE001
            why = "robots.txt could not be checked"
    if why is None:
        try:
            html, _final = (fetcher or fetch_html)(url, float(rc.get("timeout_seconds", 8)))
            ex = extract(html)
            good = [p for p in ex["paragraphs"] if not _PAYWALL.search(p)]            # a wall / "subscribe" paragraph is not article text; a teaser's real lead still is
            if sum(len(p) for p in good) >= MIN_CHARS:
                paras, via, status = good, ex["via"], "OK"
                published = published or parse_ts(ex.get("published") or "")
                _note(conn, d, True, "OK", now)
            else:
                status = "PAYWALL_OR_SHORT"
                _note(conn, d, False, status, now)
        except Blocked as e:
            status = "BLOCKED: %s" % e
        except net.FetchError as e:
            http = e.code
            status = "FETCH_FAILED: %s" % str(e)[:60]
            _note(conn, d, False, status, now)
        except Exception as e:  # noqa: BLE001
            status = "ERROR: %s" % type(e).__name__
    else:
        status = "SKIPPED: %s" % why
    if not paras and summary:
        paras = [summary]
    res = lens.analyze(title, paras, published, names_by_ticker, market_cap, url=url or "") if paras else lens.analyze(title, [], published, names_by_ticker, market_cap, url=url or "")
    res["via"], res["status"], res["domain"] = ("page" if via in ("article", "ld+json") else "snippet"), status, d
    try:
        conn.execute("INSERT OR REPLACE INTO article_reads(uid,url,fetched_at,status,http,via,chars,domain,analysis_json) VALUES(?,?,?,?,?,?,?,?,?)",
                     (uid, (url or "")[:400], iso(now), status[:80], http, res["via"], res.get("chars", 0), d, json.dumps(res, default=str, sort_keys=True)))
    except Exception:  # noqa: BLE001
        pass
    return res


# ----------------------------------------------------------------------------- folding the article into the headline's reading

_HINT_TYPE = {"eps_surprise": None, "beat_generic": "earnings_beat", "miss_generic": "earnings_miss", "guidance_raise": "guidance_raise", "guidance_cut": "guidance_cut", "contract": None, "buyback": "buyback_dividend",
              "dividend_raise": "buyback_dividend", "fda_approve": "regulatory_approval", "fda_reject": "regulatory_action", "investigation": "regulatory_action", "recall": "production_disruption",
              "lawsuit": "litigation", "bankruptcy": "bankruptcy_distress", "cyber": "cyber_incident", "strike_outage": "production_disruption", "partnership": "partnership", "restatement": "regulatory_action",
              "delisting": "regulatory_action", "downgrade": "analyst_action", "upgrade": "analyst_action"}


def event_type_hint(res: Dict) -> Optional[str]:
    for f in res.get("facts", []):
        if f["w"] < 0.4 or f["kind"] == "deal":
            continue
        k = f["kind"]
        if k == "eps_surprise":
            return "earnings_beat" if f["pol"] > 0 else "earnings_miss"
        if k == "contract":
            return "contract_win" if f["pol"] > 0 else "contract_loss"
        t = _HINT_TYPE.get(k)
        if t:
            return t
    return None


def merge(a: dict, res: Dict, mode: str, tickers: List[str]) -> str:
    """Fold the article's reading into the headline analysis ``a`` (in place). Returns a one-line description of what changed ("" = nothing). Never raises on odd input."""
    if mode in ("off", "shadow") or not res:
        return ""
    notes: List[str] = []
    def num(x):
        try:
            return float(x or 0.0)
        except (TypeError, ValueError):
            return 0.0
    h, L, conf = num(a.get("direction")), num(res.get("direction")), num(res.get("confidence"))
    page = res.get("via") == "page" and (res.get("paragraphs") or 0) >= 3
    # 1. what KIND of text is it
    if page and res.get("type") in ("opinion", "promo", "transcript") and a.get("event_type") not in ("opinion_piece", "price_recap", "sec_filing"):
        a["event_type"], a["direction"] = "opinion_piece", 0.0
        notes.append("the full text is %s, not news" % {"opinion": "an opinion / list article", "promo": "sponsored content", "transcript": "a call transcript"}[res["type"]])
        return "; ".join(notes)
    # 2. is it old news
    if res.get("stale") and conf >= 0.3 and page:
        a["article_stale"] = res.get("stale_why") or "the article says this happened days ago"
        notes.append("old news: " + a["article_stale"])
    # 3. does the body contradict the headline
    if h and L and (h > 0) != (L > 0) and abs(h) >= 0.25 and abs(L) >= 0.30 and conf >= 0.45 and not res.get("mixed"):
        a["article_contradicts"] = "the headline reads %s but the article's facts read %s" % ("good" if h > 0 else "bad", "bad" if h > 0 else "good")
        a["direction"] = 0.0
        notes.append(a["article_contradicts"])
        return "; ".join(notes)
    # 4. mixed results: a beat AND a cut in the same article is not a clean signal
    if res.get("mixed") and abs(h) >= 0.25:
        a["direction"] = h * 0.6
        notes.append("mixed signals in the article: direction x0.6")
    # 5. which companies is the article actually about (guards against a company that is only mentioned in passing)
    subj = res.get("subject") or {}
    if page and subj and len(subj) >= 2:
        best_t, best_s = max(subj.items(), key=lambda kv: kv[1])
        off = [t for t in tickers if t in subj and subj[t] < 1.0 and best_s >= 4.0 and t != best_t]
        if off:
            a["article_not_about"] = sorted(set(a.get("article_not_about", []) + off))
            notes.append("the article is about %s, only mentions %s" % (best_t, ", ".join(off)))
    if mode != "full":
        return "; ".join(notes)
    # ---- full mode: the article may also confirm, strengthen or rescue
    ok_type = res.get("type") in ("report", "release")
    if page and ok_type and conf >= 0.55:
        if abs(h) >= 0.25 and (h > 0) == (L > 0) and abs(L) >= 0.3:
            new = max(-1.0, min(1.0, 0.45 * h + 0.55 * L))
            a["direction"] = new
            a["magnitude"] = max(num(a.get("magnitude")), min(1.0, num(res.get("magnitude"))))
            notes.append("the article confirms it (direction %+.2f -> %+.2f)" % (h, new))
        elif abs(h) < 0.25 and abs(L) >= 0.45:
            a["direction"] = L * 0.8
            a["magnitude"] = max(num(a.get("magnitude")), min(1.0, num(res.get("magnitude"))), 0.35)
            hint = event_type_hint(res)
            if hint and a.get("event_type") == "general_news":
                a["event_type"] = hint
            notes.append("the headline was vague; the article's numbers read %s (%+.2f)" % ("good" if L > 0 else "bad", a["direction"]))
    return "; ".join(notes)


# ----------------------------------------------------------------------------- the per-cycle step

_NEWS_PRIORITY = re.compile(r"\b(?:earnings|results|quarter|guidance|outlook|forecast|acquire|acquisition|merger|contract|award|fda|approval|downgrade|upgrade|investigation|recall|bankruptcy|buyback|dividend)\b", re.I)


def prefetch_pages(urls: List[str], fetcher: Optional[Callable], workers: int, budget_s: float, timeout: float, clock: Callable[[], float] = time.monotonic):
    """Download the pages side by side (NETWORK ONLY: no database, no analysis). Returns {url: parallel.Outcome}; an outcome's value is ("robots", None) when the site asks bots to stay away,
    otherwise ("page", (html, final_url)). A real fetch asks robots.txt first, exactly as read_one does; a test's fake fetcher never does."""
    from . import parallel
    uniq = list(dict.fromkeys(u for u in urls if u))

    def job(u: str):
        def fn():
            if fetcher is None and not robots_allows(u):
                return ("robots", None)
            return ("page", (fetcher or fetch_html)(u, timeout))
        return {"key": u, "host": urllib.parse.urlsplit(u).netloc.lower(), "fn": fn}
    outs = parallel.run_jobs([job(u) for u in uniq], workers=workers, per_host=2, deadline_s=budget_s, clock=clock)
    return {u: o for u, o in zip(uniq, outs)}


def enrich(conn, universe, cfg: dict, pre: list, now, fetcher: Optional[Callable] = None, clock: Callable[[], float] = time.time) -> Dict:
    """Between reading the headlines and deciding what is a candidate: read the article behind the items worth reading, within the budget, and fold it into each item's analysis.
    ``pre`` is evidence.process_new_news' list of (news row, analysis, published, age_h, stale, matches); analyses are updated in place."""
    rc = cfg.get("reader") or {}
    mode = rc.get("mode", "guard")
    stats = {"mode": mode, "considered": 0, "read": 0, "page": 0, "snippet": 0, "changed": 0, "notes": []}
    if mode == "off":
        return stats
    cap = int(rc.get("max_per_cycle", 8))
    deadline = clock() + float(rc.get("time_budget_seconds", 25))
    cands = []
    for (n, a, pub, age_h, stale, matches) in pre:
        tick = [t for t, c, h in matches if c >= 0.6]
        if stale or not tick or a.get("scope") not in ("company", "sector") or a.get("event_type") in ("sec_filing", "price_recap", "opinion_piece"):
            continue
        if conn.execute("SELECT 1 FROM article_reads WHERE uid=?", (n["uid"],)).fetchone():
            continue
        title = n["title"] or ""
        prio = (2.0 if _NEWS_PRIORITY.search(title) else 0.0) + abs(a.get("direction", 0.0)) + (1.0 if abs(a.get("direction", 0.0)) < 0.25 and _NEWS_PRIORITY.search(title) else 0.0) + float(n["trust"] or 0.5) - age_h / 24.0
        cands.append((prio, n, a, pub, tick))
    stats["considered"] = len(cands)
    cands.sort(key=lambda x: -x[0])
    # 1. (database reads) which of the best-ranked items will actually be downloaded   2. (network only, side by side) download them   3. (analysis and database writes) one at a time, in rank order
    planned = [i for i, c in enumerate(cands) if i < cap and (c[1]["url"] or "") and skip_reason(conn, c[1]["url"] or "", now) is None]
    pages = prefetch_pages([cands[i][1]["url"] for i in planned], fetcher, int((cfg.get("runtime") or {}).get("fetch_workers", 6)), max(1.0, deadline - clock()), float(rc.get("timeout_seconds", 8)), clock)
    stats["pages_fetched"] = sum(1 for o in pages.values() if o.ok and o.value[0] == "page")
    for i, (prio, n, a, pub, tick) in enumerate(cands):
        got = pages.get(n["url"] or "") if i in planned else None
        fetch_ok = i < cap and (got is None or not got.timed_out) and (clock() < deadline or got is not None)
        use_fetcher, use_robots = fetcher, None
        if got is not None and fetch_ok:
            def use_fetcher(u, t=8, got=got):                              # the page was already downloaded: hand back exactly what the network said (or raise exactly what it raised)
                if not got.ok:
                    raise got.error
                return got.value[1]
            use_robots = (lambda u, got=got: not (got.ok and got.value[0] == "robots"))
        names = {t: _names_for(universe, t) for t in tick}
        cap_val = None
        try:
            r = conn.execute("SELECT market_cap FROM companies WHERE ticker=?", (tick[0],)).fetchone()
            cap_val = float(r["market_cap"]) if r and r["market_cap"] else None
        except Exception:  # noqa: BLE001
            cap_val = None
        try:
            res = read_one(conn, n["uid"], n["title"] or "", n["summary"] or "", n["url"] or "", pub, now, use_fetcher, cfg, names, cap_val, fetch_page=fetch_ok, robots=use_robots)
            note = merge(a, res, mode, tick)
        except Exception:  # noqa: BLE001 - one odd article must not cost the others their reading
            stats["errors"] = stats.get("errors", 0) + 1
            continue
        stats["read"] += 1
        stats["page" if res["via"] == "page" else "snippet"] += 1
        if note:
            stats["changed"] += 1
            stats["notes"].append("%s: %s" % ((n["title"] or "")[:60], note))
            try:
                conn.execute("UPDATE article_reads SET merge_note=? WHERE uid=?", (note[:300], n["uid"]))
            except Exception:  # noqa: BLE001
                pass
    return stats


def _names_for(universe, ticker: str) -> List[str]:
    """Name strings for a ticker, from the universe's own alias index (so 'Alphabet', 'Google' and 'GOOGL' all count)."""
    try:
        if not hasattr(universe, "_by_ticker"):
            bt: Dict[str, List[str]] = {}
            for entries in universe.index.values():
                for ph, tk, _s, _c in entries:
                    bt.setdefault(tk, []).append(ph)
            universe._by_ticker = bt
        names = [p for p in universe._by_ticker.get(ticker, []) if len(p) >= 4]
        nm = (universe.watch.get(ticker) or {}).get("name")
        return sorted(set(names + ([nm] if nm else []) + [ticker]), key=len, reverse=True)[:8]
    except Exception:  # noqa: BLE001
        return [ticker]
