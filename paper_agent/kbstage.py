"""The knowledge base's share of each trading cycle: small, bounded and failure-proof. Every sub-step is isolated (an error in one is recorded, never raised), the whole stage has a time budget, and nothing here
changes a trade in the default observe modes. Heavy work (a full SEC refresh of the universe) runs from the command line, never here."""
from __future__ import annotations

import json
import time
from typing import Callable, Dict, Optional

from . import db, kb, kbevent, kbmeasure, kbrel, kbregime, kbsnap, filingintel
from .timeutil import iso, utcnow

CATALYST_FLOOR = 0.45                # events below this magnitude are not given lifecycle, materiality or propagation records (they are not catalysts)


def _step(out: dict, name: str, fn: Callable, clock: Callable, budget_end: float):
    if clock() > budget_end:
        out.setdefault("skipped_for_time", []).append(name)
        return None
    t0 = clock()
    try:
        r = fn()
        out[name] = r if r is not None else "ok"
        return r
    except Exception as ex:  # noqa: BLE001 - a knowledge-base step must never reach the trading path
        out.setdefault("errors", []).append("%s: %s: %s" % (name, type(ex).__name__, str(ex)[:120]))
        return None
    finally:
        out.setdefault("seconds", {})[name] = round(clock() - t0, 3)


def sync_identity(conn, universe_watch: dict, now) -> dict:
    """Keep the knowledge base's identity layer equal to the live universe. Cheap (a fraction of a second for the whole universe) and idempotent: it only runs when the universe differs from what is stored."""
    n_open = conn.execute("SELECT COUNT(*) FROM kb_identifiers WHERE kind='ticker' AND valid_to IS NULL").fetchone()[0]
    if n_open == len(universe_watch) and not kb.verify_universe(conn, universe_watch):
        return {"synced": False, "universe_rows": len(universe_watch)}
    r = kb.bootstrap(conn, universe_watch, now)
    kb.assign_priorities(conn, now)
    return {"synced": True, "new": r.get("new"), "closed": r.get("closed_identifiers"), "universe_rows": len(universe_watch)}


def run(conn, cfg: dict, universe_watch: dict, now=None, fetch_json: Optional[Callable] = None, clock: Callable = time.monotonic, sleep: Callable = time.sleep) -> dict:
    now = now or utcnow()
    k = cfg.get("kb") or {}
    if not k.get("enabled", True) or not universe_watch:
        return {"status": "disabled"}
    budget_end = clock() + float(k.get("max_db_seconds", 5)) + float(k.get("max_network_seconds", 20)) + 5.0
    out: dict = {"status": "ok"}
    _step(out, "identity", lambda: sync_identity(conn, universe_watch, now), clock, budget_end)
    _step(out, "rules", lambda: kbevent.seed_rules(conn), clock, budget_end)
    if db.due(conn, "kb_seeds", 24, now):
        _step(out, "seeds", lambda: {"relationships": kbsnap.import_relationship_seed(conn, now)["imported"], "exposures": kbsnap.import_exposure_seed(conn, now)["imported"]}, clock, budget_end)
        db.mark_run(conn, "kb_seeds", "", now)
    if db.due(conn, "kb_priorities", 6, now):
        _step(out, "priorities", lambda: kb.assign_priorities(conn, now), clock, budget_end)
        db.mark_run(conn, "kb_priorities", "", now)
    if fetch_json is not None and int(k.get("refresh_companies_per_cycle", 12)) > 0:
        _step(out, "sec_refresh", lambda: kb.refresh(conn, cfg, now, fetch_json, clock=clock, sleep=sleep), clock, budget_end)
    _step(out, "snapshots", lambda: kbsnap.snapshot_batch(conn, cfg, now, limit=20), clock, budget_end)
    if (k.get("regime_mode", "observe") != "off"):
        _step(out, "regime", lambda: [r["regime_id"] for r in kbregime.detect(conn, now)["detected"]], clock, budget_end)
    _step(out, "events", lambda: process_new_events(conn, cfg, now, clock, budget_end), clock, budget_end)
    _step(out, "outcomes", lambda: kbmeasure.measure_due(conn, cfg, now), clock, budget_end)
    conn.commit()
    if out.get("errors"):
        out["status"] = "partial"
    return out


def _meta_get(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def process_new_events(conn, cfg: dict, now, clock: Callable, budget_end: float, max_events: Optional[int] = None) -> dict:
    """New stored events since the last pass, each a catalyst candidate: lifecycle (expiry), economic materiality, relationship propagation; then the event state of the companies involved. Bounded per cycle."""
    from . import reasoning as R
    k = cfg.get("kb") or {}
    last = int(_meta_get(conn, "kb_last_event_id", "0") or 0)
    if last == 0:
        last = max(0, (conn.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0) - 400)          # first run: the most recent few hundred only, never the whole history
    cap = int(max_events or k.get("propagate_events_per_cycle", 40))
    rows = conn.execute("SELECT id,event_type,direction,magnitude,headline,published,created,tickers_json,amount_usd FROM events WHERE id>? AND tickers_json!='[]' ORDER BY id LIMIT ?", (last, cap * 6)).fetchall()
    done = lc_n = mat_n = prop_n = 0
    touched = set()
    top = last
    for r in rows:
        top = r["id"]
        if abs(r["magnitude"] or 0) < CATALYST_FLOOR or r["event_type"] in kbevent.NON_CATALYST:
            continue
        if clock() > budget_end or done >= cap:
            top = r["id"] - 1
            break
        done += 1
        try:
            tickers = json.loads(r["tickers_json"] or "[]")[:3]
        except ValueError:
            continue
        for t in tickers:
            if not kbsnap.entity_of(conn, t):
                continue
            st = R.event_state(r["headline"] or "")
            kbevent.lifecycle(conn, cfg, {"event_id": r["id"], "ticker": t, "event_type": r["event_type"], "state": st, "text": r["headline"], "published": r["published"], "created": r["created"]}, now, persist=True)
            lc_n += 1
            if (k.get("materiality_mode", "observe") != "off"):
                mat_n += len(kbevent.materiality_for(conn, cfg, {"event_id": r["id"], "ticker": t, "event_type": r["event_type"], "text": r["headline"], "amount_usd": r["amount_usd"]}, now, persist=True))
            if (k.get("relationship_mode", "observe") != "off") and r["event_type"] not in kbrel.CORR_TYPES:
                imp = kbrel.propagate(conn, cfg, {"event_id": r["id"], "ticker": t, "event_type": r["event_type"], "direction": r["direction"] or 0.0, "magnitude": r["magnitude"], "state": "confirmed" if st in ("reported", "agreed", "completed") else st,
                                                  "industry_wide": False, "source_role": "subject", "headline": r["headline"]}, now, persist=True)
                prop_n += len(imp)
            touched.add((t, r["id"]))
    states = 0
    if (k.get("state_mode", "observe") != "off"):
        for t, ev_id in list(touched)[:cap]:
            if clock() > budget_end:
                break
            st = kbevent.build_state(conn, cfg, t, now)
            kbevent.save_state(conn, t, st, ev_id, now)
            states += 1
    conn.execute("INSERT OR REPLACE INTO meta VALUES('kb_last_event_id',?)", (str(top),))
    return {"events_examined": len(rows), "catalysts": done, "lifecycles": lc_n, "materiality_rows": mat_n, "impacts": prop_n, "states_updated": states, "cursor": top}
