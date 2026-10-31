# The Financial Reasoning Engine (build 41)

A rules-and-data replacement for everything the bot used an LLM for. No language model, no key, no network, no cost. It is the default; `intelligence.engine = llm` brings the old path back (kept only for comparison).

## What the LLM used to do, and what does it now
| LLM job | Now | Where |
|---|---|---|
| Analyst: read a news item, say what happened, who benefits and loses, by causal order, which themes it moves | `reasoning.analyze_item` / `make_analyst`: same output structure, so `event_intel.apply` and `candidates_from` are unchanged | pipeline -> evidence |
| Red team: argue against a trade, find a fatal flaw, list tripwires | `reasoning.review`: same structure; `redteam.gate` still decides; tripwires work for shorts too | trader |
| Triage: which items deserve a deeper look | the deterministic reading already did this; the engine reads up to 80 items a cycle (it is free) | evidence |
| Q&A about the portfolio, a stock, an event | `reasoning.ask`, the `ask` command and the Ask box on the Intelligence tab | CLI, dashboard |
| Reading SEC filings into a company knowledge base | NOT replaced: that needs a language model. The stage is skipped, honestly (the deterministic company data and the article reader remain) | pipeline |

## How it reasons
1. READ the deterministic event reading plus the article's own facts (earnings vs estimate, guidance, deals).
2. QUALIFY: confirmed / plan / unconfirmed rumour / denial / history / opinion each change how certain the event is.
3. ROLES: acquirer vs target, plaintiff vs defendant, otherwise everyone named shares the event, with confidence reduced for crowds.
4. PROPAGATE along the relationship graph (suppliers, customers, competitors, peers, measured co-movers), two hops, confidence decaying per hop; and along macro exposures (oil, gold, copper, uranium, rates, chip export curbs, conflict) to the relationship-map groups they hit.
5. THEMES it supports or contradicts.
6. DOUBT: contradictions (headline vs body, good and bad facts together), and ABSTENTION when the evidence is thin. Every result carries `trace` (the rules that fired) and `quotes` (verbatim supporting text).

## Review of the Copilot proposal (what was kept, fixed or rejected)
| Copilot proposed | Reality / decision |
|---|---|
| A structured `analyse_event` interface returning affected tickers, winners/losers, confidence, horizons, evidence, counterarguments | KEPT, and made the same shape the pipeline already consumes, so nothing downstream changes |
| Versioned event taxonomy, negation and uncertainty handling, article-level extraction | ALREADY EXISTED (nlp event types, the article reader, the lens). Reused, not rebuilt |
| Company / supplier / customer / competitor / partner / sector / commodity / country relationships | Suppliers, customers, competitors, peers and measured co-movement already exist (the graph, build 39); commodity and macro exposure ADDED here; partner / country not yet |
| Confidence decay per hop; indirect impact weaker unless independently supported | KEPT: the existing chain-decay setting; reliability per hop is learned from outcomes |
| Contradiction engine (headline vs body, beat but guidance cut, rumour vs denial vs confirmed, forward vs historical) | Mostly ADDED here (all but "company statement vs independent reporting" and syndicated-duplicate suppression, which the existing novelty clustering partly covers) |
| Outcome learning: 1h/1d/5d/20d outcomes, MFE/MAE, accuracy by rule, calibration, small-sample caution | The outcome tracker, calibration and small-sample caution ALREADY EXIST (predictions, learning, observe mode); the engine's own source tag lets each hop and event type be scored separately. MFE/MAE per prediction is NOT added yet |
| Shadow mode before trading on it | The strategy-freeze stamp and the missed-trade engine already give this. The engine is on by default; to compare, set engine=llm for the old behaviour |
| "Support inverse instruments" | REJECTED: inverse ETFs decay and do not match what shorts, puts and the graph already provide |
| "Fail closed when data is missing" | KEPT: it abstains and returns None |
| "No pandas, no heavy runtime, no network in tests" | KEPT: standard library only; no test touches a network |
| A multi-week phased plan and "send me the archive" | NOT NEEDED: it was built directly against the real code in one pass |

## Honest limits
It handles the event types, phrases and relationships it has rules for. It is not a language model: it will not read an unusual sentence the way an LLM can. When it cannot, it abstains or says "I can't answer that" rather than guess. The relationship and macro maps are starter data written from public knowledge: review `/etc/paper-agent/relations.json`. Its usefulness is measured the same way as everything else: the learned reliability of `RULES-analyst:o1/o2/o3` per event type once outcomes accumulate.
