# Company knowledge base (Build 45)

A record of what each company IS, what is KNOWN about it and where each fact came from, kept beside the trading logic. **With every mode on observe (the default) it changes no trade**: a test runs a full simulated morning with it off and on and compares every decision, position and cash value.

## Identity
* Identity is the CIK (`entity_id`), never the ticker. Tickers are identifiers attached to an entity and can be closed (never deleted); share classes share one entity; a company with no CIK keeps a provisional identity marked `unresolved`.
* The **live universe file is authoritative**. The knowledge base mirrors it exactly and reports drift against the previous snapshot; it never adds or removes a tradable symbol. (The number of instruments is whatever the file holds today, not a fixed figure.)

## Layers
* **A** every company: identity, name-based instrument kind, SEC submissions (SIC, incorporation state, fiscal year end, former names) once fetched.
* **B** holdings, open and queued ideas, recent material events, frequent mentions and their first-hop neighbours are refreshed first (priority 1-5).
* **C** a bounded number of incomplete companies per cycle. Full-universe work runs from the command line and from the weekly universe job, never inside the trading cycle.

## What is NOT known (read this before trusting a number)
* Instrument kind (fund, shell, REIT, ADR) is judged from the **name** until the SEC record is fetched.
* Sector, industry and the industry traits (cyclicality, capital intensity ...) are derived from the SIC code by fixed tables and stored as **inferred**, low confidence, with the rule id. There is no business description unless structured data supports one.
* Relationships and exposures come only from the curated seed files shipped with the bot (about 67 edges, 41 peer groups, 10 macro drivers). They are marked unverified. Coverage is therefore small; nothing was invented to enlarge it.
* Financial facts exist only for companies the SEC facts reader has fetched. Materiality is `unknown` wherever an input is missing, never zero.
* Nothing in the SEC refresh, the filing parsers (Form 4, 13D/G, offerings, guidance) or the `universe-check` diagnostic has been run against the live SEC from the build environment (no network there); they are tested on documents constructed from the published structures.

## Influence rules
Direct company effects are the existing engine's business. First-hop relationship effects: **observe** (recorded and measured) unless the owner sets `kb.relationship_mode = active`. Second-hop: research only. Third-hop: explanation only. Correlation can never create, approve, size or block anything. Event-aware expiry, materiality, event state and regime context are observe-only by default and are measured against the existing behaviour (the flat 36-hour stale rule is the comparison).

## Corrections that are NOT behind a switch
Defects found by evaluating the engine on real stored news are fixed, because hiding a known defect behind a setting would be wrong: plural-verb patterns that could never match (`miss`, `slash`, `launch`), possessives ("Google's") and sentence-final names that hid companies, filings attributed by name words instead of their own CIK, fund and shell names matching on generic words, ALL-CAPS headlines producing fake tickers, recalls classed as regulatory actions, results reports with no stated beat or miss not recognised, a mixed event resolved against its own explicit cue, and a shared **event-worthiness gate** (questions, lists, roundups, commentary, previews, promotion, price recaps, comparisons, law-firm solicitations, routine prospectus supplements, passing mentions). See `EVALUATION.md` and `BUILD45_DECISION_DIFFERENCES.md`. Strategy version moves to S9, which starts a new measurement period.

## Commands
```
python3 -m paper_agent kb reconcile            # what the universe contains, before anything is ingested
python3 -m paper_agent kb bootstrap --dry-run  # then without --dry-run; resumable (--max-batches N to do it in pieces)
python3 -m paper_agent kb refresh --limit 50   # SEC submissions for the highest-priority companies (--only AAPL,MSFT, --dry-run)
python3 -m paper_agent kb coverage             # coverage by data category (never "complete")
python3 -m paper_agent kb unresolved | provenance | stale | resources
python3 -m paper_agent kb universe-report      # the universe by kind, with additions and removals
python3 -m paper_agent kb universe-check       # fetches the SEC file and explains why V / NEM / GOOG are in or out (needs network)
python3 -m paper_agent company NVDA            # everything known about a company and what is missing
python3 -m paper_agent eval                    # score the engine on the labelled real-news sets (each aspect separately)
```
The dashboard has a **Company** tab; **Decisions -> Why** and **Intelligence -> Ask "why TICKER"** show the same case file (what the company does, why the event matters, materiality, relationships, event chain and expiry, regime, red-team interventions, facts used, what is missing, and what the bot knew THEN versus now). All of it is read-only.
