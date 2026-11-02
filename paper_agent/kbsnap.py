"""Point-in-time company facts: financial and market snapshots, deterministic profiles, and the import of curated relationship / exposure seeds. Nothing is estimated: a figure that cannot be built reliably is
stored as unavailable with the reason. A market observation older than its limit is RECORDED (status 'expired') but never offered as a current fact. Every row has its source and a first-known time, so a historical
replay can ask 'what did the bot know then?'."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from typing import Dict, List, Optional

from . import db, facts as facts_mod, kbdata
from .timeutil import iso, parse_ts, utcnow

REQUIRED_FINANCIALS = ["revenue_ttm", "revenue_growth", "net_income_ttm", "op_income_ttm", "gross_profit_ttm", "cfo_ttm", "capex_ttm", "cash", "debt_lt", "debt_st", "assets_current", "liab_current", "equity", "share_growth", "interest_ttm"]
DERIVED_FINANCIALS = ["op_margin", "gross_margin", "net_margin", "fcf_ttm", "debt", "net_debt", "current_ratio", "interest_cover"]
FLAG_FIELDS = {"dilution_flag": ("dilution",), "liquidity_flag": ("low_liquidity",), "leverage_flag": ("high_leverage", "debt_above_cash", "thin_interest_cover"),
               "profit_without_cash_flag": ("earnings_quality",), "margin_squeeze_flag": ("margin_squeeze",), "burning_cash_flag": ("burning_cash",)}
PRICE_MAX_AGE_DAYS = 5
SHARES_DIRECT_TAG_NOTE = "shares outstanding are the weighted diluted count from the filing; the cover-page count is not used"


def entity_of(conn, ticker: str) -> Optional[str]:
    r = conn.execute("SELECT entity_id FROM kb_identifiers WHERE kind='ticker' AND value=? AND valid_to IS NULL", (ticker,)).fetchone()
    return r[0] if r else None


def ticker_of(conn, eid: str) -> Optional[str]:
    r = conn.execute("SELECT value FROM kb_identifiers WHERE entity_id=? AND kind='ticker' AND valid_to IS NULL ORDER BY is_primary DESC LIMIT 1", (eid,)).fetchone()
    return r[0] if r else None


def _now(now):
    return now or utcnow()


# ------------------------------------------------------------------------------------------------ financial context
def financial_snapshot(conn, cfg: dict, eid: str, ticker: str, now=None) -> dict:
    """The reliable, point-in-time financial context from the SEC facts the bot already stores. One row per distinct facts retrieval; nothing is carried forward from older annual data."""
    now = _now(now)
    f = facts_mod.get(conn, ticker)
    if not f:
        _cov(conn, eid, "financial_status", "unavailable" if _operating(conn, eid) else "not_applicable")
        return {"status": "unavailable", "reason": "no SEC facts stored for %s yet" % ticker}
    max_age = int((cfg.get("kb") or {}).get("financial_max_age_days", 150))
    metrics: Dict[str, dict] = {}
    for name, rec in (f.get("metrics") or {}).items():
        rel = "ok" if (rec.get("age_days") is not None and rec["age_days"] <= max_age) else "stale"
        metrics[name] = dict(rec, reliability=rel)
    derived = {k: f.get(k) for k in DERIVED_FINANCIALS if f.get(k) is not None}
    unavailable = dict(f.get("unavailable") or {})
    for k in REQUIRED_FINANCIALS:
        if k not in metrics and k not in unavailable:
            unavailable[k] = "not reported or not reliably constructible"
    flags_present = {x.get("flag") for x in (f.get("flags") or [])}
    flags = {field: any(n in flags_present for n in names) for field, names in FLAG_FIELDS.items()}
    flags["details"] = [{"flag": x.get("flag"), "text": x.get("text"), "period_end": x.get("period_end"), "filed": x.get("filed")} for x in (f.get("flags") or [])]
    have = sum(1 for k in REQUIRED_FINANCIALS if k in metrics)
    stale = sum(1 for m in metrics.values() if m["reliability"] == "stale")
    status = "unavailable" if have == 0 else ("stale" if stale == len(metrics) else ("ok" if have >= 0.6 * len(REQUIRED_FINANCIALS) and stale == 0 else "partial"))
    accns = sorted({a for m in metrics.values() for a in (m.get("accn") or [])})
    taken = f.get("retrieved") or f.get("fetched") or iso(now)
    last = conn.execute("SELECT taken FROM kb_financial_snapshots WHERE entity_id=? ORDER BY id DESC LIMIT 1", (eid,)).fetchone()
    if not last or last[0] != taken:
        conn.execute("INSERT INTO kb_financial_snapshots(entity_id,ticker,taken,period_end,filed,metrics_json,flags_json,freshness_json,source_accession,taxonomy,status,reliability,first_known) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (eid, ticker, taken, f.get("asof"), max((m.get("filed") or "" for m in metrics.values()), default=None) or None, json.dumps({"reported": metrics, "derived": derived, "unavailable": unavailable, "shares_note": SHARES_DIRECT_TAG_NOTE}, sort_keys=True, default=str),
                      json.dumps(flags, sort_keys=True, default=str), json.dumps({"max_age_days": max_age, "stale_metrics": sorted(k for k, m in metrics.items() if m["reliability"] == "stale"), "retrieved": taken}), ",".join(accns[:8]) or None, "us-gaap",
                      status, "reliable" if status == "ok" else "partial_or_stale" if status in ("partial", "stale") else "none", iso(now)))
    _cov(conn, eid, "financial_status", status)
    return {"status": status, "metrics": len(metrics), "unavailable": len(unavailable), "flags": [k for k, v in flags.items() if v is True]}


def financial_as_of(conn, eid: str, when) -> Optional[dict]:
    """The financial snapshot the bot KNEW at `when` (first_known <= when): never a later one. Point-in-time correct for replays."""
    r = conn.execute("SELECT * FROM kb_financial_snapshots WHERE entity_id=? AND first_known<=? ORDER BY first_known DESC,id DESC LIMIT 1", (eid, iso(when) if hasattr(when, "isoformat") else when)).fetchone()
    return dict(r) if r else None


def _operating(conn, eid: str) -> bool:
    r = conn.execute("SELECT operating FROM kb_entities WHERE entity_id=?", (eid,)).fetchone()
    return not (r and r[0] == 0)


def _cov(conn, eid: str, col: str, status: str) -> None:
    conn.execute("UPDATE knowledge_coverage SET %s=? WHERE entity_id=?" % col, (status, eid))


# ------------------------------------------------------------------------------------------------ market and trading characteristics
def _closes(conn, symbol: str, n: int = 70) -> List[dict]:
    rows = conn.execute("SELECT day,close,volume FROM market_series WHERE symbol=? ORDER BY day DESC LIMIT ?", (symbol, n)).fetchall()
    return [dict(r) for r in reversed(rows)]


def liquidity_class(adv_usd: Optional[float], min_adv: float = 1_000_000.0) -> str:
    if adv_usd is None:
        return "unknown"
    return "high" if adv_usd >= 50e6 else "medium" if adv_usd >= 5e6 else "low" if adv_usd >= min_adv else "illiquid"


def market_snapshot(conn, cfg: dict, eid: str, ticker: str, now=None) -> dict:
    """Latest verified market facts. A price older than PRICE_MAX_AGE_DAYS (5) is expired: stored with status expired and excluded from current_market(). Missing inputs stay NULL."""
    now = _now(now)
    ser = _closes(conn, ticker)
    price = price_ts = None
    if ser:
        price, price_ts = ser[-1]["close"], ser[-1]["day"]
    q = conn.execute("SELECT price,quoted_at FROM quote_cache WHERE ticker=?", (ticker,)).fetchone() if db.tables(conn) and "quote_cache" in db.tables(conn) else None
    if q and (price_ts is None or q["quoted_at"][:10] >= price_ts):
        price, price_ts = q["price"], q["quoted_at"]
    co = conn.execute("SELECT sector,industry,market_cap,adv_usd,vol_daily FROM companies WHERE ticker=?", (ticker,)).fetchone() if "companies" in db.tables(conn) else None
    closes = [x["close"] for x in ser if x["close"]]
    chg = lambda n: (closes[-1] / closes[-1 - n] - 1.0) if len(closes) > n and closes[-1 - n] else None  # noqa: E731
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(max(1, len(closes) - 60), len(closes)) if closes[i - 1]]
    vol = (math.sqrt(sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) >= 10 else None)
    vols = [x["volume"] for x in ser[-20:] if x.get("volume")]
    adv_sh = sum(vols) / len(vols) if len(vols) >= 10 else None
    adv_usd = (sum(x["volume"] * x["close"] for x in ser[-20:] if x.get("volume") and x.get("close")) / len(vols)) if len(vols) >= 10 else None
    if adv_usd is None and co and co["adv_usd"]:
        adv_usd = co["adv_usd"]
    mcap = co["market_cap"] if co and co["market_cap"] else None
    age = None
    if price_ts:
        try:
            age = (now - parse_ts(price_ts if "T" in str(price_ts) else str(price_ts) + "T00:00:00+00:00")).days
        except ValueError:
            age = None
    status = "unavailable" if price is None else ("ok" if age is not None and age <= PRICE_MAX_AGE_DAYS else "expired")
    inst = cfg.get("instruments") or {}
    short_ok = None
    if price is not None and mcap is not None and adv_usd is not None:
        short_ok = int(price >= float(inst.get("short_min_price", 5)) and mcap >= float(inst.get("short_min_market_cap", 3e8)) and adv_usd >= float(inst.get("short_min_adv_usd", 5e6)))
    opt_ok = None
    if "option_chain_cache" in db.tables(conn):
        r = conn.execute("SELECT 1 FROM option_chain_cache WHERE underlying=? LIMIT 1", (ticker,)).fetchone() if "underlying" in db.columns(conn, "option_chain_cache") else None
        opt_ok = 1 if r else None
    sector = (co["sector"] if co and co["sector"] else None) or (conn.execute("SELECT sector FROM kb_entities WHERE entity_id=?", (eid,)).fetchone() or [None])[0]
    from . import pathways
    etf = pathways.SECTOR_ETF.get((sector or "").lower()) if sector else None
    fresh = {"price_age_days": age, "price_limit_days": PRICE_MAX_AGE_DAYS, "mcap_ts": None, "mcap_note": "market capitalisation comes from the companies table and carries no timestamp of its own: treated as current only while the price is" if mcap else "unavailable",
             "series_days": len(closes)}
    vals = (eid, ticker, iso(now), price, price_ts, mcap, iso(now) if mcap and status == "ok" else None, adv_usd, adv_sh, None, (co["vol_daily"] if co and co["vol_daily"] else vol), chg(5), chg(20), chg(60), etf, None,
            liquidity_class(adv_usd, float((cfg.get("risk") or {}).get("min_adv_usd", 1e6))), short_ok, opt_ok, json.dumps(fresh), status)
    last = conn.execute("SELECT id,observed FROM kb_market_snapshots WHERE entity_id=? ORDER BY id DESC LIMIT 1", (eid,)).fetchone()
    if last and last["observed"][:10] == iso(now)[:10]:
        conn.execute("UPDATE kb_market_snapshots SET observed=?,price=?,price_ts=?,mcap=?,mcap_ts=?,adv_usd=?,adv_shares=?,spread_bps=?,vol_daily=?,chg_5d=?,chg_20d=?,chg_60d=?,sector_etf=?,index_membership=?,liquidity_class=?,short_eligible=?,options_eligible=?,freshness_json=?,status=? WHERE id=?", vals[2:] + (last["id"],))
    else:
        conn.execute("INSERT INTO kb_market_snapshots(entity_id,ticker,observed,price,price_ts,mcap,mcap_ts,adv_usd,adv_shares,spread_bps,vol_daily,chg_5d,chg_20d,chg_60d,sector_etf,index_membership,liquidity_class,short_eligible,options_eligible,freshness_json,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
    _cov(conn, eid, "market_status", {"ok": "ok", "expired": "stale", "unavailable": "unavailable"}[status])
    return {"status": status, "price": price, "age_days": age, "liquidity": liquidity_class(adv_usd)}


def current_market(conn, eid: str, now=None) -> Optional[dict]:
    """The latest market snapshot, or None when it has EXPIRED: an expired observation is not a current company fact."""
    r = conn.execute("SELECT * FROM kb_market_snapshots WHERE entity_id=? AND status='ok' ORDER BY id DESC LIMIT 1", (eid,)).fetchone()
    return dict(r) if r else None


# ------------------------------------------------------------------------------------------------ profile
def profile_summary(ent: dict, prof: dict, curated: Optional[dict] = None) -> Optional[Dict[str, str]]:
    """A short, deterministic description from structured facts, or None. Templates only: no free text, no model."""
    name = ent.get("legal_name") or ent.get("entity_id")
    if curated and curated.get("products"):
        prods = ", ".join(curated["products"][:4])
        drivers = (" Revenue is primarily linked to %s." % ", ".join(curated["drivers"][:3])) if curated.get("drivers") else ""
        return {"text": "%s makes or supplies %s.%s" % (name, prods, drivers), "rule": "tmpl:curated_v1"}
    if ent.get("operating") == 0 and ent.get("entity_kind") in ("fund", "spac", "etf", "warrant", "right", "unit", "preferred", "note"):
        kind = {"fund": "a fund or investment vehicle", "spac": "an acquisition shell (a blank-check company with no operating business yet)", "etf": "an exchange-traded product"}.get(ent["entity_kind"], "a non-operating security")
        return {"text": "%s appears to be %s, judged from its name; it is not an operating company." % (name, kind), "rule": "tmpl:instrument_v1"}
    if prof.get("industry") and ent.get("sic"):
        return {"text": "%s is classified by the SEC under SIC %s (%s): %s sector, %s industry. This is a classification, not a description of its products." % (name, ent["sic"], ent.get("sic_description") or "no description", prof.get("sector"), prof["industry"]), "rule": "tmpl:sic_v1"}
    return None


def build_profile(conn, eid: str, now=None) -> dict:
    now = _now(now)
    ent = conn.execute("SELECT * FROM kb_entities WHERE entity_id=?", (eid,)).fetchone()
    if not ent:
        return {"status": "no_entity"}
    ent = dict(ent)
    prof = conn.execute("SELECT * FROM kb_profiles WHERE entity_id=?", (eid,)).fetchone()
    prof = dict(prof) if prof else {}
    products = [r[0] for r in conn.execute("SELECT name FROM kb_products WHERE entity_id=? AND confidence>=0.5 ORDER BY confidence DESC LIMIT 6", (eid,))]
    drivers = [r[0] for r in conn.execute("SELECT subject FROM kb_exposures WHERE entity_id=? AND confidence>=0.5 ORDER BY strength DESC LIMIT 3", (eid,))]
    s = profile_summary(ent, prof, {"products": products, "drivers": drivers} if products else None)
    conn.execute("INSERT OR IGNORE INTO kb_profiles(entity_id,status,updated) VALUES(?,?,?)", (eid, "pending", iso(now)))
    if s:
        conn.execute("UPDATE kb_profiles SET summary=?,summary_rule=?,updated=? WHERE entity_id=?", (s["text"], s["rule"], iso(now), eid))
    status = "ok" if (s and s["rule"] == "tmpl:curated_v1") else "inferred" if s else ("not_applicable" if ent.get("operating") == 0 else "partial" if prof.get("industry") else "pending")
    _cov(conn, eid, "profile_status", status)
    return {"summary": s["text"] if s else None, "rule": s["rule"] if s else None, "status": status}


# ------------------------------------------------------------------------------------------------ seeds: curated relationships and exposures
CORR_TYPES = {"corr", "correlation", "comovement"}
DRIVER_EXPOSURE = {"oil": ("energy_prices", "oil price"), "natgas": ("energy_prices", "natural gas price"), "gold": ("commodity_price", "gold price"), "copper": ("commodity_price", "copper price"), "uranium": ("commodity_price", "uranium price"),
                   "dollar": ("currency", "US dollar"), "rates": ("interest_rates", "interest rates"), "chip_curbs": ("export_controls", "semiconductor export controls"), "conflict": ("geopolitical_conflict", "armed conflict"),
                   "ai_capex": ("ai_capex", "AI capital spending")}
SEED_NOTE = "starter data from public knowledge, shipped with the bot; NOT independently verified: review it"


def _seed_paths(name: str) -> List[str]:
    from . import config
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [os.path.join(config.CONFIG_DIR, name), os.path.join(here, "config", name)]


def _load_seed(name: str) -> Optional[dict]:
    for p in _seed_paths(name):
        if os.path.exists(p):
            try:
                with open(p) as f:
                    d = json.load(f)
                d["_path"] = p
                return d
            except (OSError, ValueError):
                continue
    return None


def _rel_id(src: str, dst: str, typ: str) -> str:
    return "R" + hashlib.sha1(("%s|%s|%s" % (src, dst, typ)).encode()).hexdigest()[:12]


def import_relationship_seed(conn, now=None, seed: Optional[dict] = None) -> dict:
    """relations.json -> kb_relationships (economic ones only). 'supplies' becomes supplier(src of dst); 'competes' competitor; a curated peer group becomes sector_peer pairs. Correlation is NEVER imported as a relationship."""
    now = _now(now)
    seed = seed or _load_seed("relations.json") or {}
    now_s = iso(now)
    n_new = n_skip = 0
    unresolved: List[str] = []

    def put(src_t: str, dst_t: str, typ: str, directional: int, mech: str, strength: float, conf: float, note: str, event_types=None):
        nonlocal n_new, n_skip
        a, b = entity_of(conn, src_t), entity_of(conn, dst_t)
        if not a or not b or a == b:
            for t, e in ((src_t, a), (dst_t, b)):
                if not e and t not in unresolved:
                    unresolved.append(t)
            n_skip += 1
            return
        rid = _rel_id(a, b, typ) if directional else _rel_id(*sorted([a, b]), typ)
        if conn.execute("SELECT 1 FROM kb_relationships WHERE rel_id=?", (rid,)).fetchone():
            conn.execute("UPDATE kb_relationships SET last_verified=? WHERE rel_id=?", (now_s, rid))
            return
        conn.execute("INSERT INTO kb_relationships(rel_id,src_entity,dst_entity,rel_type,directional,mechanism,strength,confidence,source,evidence,event_types_json,last_verified,first_known,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (rid, a, b, typ, directional, mech, strength, conf, "seed:relations.json", "%s; %s" % (note or "curated edge", SEED_NOTE), json.dumps(event_types) if event_types else None, now_s, now_s, "active"))
        conn.execute("INSERT INTO kb_relationship_evidence(rel_id,kind,url,note,source,retrieved) VALUES(?,?,?,?,?,?)", (rid, "curated_seed", None, (note or "")[:300], "relations.json", now_s))
        n_new += 1
    with db.tx(conn):
        for e in seed.get("edges") or []:
            kind = e.get("kind")
            if kind == "supplies":
                put(e["src"], e["dst"], "supplier", 1, "%s supplies %s: %s" % (e["src"], e["dst"], e.get("note") or "a customer-supplier relationship"), float(e.get("weight") or 0.3), 0.5, e.get("note", ""))
            elif kind == "competes":
                put(e["src"], e["dst"], "competitor", 0, "%s and %s compete for the same customers%s" % (e["src"], e["dst"], (": " + e["note"]) if e.get("note") else ""), float(e.get("weight") or 0.3), 0.5, e.get("note", ""))
        for g in seed.get("peer_groups") or []:
            ts = [t for t in g.get("tickers", [])]
            for i in range(len(ts)):
                for j in range(i + 1, len(ts)):
                    put(ts[i], ts[j], "sector_peer", 0, "both belong to the curated peer group '%s' (a classification of what they do, not a statistical link)" % g.get("name"), float(g.get("weight") or 0.2), 0.45, "peer group: %s" % g.get("name"))
    conn.execute("UPDATE knowledge_coverage SET relationship_status='ok' WHERE entity_id IN (SELECT src_entity FROM kb_relationships WHERE status='active' UNION SELECT dst_entity FROM kb_relationships WHERE status='active')")
    return {"imported": n_new, "skipped": n_skip, "unresolved_tickers": sorted(unresolved), "source": (seed or {}).get("_path")}


def import_exposure_seed(conn, now=None, seed: Optional[dict] = None, groups: Optional[Dict[str, List[str]]] = None) -> dict:
    """macro_exposures.json drivers -> kb_exposures: one record per (company, driver) from the curated driver->group channels. dir_fall is the mirror of dir_rise, stated as an assumption in the evidence."""
    from . import reasoning
    now = _now(now)
    seed = seed or _load_seed("macro_exposures.json") or {}
    groups = groups if groups is not None else reasoning.groups()
    now_s = iso(now)
    n_new = 0
    unresolved: List[str] = []
    with db.tx(conn):
        for d in seed.get("drivers") or []:
            etype, subject = DRIVER_EXPOSURE.get(d.get("id"), ("macro_driver", d.get("name") or d.get("id")))
            for ch in d.get("channels") or []:
                for t in groups.get(ch.get("group"), []):
                    eid = entity_of(conn, t)
                    if not eid:
                        if t not in unresolved:
                            unresolved.append(t)
                        continue
                    if conn.execute("SELECT 1 FROM kb_exposures WHERE entity_id=? AND exp_type=? AND subject=? AND channel=? AND source='seed:macro_exposures.json'", (eid, etype, subject, ch.get("channel"))).fetchone():
                        continue
                    sign = float(ch.get("sign") or 0.0)
                    conn.execute("INSERT INTO kb_exposures(entity_id,exp_type,subject,channel,dir_rise,dir_fall,strength,confidence,event_types_json,lag_days,horizon,source,evidence,last_verified,first_known,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (eid, etype, subject, ch.get("channel"), sign, -sign, abs(sign), float(ch.get("confidence") or 0.3), json.dumps(["macro_driver:%s" % d.get("id")]), float(ch.get("lag_days") or 0), ch.get("horizon"),
                                  "seed:macro_exposures.json", "group '%s' -> %s; the fall direction mirrors the rise direction (an assumption of the seed, not separately verified); %s" % (ch.get("group"), ch.get("channel"), SEED_NOTE), now_s, now_s, "ok"))
                    n_new += 1
    conn.execute("UPDATE knowledge_coverage SET exposure_status='ok' WHERE entity_id IN (SELECT entity_id FROM kb_exposures WHERE status='ok')")
    return {"imported": n_new, "unresolved_tickers": sorted(unresolved), "source": (seed or {}).get("_path")}


# ------------------------------------------------------------------------------------------------ per-cycle batch
def snapshot_batch(conn, cfg: dict, now=None, limit: int = 20) -> dict:
    """Bounded: refresh snapshots and profiles for the highest-priority entities whose snapshot is oldest. Never loads the base into memory."""
    now = _now(now)
    rows = conn.execute("SELECT c.entity_id,c.ticker FROM knowledge_coverage c JOIN kb_entities e USING(entity_id) WHERE e.active=1 ORDER BY c.priority, c.market_status='pending' DESC, IFNULL((SELECT MAX(observed) FROM kb_market_snapshots m WHERE m.entity_id=c.entity_id),'') LIMIT ?", (limit,)).fetchall()
    fin = mkt = prof = 0
    with db.tx(conn):
        for r in rows:
            mkt += 1 if market_snapshot(conn, cfg, r["entity_id"], r["ticker"], now)["status"] != "unavailable" else 0
            if financial_snapshot(conn, cfg, r["entity_id"], r["ticker"], now)["status"] in ("ok", "partial", "stale"):
                fin += 1
            build_profile(conn, r["entity_id"], now)
            prof += 1
    return {"entities": len(rows), "with_market": mkt, "with_financials": fin, "profiles_built": prof}
