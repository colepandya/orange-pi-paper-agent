"""Company knowledge base, layers A-C. IDENTITY is the CIK (entity_id), never the ticker: a ticker is an identifier attached to an entity and may change, repeat across share classes or disappear.
Layer A  every company in the live universe: identity, SEC metadata, classification, freshness.   Layer B  holdings, open/queued ideas, recent material events, frequent mentions and their first-hop
neighbours: enrichment first.   Layer C  a small bounded number of incomplete companies per cycle. Heavy full-universe work runs from the command line, never inside the trading cycle.
Rules: nothing is invented (unknown stays unknown, with a status); every row carries source, confidence and a last-verified time; nothing is ever deleted (a company that leaves the universe is closed, not erased)."""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from collections import defaultdict
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import db, kbdata
from .timeutil import iso, parse_ts, utcnow

SOURCE_UNIVERSE = "universe:watchlist.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK%010d.json"
STATUSES = ("ok", "inferred", "partial", "pending", "unavailable", "stale", "unresolved", "not_applicable")
REFRESH_DAYS = {1: 3, 2: 3, 3: 7, 4: 14, 5: 30, 6: 90}                      # how long a successful refresh stays fresh, by priority


class Interrupted(Exception):
    """A bootstrap or refresh stopped part-way (by request or by failure); it can be resumed."""


def _has(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


_SUFFIX = re.compile(r"\b(incorporated|inc|corporation|corp|company|co|ltd|limited|plc|llc|lp|l\.p|holdings?|group|the|sa|nv|ag|se|de cv)\b\.?", re.I)


def norm_name(s: str) -> str:
    """A comparison key for company names: lower-case, no punctuation, no corporate-form words."""
    s = re.sub(r"[^a-z0-9& ]+", " ", (s or "").lower().replace("&amp;", "&"))
    s = _SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def valid_cik(v) -> Optional[int]:
    """A CIK is a positive integer of at most ten digits. Anything else (text, zero, a negative, a number too large to store) is NOT a CIK: the company is then unresolved rather than the base being damaged."""
    try:
        c = int(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return c if 0 < c <= 9_999_999_999 else None


def entity_id_for(cik, ticker: str) -> str:
    c = valid_cik(cik)
    if c:
        return "E%010d" % c
    return "U" + (ticker or "").upper()                                       # no valid CIK: a provisional, clearly marked identity (status 'unresolved')


def entries(universe: Dict[str, dict]) -> List[dict]:
    out = []
    for t in sorted(universe):
        v = universe[t] or {}
        out.append({"ticker": t, "name": v.get("name") or "", "cik": v.get("cik"), "exchange": v.get("exchange"), "aliases": list(v.get("aliases") or []), "sector": v.get("sector"), "verified": bool(v.get("verified"))})
    return out


def _primary_ticker(tickers: List[str]) -> str:
    return sorted(tickers, key=lambda t: (bool(kbdata.TICKER_CLASS_RX.search(t)), len(t), t))[0]


# ------------------------------------------------------------------------------------------------ reconciliation
def reconcile(universe: Dict[str, dict], previous: Optional[Dict[str, dict]] = None, conn=None) -> dict:
    """The report that must come BEFORE ingestion: what the universe actually contains. Pure; reads the universe (and, if given, the previous snapshot and the knowledge base)."""
    rows = entries(universe)
    n = len(rows)
    norm_t: Dict[str, List[str]] = defaultdict(list)
    for r in rows:
        norm_t[re.sub(r"[.\-/ ]", "", r["ticker"].upper())].append(r["ticker"])
    dup_norm = {k: v for k, v in norm_t.items() if len(v) > 1}
    by_cik: Dict[int, List[str]] = defaultdict(list)
    no_cik = []
    for r in rows:
        c = valid_cik(r["cik"]) or 0
        if c > 0:
            by_cik[c].append(r["ticker"])
        else:
            no_cik.append(r["ticker"])
    multi = {c: ts for c, ts in by_cik.items() if len(ts) > 1}
    by_name: Dict[str, set] = defaultdict(set)
    for r in rows:
        k = norm_name(r["name"])
        if k and valid_cik(r["cik"]):
            by_name[k].add(valid_cik(r["cik"]))
    similar = {k: sorted(v) for k, v in by_name.items() if len(v) > 1}
    kinds: Dict[str, List[str]] = defaultdict(list)
    adr, foreign, shareclass = [], [], []
    for r in rows:
        c = kbdata.classify_instrument(r["name"], r["ticker"])
        kinds[c["kind"]].append(r["ticker"])
        if c["adr"]:
            adr.append(r["ticker"])
        if c["foreign_hint"]:
            foreign.append(r["ticker"])
        if c["share_class"]:
            shareclass.append(r["ticker"])
    dropped = renamed = added = []
    if previous is not None:
        dropped = sorted(set(previous) - set(universe))
        added = sorted(set(universe) - set(previous))
        renamed = sorted(t for t in set(previous) & set(universe) if norm_name((previous[t] or {}).get("name", "")) != norm_name((universe[t] or {}).get("name", "")))
        moved = sorted(t for t in set(previous) & set(universe) if (previous[t] or {}).get("cik") != (universe[t] or {}).get("cik"))
    else:
        moved = []
    non_operating = [t for k in ("fund", "etf", "warrant", "right", "unit", "preferred", "note", "spac") for t in kinds.get(k, [])]
    return {
        "universe_rows": n, "unique_tickers": len(universe), "duplicate_tickers_after_normalisation": {k: v for k, v in sorted(dup_norm.items())},
        "missing_cik": sorted(no_cik), "unresolved_companies": sorted(no_cik), "multiple_tickers_one_issuer": {str(c): ts for c, ts in sorted(multi.items())}, "issuers_with_similar_names": {k: v for k, v in sorted(similar.items())},
        "instrument_kinds": {k: len(v) for k, v in sorted(kinds.items())}, "non_operating_instruments": sorted(non_operating), "etfs": sorted(kinds.get("etf", [])), "funds": sorted(kinds.get("fund", [])), "spacs": sorted(kinds.get("spac", [])),
        "warrants_rights_units_preferred_notes": sorted(t for k in ("warrant", "right", "unit", "preferred", "note") for t in kinds.get(k, [])),
        "adr_by_name": sorted(adr), "foreign_hint_by_name": sorted(foreign), "foreign_hint_note": "a name suffix (plc, SA, AG ...) is only a hint; real incorporation country comes from SEC submissions during refresh", "share_class_instruments": sorted(shareclass),
        "dropped_since_previous_snapshot": dropped, "added_since_previous_snapshot": added, "renamed_since_previous_snapshot": renamed, "cik_changed_since_previous_snapshot": moved,
        "no_usable_sec_structured_data": {"known_now": [], "note": "unknown until each company's SEC submissions and facts are fetched; funds, SPAC shells and similar instruments are expected to have little or none"},
        "likely_no_sec_financials": sorted(set(non_operating)),
    }


# ------------------------------------------------------------------------------------------------ bootstrap (identity from the live universe)
def _start_run(conn, kind: str, params: dict, now, resumed_from=None) -> int:
    cur = conn.execute("INSERT INTO knowledge_refresh_runs(kind,started,status,params_json,resumed_from) VALUES(?,?,?,?,?)", (kind, iso(now), "running", json.dumps(params, sort_keys=True, default=str), resumed_from))
    return cur.lastrowid


def _finish_run(conn, rid: int, status: str, processed: int, failed: int, cursor, now, notes: str = "") -> None:
    conn.execute("UPDATE knowledge_refresh_runs SET finished=?,status=?,processed=?,failed=?,cursor=?,notes=? WHERE id=?", (iso(now), status, processed, failed, cursor, notes[:2000], rid))


def last_unfinished(conn, kind: str) -> Optional[dict]:
    r = conn.execute("SELECT * FROM knowledge_refresh_runs WHERE kind=? AND status IN ('running','interrupted') ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
    return dict(r) if r else None


def _upsert(conn, e: dict, cl: dict, now_s: str, legal_name: Optional[str] = None) -> str:
    """One universe entry -> entity, ticker identifier, CIK identifier, aliases. Returns 'new' | 'changed' | 'same'."""
    t, name = e["ticker"], (legal_name if legal_name else e["name"])
    eid = entity_id_for(e["cik"], t)
    cik = valid_cik(e["cik"])
    status = "identity_only" if cik else "unresolved"
    conf = (0.9 if e["verified"] else 0.6) if cik else 0.2
    row = conn.execute("SELECT legal_name,version,source FROM kb_entities WHERE entity_id=?", (eid,)).fetchone()
    outcome = "same"
    sec_owned = bool(row and (row["source"] or "").startswith("sec:"))
    if row is None:
        sect = e["sector"] if e["sector"] and e["sector"] != "Unclassified" else None
        conn.execute("INSERT INTO kb_entities(entity_id,cik,legal_name,entity_kind,security_type,adr,operating,active,exchange,sector,valid_from,first_seen,last_verified,source,source_record,confidence,knowledge_status,version,updated) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (eid, cik, name, cl["kind"], cl["security_type"], cl["adr"], cl["operating"], 1, e["exchange"], sect, now_s, now_s, now_s, SOURCE_UNIVERSE,
                                                                       "cik:%s|ticker:%s" % (cik, t), conf, status, 1, now_s))
        outcome = "new"
    else:
        if (row["legal_name"] or "") != name and not sec_owned:
            if row["legal_name"]:
                _alias(conn, eid, row["legal_name"], "former", now_s, SOURCE_UNIVERSE, 0.7, valid_to=now_s)
            conn.execute("UPDATE kb_entities SET legal_name=?,version=version+1,updated=? WHERE entity_id=?", (name, now_s, eid))
            outcome = "changed"
        conn.execute("UPDATE kb_entities SET last_verified=?,active=1,valid_to=NULL WHERE entity_id=?", (now_s, eid))
    ident = conn.execute("SELECT id FROM kb_identifiers WHERE kind='ticker' AND value=? AND valid_to IS NULL", (t,)).fetchone()
    if ident is None:
        conn.execute("INSERT INTO kb_identifiers(entity_id,kind,value,exchange,security_type,share_class,adr,is_primary,valid_from,first_seen,last_verified,source,source_record,confidence,flags_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (eid, "ticker", t, e["exchange"], cl["security_type"], cl["share_class"], cl["adr"], 0, now_s, now_s, now_s, SOURCE_UNIVERSE, "cik:%s" % cik, conf, json.dumps({"flags": cl["flags"], "rules": cl["rules"]})))
        outcome = outcome if outcome != "same" else "changed"
    else:
        conn.execute("UPDATE kb_identifiers SET entity_id=?,last_verified=?,exchange=? WHERE id=?", (eid, now_s, e["exchange"], ident["id"]))
    if cik and conn.execute("SELECT 1 FROM kb_identifiers WHERE kind='cik' AND value=?", (str(cik),)).fetchone() is None:
        conn.execute("INSERT INTO kb_identifiers(entity_id,kind,value,valid_from,first_seen,last_verified,source,source_record,confidence) VALUES(?,?,?,?,?,?,?,?,?)", (eid, "cik", str(cik), now_s, now_s, now_s, SOURCE_UNIVERSE, "ticker:%s" % t, conf))
    _alias(conn, eid, name, "legal", now_s, SOURCE_UNIVERSE, conf)
    for a in e["aliases"]:
        _alias(conn, eid, a, "alt", now_s, SOURCE_UNIVERSE, min(conf, 0.6))
    return outcome


def _alias(conn, eid: str, alias: str, kind: str, now_s: str, source: str, conf: float, valid_to: Optional[str] = None) -> None:
    n = norm_name(alias)
    if not n or len(n) < 2:
        return
    if conn.execute("SELECT 1 FROM kb_aliases WHERE entity_id=? AND alias_norm=? AND kind=?", (eid, n, kind)).fetchone() is None:
        conn.execute("INSERT INTO kb_aliases(entity_id,alias,alias_norm,kind,valid_from,valid_to,source,confidence,last_verified) VALUES(?,?,?,?,?,?,?,?,?)", (eid, alias[:200], n, kind, now_s, valid_to, source, conf, now_s))


def bootstrap(conn, universe: Dict[str, dict], now=None, previous: Optional[Dict[str, dict]] = None, dry_run: bool = False, batch: int = 500, max_batches: Optional[int] = None, resume: bool = True) -> dict:
    """Identity for EVERY universe entry (none removed, none added, none replaced). Idempotent and resumable: a run that stops part-way continues after its last committed ticker."""
    now = now or utcnow()
    now_s = iso(now)
    rows = entries(universe)
    plan = {"universe_rows": len(rows), "with_cik": sum(1 for r in rows if valid_cik(r["cik"])), "dry_run": dry_run}
    if dry_run:
        known = {r[0] for r in conn.execute("SELECT value FROM kb_identifiers WHERE kind='ticker' AND valid_to IS NULL")} if _has(conn, "kb_identifiers") else set()
        plan.update({"would_add": sum(1 for r in rows if r["ticker"] not in known), "would_keep": sum(1 for r in rows if r["ticker"] in known), "would_close": len(known - set(universe))})
        return plan
    prev_run = last_unfinished(conn, "bootstrap") if resume else None
    cursor = prev_run["cursor"] if prev_run and prev_run.get("cursor") else None
    if prev_run:
        conn.execute("UPDATE knowledge_refresh_runs SET status='superseded' WHERE id=?", (prev_run["id"],))
    rid = _start_run(conn, "bootstrap", {"universe_rows": len(rows), "batch": batch}, now, resumed_from=prev_run["id"] if prev_run else None)
    conn.commit()
    todo = [r for r in rows if cursor is None or r["ticker"] > cursor]
    counts = {"new": 0, "changed": 0, "same": 0}
    done, nb, last = 0, 0, cursor
    by_cik: Dict[int, List[str]] = defaultdict(list)
    for r_ in rows:
        if valid_cik(r_["cik"]):
            by_cik[valid_cik(r_["cik"])].append(r_["ticker"])
    primary_name = {c_: universe[_primary_ticker(ts)].get("name") or "" for c_, ts in by_cik.items()}      # a company has ONE legal name: its primary ticker's, never whichever class was read last
    try:
        for i in range(0, len(todo), batch):
            if max_batches is not None and nb >= max_batches:
                raise Interrupted("stopped after %d batch(es) as requested" % nb)
            b_done, b_last, b_counts = 0, last, {"new": 0, "changed": 0, "same": 0}
            with db.tx(conn):
                for e in todo[i:i + batch]:
                    cl = kbdata.classify_instrument(e["name"], e["ticker"])
                    cik_i = valid_cik(e["cik"]) or 0
                    b_counts[_upsert(conn, e, cl, now_s, primary_name.get(cik_i))] += 1
                    b_last = e["ticker"]
                    b_done += 1
                conn.execute("UPDATE knowledge_refresh_runs SET processed=?,cursor=? WHERE id=?", (done + b_done, b_last, rid))
            done, last = done + b_done, b_last                                               # only AFTER the batch committed: a rolled-back batch must never move the cursor
            for k_, v_ in b_counts.items():
                counts[k_] += v_
            nb += 1
    except Interrupted as ex:
        _finish_run(conn, rid, "interrupted", done, 0, last, now, str(ex))
        conn.commit()
        return dict(plan, status="interrupted", processed=done, cursor=last, **counts)
    except Exception as ex:  # noqa: BLE001 - leave a resumable record, then re-raise
        conn.rollback()
        _finish_run(conn, rid, "interrupted", done, 1, last, now, "%s: %s" % (type(ex).__name__, ex))
        conn.commit()
        raise
    with db.tx(conn):
        closed = _close_missing(conn, universe, now_s)
        _finalise(conn, now_s)
        _finish_run(conn, rid, "ok", done, 0, last, now, "closed %d identifiers no longer in the universe" % closed)
    return dict(plan, status="ok", processed=done, closed_identifiers=closed, **counts)


def _close_missing(conn, universe: Dict[str, dict], now_s: str) -> int:
    """A ticker that left the universe is CLOSED (valid_to set), never deleted: old decisions still refer to it."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _uni(t TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _uni")
    conn.executemany("INSERT OR IGNORE INTO _uni VALUES(?)", [(t,) for t in universe])
    n = conn.execute("UPDATE kb_identifiers SET valid_to=? WHERE kind='ticker' AND valid_to IS NULL AND value NOT IN (SELECT t FROM _uni)", (now_s,)).rowcount
    conn.execute("UPDATE kb_entities SET active=0,valid_to=? WHERE active=1 AND entity_id NOT IN (SELECT entity_id FROM kb_identifiers WHERE kind='ticker' AND valid_to IS NULL)", (now_s,))
    conn.execute("DROP TABLE IF EXISTS _uni")
    return n


def _finalise(conn, now_s: str) -> None:
    """Primary ticker per entity, alias collision flags, and a coverage row for every active entity."""
    ents = conn.execute("SELECT entity_id FROM kb_entities WHERE active=1").fetchall()
    for r in ents:
        ts = [x[0] for x in conn.execute("SELECT value FROM kb_identifiers WHERE entity_id=? AND kind='ticker' AND valid_to IS NULL", (r[0],))]
        if not ts:
            continue
        prim = _primary_ticker(ts)
        conn.execute("UPDATE kb_identifiers SET is_primary=(value=?) WHERE entity_id=? AND kind='ticker' AND valid_to IS NULL", (prim, r[0]))
        cov = conn.execute("SELECT 1 FROM knowledge_coverage WHERE entity_id=?", (r[0],)).fetchone()
        ent = conn.execute("SELECT knowledge_status,entity_kind,operating FROM kb_entities WHERE entity_id=?", (r[0],)).fetchone()
        unresolved = ent["knowledge_status"] == "unresolved"
        fin = "not_applicable" if ent["operating"] == 0 else "pending"
        if cov is None:
            conn.execute("INSERT INTO knowledge_coverage(entity_id,ticker,layer,identity_status,profile_status,financial_status,market_status,exposure_status,relationship_status,filings_status,completeness,priority,updated) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (r[0], prim, "A", "unresolved" if unresolved else "ok", "pending", fin, "pending", "unavailable", "unavailable", "pending" if not ent["operating"] == 0 else "not_applicable", 0.0, 6, now_s))
        else:
            conn.execute("UPDATE knowledge_coverage SET ticker=?,updated=? WHERE entity_id=?", (prim, now_s, r[0]))
    conn.execute("UPDATE kb_aliases SET collision=0")
    conn.execute("UPDATE kb_aliases SET collision=1 WHERE valid_to IS NULL AND alias_norm IN (SELECT alias_norm FROM kb_aliases WHERE valid_to IS NULL GROUP BY alias_norm HAVING COUNT(DISTINCT entity_id)>1)")
    recompute_completeness(conn)


def recompute_completeness(conn) -> None:
    w = {"ok": 1.0, "inferred": 0.6, "partial": 0.5, "stale": 0.4}
    rows = conn.execute("SELECT entity_id,identity_status,profile_status,financial_status,market_status,exposure_status,relationship_status,filings_status FROM knowledge_coverage").fetchall()
    for r in rows:
        dom = [r[k] for k in ("identity_status", "profile_status", "financial_status", "market_status", "exposure_status", "relationship_status", "filings_status") if r[k] != "not_applicable"]
        c = sum(w.get(s, 0.0) for s in dom) / max(len(dom), 1)
        conn.execute("UPDATE knowledge_coverage SET completeness=? WHERE entity_id=?", (round(c, 3), r[0]))


def verify_universe(conn, universe: Dict[str, dict]) -> List[str]:
    """Every universe ticker must have exactly one open ticker identifier, an active entity and a coverage row; and nothing outside the universe may be open. [] means the universe is preserved exactly."""
    problems = []
    open_ids = defaultdict(list)
    for r in conn.execute("SELECT value,entity_id FROM kb_identifiers WHERE kind='ticker' AND valid_to IS NULL"):
        open_ids[r[0]].append(r[1])
    for t in universe:
        if len(open_ids.get(t, [])) != 1:
            problems.append("%s has %d open identifiers" % (t, len(open_ids.get(t, []))))
    extra = sorted(set(open_ids) - set(universe))
    problems += ["%s is open but not in the universe" % t for t in extra[:50]]
    n_cov = conn.execute("SELECT COUNT(DISTINCT entity_id) FROM knowledge_coverage").fetchone()[0]
    n_ent = len({e for ids in open_ids.values() for e in ids})
    if n_cov < n_ent:
        problems.append("coverage rows (%d) fewer than entities with open tickers (%d)" % (n_cov, n_ent))
    return problems


# ------------------------------------------------------------------------------------------------ priorities
def assign_priorities(conn, now=None) -> dict:
    """1 holdings, 2 open/queued ideas, 3 recent material events, 4 frequently mentioned, 5 first-hop neighbours, 6 the rest. Layer B = 1-5, Layer A = 6. Bounded set queries; nothing is loaded wholesale."""
    now = now or utcnow()
    P: Dict[str, int] = {}

    def put(ts: Iterable[str], p: int) -> None:
        for t in ts:
            if t and (t not in P or p < P[t]):
                P[t] = p
    if _has(conn, "positions"):
        put([r[0] for r in conn.execute("SELECT DISTINCT ticker FROM positions WHERE status='OPEN'")], 1)
    if _has(conn, "option_positions"):
        put([r[0] for r in conn.execute("SELECT DISTINCT underlying FROM option_positions WHERE status='OPEN'")], 1)
    since3 = iso(now - dt.timedelta(days=3))
    if _has(conn, "decisions"):
        put([r[0] for r in conn.execute("SELECT DISTINCT ticker FROM decisions WHERE created>=? AND status IN ('AWAITING_MARKET','AWAITING_QUOTE','AWAITING_CAPACITY','CAPACITY_DEFERRED','HELD','PENDING')", (since3,))], 2)
    if _has(conn, "candidate_queue"):
        try:
            put([r[0] for r in conn.execute("SELECT DISTINCT ticker FROM candidate_queue")], 2)
        except Exception:  # noqa: BLE001 - schema differences must not stop prioritisation
            pass
    mention: Dict[str, int] = defaultdict(int)
    material = set()
    if _has(conn, "events"):
        for r in conn.execute("SELECT tickers_json,magnitude FROM events WHERE created>=?", (iso(now - dt.timedelta(days=30)),)):
            try:
                ts = json.loads(r[0] or "[]")
            except ValueError:
                continue
            recent = r[1] is not None and abs(r[1]) >= 0.5
            for t in ts[:6]:
                mention[t] += 1
                if recent:
                    material.add(t)
        put([t for t in material if mention[t] >= 1], 3)
        put([t for t, n in sorted(mention.items(), key=lambda kv: -kv[1])[:300] if n >= 3], 4)
    top = [t for t, p in P.items() if p <= 4]
    nb: set = set()
    for t in top[:400]:
        if _has(conn, "relations"):
            nb.update(r[0] for r in conn.execute("SELECT dst FROM relations WHERE src=? AND kind!='corr'", (t,)))
            nb.update(r[0] for r in conn.execute("SELECT src FROM relations WHERE dst=? AND kind!='corr'", (t,)))
    put(nb, 5)
    conn.execute("UPDATE knowledge_coverage SET priority=6,layer='A'")
    for t, p in P.items():
        conn.execute("UPDATE knowledge_coverage SET priority=?,layer='B' WHERE ticker=? OR entity_id IN (SELECT entity_id FROM kb_identifiers WHERE kind='ticker' AND value=? AND valid_to IS NULL)", (p, t, t))
    conn.commit()
    return {"by_priority": {p: sum(1 for v in P.values() if v == p) for p in range(1, 6)}, "layer_b": len(P)}


# ------------------------------------------------------------------------------------------------ SEC submissions refresh
def parse_submissions(js: dict) -> Optional[dict]:
    """The identity facts in an SEC submissions record. Only what is present is returned; nothing is filled in."""
    if not isinstance(js, dict) or not js.get("cik"):
        return None
    try:
        cik = int(js["cik"])
    except (TypeError, ValueError):
        return None
    former = []
    for f in js.get("formerNames") or []:
        if isinstance(f, dict) and f.get("name"):
            former.append({"name": str(f["name"])[:200], "from": str(f.get("from") or "")[:10] or None, "to": str(f.get("to") or "")[:10] or None})
    addr = ((js.get("addresses") or {}).get("business") or {}) if isinstance(js.get("addresses"), dict) else {}
    rec = (js.get("filings") or {}).get("recent") or {}
    filings = []
    for i, acc in enumerate(rec.get("accessionNumber") or []):
        try:
            filings.append({"accession": acc, "form": (rec.get("form") or [])[i], "filed": (rec.get("filingDate") or [])[i], "period": (rec.get("reportDate") or [""] * 9999)[i] or None,
                            "items": (rec.get("items") or [""] * 9999)[i] or "", "doc": (rec.get("primaryDocument") or [""] * 9999)[i] or ""})
        except IndexError:
            break
    return {"cik": cik, "name": js.get("name"), "sic": str(js.get("sic") or "") or None, "sic_description": js.get("sicDescription") or None, "state_inc": js.get("stateOfIncorporation") or None,
            "fiscal_year_end": js.get("fiscalYearEnd") or None, "entity_type": js.get("entityType") or None, "tickers": [str(t).upper() for t in js.get("tickers") or []], "exchanges": js.get("exchanges") or [],
            "former_names": former, "business_state_or_country": addr.get("stateOrCountry") or None, "business_city": addr.get("city") or None, "filings": filings}


def apply_submissions(conn, eid: str, sub: dict, now_s: str) -> dict:
    """Write what the SEC says. The classification (sector, industry, traits) is derived from the SIC code by fixed tables and marked INFERRED."""
    cl = kbdata.classify_sic(sub.get("sic"))
    operating = None
    if sub.get("entity_type"):
        operating = 1 if str(sub["entity_type"]).lower() == "operating" else 0
    if cl and cl.get("non_operating"):
        operating = 0
    src = "sec:submissions CIK%010d" % sub["cik"]
    with db.tx(conn):
        conn.execute("UPDATE kb_entities SET legal_name=COALESCE(?,legal_name),state_inc=?,fiscal_year_end=?,sic=?,sic_description=?,sector=COALESCE(?,sector),industry=COALESCE(?,industry),"
                     "operating=COALESCE(?,operating),country=COALESCE(?,country),source=?,source_record=?,confidence=?,knowledge_status='sec_identity',last_verified=?,updated=?,version=version+1 WHERE entity_id=?",
                     (sub.get("name"), sub.get("state_inc"), sub.get("fiscal_year_end"), sub.get("sic"), sub.get("sic_description"), cl["sector"] if cl else None, cl["industry"] if cl else None, operating,
                      "US" if (sub.get("state_inc") and len(str(sub["state_inc"])) == 2 and str(sub["state_inc"]).upper() in _US_STATES) else None, src, "accession-list:%d recent" % len(sub.get("filings") or []), 0.95, now_s, now_s, eid))
        for f in sub.get("former_names") or []:
            _alias(conn, eid, f["name"], "former", f.get("from") or now_s, src, 0.9, valid_to=f.get("to"))
        if sub.get("name"):
            _alias(conn, eid, sub["name"], "legal", now_s, src, 0.95)
        prof = traits = None
        if cl:
            traits = kbdata.traits_for(cl["sector"], cl["industry"])
        conn.execute("INSERT OR IGNORE INTO kb_profiles(entity_id,status,updated) VALUES(?,?,?)", (eid, "pending", now_s))
        conn.execute("UPDATE kb_profiles SET sector=?,industry=?,cyclicality=?,capital_intensity=?,regulatory_intensity=?,gov_dependency=?,revenue_model=?,regions_json=?,status=?,confidence=?,source=?,evidence_json=?,last_verified=?,updated=? WHERE entity_id=?",
                     (cl["sector"] if cl else None, cl["industry"] if cl else None, traits["cyclicality"] if traits else None, traits["capital_intensity"] if traits else None, traits["regulatory_intensity"] if traits else None,
                      traits["gov_dependency"] if traits else None, traits["revenue_model"] if traits else None,
                      json.dumps([{"region": sub["business_state_or_country"], "basis": "business address in the SEC record: a headquarters location, not a list of operating regions"}]) if sub.get("business_state_or_country") else None,
                      "inferred" if cl else "partial", 0.4 if cl else 0.3, "rule:%s;%s" % (kbdata.SIC_MAP_VERSION, kbdata.TRAITS_VERSION),
                      json.dumps({"sic": sub.get("sic"), "sic_description": sub.get("sic_description"), "classification_rule": cl["rule"] if cl else None, "traits_rule": traits["rule"] if traits else None, "non_operating": cl.get("non_operating") if cl else None}), now_s, now_s, eid))
        conn.execute("UPDATE knowledge_coverage SET profile_status=?,identity_status='ok' WHERE entity_id=?", ("inferred" if cl else "partial", eid))
    return {"sic": sub.get("sic"), "classified": bool(cl), "operating": operating}


_US_STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR GU VI".split())


def sec_headers(cfg: dict) -> dict:
    contact = ((cfg.get("runtime") or {}).get("contact_email") or "").strip()
    return {"User-Agent": "paper-agent research %s" % (contact or "contact-not-set@example.com"), "Accept": "application/json"}


def select_due(conn, now, limit: int, only: Optional[List[str]] = None) -> List[dict]:
    q = ("SELECT c.entity_id,c.ticker,c.priority,c.failures,e.cik FROM knowledge_coverage c JOIN kb_entities e ON e.entity_id=c.entity_id WHERE e.active=1 AND e.cik IS NOT NULL "
         "AND (c.next_due IS NULL OR c.next_due<=?) %s ORDER BY c.priority, CASE WHEN c.last_ok IS NULL THEN 0 ELSE 1 END, IFNULL(c.next_due,''), c.entity_id LIMIT ?")
    if only:
        ph = ",".join("?" * len(only))
        return [dict(r) for r in conn.execute(q % ("AND c.ticker IN (%s)" % ph), [iso(now)] + list(only) + [limit])]
    return [dict(r) for r in conn.execute(q % "", (iso(now), limit))]


def _backoff_until(conn) -> Optional[dt.datetime]:
    r = conn.execute("SELECT value FROM meta WHERE key='kb_sec_backoff_until'").fetchone()
    try:
        return parse_ts(r[0]) if r and r[0] else None
    except ValueError:
        return None


def refresh(conn, cfg: dict, now, fetch_json: Callable, limit: Optional[int] = None, dry_run: bool = False, only: Optional[List[str]] = None, sleep: Callable = time.sleep, clock: Callable = time.monotonic) -> dict:
    """A bounded batch of SEC submissions refreshes. NEVER raises into the trading cycle: any failure is recorded, backed off and reported. 403/429 are TEMPORARY source failures that pause the source."""
    from . import net
    k = (cfg.get("kb") or {})
    if not k.get("enabled", True):
        return {"status": "disabled"}
    limit = int(limit if limit is not None else k.get("refresh_companies_per_cycle", 12))
    now_dt = now
    until = _backoff_until(conn)
    if until and until > now_dt and not dry_run:
        return {"status": "source_backoff", "until": iso(until), "processed": 0}
    due = select_due(conn, now_dt, limit, only)
    if dry_run:
        return {"status": "dry_run", "would_refresh": [d["ticker"] for d in due], "count": len(due)}
    rid = _start_run(conn, "refresh", {"limit": limit, "only": only}, now_dt)
    conn.commit()
    t0, net_budget = clock(), float(k.get("max_network_seconds", 20))
    gap = 1.0 / max(1.0, float(k.get("sec_requests_per_second", 5)))
    hdr = sec_headers(cfg)
    ok = fail = 0
    stopped = ""
    errors: List[str] = []
    for d in due:
        if clock() - t0 > net_budget:
            stopped = "network time budget (%.0fs) used" % net_budget
            break
        eid, t = d["entity_id"], d["ticker"]
        try:
            js, _ = fetch_json(SUBMISSIONS_URL % int(d["cik"]), headers=hdr, timeout=20)
            sub = parse_submissions(js)
            if not sub:
                raise ValueError("the SEC record could not be read")
            apply_submissions(conn, eid, sub, iso(now_dt))
            try:
                from . import filingintel
                filingintel.ingest_submissions(conn, eid, t, sub, now_dt, int(k.get("filing_window_days", 120)), int(k.get("filing_rows_per_company", 60)))
            except Exception as ex:  # noqa: BLE001 - filing events are an extra, never a reason to lose the identity refresh
                errors.append("%s filings: %s" % (t, type(ex).__name__))
            days = REFRESH_DAYS.get(int(d["priority"] or 6), 90)
            days = min(days, int(k.get("max_data_age_days", 90))) if d["priority"] == 6 else days
            conn.execute("UPDATE knowledge_coverage SET last_attempt=?,last_ok=?,failures=0,last_error=NULL,next_due=? WHERE entity_id=?", (iso(now_dt), iso(now_dt), iso(now_dt + dt.timedelta(days=days)), eid))
            conn.commit()
            ok += 1
        except net.FetchError as ex:
            code = getattr(ex, "code", None)
            fail += 1
            if code in (403, 429) or (isinstance(code, int) and code >= 500):
                n = int((conn.execute("SELECT value FROM meta WHERE key='kb_sec_backoff_n'").fetchone() or ["0"])[0] or 0) + 1
                wait = min(float(k.get("retry_delay_hours", 6)) * (2 ** (n - 1)), float(k.get("failure_backoff_max_hours", 72)))
                conn.execute("INSERT OR REPLACE INTO meta VALUES('kb_sec_backoff_until',?)", (iso(now_dt + dt.timedelta(hours=wait)),))
                conn.execute("INSERT OR REPLACE INTO meta VALUES('kb_sec_backoff_n',?)", (str(n),))
                _fail(conn, eid, now_dt, k, "SEC temporarily refused (%s)" % code)
                conn.commit()
                stopped = "SEC answered %s: the source is paused for %.0f hours (a temporary failure, not an error in the bot)" % (code, wait)
                break
            if code == 404:
                conn.execute("UPDATE kb_entities SET knowledge_status='no_sec_data',last_verified=? WHERE entity_id=?", (iso(now_dt), eid))
                conn.execute("UPDATE knowledge_coverage SET last_attempt=?,failures=failures+1,last_error='no SEC submissions record (404)',next_due=?,filings_status='unavailable',financial_status=CASE WHEN financial_status='pending' THEN 'unavailable' ELSE financial_status END WHERE entity_id=?",
                             (iso(now_dt), iso(now_dt + dt.timedelta(days=30)), eid))
            else:
                _fail(conn, eid, now_dt, k, "fetch failed (%s)" % (code or type(ex).__name__))
            conn.commit()
        except Exception as ex:  # noqa: BLE001
            fail += 1
            _fail(conn, eid, now_dt, k, "%s: %s" % (type(ex).__name__, str(ex)[:120]))
            conn.commit()
        sleep(gap)
    if ok and not stopped:
        conn.execute("INSERT OR REPLACE INTO meta VALUES('kb_sec_backoff_n','0')")
    recompute_completeness(conn)
    _finish_run(conn, rid, "ok" if not stopped else "stopped", ok, fail, None, now_dt, stopped + ("; " + "; ".join(errors[:5]) if errors else ""))
    conn.commit()
    return {"status": "ok" if not stopped else "stopped", "refreshed": ok, "failed": fail, "stopped": stopped, "due_remaining": len(select_due(conn, now_dt, 100000)) if not only else None, "seconds": round(clock() - t0, 2)}


def _fail(conn, eid: str, now_dt, k: dict, msg: str) -> None:
    n = (conn.execute("SELECT failures FROM knowledge_coverage WHERE entity_id=?", (eid,)).fetchone() or [0])[0] + 1
    wait = min(float(k.get("retry_delay_hours", 6)) * (2 ** (n - 1)), float(k.get("failure_backoff_max_hours", 72)))
    conn.execute("UPDATE knowledge_coverage SET last_attempt=?,failures=?,last_error=?,next_due=? WHERE entity_id=?", (iso(now_dt), n, msg[:200], iso(now_dt + dt.timedelta(hours=wait)), eid))


# ------------------------------------------------------------------------------------------------ reports
def coverage_report(conn, universe: Optional[Dict[str, dict]] = None) -> dict:
    g = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    n_ent = g("SELECT COUNT(*) FROM kb_entities WHERE active=1")
    n_tick = g("SELECT COUNT(*) FROM kb_identifiers WHERE kind='ticker' AND valid_to IS NULL")
    out = {"universe_rows": len(universe) if universe is not None else None, "open_ticker_identifiers": n_tick, "active_entities": n_ent, "entities_with_cik": g("SELECT COUNT(*) FROM kb_entities WHERE active=1 AND cik IS NOT NULL"),
           "entities_without_cik": g("SELECT COUNT(*) FROM kb_entities WHERE active=1 AND cik IS NULL"), "closed_entities": g("SELECT COUNT(*) FROM kb_entities WHERE active=0"),
           "closed_ticker_identifiers": g("SELECT COUNT(*) FROM kb_identifiers WHERE kind='ticker' AND valid_to IS NOT NULL"),
           "entities_with_sec_submissions": g("SELECT COUNT(*) FROM kb_entities WHERE active=1 AND knowledge_status='sec_identity'"), "entities_with_no_sec_record": g("SELECT COUNT(*) FROM kb_entities WHERE active=1 AND knowledge_status='no_sec_data'"),
           "with_sic": g("SELECT COUNT(*) FROM kb_entities WHERE active=1 AND sic IS NOT NULL"), "with_fiscal_year_end": g("SELECT COUNT(*) FROM kb_entities WHERE active=1 AND fiscal_year_end IS NOT NULL"),
           "with_state_of_incorporation": g("SELECT COUNT(*) FROM kb_entities WHERE active=1 AND state_inc IS NOT NULL"),
           "companies_with_profiles": g("SELECT COUNT(*) FROM knowledge_coverage WHERE profile_status IN ('ok','inferred')"), "companies_with_financial_facts": g("SELECT COUNT(DISTINCT entity_id) FROM kb_financial_snapshots WHERE status IN ('ok','partial')"),
           "companies_with_current_market_data": g("SELECT COUNT(DISTINCT entity_id) FROM kb_market_snapshots WHERE status='ok'"), "companies_with_exposure_records": g("SELECT COUNT(DISTINCT entity_id) FROM kb_exposures WHERE status='ok' OR status IS NULL"),
           "companies_with_relationships": g("SELECT COUNT(*) FROM (SELECT src_entity AS e FROM kb_relationships WHERE status='active' UNION SELECT dst_entity FROM kb_relationships WHERE status='active')"),
           "relationships_by_type": {r[0]: r[1] for r in conn.execute("SELECT rel_type,COUNT(*) FROM kb_relationships WHERE status='active' GROUP BY rel_type ORDER BY 2 DESC")},
           "by_layer": {r[0]: r[1] for r in conn.execute("SELECT layer,COUNT(*) FROM knowledge_coverage GROUP BY layer")}, "by_priority": {str(r[0]): r[1] for r in conn.execute("SELECT priority,COUNT(*) FROM knowledge_coverage GROUP BY priority ORDER BY 1")},
           "status_by_domain": {d: {r[0]: r[1] for r in conn.execute("SELECT %s,COUNT(*) FROM knowledge_coverage GROUP BY 1" % d)} for d in ("identity_status", "profile_status", "financial_status", "market_status", "exposure_status", "relationship_status", "filings_status")},
           "mean_completeness": round(g("SELECT IFNULL(AVG(completeness),0) FROM knowledge_coverage"), 3), "unresolved_aliases": g("SELECT COUNT(DISTINCT alias_norm) FROM kb_aliases WHERE collision=1 AND valid_to IS NULL"),
           "refresh_failures_open": g("SELECT COUNT(*) FROM knowledge_coverage WHERE failures>0")}
    if universe is not None:
        out["universe_preserved"] = not verify_universe(conn, universe)
    return out


def stale_report(conn, cfg: dict, now=None) -> dict:
    now = now or utcnow()
    k = cfg.get("kb") or {}
    old = iso(now - dt.timedelta(days=float(k.get("max_data_age_days", 90))))
    return {"identity_not_verified_since": old, "never_refreshed_from_sec": conn.execute("SELECT COUNT(*) FROM knowledge_coverage c JOIN kb_entities e USING(entity_id) WHERE e.active=1 AND e.cik IS NOT NULL AND c.last_ok IS NULL").fetchone()[0],
            "overdue": conn.execute("SELECT COUNT(*) FROM knowledge_coverage WHERE last_ok IS NOT NULL AND next_due<?", (iso(now),)).fetchone()[0],
            "stale_financials": conn.execute("SELECT COUNT(*) FROM knowledge_coverage WHERE financial_status='stale'").fetchone()[0], "stale_market": conn.execute("SELECT COUNT(*) FROM knowledge_coverage WHERE market_status='stale'").fetchone()[0]}


def unresolved_report(conn) -> dict:
    return {"no_cik": [dict(r) for r in conn.execute("SELECT entity_id,ticker FROM knowledge_coverage WHERE identity_status='unresolved' ORDER BY ticker")],
            "no_sec_record": [r[0] for r in conn.execute("SELECT c.ticker FROM knowledge_coverage c JOIN kb_entities e USING(entity_id) WHERE e.knowledge_status='no_sec_data' ORDER BY 1")],
            "alias_collisions": [{"alias_norm": r[0], "entities": r[1]} for r in conn.execute("SELECT alias_norm,COUNT(DISTINCT entity_id) FROM kb_aliases WHERE collision=1 AND valid_to IS NULL GROUP BY alias_norm ORDER BY 2 DESC LIMIT 200")],
            "repeated_refresh_failures": [{"ticker": r[0], "failures": r[1], "error": r[2]} for r in conn.execute("SELECT ticker,failures,last_error FROM knowledge_coverage WHERE failures>=3 ORDER BY failures DESC LIMIT 100")]}


def provenance_report(conn) -> dict:
    out = {}
    for tbl, extra in (("kb_relationships", "status='active'"), ("kb_exposures", "1=1")):
        out[tbl] = {"by_source": {r[0] or "unknown": r[1] for r in conn.execute("SELECT source,COUNT(*) FROM %s WHERE %s GROUP BY source ORDER BY 2 DESC" % (tbl, extra))},
                    "without_evidence": conn.execute("SELECT COUNT(*) FROM %s WHERE %s AND (evidence IS NULL OR evidence='')" % (tbl, extra)).fetchone()[0],
                    "without_last_verified": conn.execute("SELECT COUNT(*) FROM %s WHERE %s AND last_verified IS NULL" % (tbl, extra)).fetchone()[0],
                    "total": conn.execute("SELECT COUNT(*) FROM %s WHERE %s" % (tbl, extra)).fetchone()[0]}
    out["kb_relationships"]["without_mechanism"] = conn.execute("SELECT COUNT(*) FROM kb_relationships WHERE status='active' AND (mechanism IS NULL OR mechanism='')").fetchone()[0]
    out["kb_relationships"]["correlation_typed"] = conn.execute("SELECT COUNT(*) FROM kb_relationships WHERE rel_type IN ('corr','correlation','comovement')").fetchone()[0]
    return out


# ------------------------------------------------------------------------------------------------ universe quality and coverage (what the data actually supports; nothing here says "complete")
_REIT_HINT = re.compile(r"\b(?:REIT|REALTY|REAL ESTATE|PROPERTIES|PROPERTY TRUST|APARTMENT (?:COMMUNITIES|INVESTMENT)|RESIDENTIAL (?:TRUST|PROPERTIES)|STORAGE (?:TRUST|PROPERTIES))\b", re.I)


def universe_quality(universe: Dict[str, dict], previous: Optional[Dict[str, dict]] = None) -> dict:
    """What the live universe is made of. Every classification here comes from the NAME (the universe file carries a name, an exchange and a CIK): hints with their limits stated, to be confirmed by the SEC submissions refresh."""
    rep = reconcile(universe, previous)
    rows = entries(universe)
    reits = sorted(r["ticker"] for r in rows if _REIT_HINT.search(r["name"]) and kbdata.classify_instrument(r["name"], r["ticker"])["kind"] == "operating_equity")
    kinds = rep["instrument_kinds"]
    by_exch: Dict[str, int] = defaultdict(int)
    for r in rows:
        by_exch[str(r["exchange"])] += 1
    multi_issuers = {c: ts for c, ts in rep["multiple_tickers_one_issuer"].items()}
    return {"instruments": rep["universe_rows"], "operating_companies_by_name": kinds.get("operating_equity", 0), "closed_end_funds_by_name": kinds.get("fund", 0), "spac_shells_by_name": kinds.get("spac", 0),
            "etfs_by_name": kinds.get("etf", 0), "warrants_rights_units_preferred_notes": len(rep["warrants_rights_units_preferred_notes"]), "adr_by_name": len(rep["adr_by_name"]), "foreign_name_suffix_hint": len(rep["foreign_hint_by_name"]),
            "reit_by_name_hint": len(reits), "reit_tickers_sample": reits[:12], "issuers_with_multiple_tickers": len(multi_issuers), "tickers_in_multi_ticker_issuers": sum(len(v) for v in multi_issuers.values()),
            "share_class_instruments_by_name_or_ticker": len(rep["share_class_instruments"]), "missing_cik": len(rep["missing_cik"]), "missing_cik_tickers": rep["missing_cik"], "duplicate_tickers_after_normalisation": len(rep["duplicate_tickers_after_normalisation"]),
            "issuer_name_collisions": len(rep["issuers_with_similar_names"]), "unresolved_classification": {"count": rep["universe_rows"] - kinds.get("operating_equity", 0) + 0, "meaning": "not an operating company by name, OR (for every instrument) SIC/sector not yet fetched from the SEC; run: kb refresh"},
            "no_sec_sector_yet": rep["universe_rows"], "exchange_mix": dict(by_exch), "additions_since_previous": rep["added_since_previous_snapshot"], "removals_since_previous": rep["dropped_since_previous_snapshot"],
            "renamed_since_previous": len(rep["renamed_since_previous_snapshot"]), "cik_changed_since_previous": rep["cik_changed_since_previous_snapshot"], "previous_rows": len(previous) if previous else None}


def universe_check(sec_payload: dict, tickers: List[str], watch: Dict[str, dict]) -> dict:
    """Why is a ticker in or out of the universe? Compares the SEC's current company_tickers_exchange file with the live universe, judging each ticker by the package's own filter (universe.build): present in the SEC file, its exchange, its shape, whether
    the filter keeps it, and whether it is in the live universe. Offline-testable: pass the payload; on the Pi the command fetches it."""
    from . import universe as U
    fields = sec_payload["fields"]
    ix = {f: fields.index(f) for f in ("cik", "name", "ticker", "exchange")}
    sec = {}
    for row in sec_payload["data"]:
        sec.setdefault(str(row[ix["ticker"]]).upper(), []).append({"cik": row[ix["cik"]], "name": row[ix["name"]], "exchange": row[ix["exchange"]]})
    built = U.build(sec_payload)
    out: Dict[str, object] = {"sec_rows": len(sec_payload["data"]), "kept_by_the_package_filter": len(built), "live_universe": len(watch), "in_both": len(set(built) & set(watch)), "kept_but_not_in_live": len(set(built) - set(watch)),
                              "live_but_not_kept_by_filter": len(set(watch) - set(built)), "exchange_labels_in_sec_file": sorted({str(r[ix["exchange"]]) for r in sec_payload["data"]}), "exchange_labels_in_live_universe": sorted({str(v.get("exchange")) for v in watch.values()}),
                              "package_filter_accepts_exchanges": sorted(U.EXCH)}
    rows = {}
    for t in tickers:
        T = t.upper()
        hit = sec.get(T) or sec.get(T.replace(".", "-")) or sec.get(T.replace("-", "."))
        why = []
        if not hit:
            why.append("not in the SEC file (it lists no row for this ticker)")
        else:
            h = hit[0]
            if h["exchange"] not in U.EXCH:
                why.append("exchange label %r is not in the package filter's list" % h["exchange"])
            if not re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", T.replace(".", "-")):
                why.append("ticker shape rejected by the filter")
        same_cik = sorted(k for k, v in watch.items() if hit and valid_cik(hit[0]["cik"]) and valid_cik(v.get("cik")) == valid_cik(hit[0]["cik"]))
        rows[T] = {"in_sec_file": bool(hit), "sec_row": hit[0] if hit else None, "kept_by_package_filter": T in built, "in_live_universe": T in watch, "other_live_tickers_with_the_same_cik": same_cik, "reasons_the_package_filter_would_drop_it": why}
    excluded: Dict[str, List[str]] = defaultdict(list)
    for t_, hits_ in sec.items():
        ex_ = str(hits_[0]["exchange"])
        if ex_ not in U.EXCH and re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", t_):
            excluded[ex_].append(t_)
    out["excluded_by_exchange_label"] = {k: {"count": len(v), "sample": sorted(v)[:12]} for k, v in sorted(excluded.items())}
    out["exchange_note"] = "Rows on these exchanges are NOT in the universe because the filter does not accept the label. Adding one (for example TXSE) changes what the bot may trade; check first that your quote providers carry those names."
    out["tickers"] = rows
    out["live_universe_tickers_absent_from_sec_file"] = sorted(t for t in watch if t not in sec and t.replace(".", "-") not in sec)[:60]
    return out


CATEGORIES = [("identity (an entity exists)", "SELECT COUNT(*) FROM kb_entities WHERE active=1"), ("CIK resolved", "SELECT COUNT(*) FROM kb_entities WHERE active=1 AND cik IS NOT NULL"),
              ("instrument kind known from the name", "SELECT COUNT(*) FROM kb_entities WHERE active=1 AND entity_kind IS NOT NULL"), ("SEC classification (SIC code) fetched", "SELECT COUNT(*) FROM kb_entities WHERE active=1 AND sic IS NOT NULL"),
              ("sector/industry assigned", "SELECT COUNT(*) FROM kb_entities WHERE active=1 AND industry IS NOT NULL"), ("business profile with a summary", "SELECT COUNT(*) FROM kb_profiles p JOIN kb_entities e USING(entity_id) WHERE e.active=1 AND p.summary IS NOT NULL"),
              ("SEC financial facts (reliable or partial)", "SELECT COUNT(DISTINCT s.entity_id) FROM kb_financial_snapshots s JOIN kb_entities e USING(entity_id) WHERE e.active=1 AND s.status IN ('ok','partial')"),
              ("current market data (price not expired)", "SELECT COUNT(DISTINCT m.entity_id) FROM kb_market_snapshots m JOIN kb_entities e USING(entity_id) WHERE e.active=1 AND m.status='ok'"),
              ("exposure records", "SELECT COUNT(DISTINCT x.entity_id) FROM kb_exposures x JOIN kb_entities e USING(entity_id) WHERE e.active=1"),
              ("economic relationships", "SELECT COUNT(*) FROM (SELECT src_entity e FROM kb_relationships WHERE status='active' UNION SELECT dst_entity FROM kb_relationships WHERE status='active')"),
              ("recent filing events", "SELECT COUNT(DISTINCT f.entity_id) FROM kb_filing_events f JOIN kb_entities e USING(entity_id) WHERE e.active=1")]


def coverage_summary(conn) -> dict:
    """Coverage by data category, as counts and shares of the active entities. Nothing here is 'complete': a row existing is not knowledge."""
    total = conn.execute("SELECT COUNT(*) FROM kb_entities WHERE active=1").fetchone()[0]
    op = conn.execute("SELECT COUNT(*) FROM kb_entities WHERE active=1 AND (operating IS NULL OR operating=1)").fetchone()[0]
    rows = []
    for name, sql in CATEGORIES:
        n = conn.execute(sql).fetchone()[0]
        rows.append({"category": name, "entities": n, "share_of_all_entities": round(n / total, 4) if total else None, "share_of_operating_companies": round(min(n, op) / op, 4) if op else None})
    return {"active_entities": total, "operating_or_unclassified": op, "categories": rows, "caution": "These are counts of entities with at least one record in each category. A bootstrapped identity is NOT a profile, a financial history or a relationship."}


def refresh_facts(conn, cfg: dict, now, fetch_json: Callable, limit: int = 10, budget_s: Optional[float] = None, dry_run: bool = False, sleep: Callable = time.sleep, clock: Callable = time.monotonic) -> dict:
    """SEC financial facts for the highest-priority OPERATING companies that have none (about 20 requests each, so a handful per run, never the whole universe). Same safety as the submissions refresh:
    paused on 403/429, bounded by a time budget, and it never raises."""
    from . import facts, net, kbsnap
    k = (cfg.get("kb") or {})
    if not k.get("enabled", True) or int(limit) <= 0:
        return {"status": "disabled" if not k.get("enabled", True) else "off", "refreshed": 0}
    until = _backoff_until(conn)
    if until and until > now and not dry_run:
        return {"status": "source_backoff", "until": iso(until), "refreshed": 0}
    rows = [dict(r) for r in conn.execute(
        "SELECT c.entity_id,c.ticker,e.cik FROM knowledge_coverage c JOIN kb_entities e ON e.entity_id=c.entity_id WHERE e.active=1 AND e.cik IS NOT NULL AND (e.operating IS NULL OR e.operating=1) "
        "AND c.financial_status IN ('pending','stale','unavailable') AND (c.facts_next_due IS NULL OR c.facts_next_due<=?) ORDER BY c.priority, c.entity_id LIMIT ?", (iso(now), int(limit)))]
    if dry_run:
        return {"status": "dry_run", "would_fetch_facts_for": [r["ticker"] for r in rows], "count": len(rows)}
    t0, budget = clock(), float(budget_s if budget_s is not None else k.get("max_network_seconds", 20))
    cfg2 = dict(cfg, runtime=dict(cfg.get("runtime") or {}))
    ok = fail = 0
    stopped = ""
    for r in rows:
        if clock() - t0 > budget:
            stopped = "network time budget (%.0fs) used" % budget
            break
        try:
            facts.refresh(conn, cfg2, r["ticker"], r["cik"], lambda url, **kw: fetch_json(url, headers=dict(sec_headers(cfg), **(kw.get("headers") or {})), timeout=kw.get("timeout", 20)), now, sleep)
            kbsnap.financial_snapshot(conn, cfg, r["entity_id"], r["ticker"], now)
            conn.execute("UPDATE knowledge_coverage SET facts_next_due=? WHERE entity_id=?", (iso(now + dt.timedelta(days=int(k.get("financial_max_age_days", 150)) // 2)), r["entity_id"]))
            conn.commit()
            ok += 1
        except net.FetchError as ex:
            code = getattr(ex, "code", None)
            fail += 1
            if code in (403, 429) or (isinstance(code, int) and code >= 500):
                wait = float(k.get("retry_delay_hours", 6))
                conn.execute("INSERT OR REPLACE INTO meta VALUES('kb_sec_backoff_until',?)", (iso(now + dt.timedelta(hours=wait)),))
                conn.commit()
                stopped = "SEC answered %s: the source is paused for %.0f hours (a temporary failure, not an error in the bot)" % (code, wait)
                break
            conn.execute("UPDATE knowledge_coverage SET facts_next_due=? WHERE entity_id=?", (iso(now + dt.timedelta(days=7)), r["entity_id"]))
            conn.commit()
        except Exception:  # noqa: BLE001 - never into the caller
            fail += 1
            conn.execute("UPDATE knowledge_coverage SET facts_next_due=? WHERE entity_id=?", (iso(now + dt.timedelta(days=7)), r["entity_id"]))
            conn.commit()
    return {"status": "stopped" if stopped else "ok", "refreshed": ok, "failed": fail, "stopped": stopped, "seconds": round(clock() - t0, 2)}


def prune_filings(conn, cfg: dict) -> dict:
    """One pass over the stored filing events: routine note supplements removed, every company cut to the retention policy (material events always kept). Idempotent; run once automatically after an upgrade and on demand (kb prune)."""
    from . import filingintel
    k = cfg.get("kb") or {}
    cap_total, before = int(k.get("filing_rows_per_company", 60)), conn.execute("SELECT COUNT(*) FROM kb_filing_events").fetchone()[0]
    routine = conn.execute("DELETE FROM kb_filing_events WHERE form IN (%s)" % ",".join("?" * len(filingintel.ROUTINE_FORMS)), sorted(filingintel.ROUTINE_FORMS)).rowcount
    pruned = 0
    for r in conn.execute("SELECT entity_id FROM kb_filing_events GROUP BY entity_id HAVING COUNT(*) > 12").fetchall():
        pruned += filingintel.prune(conn, r[0], cap_total, 12)
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM kb_filing_events").fetchone()[0]
    return {"before": before, "routine_note_supplements_removed": routine, "pruned_to_policy": pruned, "after": after, "largest_company_now": (conn.execute("SELECT MAX(n) FROM (SELECT COUNT(*) n FROM kb_filing_events GROUP BY entity_id)").fetchone()[0] or 0)}
