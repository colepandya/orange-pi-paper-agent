"""ONE explanation, two doors. `case_file` assembles everything the bot knows about a company (and, when asked about a decision, what it knew AT THE TIME, side by side with what it knows now); `render` turns it into the
page shown by BOTH Decisions -> Why and Intelligence -> Ask 'why TICKER'. Strictly READ-ONLY: every function it calls is used with persistence off, and the dashboard runs it on a read-only connection.
Each section says how complete it is and exactly what is missing: an absent fact is shown as absent, never filled in."""
from __future__ import annotations

import html
import json
from typing import Dict, List, Optional

from . import db, kbevent, kbregime, kbrel, kbsnap, filingintel
from .timeutil import iso, parse_ts, to_nz, utcnow

E = lambda x: html.escape("" if x is None else str(x), quote=True)  # noqa: E731
SECTION_ORDER = ["verdict", "company", "event", "chain", "relationships", "redteam", "regime", "facts", "missing", "then_now", "provenance"]
TITLES = {"verdict": "Verdict", "company": "What the company is", "event": "Why the event matters", "chain": "The company's event chain", "relationships": "Related companies", "redteam": "The red team",
          "regime": "Market regime", "facts": "Facts used", "missing": "What is missing", "then_now": "Then versus now", "provenance": "Where this comes from"}


KIND_WORDS = {"operating_equity": "operating company (judged from its name until the SEC record is fetched)", "fund": "closed-end fund or investment vehicle (judged from its name)", "spac": "acquisition shell / blank-check company (judged from its name)",
              "etf": "exchange-traded product", "warrant": "warrant", "right": "right", "unit": "unit", "preferred": "preferred share", "note": "debt note"}


def _has(conn, t: str) -> bool:
    return t in db.tables(conn)


def _j(s, d=None):
    try:
        return json.loads(s) if s else d
    except (ValueError, TypeError):
        return d


def _sec(key: str, status: str, summary: str, rows=None, items=None, missing=None, extra=None) -> dict:
    return {"key": key, "title": TITLES[key], "status": status, "summary": summary, "rows": rows or [], "items": items or [], "missing": missing or [], "extra": extra or {}}


def _fmt_money(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "unknown"
    a = abs(v)
    return "%s$%.2fT" % ("-" if v < 0 else "", a / 1e12) if a >= 1e12 else "%s$%.1fB" % ("-" if v < 0 else "", a / 1e9) if a >= 1e9 else "%s$%.1fM" % ("-" if v < 0 else "", a / 1e6) if a >= 1e6 else "%s$%.0f" % ("-" if v < 0 else "", a)


def case_file(conn, cfg: dict, ticker: Optional[str] = None, decision_id: Optional[int] = None, now=None) -> dict:
    now = now or utcnow()
    d = None
    if decision_id:
        r = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        d = dict(r) if r else None
        if d:
            ticker = ticker or d.get("ticker")
    ticker = (ticker or "").upper()
    when = d["created"] if d else iso(now)
    eid = kbsnap.entity_of(conn, ticker) if _has(conn, "kb_identifiers") and ticker else None
    ent = dict(conn.execute("SELECT * FROM kb_entities WHERE entity_id=?", (eid,)).fetchone() or {}) if eid else {}
    cov = dict(conn.execute("SELECT * FROM knowledge_coverage WHERE entity_id=?", (eid,)).fetchone() or {}) if eid else {}
    ev = None
    ev_id = (d or {}).get("event_id")
    if ev_id:
        r = conn.execute("SELECT * FROM events WHERE id=?", (ev_id,)).fetchone()
        ev = dict(r) if r else None
    if ev is None and ticker and _has(conn, "events"):
        r = conn.execute("SELECT * FROM events WHERE tickers_json LIKE ? AND created<=? AND event_type NOT IN ('general_news','opinion_piece','price_recap') ORDER BY created DESC LIMIT 1", ('%"' + ticker + '"%', when)).fetchone()
        ev = dict(r) if r else None
    sections: Dict[str, dict] = {}
    missing: List[str] = []

    # ---- company
    prof = dict(conn.execute("SELECT * FROM kb_profiles WHERE entity_id=?", (eid,)).fetchone() or {}) if eid else {}
    if not eid:
        sections["company"] = _sec("company", "missing", "%s is not in the company knowledge base." % (ticker or "No company"), missing=["the company is not in the live universe, or the knowledge base has not been built yet (run: kb bootstrap)"])
        missing.append("company identity")
    else:
        rows = [["Name", ent.get("legal_name") or "unknown"], ["Ticker(s)", ", ".join(r[0] for r in conn.execute("SELECT value FROM kb_identifiers WHERE entity_id=? AND kind='ticker' AND valid_to IS NULL ORDER BY is_primary DESC", (eid,)))],
                ["CIK", ent.get("cik") or "unresolved"], ["Instrument", "%s%s%s" % (KIND_WORDS.get(ent.get("entity_kind"), ent.get("entity_kind") or "unknown"), " (ADR)" if ent.get("adr") else "", "" if ent.get("operating") != 0 else ", not an operating company")],
                ["Sector / industry", "%s / %s" % (ent.get("sector") or prof.get("sector") or "unknown", ent.get("industry") or prof.get("industry") or "unknown")], ["SIC", ("%s %s" % (ent["sic"], ent.get("sic_description") or "")).strip() if ent.get("sic") else "unknown (not yet fetched from the SEC)"],
                ["State of incorporation", ent.get("state_inc") or "unknown"], ["Fiscal year end", ent.get("fiscal_year_end") or "unknown"], ["Exchange", ent.get("exchange") or "unknown"]]
        if prof.get("cyclicality"):
            rows += [["Cyclicality (inferred from the industry)", prof["cyclicality"]], ["Capital intensity (inferred)", prof.get("capital_intensity") or "unknown"], ["Regulatory intensity (inferred)", prof.get("regulatory_intensity") or "unknown"]]
        miss = [k for k, v in (("SIC code", ent.get("sic")), ("state of incorporation", ent.get("state_inc")), ("business summary", prof.get("summary"))) if not v]
        sections["company"] = _sec("company", "complete" if not miss else "partial", prof.get("summary") or "No business summary is available: the summary is built only from structured facts, never guessed.", rows=rows, missing=miss,
                                   extra={"summary_rule": prof.get("summary_rule"), "profile_status": prof.get("status"), "knowledge_status": ent.get("knowledge_status"), "last_verified": ent.get("last_verified")})
        missing += miss

    # ---- event
    if ev:
        lc = _j(None)
        row = conn.execute("SELECT * FROM event_lifecycle WHERE event_id=? AND ticker=? ORDER BY id DESC LIMIT 1", (ev["id"], ticker)).fetchone() if _has(conn, "event_lifecycle") else None
        from . import reasoning as R
        st = R.event_state(ev.get("headline") or "")
        lcd = kbevent.lifecycle(conn, cfg, {"event_id": ev["id"], "ticker": ticker, "event_type": ev["event_type"], "state": st, "text": ev.get("headline"), "published": ev.get("published"), "created": ev.get("created")}, parse_ts(when), persist=False)
        mats = [dict(m) for m in conn.execute("SELECT * FROM event_materiality WHERE event_id=? AND ticker=? ORDER BY id", (ev["id"], ticker))] if _has(conn, "event_materiality") else []
        if not mats:
            mats = kbevent.materiality_for(conn, cfg, {"event_id": ev["id"], "ticker": ticker, "event_type": ev["event_type"], "text": ev.get("headline"), "amount_usd": ev.get("amount_usd")}, parse_ts(when), as_of=when, persist=False)
        rows = [["Headline", ev.get("headline") or ""], ["Type", (ev["event_type"] or "").replace("_", " ")], ["How the story stands", st], ["Direction / size (engine)", "%+.2f / %.2f" % (ev.get("direction") or 0, ev.get("magnitude") or 0)],
                ["Published", to_nz(ev.get("published") or ev.get("created"), "%d %b %Y %H:%M")], ["Allowed to stay tradable", "%.0f hours (rule %s)" % (lcd["hours_allowed"], lcd["rule_id"])], ["Expiry (event-aware)", to_nz(lcd["effective_expiry"], "%d %b %H:%M") + (" &mdash; EXPIRED" if lcd["eventaware_stale"] else "")],
                ["Flat 36-hour rule says", "stale" if lcd["generic_stale"] else "still live"]]
        mrows = []
        for m in mats:
            if m.get("result") is None:
                mrows.append([m["kind"].replace("_", " "), "unknown", "; ".join(_j(m.get("missing_json") or json.dumps(m.get("missing")), []) if not isinstance(m.get("missing"), list) else m["missing"]) or "inputs missing"])
            else:
                mrows.append([m["kind"].replace("_", " "), "%.2f%% of the denominator (%s)" % (m["result"] * 100, m["band"]), (m.get("calc") or "")[:90]])
        unknown = sum(1 for m in mats if m.get("result") is None)
        sections["event"] = _sec("event", "partial" if unknown else "complete", lcd["explanation"], rows=rows + [["Materiality: " + a, b + " &mdash; " + c] for a, b, c in mrows], missing=["materiality: %s" % m["kind"].replace("_", " ") for m in mats if m.get("result") is None],
                                 extra={"modifiers": lcd["modifiers"]})
        if unknown:
            missing.append("materiality for %d of %d calculation(s)" % (unknown, len(mats)))
    else:
        sections["event"] = _sec("event", "missing", "No recent catalyst event is stored for %s." % ticker, missing=["no company-specific event found before this time"])

    # ---- chain (company event state)
    cur = _j((conn.execute("SELECT state_json FROM company_event_states WHERE ticker=?", (ticker,)).fetchone() or [None])[0]) if _has(conn, "company_event_states") and ticker else None
    st_then = kbevent.state_as_of(conn, ticker, when) if _has(conn, "company_event_state_history") and ticker else None
    live = kbevent.build_state(conn, cfg, ticker, parse_ts(iso(now)) if not d else parse_ts(when), until=when) if ticker and _has(conn, "events") else None
    use = st_then or live
    if use and use.get("items"):
        items = ["%s &mdash; %s%s%s" % (E(i["event_type"].replace("_", " ")), E(i["status"]), (" (%s)" % E(i["note"])) if i["note"] else "", " &mdash; expired" if i["expired"] else "") + "<br><span class=mut>%s</span>" % E(i["headline"]) for i in use["items"][:8]]
        sections["chain"] = _sec("chain", "complete", "Near term: %s. Medium term: %s.%s" % (use["near_term"]["label"], use["medium_term"]["label"], " The two point in different directions." if use.get("diverging") else ""), items=items,
                                 rows=[["Net direction", {1.0: "positive", -1.0: "negative"}.get(use["net_direction"], "none / contested")], ["Catalyst expiry", to_nz(use["catalyst_expiry"], "%d %b %H:%M") if use.get("catalyst_expiry") else "none live"],
                                       ["Denials / confirmations / corrections", "%d / %d / %d" % (len(use["denials"]), len(use["confirmations"]), len(use["corrections"]))], ["Unresolved", str(len(use["unresolved"]))]] + [["Would end it", x] for x in use["invalidation"][:3]],
                                 extra={"source": "stored at the time" if st_then else "rebuilt from the event history up to this moment"})
    else:
        sections["chain"] = _sec("chain", "missing", "No other catalyst events for %s in the last 30 days." % ticker, missing=["no event history"])

    # ---- relationships
    rel_rows, impacts = [], []
    if eid and _has(conn, "kb_relationships"):
        for nb in kbrel.neighbours(conn, eid, when, cap=14):
            rel_rows.append([kbrel._ticker(conn, nb["neighbour"], (nb.get("mechanism") or "") + " " + (nb.get("evidence") or "")), nb["role"].replace("_", " "), "%.2f" % (nb.get("strength") or 0), nb.get("mechanism") or "", "%s, known since %s" % (nb.get("source"), (nb.get("first_known") or "?")[:10])])
        if ev and _has(conn, "kb_derived_impacts"):
            impacts = [dict(r) for r in conn.execute("SELECT * FROM kb_derived_impacts WHERE event_id=? ORDER BY hops, id LIMIT 24", (ev["id"],))]
    if rel_rows:
        its = []
        for i in impacts:
            if i["abstain_reason"]:
                its.append("<b>%s</b>: abstains &mdash; %s" % (E(i["dst_ticker"]), E(i["abstain_reason"])))
            else:
                its.append("<b>%s</b> hop %d (%s): %s %s, confidence %.2f &mdash; %s <span class=mut>[rule %s]</span>" % (E(i["dst_ticker"]), i["hops"], E(i["influence_class"]), "up" if i["direction"] > 0 else "down", "", i["direction_conf"] or 0, E(i["mechanism"]), E(i["rule_id"])))
        sections["relationships"] = _sec("relationships", "complete" if impacts else "partial", "%d recorded relationship(s); %d effect(s) worked out for this event. First-hop effects are recorded and measured; they do not trade while relationship effects are on observe." % (len(rel_rows), len(impacts)),
                                         rows=rel_rows, items=its, missing=[] if impacts else ["no relationship effect has been worked out for this event"], extra={"headers": ["Company", "Role relative to " + ticker, "Strength", "Mechanism", "Source"]})
    else:
        sections["relationships"] = _sec("relationships", "missing", "No economic relationships are recorded for %s." % ticker, missing=["relationship coverage is limited to curated seed data (see the coverage report)"])
        missing.append("relationships")

    # ---- red team
    iv = [dict(r) for r in conn.execute("SELECT i.*,o.r1,o.r3,o.r5,o.first_hit,o.net_benefit,o.status AS ostatus FROM redteam_interventions i LEFT JOIN intervention_outcomes o ON o.intervention_id=i.id WHERE i.decision_id=? ORDER BY i.id DESC LIMIT 1", (decision_id,))] if decision_id and _has(conn, "redteam_interventions") else []
    if iv:
        x = iv[0]
        info = _j(x["objections_json"], {}) or {}
        objs = info.get("objections") or []
        rows = [["Original", "%s at %.2f%% of equity" % (x["original_action"], x["original_size_pct"] or 0)], ["Final", "%s at %.2f%% (size x%.2f)" % (x["final_action"].upper(), x["final_size_pct"] or 0, x["combined_mult"] or 1)], ["Hard veto", "yes" if x["hard_veto"] else "no"],
                ["Mode", x["mode"]], ["Hypothetical entry / stop / target", "$%s / %s / %s" % ("%.2f" % x["hyp_entry"] if x["hyp_entry"] else "?", "%.1f%%" % (x["hyp_stop"] * 100) if x["hyp_stop"] else "?", "%.1f%%" % (x["hyp_target"] * 100) if x["hyp_target"] else "?")]]
        if x["block_reason"]:
            rows.append(["Exact block reason", x["block_reason"]])
        if x["ostatus"]:
            rows.append(["Afterwards (hypothetical)", "1d %s, 3d %s, 5d %s; first to hit: %s; economic effect of the intervention %s" % tuple((("%+.1f%%" % (v * 100)) if isinstance(v, (int, float)) else "n/a") for v in (x["r1"], x["r3"], x["r5"])) + (x["first_hit"] or "none", ("%+.3f%% of equity" % (x["net_benefit"] * 100)) if x["net_benefit"] is not None else "n/a")])
        else:
            rows.append(["Afterwards", "not measured yet (needs a few sessions of price data)"])
        sections["redteam"] = _sec("redteam", "complete", "%s: %d objection(s) raised." % (x["final_action"].capitalize(), len(objs)), rows=rows,
                                   items=["<b>%s</b> (%s, severity %.2f, size x%s)%s" % (E((o.get("rule") or "").replace("_", " ")), E(o.get("class")), o.get("severity") or 0, o.get("mult"), (" &mdash; " + E(o.get("evidence"))) if o.get("evidence") else "") for o in objs])
    elif decision_id:
        rv = conn.execute("SELECT review_json FROM redteam_reviews WHERE decision_id=? ORDER BY id DESC LIMIT 1", (decision_id,)).fetchone() if _has(conn, "redteam_reviews") else None
        bp = (_j(rv[0], {}) or {}).get("bear_points", []) if rv else []
        sections["redteam"] = _sec("redteam", "partial" if bp else "missing", "The red team's older review record is shown; the full intervention record did not exist when this decision was made." if bp else "The red team did not review this decision.",
                                   items=["<b>%s</b> (severity %.2f)" % (E((b.get("rule") or "").replace("_", " ")), b.get("severity") or 0) for b in bp], missing=[] if bp else ["no red-team record for this decision"])
    else:
        sections["redteam"] = _sec("redteam", "missing", "Not tied to a decision: open a decision's Why page to see the red team's interventions.", missing=[])

    # ---- regime
    now_r = kbregime.active(conn, iso(now)) if _has(conn, "market_regimes") else []
    then_r = kbregime.active(conn, when) if _has(conn, "market_regimes") else []
    ids = [r["regime_id"] for r in then_r] or []
    if ids:
        adj = kbregime.adjust(ids, "SHORT" if (d or {}).get("action") == "SHORT" else "BUY", (ev or {}).get("direction") or 0.0, 0.6)
        sections["regime"] = _sec("regime", "complete", "Active at the time: %s. In observe mode these only record what they would change." % ", ".join(i.replace("_", " ") for i in ids), rows=[[r["regime_id"].replace("_", " "), "confidence %.2f" % (r["confidence"] or 0), r["rule_trace"][:120]] for r in then_r] + [["Would have changed (recorded only)", "size x%.2f, confidence bar +%.2f, holding x%.2f; direction never changed" % (adj["size_mult"], adj["confidence_add"], adj["holding_mult"]), "; ".join(adj["notes"])]])
    else:
        sections["regime"] = _sec("regime", "missing", "No market regime was recorded at this time.", missing=["regime detection had not run (or its inputs were stale)"])
        missing.append("market regime")

    # ---- facts used
    fin_then = kbsnap.financial_as_of(conn, eid, when) if eid else None
    fin_now = kbsnap.financial_as_of(conn, eid, iso(now)) if eid else None
    mkt = kbsnap.current_market(conn, eid) if eid else None
    frows, fmiss = [], []
    if fin_then:
        rep = (_j(fin_then["metrics_json"], {}) or {}).get("reported", {})
        for k in ("revenue_ttm", "net_income_ttm", "op_income_ttm", "cfo_ttm", "cash", "equity"):
            m = rep.get(k)
            if m:
                frows.append([k.replace("_", " "), _fmt_money(m["value"]), "period %s, filed %s, %s %s" % (m.get("period_end"), m.get("filed"), (m.get("accn") or ["?"])[0], m.get("tag") or ""), m.get("reliability")])
            else:
                fmiss.append(k.replace("_", " "))
    else:
        fmiss.append("financial snapshot (SEC facts not stored for this company at that time)")
    if mkt:
        frows.append(["market", "price %s, liquidity %s" % (mkt["price"], mkt["liquidity_class"]), "observed %s" % mkt["observed"][:16], "ok"])
    else:
        fmiss.append("current market snapshot (none, or expired)")
    sections["facts"] = _sec("facts", "complete" if frows and not fmiss else "partial" if frows else "missing", "Figures used or available, each with its period, filing and tag." if frows else "No financial or market facts are stored for this company.", rows=frows, missing=fmiss,
                             extra={"headers": ["Fact", "Value", "Source", "Reliability"]})
    missing += fmiss

    # ---- then vs now
    diffs = []
    if d:
        diffs.append(["Financial snapshot", (fin_then or {}).get("taken", "none known then"), (fin_now or {}).get("taken", "none")])
        nrel_then = len(kbrel.neighbours(conn, eid, when, cap=200)) if eid else 0
        nrel_now = len(kbrel.neighbours(conn, eid, iso(now), cap=200)) if eid else 0
        diffs.append(["Relationships known", str(nrel_then), str(nrel_now)])
        diffs.append(["Regimes", ", ".join(ids) or "none recorded", ", ".join(r["regime_id"] for r in now_r) or "none recorded"])
        diffs.append(["Event-chain version", str((st_then or {}).get("as_of", "none stored then")), str((cur or {}).get("as_of", "none"))])
        sections["then_now"] = _sec("then_now", "complete", "What the bot KNEW when it decided, beside what it knows now. Only knowledge that existed then was used for the decision.", rows=diffs, extra={"headers": ["", "At the decision", "Now"]})
    else:
        sections["then_now"] = _sec("then_now", "missing", "Open a decision's Why page for the then-versus-now comparison.", missing=[])

    # ---- missing, provenance
    cm = [k for k in ("identity_status", "profile_status", "financial_status", "market_status", "exposure_status", "relationship_status", "filings_status") if cov.get(k) in ("pending", "unavailable", "unresolved", "stale")]
    sections["missing"] = _sec("missing", "complete" if not (missing or cm) else "partial", "Everything the bot does not have for %s. An absent fact is shown as absent." % ticker if (missing or cm) else "Nothing material is missing.",
                               items=[E(m) for m in dict.fromkeys(missing)] + ["knowledge status &mdash; %s: %s" % (E(k.replace("_status", "")), E(cov.get(k))) for k in cm])
    src = [["Identity", ent.get("source") or "n/a", ent.get("last_verified") or ""], ["Profile", prof.get("source") or "n/a", prof.get("last_verified") or ""], ["Financials", "SEC XBRL via the facts reader", (fin_now or {}).get("taken", "")],
           ["Relationships", ", ".join(sorted({r[4].split(",")[0] for r in rel_rows})) or "n/a", ""], ["Filings", "%d known filing event(s)" % len(filingintel.known_events(conn, ticker, when, limit=50)) if ticker and _has(conn, "kb_filing_events") else "n/a", ""]]
    sections["provenance"] = _sec("provenance", "complete", "Each fact carries its source and when it was last verified.", rows=src, extra={"headers": ["Area", "Source", "Last verified / taken"]})

    # ---- verdict
    verdict = {"ticker": ticker, "name": ent.get("legal_name") or ticker, "decision": None, "tone": "info", "headline": "", "summary": "", "ex": None}
    if d:
        from . import explain
        ex = explain.explain(conn, cfg, decision_id, now)
        if ex:
            verdict.update({"decision": {"id": decision_id, "status": ex["status"], "created": d["created"]}, "tone": ex["tone"], "headline": ex["headline"], "summary": ex["summary"], "ex": ex})
    else:
        verdict["headline"] = "%s: what the reasoning engine knows" % (ent.get("legal_name") or ticker)
        verdict["summary"] = (sections["chain"]["summary"] if sections["chain"]["status"] == "complete" else sections["event"]["summary"])
    sections["verdict"] = _sec("verdict", "complete", verdict["headline"], extra=verdict)
    return {"ticker": ticker, "decision_id": decision_id, "as_of": when, "now": iso(now), "sections": [sections[k] for k in SECTION_ORDER]}


# ================================================================================================================================ rendering
CSS = """.cf{max-width:1000px}.cf h2{margin:.1em 0 .2em}.cf .hd{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--acc);border-radius:9px;padding:12px 16px;margin:0 0 10px}
.cf .hd.good{border-left-color:var(--ok)}.cf .hd.bad{border-left-color:var(--bad)}.cf .hd.wait{border-left-color:var(--warn)}.cf .hd p{margin:.2em 0}
.cf nav.cfn{margin:0 0 12px}.cf nav.cfn a{display:inline-block;margin:2px 4px 2px 0;padding:3px 9px;border:1px solid var(--line);border-radius:14px;color:var(--fg);text-decoration:none;font-size:12.5px}.cf nav.cfn a:hover{border-color:var(--acc)}
.cf section.cs{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:4px 14px 8px;margin:0 0 12px}.cf section.cs>h3{margin:.7em 0 .1em;font-size:15px;border:0}.cf .chip{font-size:11px;font-weight:600;padding:1px 8px;border-radius:10px;margin-left:8px;vertical-align:1px}
.cf .chip.complete{background:rgba(10,125,59,.15);color:var(--ok)}.cf .chip.partial{background:rgba(154,103,0,.15);color:var(--warn)}.cf .chip.missing{background:rgba(179,38,30,.13);color:var(--bad)}
.cf table{margin:4px 0 6px}.cf td{padding:4px 10px 4px 0}.cf td:first-child{color:var(--mut);white-space:nowrap}.cf ul{margin:.3em 0 .3em 1.1em;padding:0}.cf li{margin:.25em 0}.cf .miss{color:var(--bad);font-size:12.5px;margin:.2em 0}
.cf .links{margin:.2em 0 .6em}.cf .links a{margin-right:14px}.cf details{margin:.4em 0}.cf .two td:first-child{white-space:normal}"""


def _table(rows, headers=None) -> str:
    if not rows:
        return ""
    head = "<tr>%s</tr>" % "".join("<th>%s</th>" % E(h) for h in headers) if headers else ""
    return "<table>%s%s</table>" % (head, "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % (c if isinstance(c, str) and ("<b>" in c or "&mdash;" in c or "<br" in c or "&amp;" in c) else E(c)) for c in r) for r in rows))


def render(cf: dict, standalone_links: bool = True) -> str:
    tick = cf["ticker"]
    S = {s["key"]: s for s in cf["sections"]}
    v = S["verdict"]["extra"]
    tone = {"good": "good", "bad": "bad", "wait": "wait", "warn": "wait"}.get(v.get("tone"), "")
    o = ["<style>%s</style><div class='cf'>" % CSS]
    o.append("<div class='hd %s'><h2>%s%s</h2><p><b>%s</b></p><p class=mut>%s</p>" % (tone, E(v.get("name") or tick), (" (%s)" % E(tick)) if tick and v.get("name") != tick else "", E(v.get("headline")), E(v.get("summary"))))
    links = []
    if cf.get("decision_id"):
        links.append("<a href='/?tab=decisions'>&larr; all decisions</a>")
    if tick:
        links += ["<a href='/?tab=company&amp;t=%s'>Company knowledge</a>" % E(tick), "<a href='/?tab=intelligence&amp;ask=why+%s'>Ask the engine: why %s</a>" % (E(tick), E(tick))]
    o.append("<div class=links>%s</div></div>" % " ".join(links))
    o.append("<nav class=cfn>%s</nav>" % "".join("<a href='#cf-%s'>%s%s</a>" % (s["key"], E(s["title"]), "" if s["status"] == "complete" else " &middot; %s" % E(s["status"])) for s in cf["sections"] if s["key"] != "verdict"))
    ex = v.get("ex")
    if ex:
        from . import explain
        mark = {True: "<span class=ok>&#10003;</span>", False: "<span class=bad>&#10007;</span>", None: "<span class=mut>&#8987;</span>"}
        body = ""
        if ex.get("trigger"):
            t = ex["trigger"]
            body += "<h4>What triggered it</h4><blockquote>&ldquo;%s&rdquo;<br><span class=mut>%s</span></blockquote><p>%s %s</p>" % (E(t["headline"]), E(t["source"]), E(t["line"]), E(t["match"]))
        body += explain.article_html(ex, "h4")
        if ex.get("checks"):
            body += "<h4>The checks, in plain words</h4><ul class=checks>%s</ul>" % "".join("<li>%s %s</li>" % (mark[ok if ok in (True, False) else None], E(txt)) for ok, txt in ex["checks"])
        if ex.get("numbers"):
            body += "<h4>How we sized it up</h4>" + _table([[a, "<b>%s</b>" % E(b), c] for a, b, c in ex["numbers"]])
        if ex.get("scenarios"):
            body += "<h4>What we think could happen</h4><table><tr><th>Scenario</th><th>Chance</th><th>Return</th></tr>%s</table>" % "".join("<tr><td>%s</td><td>%.0f%%</td><td class=%s>%s</td></tr>" % (E(k.replace("_", " ")), p * 100, "ok" if r >= 0 else "bad", explain.pct(r, 2)) for k, p, r in ex["scenarios"])
        if ex.get("plan"):
            body += "<h4>The plan</h4>" + _table(ex["plan"])
        if ex.get("next"):
            body += "<h4>What happens next / what would change it</h4><ul>%s</ul>" % "".join("<li>%s</li>" % E(x) for x in ex["next"])
        if ex.get("timeline"):
            body += "<h4>Timeline</h4>" + _table(ex["timeline"])
        o.append("<section class=cs id='cf-what'><h3>What happened<span class='chip complete'>decision record</span></h3>%s</section>" % body)
    for s in cf["sections"]:
        if s["key"] == "verdict":
            continue
        head = "<h3>%s<span class='chip %s'>%s</span></h3><p>%s</p>" % (E(s["title"]), E(s["status"]), E(s["status"]), E(s["summary"]) if s["key"] != "event" else E(s["summary"]))
        body = ""
        if s["rows"]:
            body += _table(s["rows"], s["extra"].get("headers"))
        if s["items"]:
            body += "<ul>%s</ul>" % "".join("<li>%s</li>" % it for it in s["items"])
        if s["missing"] and s["key"] not in ("missing",):
            body += "<p class=miss>Missing: %s</p>" % E("; ".join(s["missing"]))
        o.append("<section class=cs id='cf-%s'>%s%s</section>" % (s["key"], head, body))
    tech = {s["key"]: {"status": s["status"], "extra": {k: v for k, v in s["extra"].items() if k != "ex"}} for s in cf["sections"]}
    o.append("<details><summary class=mut>Technical record</summary><pre>%s</pre></details></div>" % E(json.dumps({"as_of": cf["as_of"], "sections": tech, "decision_record": (ex or {}).get("technical")}, indent=1, default=str)[:12000]))
    return "".join(o)
