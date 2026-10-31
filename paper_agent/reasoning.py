"""The Financial Reasoning Engine: the bot's own analyst, red team and question-answering, built from rules and data. No LLM, no network, no key, no cost.

It replaces the three jobs the LLM used to do, producing the SAME structures so everything downstream (evidence, candidates, the red-team gate, tripwires, outcome learning) is unchanged:
  analyst   make_analyst() / analyze_item()   what happened, how certain it is, who benefits and who loses, by causal order, with the supporting quotes and the contradictions found
  red team  review()                          the case AGAINST a proposed trade, with severities, a fatal-flaw test and tripwires; redteam.gate() then decides (the engine informs, the gate decides)
  questions ask()                             "why NVDA", "what if TSM guidance cut", "what could hurt us", "compare NVDA AMD", "theme ai_infrastructure", "news NVDA", "today": structured answers + plain text

How it reasons (every conclusion carries the rules that produced it in `trace`, and the verbatim text that supports it in `quotes`):
  1. READ        the deterministic event reading (event type, direction, magnitude) is the starting point; the lens adds the article's facts (earnings vs estimate, guidance, deals) and its own contradiction flag
  2. QUALIFY     is it confirmed, a plan, an unconfirmed rumour, a denial, history, or an opinion? Each changes how certain the event is
  3. ROLES       who is the subject, who is the acquirer and who the target, who sues whom: so winners and losers are not just "everyone named"
  4. PROPAGATE   along the relationship graph (suppliers, customers, competitors, peers, measured co-movers) with a decay per hop, and along macro exposures (oil, rates, gold, war...) to the groups they hit
  5. THEMES      which narratives the event supports or contradicts
  6. DOUBT       contradictions, abstention when the evidence is thin, and (for a trade) the strongest case against it
Honest limits: it understands the event types, phrases and relationships it has rules for. It does not read free text the way an LLM does: when it cannot, it says so (abstains or answers "I can't help with that") instead of guessing. Its reliability is learned per hop and per event type from outcomes, exactly as the LLM's was."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import db, facts as facts_mod, graph, lens, nlp
from .theses_seed import SEED_THESES
from .timeutil import iso, parse_ts, utcnow

ENGINE_VERSION = "R2"
TAG = "RULES-analyst"
MAG_LABEL = ((0.35, "low"), (0.65, "medium"), (9.0, "high"))
HORIZON = {"guidance_cut": "weeks", "guidance_raise": "weeks", "earnings_beat": "weeks", "earnings_miss": "weeks", "contract_win": "weeks", "contract_loss": "weeks", "product_launch": "weeks",
           "merger_acquisition": "weeks", "regulatory_action": "1-3m", "regulatory_approval": "1-3m", "litigation": "1-3m", "production_disruption": "days", "recall": "days", "cyber_incident": "days",
           "war_escalation": "days", "bankruptcy_distress": "weeks", "partnership": "weeks"}
THEMES = {t["key"]: t for t in SEED_THESES}

# ---- how a report is worded: confirmed fact, plan, unconfirmed rumour, denial
RUMOR = re.compile(r"\b(reportedly|according to (?:people|sources|a person)|people familiar|sources (?:say|said|told|close)|rumou?rs?|speculat\w+|exploring|in talks|weighing|considering|may (?:acquire|buy|sell|cut|raise|lay)|could (?:acquire|buy|bid)|eyeing|mulls?)\b", re.I)
PLAN = re.compile(r"\b(plans? to|set to|expected to|poised to|intends? to|will (?:acquire|buy|build|launch|cut|raise)|aims? to|looking to|proposes?)\b", re.I)
CONFIRMED = re.compile(r"\b(announced?|announces|reported|reports|said in a (?:filing|statement)|filed|confirmed?|completed?|agreed|signed|approved|declared|raises|raised|cuts|cut|lowers|lowered|beats?|missed|misses|posted|awarded|wins|won|launched|recalls?)\b", re.I)
DENIAL = re.compile(r"\b(denies|denied|dismiss(?:es|ed)|no plans to|not planning|rejects?|rejected|false report|unfounded|walks? back|retracts?)\b", re.I)
ACQUIRE = re.compile(r"\b(to acquire|acquires|will acquire|agrees? to (?:buy|acquire)|to buy|buys|bids? for|offers? to buy|takeover (?:bid )?(?:of|for)|to take over|merge[sd]? with)\b", re.I)
SUE = re.compile(r"\b(sues|suing|files? (?:a )?(?:lawsuit|complaint|suit) against|charges|fines|penali[sz]es|investigates|probes)\b", re.I)
UP = re.compile(r"\b(surge[sd]?|jump(?:s|ed)?|soar(?:s|ed)?|rall(?:y|ies|ied)|climb(?:s|ed)?|ris(?:e|es|en)|rose|higher|hike[sd]?|spik(?:e|es|ed)|tighten\w*|restrict\w*|ban(?:s|ned)?|curbs?)\b", re.I)
DOWN = re.compile(r"\b(fall(?:s|en)?|fell|plunge[sd]?|drop(?:s|ped)?|slump(?:s|ed)?|tumbl\w+|declin\w+|lower|cut(?:s)?|slid\w*|eas(?:e|es|ed)|lift(?:s|ed)?|slash\w*)\b", re.I)

# ---- macro exposures: data, not code (config/macro_exposures.json): each driver has CHANNELS (group, sign, confidence, mechanism, lag, horizon); channels for one group are netted
_MACRO_CACHE: Dict[str, object] = {}


def _macro_paths() -> List[str]:
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        from . import config as _cfg
        base = _cfg.CONFIG_DIR
    except Exception:  # noqa: BLE001
        base = "/etc/paper-agent"
    return [os.path.join(base, "macro_exposures.json"), os.path.join(base, "macro_exposures.default.json"), os.path.join(os.path.dirname(here), "config", "macro_exposures.json")]


def macro_drivers(path: Optional[str] = None) -> Tuple[str, List[dict]]:
    """(version, drivers with their patterns compiled). Re-read only when the file changes."""
    import os
    for p in ([path] if path else _macro_paths()):
        try:
            mt = os.stat(p).st_mtime
        except OSError:
            continue
        if _MACRO_CACHE.get("p") == p and _MACRO_CACHE.get("mt") == mt:
            return _MACRO_CACHE["v"], _MACRO_CACHE["d"]
        try:
            with open(p) as fh:
                data = json.load(fh)
            drv = []
            for d in data.get("drivers", []):
                drv.append(dict(d, rx=[re.compile(x, re.I) for x in d.get("patterns", [])]))
            _MACRO_CACHE.update({"p": p, "mt": mt, "v": str(data.get("version", "?")), "d": drv})
            return _MACRO_CACHE["v"], _MACRO_CACHE["d"]
        except (OSError, ValueError, re.error):
            continue
    return "none", []


def macro_effects(text: str, grp: Dict[str, List[str]]) -> List[dict]:
    """Which groups does an outside driver in this text reach, in which direction, and how sure are we? Channels for the same group are NETTED: if they cancel, the effect is MIXED and nothing is said."""
    out = []
    for drv in macro_drivers()[1]:
        m = next((rx.search(text) for rx in drv["rx"] if rx.search(text)), None)
        if not m:
            continue
        d = 1 if drv.get("fixed") else _word_dir(text, m.start())
        if d == 0:
            continue
        by: Dict[str, List[dict]] = {}
        for ch in drv.get("channels", []):
            by.setdefault(ch["group"], []).append(ch)
        for g, chs in by.items():
            net = sum(c["sign"] * c["confidence"] for c in chs)
            tot = sum(c["confidence"] for c in chs)
            if abs(net) < 0.15:
                out.append({"driver": drv["name"], "group": g, "mixed": True, "channels": [c["channel"] for c in chs], "tickers": grp.get(g, [])})
                continue
            top = max(chs, key=lambda c: c["confidence"])
            out.append({"driver": drv["name"], "group": g, "mixed": False, "sign": d * (1.0 if net > 0 else -1.0), "confidence": min(0.6, 0.6 * (abs(net) / max(tot, 1e-9)) * top["confidence"] / 0.8),
                        "channels": ["%s (%s%s)" % (c["channel"], "+" if c["sign"] > 0 else "-", ", lag ~%dd" % c["lag_days"] if c.get("lag_days") else "") for c in chs], "lag_days": max(c.get("lag_days", 0) for c in chs),
                        "horizon": top.get("horizon", "short"), "tickers": grp.get(g, []), "rising": d > 0})
    return out


def _clip(x, lo, hi, d=0.0):
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return d


def _label(m: float) -> str:
    for lim, name in MAG_LABEL:
        if m < lim:
            return name
    return "high"



# ---- the canonical record: what STATE the event is in, how NEW it is, how MATERIAL, how many INDEPENDENT sources, and which time horizons it acts over
STATE_RX = [("denied", DENIAL), ("rumored", RUMOR), ("rejected", re.compile(r"\b(rejects?|rejected|blocks?|blocked|vetoed|turned down)\b", re.I)), ("completed", re.compile(r"\b(completes?|completed|closes?|closed|finali[sz]es?|finali[sz]ed)\b", re.I)),
            ("approved", re.compile(r"\b(approves?|approved|clears?|cleared|green-?lights?)\b", re.I)), ("pending", re.compile(r"\b(pending|awaiting|subject to (?:regulatory )?approval)\b", re.I)),
            ("agreed", re.compile(r"\b(agrees?|agreed|signs?|signed|definitive agreement)\b", re.I)), ("proposed", re.compile(r"\b(proposes?|proposal|plans? to|offers? to|bids? for|to acquire|to buy)\b", re.I))]
NOVELTY_RX = [("correction", re.compile(r"\b(correction|corrects?|corrected|amended|amends|restates?|restated|revised|updates? (?:earlier|previous))\b", re.I)),
              ("recap", re.compile(r"\b(recap|wrap|round-?up|stocks to watch|what to know|how (?:\w+ ){0,3}(?:stocks|shares) (?:fared|moved))\b", re.I)), ("update", re.compile(r"\b(update[sd]?|follow-?up|after earlier)\b", re.I))]
ORIGIN = re.compile(r"\((Reuters|AP|Bloomberg|PRNewswire|GlobeNewswire|Business Wire|Dow Jones)\)|\b(Reuters|Associated Press|Bloomberg)\b\s*[-:\u2013\u2014]", re.I)
PRIMARY = re.compile(r"(sec\.gov|business ?wire|prnewswire|globenewswire|investor\.|ir\.|8-k)", re.I)
HORIZON_PROFILE = {"earnings_beat": {"immediate": .55, "short": .30, "medium": .12, "long": .03}, "earnings_miss": {"immediate": .55, "short": .30, "medium": .12, "long": .03},
                   "guidance_raise": {"immediate": .40, "short": .30, "medium": .22, "long": .08}, "guidance_cut": {"immediate": .40, "short": .30, "medium": .22, "long": .08},
                   "contract_win": {"immediate": .20, "short": .25, "medium": .30, "long": .25}, "contract_loss": {"immediate": .20, "short": .25, "medium": .30, "long": .25},
                   "product_launch": {"immediate": .20, "short": .30, "medium": .30, "long": .20}, "merger_acquisition": {"immediate": .45, "short": .25, "medium": .20, "long": .10},
                   "regulatory_action": {"immediate": .15, "short": .20, "medium": .30, "long": .35}, "regulatory_approval": {"immediate": .30, "short": .25, "medium": .25, "long": .20},
                   "litigation": {"immediate": .15, "short": .15, "medium": .30, "long": .40}, "production_disruption": {"immediate": .45, "short": .35, "medium": .15, "long": .05},
                   "recall": {"immediate": .45, "short": .30, "medium": .18, "long": .07}, "cyber_incident": {"immediate": .50, "short": .30, "medium": .15, "long": .05},
                   "bankruptcy_distress": {"immediate": .40, "short": .30, "medium": .20, "long": .10}, "war_escalation": {"immediate": .50, "short": .30, "medium": .15, "long": .05}}


def event_state(text: str) -> str:
    for name, rx in STATE_RX:
        if rx.search(text):
            return name
    return "reported"


def novelty_of(text: str) -> str:
    for name, rx in NOVELTY_RX:
        if rx.search(text):
            return name
    return "new"


def materiality(magnitude: float, amount, mcap, etype: str) -> str:
    ratio = (float(amount) / float(mcap)) if amount and mcap else 0.0
    if ratio >= 0.15 or (etype == "merger_acquisition" and magnitude >= 0.7):
        return "transformative"
    if magnitude >= 0.55 or ratio >= 0.03:
        return "material"
    return "moderate" if magnitude >= 0.30 else "immaterial"


def independent_sources(conn, item: dict, now) -> Tuple[int, bool]:
    """How many INDEPENDENT origins carry this story? Outlets that repeat one wire story are one source, not corroboration. -> (count, whether a primary source (a filing or the company's own release) is among them)."""
    from .skeptic import same_story
    title = item.get("title") or ""
    rows = conn.execute("SELECT uid,source,title,summary,url FROM news WHERE published>=? AND published<=? ORDER BY id DESC LIMIT 400", (iso(now - dt.timedelta(hours=36)), iso(now + dt.timedelta(minutes=1)))).fetchall()
    origins, primary = set(), False
    for r in rows:
        if r["uid"] != item.get("uid") and not same_story(title, r["title"] or "", 0.55):
            continue
        blob = "%s %s" % (r["title"] or "", (r["summary"] or "")[:300])
        m = ORIGIN.search(blob)
        origins.add((m.group(1) or m.group(2)).lower() if m else re.sub(r"[^a-z0-9]", "", (r["source"] or "").lower()) or "unknown")
        primary = primary or bool(PRIMARY.search("%s %s" % (r["source"] or "", r["url"] or "")))
    return max(1, len(origins)), primary


def ref_version(conn) -> str:
    import hashlib
    return hashlib.sha1(("%s|%s|%s" % (db.param(conn, "relations_hash") or "", macro_drivers()[0], ENGINE_VERSION)).encode()).hexdigest()[:10]


# ------------------------------------------------------------------------------------------------ small readers
def report_status(text: str) -> Tuple[str, float, List[str]]:
    """confirmed / plan / rumor / denied / unclear, and how much that changes the certainty that the event is real."""
    hits = []
    if DENIAL.search(text):
        hits.append("denial wording")
        return "denied", 0.25, hits
    if RUMOR.search(text) and not re.search(r"\b(announced?|confirmed?|filed|said in a)\b", text, re.I):
        hits.append("unconfirmed-source wording (%s)" % RUMOR.search(text).group(0).lower())
        return "rumor", 0.5, hits
    if CONFIRMED.search(text):
        hits.append("confirmed-event wording (%s)" % CONFIRMED.search(text).group(0).lower())
        return "confirmed", 0.92, hits
    if PLAN.search(text):
        hits.append("plan wording (%s)" % PLAN.search(text).group(0).lower())
        return "plan", 0.7, hits
    return "unclear", 0.65, hits


def _names(universe, t: str) -> List[str]:
    try:
        from . import reader
        return [n for n in reader._names_for(universe, t) if n]
    except Exception:  # noqa: BLE001
        return [t]


def _first_pos(text: str, names: List[str]) -> int:
    best = -1
    low = text.lower()
    for n in names:
        p = low.find(n.lower())
        if p >= 0 and (best < 0 or p < best):
            best = p
    return best


def _word_dir(text: str, around: int, span: int = 60) -> int:
    seg = text[max(0, around - span): around + span]
    u, d = bool(UP.search(seg)), bool(DOWN.search(seg))
    return 1 if u and not d else -1 if d and not u else 0


_GROUPS: Dict[str, Tuple[float, Dict[str, List[str]]]] = {}


def groups() -> Dict[str, List[str]]:
    t = time.time()
    if not _GROUPS or t - _GROUPS["g"][0] > 300:
        d = graph.load_seed()
        _GROUPS["g"] = (t, {g["name"]: [str(x).upper() for x in g.get("tickers", [])] for g in d.get("peer_groups", [])})
    return _GROUPS["g"][1]


# ------------------------------------------------------------------------------------------------ roles: who is acting on whom
def resolve_roles(text: str, tickers: List[str], universe, sign: float, event_type: str) -> Tuple[Dict[str, Tuple[float, float, str]], List[str]]:
    """{ticker: (sign, confidence, why)}: a takeover's TARGET gains and its acquirer is slightly hurt; the party SUED or fined loses and the one suing gains a little; otherwise every named company shares the event.
    Returns also the list of rules that fired."""
    out: Dict[str, Tuple[float, float, str]] = {}
    trace: List[str] = []
    pos = {t: _first_pos(text, _names(universe, t)) for t in tickers}
    m = ACQUIRE.search(text)
    if m and len(tickers) >= 2 and event_type in ("merger_acquisition", "general_news", "other"):
        cut = m.start()
        before = [t for t in tickers if 0 <= pos[t] < cut]
        after = [t for t in tickers if pos[t] > cut]
        if before and after:
            trace.append("roles: '%s' marks %s as acquirer and %s as target" % (m.group(0).lower(), ",".join(before), ",".join(after)))
            for t in after:
                out[t] = (1.0, 0.9, "takeover target: the premium goes to its shareholders")
            for t in before:
                out[t] = (-1.0, 0.4, "acquirer: pays a premium and takes on integration risk")
            return out, trace
    m = SUE.search(text)
    if m and len(tickers) >= 2:
        cut = m.start()
        before = [t for t in tickers if 0 <= pos[t] < cut]
        after = [t for t in tickers if pos[t] > cut]
        if before and after:
            trace.append("roles: '%s' marks %s as the aggressor and %s as the target" % (m.group(0).lower(), ",".join(before), ",".join(after)))
            for t in after:
                out[t] = (-1.0, 0.85, "the party being sued, fined or investigated")
            for t in before:
                out[t] = (1.0, 0.35, "the party bringing the action")
            return out, trace
    n = max(1, len(tickers))
    for t in tickers:
        out[t] = (sign, (0.9 if n == 1 else max(0.45, 0.9 / math.sqrt(n))), "named in the event")
    if n > 1:
        trace.append("roles: no acquirer/target or plaintiff/defendant pattern; all %d named companies share the event, each with confidence reduced for the crowd" % n)
    return out, trace


# ------------------------------------------------------------------------------------------------ the analyst
def analyze_item(conn, cfg: dict, universe, item: dict, now=None) -> Optional[dict]:
    """item = {title, summary, analysis (the deterministic reading), tickers}. Returns the structured analysis (the schema event_intel.apply expects) or None when the evidence is too thin to say anything."""
    now = now or utcnow()
    title, summary = item.get("title") or "", item.get("summary") or ""
    text = (title + ". " + summary).strip()
    a = item.get("analysis") or {}
    tickers = [t for t in (item.get("tickers") or []) if universe is None or t in universe]
    etype = a.get("event_type") or "general_news"
    trace: List[str] = ["engine %s" % ENGINE_VERSION]

    # 1. READ ------------------------------------------------------------------------------------------------
    lz = lens.analyze(title, [summary] if summary else [], None, None, None)
    adir, ldir = float(a.get("direction") or 0.0), float(lz.get("direction") or 0.0)
    direction = adir if abs(adir) >= 0.15 else ldir
    magnitude = float(a.get("magnitude") or lz.get("magnitude") or 0.0)
    scope = a.get("scope") or "company"
    contradictions: List[str] = []
    if lz.get("mixed"):
        contradictions.append("the text carries both good and bad facts (%s)" % "; ".join(f["kind"] for f in (lz.get("facts") or [])[:4]))
    try:
        td, sd = float(nlp.analyze(title, "").get("direction", 0)), float(nlp.analyze("", summary).get("direction", 0)) if summary else 0.0
        if td * sd < 0 and abs(td) >= 0.3 and abs(sd) >= 0.3:
            contradictions.append("the headline reads %s but the body reads %s" % ("positive" if td > 0 else "negative", "positive" if sd > 0 else "negative"))
    except Exception:  # noqa: BLE001
        pass
    if adir * ldir < 0 and abs(adir) >= 0.3 and abs(ldir) >= 0.3:
        contradictions.append("the event reading (%+.2f) and the article's own facts (%+.2f) point opposite ways" % (adir, ldir))
    for c in contradictions:
        trace.append("contradiction: " + c)

    # 2. QUALIFY ---------------------------------------------------------------------------------------------
    status, factor, hits = report_status(text)
    trace += ["status %s: %s" % (status, "; ".join(hits) or "no marker words")]
    certainty = float(a.get("confidence") or 0.6) * 0.5 + 0.5 * factor
    certainty *= {"opinion": 0.6, "promo": 0.5, "transcript": 0.8}.get(lz.get("type"), 1.0)
    if lz.get("stale"):
        certainty *= 0.6
        trace.append("the text describes old events (history wording)")
    if contradictions:
        certainty *= 0.8
    if status == "denied":
        direction = -direction * 0.3                                                                  # a denial mostly cancels the rumour it answers
    certainty = _clip(certainty, 0.05, 0.97)

    # 3. WHO ---------------------------------------------------------------------------------------------------
    sign = 1.0 if direction > 0 else -1.0 if direction < 0 else 0.0
    quotes = []
    for f in (lz.get("facts") or [])[:3]:
        q = (f.get("text") or "").strip()
        if len(q) >= 12 and q in text:
            quotes.append(q[:200])
    if not quotes and title:
        quotes = [title[:200]]
    beneficiaries: Dict[str, dict] = {}
    losers: Dict[str, dict] = {}
    chain: List[dict] = []

    def put(t: str, s: float, conf: float, order: int, why: str) -> None:
        d = beneficiaries if s > 0 else losers
        other = losers if s > 0 else beneficiaries
        if t in other and other[t]["order"] <= order:
            return                                                                                   # a closer effect already says the opposite: keep the closer one
        if t not in d or conf > d[t]["confidence"]:
            d[t] = {"ticker": t, "mechanism": why[:200], "confidence": round(_clip(conf, 0.05, 0.95), 3), "order": order}

    first: List[Tuple[str, float, float]] = []
    pattern = bool(ACQUIRE.search(text) or SUE.search(text))
    if etype == "merger_acquisition" and sign == 0:
        direction, sign = 0.5, 1.0                                                                       # a takeover reads as neutral to a word count, but somebody gains a premium
    if tickers and (sign != 0 or pattern):
        roles, rtrace = resolve_roles(text, tickers, universe, sign, etype)
        trace += rtrace
        for t, (s, conf, why) in roles.items():
            cs = s                                                                                   # a role is absolute: +1 gains, -1 loses (the target, the party sued, or simply the event's direction)
            strength = 0.55 + 0.4 * min(1.0, abs(direction) or 0.5)
            put(t, cs, conf * strength, 1, "%s (%s)" % (why, etype.replace("_", " ")))
            first.append((t, cs, conf * strength))
        if first:
            chain.append({"order": 1, "effect": "%s: %s" % (", ".join(t for t, _, _ in first[:4]), etype.replace("_", " ")), "confidence": round(max(c for _, _, c in first), 3), "tickers": [t for t, _, _ in first[:5]], "sector": ""})

    # 4. PROPAGATE: along the relationship graph, a decay per hop ----------------------------------------
    decay = float(cfg["intelligence"].get("chain_decay", 0.55))
    frontier = [(t, s, c) for t, s, c in first]
    for order in (2, 3):
        nxt: List[Tuple[str, float, float]] = []
        for t, s, c in frontier:
            for e in graph.related(conn, t):
                f = graph.factor(etype, e["role"])
                if f == 0.0 or e["weight"] < float(cfg["graph"].get("min_edge_weight", 0.2)) or (universe is not None and e["ticker"] not in universe) or e["ticker"] in tickers:
                    continue
                conf = min(0.7, c * abs(f) * e["weight"] * 1.6) * (decay if order == 3 else 1.0)
                if conf < (0.12 if order == 2 else 0.05):                                                  # a third hop is weak by design, so its floor is lower
                    continue
                cs = s * (1.0 if f > 0 else -1.0)
                put(e["ticker"], cs, conf, order, "%s of %s (%s)" % (e["role"], t, etype.replace("_", " ")))
                nxt.append((e["ticker"], cs, conf))
        if nxt:
            top = sorted(nxt, key=lambda x: -x[2])[:5]
            chain.append({"order": order, "effect": "via the relationship graph: %s" % ", ".join("%s %s" % (t, "up" if s > 0 else "down") for t, s, _ in top), "confidence": round(top[0][2], 3), "tickers": [t for t, _, _ in top], "sector": ""})
            trace.append("propagated %d effect(s) at order %d through the relationship graph" % (len(nxt), order))
        frontier = sorted(nxt, key=lambda x: -x[2])[:4] if order == 2 else []

    # macro exposures: oil, gold, copper, uranium, rates, the dollar, chip export curbs, conflict, AI capital spending (config/macro_exposures.json)
    grp = groups()
    for fx in macro_effects(text, grp):
        if fx["mixed"]:
            trace.append("macro: %s reaches '%s' through channels that pull both ways (%s): mixed, so nothing is claimed" % (fx["driver"], fx["group"], "; ".join(fx["channels"][:3])))
            continue
        hit = []
        for t in fx["tickers"]:
            if (universe is None or t in universe) and t not in tickers:
                put(t, fx["sign"], fx["confidence"], 2, "%s group '%s' when %s %s: %s" % ("gains" if fx["sign"] > 0 else "loses", fx["group"], fx["driver"], "rises" if fx["rising"] else "falls", "; ".join(fx["channels"][:2])))
                hit.append(t)
        if hit:
            trace.append("macro rule: %s %s -> '%s' (%s)" % (fx["driver"], "up" if fx["rising"] else "down", fx["group"], "; ".join(fx["channels"][:2])))
            chain.append({"order": 2, "effect": "%s %s: %s %s over ~%s" % (fx["driver"], "rises" if fx["rising"] else "falls", fx["group"], "up" if fx["sign"] > 0 else "down", fx["horizon"]), "confidence": round(fx["confidence"], 2), "tickers": hit[:5], "sector": ""})
            if scope in ("macro", "sector") or not tickers:
                direction = direction or fx["sign"] * 0.4

    # 5. THEMES ------------------------------------------------------------------------------------------------
    narratives = []
    named = [t for t, _, _ in first]
    for key, th in THEMES.items():
        mem, vic = set(th.get("tickers") or []), set(th.get("victims") or [])
        sup = [t for t, s, c in first if (t in mem and s > 0) or (t in vic and s < 0)]
        con = [t for t, s, c in first if (t in mem and s < 0) or (t in vic and s > 0)]
        if sup or con:
            st = "support" if len(sup) >= len(con) else "contradict"
            strength = min(0.8, 0.35 + 0.2 * max(len(sup), len(con)) + 0.2 * magnitude)
            narratives.append({"key": key, "stance": st, "strength": round(strength, 2), "why": "%s %s the '%s' theme" % (", ".join((sup or con)[:3]), "supports" if st == "support" else "undermines", th.get("name", key))})
    narratives = sorted(narratives, key=lambda n: -n["strength"])[:4]

    # 6. DOUBT: abstain when there is nothing solid to say -----------------------------------------------------
    n_eff = len(beneficiaries) + len(losers)
    abstain = None
    if etype in ("opinion_piece", "price_recap"):
        abstain = "a %s is not an event" % etype.replace("_", " ")                                          # the deterministic reader's verdict: a stock pick or a price recap reports nothing new
    elif lz.get("type") in ("opinion", "promo") and not (lz.get("facts") or []):
        abstain = "an %s piece that reports no fact" % lz.get("type")                                     # a stock pick or an advertisement is not an event
    elif certainty < 0.3:
        abstain = "certainty %.2f is too low to act on" % certainty
    elif n_eff == 0 and scope not in ("macro", "sector"):
        abstain = "no company it concerns could be identified"
    elif sign == 0 and not narratives and n_eff == 0:
        abstain = "no direction could be read"
    if abstain:
        trace.append("abstained: " + abstain)
        item["_why_not"] = abstain
        item["_trace"] = trace[:14]
        return None

    surprise = None
    for f in lz.get("facts") or []:
        if f.get("kind") in ("eps_surprise", "revenue_surprise") and f.get("surprise") is not None:
            surprise = _clip(f["surprise"] * 5, -1, 1)
            break
    bull = [f["text"] for f in (lz.get("facts") or []) if f.get("pol", 0) > 0][:2] or ([title] if direction > 0 else [])
    bear = [f["text"] for f in (lz.get("facts") or []) if f.get("pol", 0) < 0][:2] or ([title] if direction < 0 else [])
    ben = sorted(beneficiaries.values(), key=lambda e: (e["order"], -e["confidence"]))[:6]
    los = sorted(losers.values(), key=lambda e: (e["order"], -e["confidence"]))[:6]
    why = "%s%s" % (etype.replace("_", " "), (" (" + status + ")") if status != "unclear" else "")
    out = {"event_type": etype if etype in nlp.BY_NAME else "other", "summary": (title or text)[:300], "why": ("Rules read this as a %s. %s" % (why, "; ".join(contradictions)))[:300], "certainty": round(certainty, 3), "grounded": bool(quotes),
            "surprise": surprise, "horizon": HORIZON.get(etype, "days" if scope == "macro" else "weeks"), "magnitude": max(0.25, min(0.85, magnitude)) if magnitude else 0.25, "magnitude_label": _label(magnitude),
            "direction_market": _clip(direction, -1, 1), "beneficiaries": ben, "losers": los, "chain": chain[:6], "narratives": narratives, "proposed_narratives": [], "bull_case": " ".join(bull)[:400], "bear_case": " ".join(bear)[:400],
            "quotes": quotes, "contradictions": contradictions, "status": status, "engine": ENGINE_VERSION, "tag": TAG, "trace": trace[:14]}
    n_ind, primary = independent_sources(conn, item, now)
    roles = {t: round(c, 2) for t, _, c in first}
    prof = HORIZON_PROFILE.get(etype) or {"immediate": .3, "short": .3, "medium": .25, "long": .15}
    comp_row = conn.execute("SELECT market_cap FROM companies WHERE ticker=?", (first[0][0],)).fetchone() if first else None
    classification = float(a.get("confidence") or 0.6)
    truth = min(1.0, factor * (1.0 if (n_ind >= 2 or primary) else 0.92))
    role_conf = min(roles.values()) if roles else 0.0
    out.update({"state": event_state(text), "novelty": novelty_of(text), "evidence_status": "primary" if primary else "corroborated" if n_ind >= 2 else "single_source", "independent_sources": n_ind, "primary_source": primary,
                "materiality": materiality(magnitude, a.get("amount_usd"), comp_row["market_cap"] if comp_row else None, etype), "horizons": prof, "roles": roles,
                "components": {"entity": 0.8 if tickers else 0.4, "classification": round(classification, 2), "truth": round(truth, 2), "role": round(role_conf, 2), "causal": round(max([e["confidence"] for e in ben + los if e["order"] >= 2] or [0.0]), 2),
                               "direction_p": round(0.5 + 0.45 * min(1.0, abs(direction)) * min(truth, classification), 3), "magnitude": round(float(lz.get("confidence") or 0.5), 2), "timing": round(max(prof.values()), 2),
                               "completeness": round(sum(1 for x in (tickers, abs(direction) > 0.05, magnitude, quotes, lz.get("facts")) if x) / 5.0, 2)},
                "ref_version": ref_version(conn)})
    return out


def log_record(conn, cfg: dict, item: dict, intel: Optional[dict], mode: str, now) -> None:
    """Every analysis, including the ones it abstained on, is kept with the rules that produced it: the reasoning can be audited and replayed, and the scorecards can judge it."""
    row = intel or {}
    conn.execute("INSERT INTO reasoning_events(created,news_uid,mode,engine,ref_version,event_type,state,novelty,evidence_status,materiality,independent_sources,abstained,abstain_reason,direction,magnitude,certainty,components_json,horizons_json,roles_json,tickers_json,analysis_json,trace_json) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (iso(now), item.get("uid"), mode, ENGINE_VERSION, row.get("ref_version") or ref_version(conn), row.get("event_type") or (item.get("analysis") or {}).get("event_type"), row.get("state"), row.get("novelty"), row.get("evidence_status"),
                  row.get("materiality"), row.get("independent_sources"), 0 if intel else 1, item.get("_why_not") if not intel else None, row.get("direction_market"), row.get("magnitude"), row.get("certainty"),
                  json.dumps(row.get("components") or {}), json.dumps(row.get("horizons") or {}), json.dumps(row.get("roles") or {}), json.dumps(item.get("tickers") or []), json.dumps(row, sort_keys=True, default=str) if intel else None,
                  json.dumps(row.get("trace") or item.get("_trace") or [])))


def supersede(conn, tickers: List[str], etype: str, now, factor: float = 0.3) -> int:
    """A correction, denial or withdrawal REPLACES earlier evidence about the same subject: its credibility is cut, not just argued with."""
    fam = [k for k, v in __import__("paper_agent.skeptic", fromlist=["FAMILY"]).FAMILY.items() if v == __import__("paper_agent.skeptic", fromlist=["FAMILY"]).FAMILY.get(etype)] or [etype]
    n = 0
    for t in tickers:
        n += conn.execute("UPDATE evidence SET credibility=credibility*?, note=COALESCE(note,'')||' [superseded]' WHERE ticker=? AND event_type IN (%s) AND created>=? AND COALESCE(note,'') NOT LIKE '%%[superseded]%%'" % ",".join("?" * len(fam)),
                          (factor, t, *fam, iso(now - dt.timedelta(hours=72)))).rowcount
    return n


def make_analyst(conn, cfg: dict, universe=None) -> Callable:
    """The analyst the pipeline calls for each news item. It ALWAYS reasons and logs; only in `active` influence does its analysis become evidence and candidates (observe and guard record it and change nothing)."""
    mode = str(cfg["intelligence"].get("influence", "guard"))

    def analyst(item: dict) -> Optional[dict]:
        now = item.get("now") or utcnow()                                                                 # the cycle's own clock (so a replay or a test is reproducible)
        intel = analyze_item(conn, cfg, universe, item, now)
        if mode == "active":                                                                              # a denial or correction REPLACES earlier evidence, and it is usually too weak to be analysed on its own, so this happens first
            txt = ((item.get("title") or "") + ". " + (item.get("summary") or ""))
            if (novelty_of(txt) == "correction" or event_state(txt) in ("denied", "rejected")) and item.get("tickers"):
                try:
                    supersede(conn, list(item["tickers"]), (item.get("analysis") or {}).get("event_type") or "general_news", now)
                except Exception:  # noqa: BLE001
                    pass
        try:
            log_record(conn, cfg, item, intel, mode, now)
        except Exception:  # noqa: BLE001 - the audit log must never stop the cycle
            pass
        if not intel or mode != "active":
            return None
        a = item.get("analysis") or {}
        first = [e for e in intel["beneficiaries"] + intel["losers"] if e["order"] == 1]
        if a.get("scope") == "macro" or not first:
            direction = intel["direction_market"]
        else:
            top = max(first, key=lambda e: e["confidence"])
            direction = (1.0 if top in intel["beneficiaries"] else -1.0) * top["confidence"]
        return {"direction": direction * intel["certainty"], "magnitude": intel["magnitude"], "tickers": [e["ticker"] for e in first], "confidence": intel["certainty"], "rationale": intel["summary"], "intel": intel, "tier": "rules"}
    return analyst


# ------------------------------------------------------------------------------------------------ the red team
OPPOSITE_EVENTS = {"earnings_beat": ["earnings_miss", "guidance_cut"], "guidance_raise": ["guidance_cut", "earnings_miss"], "contract_win": ["contract_loss", "regulatory_action"], "product_launch": ["recall", "regulatory_action"],
                   "merger_acquisition": ["regulatory_action", "litigation"], "regulatory_approval": ["regulatory_action", "litigation"], "partnership": ["litigation", "contract_loss"]}
SHORT_FALSIFIERS = ["guidance_raise", "earnings_beat", "contract_win", "merger_acquisition"]
LONG_FALSIFIERS = ["guidance_cut", "earnings_miss", "regulatory_action", "litigation"]


def _recent_opposite(conn, ticker: str, sign: float, now, hours: int = 48) -> List[dict]:
    since = iso(now - dt.timedelta(hours=hours))
    # the same exclusions the pre-trade news veto uses: a price recap or a many-company roundup is not news ABOUT this company; and only what had happened by `now`
    rows = conn.execute("SELECT headline,direction,magnitude,credibility,event_type,created FROM events WHERE created>=? AND created<=? AND stale=0 AND tickers_json LIKE ? AND magnitude>=0.35 AND credibility>=0.5 "
                        "AND gate IS NOT 'stale' AND gate IS NOT 'too_many_companies' AND event_type NOT IN ('price_recap','opinion_piece','general_news') ORDER BY id DESC LIMIT 40",
                        (since, iso(now), '%"' + ticker + '"%')).fetchall()
    return [r for r in rows if r["direction"] * sign < -0.3]


def review(conn, cfg: dict, client, cand: dict, f: dict, comp: dict, th: dict, regime: dict, now, decision_id: Optional[int] = None) -> Optional[dict]:
    """The case against a trade (see skeptic.py). `client` is ignored: there is no LLM."""
    from . import skeptic
    return skeptic.review(conn, cfg, cand, f, comp, th, regime, now, decision_id)


def gate(rv: dict, cfg: dict, mode: str = "guard") -> dict:
    from . import skeptic
    return skeptic.gate(rv, cfg, mode)


# ------------------------------------------------------------------------------------------------ questions
EVENT_WORDS = [(r"\b(beats?|beat estimates|tops estimates|earnings beat)\b", "earnings_beat", 1), (r"\b(misses|missed|earnings miss)\b", "earnings_miss", -1), (r"\b(raises? (?:its )?(?:guidance|outlook|forecast)|guidance raise)\b", "guidance_raise", 1),
               (r"\b(cuts?|lowers?|slashes) (?:its )?(?:guidance|outlook|forecast)|guidance cut\b", "guidance_cut", -1), (r"\b(wins?|awarded|contract win)\b", "contract_win", 1), (r"\b(loses|lost) (?:a |the )?contract|contract loss\b", "contract_loss", -1),
               (r"\b(recall|recalls)\b", "recall", -1), (r"\b(lawsuit|sued|litigation|sues)\b", "litigation", -1), (r"\b(outage|shutdown|production disruption|plant fire|halts production|disruption)\b", "production_disruption", -1),
               (r"\b(acquired|takeover|buyout|merger|acquisition)\b", "merger_acquisition", 1), (r"\b(cyber ?attack|hack|breach|ransomware)\b", "cyber_incident", -1), (r"\b(fda approval|approved|approval)\b", "regulatory_approval", 1),
               (r"\b(fined|probe|investigation|antitrust|ban)\b", "regulatory_action", -1), (r"\b(launch(?:es)?|unveils?|new product)\b", "product_launch", 1), (r"\b(bankruptcy|default|insolvency|going concern)\b", "bankruptcy_distress", -1)]
INTENTS = [("help", r"\b(help|what can you|how do i ask)\b"), ("whatif", r"\b(what if|what would happen|impact of|effect of|suppose|if (?:\w+ ){0,2}(?:beats?|misses|cuts?|raises?|wins?|loses|recalls?|is sued|halts?))\b"),
           ("compare", r"\b(compare|versus|vs\.?|which is better|better than)\b"), ("risk", r"\b(risks?|hurt us|exposure|danger|worr\w+|concentrat\w+|stress|vulnerab\w+|what could go wrong)\b"),
           ("theme", r"\b(theme|narrative|thesis|theses)\b"), ("graph", r"\b(related|neighbou?rs?|suppliers?|customers?|competitors?|peers?|connected|linked)\b"),
           ("news", r"\b(news|headlines?|what happened)\b"), ("why", r"\b(why|explain|tell me about|what(?:'s| is) going on|reason)\b"), ("today", r"\b(today|summary|brief|overview|status|how are we doing|how did we do)\b")]


def find_tickers(conn, text: str, universe=None) -> List[str]:
    toks = re.findall(r"\b[A-Z]{1,5}\b", text.upper().replace("-", " "))
    known = None
    out = []
    for t in toks:
        if t in out or t in ("I", "A", "WHY", "WHAT", "IF", "THE", "AND", "OR", "VS", "OF", "TO", "IN", "ON", "US", "AI", "IS", "IT", "ME", "WE", "NEWS", "FOR", "ARE"):
            continue
        if universe is not None:
            ok = t in universe
        else:
            known = known if known is not None else ({r[0] for r in conn.execute("SELECT ticker FROM companies")} | {r[0] for r in conn.execute("SELECT src FROM relations")} | {r[0] for r in conn.execute("SELECT ticker FROM decisions ORDER BY id DESC LIMIT 2000")})
            ok = t in known
        if ok:
            out.append(t)
    return out


def _sec(h: str, lines: List[str]) -> dict:
    return {"heading": h, "lines": [l for l in lines if l]}


def _render(title: str, sections: List[dict]) -> str:
    out = [title, ""]
    for s in sections:
        out.append(s["heading"].upper())
        out += ["  " + l for l in s["lines"]]
        out.append("")
    return "\n".join(out).rstrip()


def _ans(intent: str, title: str, sections: List[dict], data: Optional[dict] = None, ok: bool = True) -> dict:
    return {"intent": intent, "ok": ok, "title": title, "sections": sections, "text": _render(title, sections), "data": data or {}}


def ask(conn, cfg: dict, question: str, universe=None, now=None) -> dict:
    """Answer a question from the bot's own data and rules. Supported: why <stock> / news <stock> / related <stock> / what if <stock> <event> / compare <a> <b> / theme <name> / what could hurt us / today / help.
    Anything else gets an honest 'I can't answer that', never a guess."""
    now = now or utcnow()
    q = (question or "").strip()
    low = q.lower()
    ts = find_tickers(conn, q, universe)
    intent = next((n for n, rx in INTENTS if re.search(rx, low)), None)
    if intent is None and ts:
        intent = "why"
    if intent == "help" or (intent is None and not low):
        return _help()
    fn = {"whatif": _whatif, "compare": _compare, "risk": _risk, "theme": _theme, "graph": _graph, "news": _news, "why": _why, "today": _today}.get(intent)
    if fn is None:
        return _ans("unsupported", "I can't answer that", [_sec("what I can do", _HELP_LINES)], ok=False)
    return fn(conn, cfg, q, ts, universe, now)


_HELP_LINES = ["why NVDA             everything the bot knows about a stock: position, decision, evidence, themes, relationships, the case against",
               "news NVDA            the recent events about a stock, with direction and credibility", "related NVDA         who supplies it, buys from it, competes with it, moves with it",
               "what if TSM cuts guidance     who is affected, in which direction, how strongly, and which of our holdings", "compare NVDA AMD    the two side by side, including whether they are linked",
               "theme ai_infrastructure       the theme's state, members and our exposure", "what could hurt us    exposure, stress test, near-twin holdings, open tripwires", "today                what happened and where we stand"]


def _help() -> dict:
    return _ans("help", "What you can ask", [_sec("questions I understand", _HELP_LINES), _sec("what I am", ["A rule-based reasoning engine: it answers from the bot's own data. It does not read free text like a language model, so it answers the question types above and says so when it cannot."])])


def _why(conn, cfg, q, ts, universe, now):
    if not ts:
        return _ans("why", "Which stock?", [_sec("", ["Name a stock, e.g. 'why NVDA'."])], ok=False)
    t = ts[0]
    sections: List[dict] = []
    pos = conn.execute("SELECT * FROM positions WHERE ticker=? AND status='OPEN' ORDER BY id DESC LIMIT 1", (t,)).fetchone()
    opt = conn.execute("SELECT * FROM option_positions WHERE underlying=? AND status='OPEN' ORDER BY id DESC LIMIT 1", (t,)).fetchone()
    held = []
    if pos:
        held.append("%s position: %d shares from %.2f, opened %s" % (pos["side"] or "LONG", pos["shares"], pos["entry_price"], (pos["opened"] or "")[:16]))
    if opt:
        held.append("%s %s %.2f expiring %s, paid $%.0f (%s)" % (opt["kind"], t, opt["strike"], opt["expiry"], opt["cost"], "real contract" if (opt["price_source"] or "") == "CHAIN" else "MODEL price"))
    sections.append(_sec("our position", held or ["not held"]))
    d = conn.execute("SELECT * FROM decisions WHERE ticker=? ORDER BY id DESC LIMIT 1", (t,)).fetchone()
    if d:
        why = ""
        try:
            R = json.loads(d["reason"] or "{}")
            why = "; ".join((R.get("gates") or [])[-2:])
        except ValueError:
            pass
        sections.append(_sec("latest decision", ["%s: %s (%s), edge %s, %s" % ((d["created"] or "")[:16], d["status"], d["action"], ("%+.1f%%" % (d["ev_net"] * 100)) if d["ev_net"] is not None else "n/a", why or "no gate notes")]))
    fc = conn.execute("SELECT * FROM forecasts WHERE ticker=? ORDER BY id DESC LIMIT 1", (t,)).fetchone()
    cand: dict = {"ticker": t}
    if fc:
        sc = json.loads(fc["scenarios_json"] or "{}")
        sections.append(_sec("forecast", ["%s %s over %d sessions, edge %+.1f%% (chance the stock rises %.0f%%)" % (fc["verdict"], fc["direction"], fc["horizon_days"], fc["ev_net"] * 100, fc["p_up"] * 100),
                                         "scenarios: " + ", ".join("%s %+.1f%% (p=%.0f%%)" % (k, v["ret"] * 100, v["p"] * 100) for k, v in sc.items())]))
        cand.update({"direction": 1.0 if fc["direction"] == "LONG" else -1.0, "headline": "", "event_type": (json.loads(fc["inputs_json"] or "{}")).get("event_type")})
    evs = conn.execute("SELECT headline,direction,magnitude,credibility,event_type,created FROM events WHERE created>=? AND stale=0 AND tickers_json LIKE ? ORDER BY magnitude*credibility DESC LIMIT 5",
                       (iso(now - dt.timedelta(hours=72)), '%"' + t + '"%')).fetchall()
    sections.append(_sec("evidence in the last 72 hours", ["%s %+.2f  %s: %s (credibility %.2f)" % ((e["created"] or "")[5:16], e["direction"], e["event_type"].replace("_", " "), (e["headline"] or "")[:90], e["credibility"]) for e in evs] or ["no events"]))
    th = [(k, v) for k, v in THEMES.items() if t in (v.get("tickers") or [])]
    rows = {r["key"]: r for r in conn.execute("SELECT key,state,probability FROM theses")} if conn.execute("SELECT 1 FROM sqlite_master WHERE name='theses'").fetchone() else {}
    sections.append(_sec("themes", ["%s: %s" % (v["name"], (rows[k]["state"] + (" (%.0f%%)" % (rows[k]["probability"] * 100) if rows[k]["probability"] is not None else "")) if k in rows else "no state yet") for k, v in th[:4]] or ["not in any tracked theme"]))
    nb = sorted(graph.related(conn, t), key=lambda e: -e["weight"])[:6]
    sections.append(_sec("relationships", ["%s of %s (%.2f)%s" % (e["role"], t, e["weight"], (": " + e["note"]) if e["note"] else "") for e in nb] or ["no relationships recorded"]))
    fx = facts_mod.get(conn, t)
    if fx:
        L = []
        if fx.get("revenue_ttm"):
            L.append("revenue $%.1fB (%s)%s" % (fx["revenue_ttm"] / 1e9, fx["basis"].get("revenue", ""), ", %+.0f%% on a year earlier" % (fx["revenue_growth"] * 100) if fx.get("revenue_growth") is not None else ""))
        if fx.get("net_margin") is not None:
            L.append("net margin %.1f%%%s" % (fx["net_margin"] * 100, ", operating margin %.1f%%" % (fx["op_margin"] * 100) if fx.get("op_margin") is not None else ""))
        if fx.get("cash") is not None:
            L.append("cash $%.1fB%s" % (fx["cash"] / 1e9, ", debt $%.1fB" % (fx["debt"] / 1e9) if fx.get("debt") is not None else ""))
        if fx.get("share_growth") is not None:
            L.append("share count %+.1f%% in a year" % (fx["share_growth"] * 100))
        L += ["FLAG: " + f["text"] for f in fx.get("flags", []) if f["severity"] > 0]
        sections.append(_sec("reported financials (SEC data, as of %s)" % (fx.get("asof") or "?"), L or ["no usable figures"]))
    rr = conn.execute("SELECT created,state,novelty,evidence_status,materiality,certainty,abstained,abstain_reason FROM reasoning_events WHERE tickers_json LIKE ? ORDER BY id DESC LIMIT 1", ('%"' + t + '"%',)).fetchone() if conn.execute("SELECT 1 FROM sqlite_master WHERE name='reasoning_events'").fetchone() else None
    if rr:
        sections.append(_sec("the engine's latest reading of news about it", ["%s: state %s, %s, %s evidence, %s materiality, certainty %s%s" % ((rr["created"] or "")[5:16], rr["state"], rr["novelty"], (rr["evidence_status"] or "?").replace("_", " "), rr["materiality"] or "?",
                                                                                                               ("%.2f" % rr["certainty"]) if rr["certainty"] is not None else "n/a", ("; abstained: " + (rr["abstain_reason"] or "")) if rr["abstained"] else "")]))
    against = []
    if fc and cand.get("direction") is not None:
        f = {"verdict": fc["verdict"], "inputs": json.loads(fc["inputs_json"] or "{}"), "stop_return": fc["stop_return"], "ev_net": fc["ev_net"], "scenarios": json.loads(fc["scenarios_json"] or "{}"), "confidence": fc["confidence"], "horizon_days": fc["horizon_days"]}
        rv = _bears_only(conn, cfg, cand, f, now)
        against = ["%s (severity %.2f)%s" % (b["point"], b["severity"], (": " + b["evidence"]) if b["evidence"] else "") for b in rv["bear_points"][:4]]
        if rv["fatal_flaw"]:
            against.insert(0, "FATAL: " + rv["fatal_reason"])
    sections.append(_sec("the case against", against or ["nothing to weigh (no forecast yet)"]))
    return _ans("why", "%s: what the bot knows" % t, sections, {"ticker": t})


def _bears_only(conn, cfg, cand, f, now) -> dict:
    """The red-team review without saving it (a question must not write to the audit log of real decisions)."""
    mark = conn.execute("SELECT COALESCE(MAX(id),0) FROM redteam_reviews").fetchone()[0]
    with db.tx(conn):
        rv = review(conn, cfg, None, dict(cand, credibility=cand.get("credibility", 0.7)), f, {}, {}, {}, now, None)
        conn.execute("DELETE FROM redteam_reviews WHERE id>?", (mark,))
    return rv


def _news(conn, cfg, q, ts, universe, now):
    if not ts:
        return _ans("news", "Which stock?", [_sec("", ["Name a stock, e.g. 'news NVDA'."])], ok=False)
    t = ts[0]
    rows = conn.execute("SELECT headline,direction,magnitude,credibility,event_type,created,stale FROM events WHERE tickers_json LIKE ? ORDER BY id DESC LIMIT 12", ('%"' + t + '"%',)).fetchall()
    return _ans("news", "%s: recent events" % t, [_sec("newest first", ["%s %+.2f mag %.2f cred %.2f %s%s: %s" % ((r["created"] or "")[5:16], r["direction"], r["magnitude"], r["credibility"], r["event_type"].replace("_", " "), " (old)" if r["stale"] else "", (r["headline"] or "")[:90]) for r in rows] or ["no events recorded"])])


def _graph(conn, cfg, q, ts, universe, now):
    if not ts:
        return _ans("graph", "Which stock?", [_sec("", ["Name a stock, e.g. 'related NVDA'."])], ok=False)
    t = ts[0]
    by: Dict[str, List[str]] = {}
    for e in sorted(graph.related(conn, t), key=lambda e: -e["weight"]):
        by.setdefault(e["role"], []).append("%s (%.2f%s)" % (e["ticker"], e["weight"], "" if e["source"] == "seed" else ", measured"))
    total = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    return _ans("graph", "%s: its relationship graph" % t, [_sec(role, [", ".join(v)]) for role, v in by.items()] or [_sec("", ["no relationships recorded for %s (the map holds %d links in total)" % (t, total)])], {"ticker": t})


def _whatif(conn, cfg, q, ts, universe, now):
    low = q.lower()
    ev_ = next(((et, sg) for rx, et, sg in EVENT_WORDS if re.search(rx, low)), None)
    if not ts or not ev_:
        return _ans("whatif", "I need a stock and an event", [_sec("example", ["what if TSM cuts guidance", "what if NVDA beats earnings", "what if AMD is sued"])], ok=False)
    t, (et, sg) = ts[0], ev_
    c = {"ticker": t, "event_type": et, "direction": 0.8 * sg, "magnitude": 0.8, "conf": 0.8, "credibility": 0.9, "age_hours": 0.0, "horizon": 6, "origin": "event", "headline": "%s %s" % (t, et.replace("_", " ")), "event_id": None}
    cfg2 = json.loads(json.dumps(cfg))
    cfg2["graph"]["enabled"] = True
    out = graph.derive(conn, cfg2, [c], universe, now)
    hold = {r["ticker"]: r for r in conn.execute("SELECT ticker,side,cost FROM positions WHERE status='OPEN'")}
    lines = ["%-6s %s %s (%s, strength %.2f)" % (d["ticker"], "UP  " if d["direction"] > 0 else "DOWN", "%+.2f" % d["direction"], d["graph_role"], abs(d["direction"]) * d["magnitude"]) + ("   <- we hold this (%s)" % hold[d["ticker"]]["side"] if d["ticker"] in hold else "") for d in out]
    sec = [_sec("assumed event", ["%s at %s: %s (a strong, confirmed event)" % (et.replace("_", " "), t, "good news" if sg > 0 else "bad news")]),
           _sec("first-order effect", ["%s %s%s" % (t, "UP" if sg > 0 else "DOWN", "   <- we hold this (%s)" % hold[t]["side"] if t in hold else "")]),
           _sec("who else is affected (relationship graph, decayed)", lines or ["nothing connected is strongly affected"])]
    mine = [d for d in out if d["ticker"] in hold]
    if t in hold:
        mine.append({"ticker": t, "direction": sg})
    if mine:
        sec.append(_sec("what it means for us", ["%s (%s) %s" % (m["ticker"], hold[m["ticker"]]["side"], "helped" if (m["direction"] > 0) == (hold[m["ticker"]]["side"] == "LONG") else "hurt") for m in mine]))
    return _ans("whatif", "What if %s at %s" % (et.replace("_", " "), t), sec, {"affected": [d["ticker"] for d in out]})


def _compare(conn, cfg, q, ts, universe, now):
    if len(ts) < 2:
        return _ans("compare", "Which two stocks?", [_sec("", ["e.g. 'compare NVDA AMD'"])], ok=False)
    a, b = ts[0], ts[1]
    rows = []
    for t in (a, b):
        c = conn.execute("SELECT sector,market_cap,adv_usd,vol_daily FROM companies WHERE ticker=?", (t,)).fetchone()
        f = conn.execute("SELECT verdict,ev_net,p_up FROM forecasts WHERE ticker=? ORDER BY id DESC LIMIT 1", (t,)).fetchone()
        ev_ = conn.execute("SELECT AVG(direction*credibility*magnitude) FROM events WHERE created>=? AND stale=0 AND tickers_json LIKE ?", (iso(now - dt.timedelta(hours=72)), '%"' + t + '"%')).fetchone()[0]
        rows.append("%-6s %s | latest forecast %s | 72h news score %s | daily vol %s" % (t, (c["sector"] if c else "?"), ("%s %+.1f%%" % (f["verdict"], f["ev_net"] * 100)) if f else "none", ("%+.2f" % ev_) if ev_ is not None else "none", ("%.1f%%" % (c["vol_daily"] * 100)) if c and c["vol_daily"] else "n/a"))
    rel = [e for e in graph.related(conn, a) if e["ticker"] == b]
    cr = graph.correlation(conn, a, b)
    link = ["%s is %s of %s (%.2f)" % (b, rel[0]["role"], a, rel[0]["weight"])] if rel else ["no recorded relationship"]
    link.append("60-session correlation: %s" % (("%.2f" % cr) if cr is not None else "unknown (not enough price history)"))
    return _ans("compare", "%s vs %s" % (a, b), [_sec("side by side", rows), _sec("are they linked?", link), _sec("reading", ["holding both is %s" % ("close to one bet" if (cr or 0) > 0.7 else "reasonably diversified" if cr is not None else "unknown")])])


def _theme(conn, cfg, q, ts, universe, now):
    key = next((k for k, v in THEMES.items() if k.lower() in q.lower() or v["name"].lower() in q.lower() or k.replace("_", " ") in q.lower()), None)
    if not key:
        return _ans("theme", "Which theme?", [_sec("tracked themes", [", ".join(sorted(THEMES))])], ok=False)
    th = THEMES[key]
    st = conn.execute("SELECT state,probability,net_strength,trend FROM theses WHERE key=?", (key,)).fetchone()
    held = [r["ticker"] for r in conn.execute("SELECT ticker FROM positions WHERE status='OPEN'") if r["ticker"] in (th.get("tickers") or [])]
    return _ans("theme", th["name"], [_sec("state", ["%s, probability %s, net strength %s, trend %s" % (st["state"], ("%.0f%%" % (st["probability"] * 100)) if st["probability"] is not None else "n/a", ("%+.2f" % st["net_strength"]) if st["net_strength"] is not None else "n/a", ("%+.3f" % st["trend"]) if st["trend"] is not None else "n/a")] if st else ["no state recorded yet"]),
                                     _sec("what it is", [th.get("description", "")]), _sec("members", [", ".join((th.get("tickers") or [])[:20])]), _sec("who loses when it wins", [", ".join(th.get("victims") or []) or "n/a"]), _sec("our exposure", [", ".join(held) or "none held"])], {"key": key})


def _risk(conn, cfg, q, ts, universe, now):
    from . import portfolio as pf
    hs = pf.holdings(conn, now)
    eq = pf.equity_of(conn, hs, now)
    sec: List[dict] = [_sec("book", ["equity $%s, cash $%s, long %s, short %s, options %s, gross %.0f%%, net %+.0f%%" % (format(round(eq["equity"]), ","), format(round(eq["cash"]), ","), format(round(eq["long_value"]), ","), format(round(eq["short_value"]), ","),
                                                                                                                     format(round(eq.get("option_value", 0)), ","), eq["gross"] * 100, eq["net"] * 100)])]
    bysec: Dict[str, float] = {}
    for h in hs:
        bysec[h.get("sector") or "?"] = bysec.get(h.get("sector") or "?", 0.0) + abs(h["value"])
    sec.append(_sec("biggest sector exposures", ["%s %.0f%%" % (k, 100 * v / eq["equity"]) for k, v in sorted(bysec.items(), key=lambda kv: -kv[1])[:4]] or ["no positions"]))
    twins = []
    cache: dict = {}
    for i, a in enumerate(hs):
        for b in hs[i + 1:]:
            if a.get("side") == b.get("side"):
                c = graph.correlation(conn, a["ticker"], b["ticker"], 60, cache)
                if c is not None and c > 0.7:
                    twins.append("%s and %s move together (correlation %.2f): one bet" % (a["ticker"], b["ticker"], c))
    sec.append(_sec("near-twin holdings", twins or ["none"]))
    if hs and eq["equity"]:
        st = pf.stress(hs, eq["equity"], cfg, {}, eq.get("option_value", 0.0))
        sec.append(_sec("stress test", ["worst scenario loses %.1f%% of equity" % (100 * float(st.get("worst_loss", 0)))]))
    tw = conn.execute("SELECT p.ticker,t.description FROM tripwires t JOIN positions p ON p.id=t.position_id WHERE t.status='OPEN' LIMIT 8").fetchall()
    sec.append(_sec("open tripwires (what would prove us wrong)", ["%s: %s" % (r["ticker"], r["description"][:90]) for r in tw] or ["none"]))
    return _ans("risk", "What could hurt us", sec)


def _today(conn, cfg, q, ts, universe, now):
    since = iso(now - dt.timedelta(hours=24))
    ev_ = conn.execute("SELECT headline,direction,magnitude,credibility,tickers_json FROM events WHERE created>=? AND stale=0 ORDER BY magnitude*credibility DESC LIMIT 5", (since,)).fetchall()
    dec = conn.execute("SELECT status,COUNT(*) n FROM decisions WHERE created>=? GROUP BY status ORDER BY n DESC", (since,)).fetchall()
    closed = conn.execute("SELECT ticker,side,pnl,review_code FROM positions WHERE status='CLOSED' AND closed>=?", (since,)).fetchall()
    return _ans("today", "The last 24 hours", [_sec("most important events", ["%+.2f %s" % (e["direction"], (e["headline"] or "")[:100]) for e in ev_] or ["none"]), _sec("decisions", ["%s: %d" % (r["status"], r["n"]) for r in dec] or ["none"]),
                                               _sec("closed positions", ["%s %s %+.0f (%s)" % (r["ticker"], r["side"] or "LONG", r["pnl"] or 0, (r["review_code"] or "").replace("_", " ").lower()) for r in closed] or ["none"])])
