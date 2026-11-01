# The Financial Reasoning Engine (build 42)

A rules-and-data engine that does what the LLM used to do. No language model, no key, no network, no cost. It is the ONLY engine; the old LLM files remain in the code base but nothing calls them.

## What it may DO: the influence setting (Settings -> System)
| Mode | What happens |
|---|---|
| off | The engine does not run. The bot behaves as it did before the engine existed. |
| observe | It reads every news item and argues against every trade, and records all of it, including what it WOULD have blocked. Nothing changes. |
| **guard** (default) | As observe, plus: it may veto an idea whose evidence is factually broken (denied, stale, the company is only mentioned, the same story twice, contradicted or superseded by a better-sourced report) and make a trade smaller. It never creates or enlarges one, and its analysis is not scored as evidence. |
| active | Its analysis becomes evidence (hops 1 and 2; a third hop is shown but never scored) and every objection acts, plus the conviction floor. |

Raise it only as the scorecard (Intelligence tab, or `python3 -m paper_agent scorecard`) earns it: what happened to the ideas it would have blocked versus the ones it would have allowed, per rule, with honest intervals.

## Parts
* reasoning.py: the analyst (event state, novelty, materiality, independent sources, roles, graph and macro propagation, themes, contradictions, abstention) and the question interface (`ask`).
* skeptic.py: the red team: graded objections tagged by rule, aligned contradictions, gates by mode, counterfactual scorecard.
* graph.py: relationships with provenance and validity; statistical links separated from causal ones; industry-wide logic.
* config/macro_exposures.json: macro drivers as channels (edit freely).
* facts.py: SEC structured financial data.
* pathways.py: multi-horizon outcomes and hierarchical statistics.

## Assessment of the independent review of build 41
Each claim was checked against the code. **Right and acted on:** deterministic only with influence modes (I had wrongly offered an LLM fallback); one record for each reasoning event; separate confidence components; contradiction alignment (the 44-of-84 figure was noise); graded severity and a hard-veto list; counterfactual scoring of vetoes; correlation is not causation; a third hop must not score; multi-horizon outcomes with best and worst excursion; shrinkage for small groups; independent-source counting; SEC filings CAN be read deterministically (my earlier claim that they could not was too broad); macro exposures as channels; relationship provenance and validity.
**Already true or already done:** ambiguous-ticker handling exists (common-word tickers need a cashtag or exchange tag), and 8-K item numbers were already read; relationships were already in database tables; the graph was already event-specific (it lacked mechanism and sign confidence, now added); the outcome tracker, observe-before-apply learning and strategy stamps existed.
**Wrong or not adopted:** that a single weight cannot depend on the event (it already did); the demand for a full ticker and company-name history database (valuable, large, and not built: it needs a paid or hand-maintained dataset); XBRL "companyfacts" bulk files (too large for the Pi: only a handful of small per-concept requests are used).
**Found by this audit that the review did not:** a byline ("Simon Sharwood / The Register") matched Simon Property Group; the reasoning record was never linked to its event; a cached red-team review ignored news that arrived after it.
**Not done yet:** a labelled corpus beyond about a dozen cases, an entity-history table, partner and country relationships, Form 4 transaction parsing, a promotion/rollback workflow for rules (the scorecard supplies the evidence; changing a rule's mode is a config edit).

## Measured accuracy (build 43)
Two labelled sets run as tests (tests/test_accuracy.py). The DEVELOPMENT set (76 cases, rules were tuned against it) scores 100%; the HELD-OUT set (49 cases, written afterwards with different wording) scored 84% before tuning and 98% after. The gap between those two numbers is the honest warning: the development score flatters the engine. Categories: direction for the company, who gains and loses in a deal or lawsuit, the state of the story, negation, hedging, history, opinion pieces and other non-events, filings, and macro drivers. Real-data audit: on 700 of the owner's material news items it analysed about 60% and abstained on the rest.
Full-chain replay on the owner's 84 real ideas (guard mode): 2% blocked, 71% at full size, average size x0.93.

## Denial, SEC arithmetic and freshness (build 43)
* A denial applies only to the proposition and companies it negates, at the time it was made, unless later confirmed (tests/test_build43_scope.py).
* A twelve-month SEC figure exists only if it can be built reliably; otherwise it is reported UNAVAILABLE with the reason and never replaced by an old annual figure (tests/test_facts_ttm.py). Each figure shows the period, filing date and retrieval time; `ask why` shows them.
* Weak fundamentals can only make a trade smaller (at most 15%) and only when relevant to the catalyst and holding period.

## Honest limits
Everything it knows comes from rules and starter data (relations.json, macro_exposures.json) written from public knowledge, and from your own price history. It will miss what an unusual sentence implies. It abstains rather than guess: about 40% of recent news items. Whether any of this improves returns is unproven; the scorecard is how that gets decided.
