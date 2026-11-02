# Build 45: independent evaluation on real stored news

Generated from the final code. **There is no combined accuracy figure on purpose.** Each aspect is scored separately with its own sample size.

## Method and limits

* Cases were SAMPLED by data characteristics (seeded hashing over named strata of a 15,143-event export), then LABELLED from the headline alone by the build's author (one labeller: **not independent annotators**). Labels were written before the engine was run on each set.
* Four sets: tuning-era (233 cases, used to find defects), and a validation set (53) drawn after the rules were frozen. Some validation cases are different wordings of events seen during tuning.
* The engine is run exactly as production runs it (entity match at confidence >= 0.5, learned common-words list, headline plus stored summary). 'Build 44' is the Build 44 classifier, analyst, matcher, evidence and red-team code under the same harness.
* Short-selling and relationship-derived cases do not exist in this export, so those strata are NOT evaluated. Materiality, relationship-propagation and contradiction metrics have very small samples and are shown only in the raw output (`eval`).
* Timeframe: the label convention (expected duration of the price effect, fixed by event family) was written before comparison and not adjusted. Disagreement is systematic (engine says medium where the convention says short) and can only be settled by measured price paths.

## VALIDATION (53, drawn after all rule changes)

| Metric | Build 44 | Build 45 |
|---|---|---|
| Entity precision | 0.63 (43/68) | 0.79 (41/52) |
| Entity recall | 0.96 (43/45) | 0.89 (41/46) |
| False-company-match rate | 0.37 (25/68) | 0.21 (11/52) |
| Role accuracy | 0.93 (40/43) | 0.88 (36/41) |
| Event-family accuracy (when a family is named) | 0.63 (17/27) | 0.94 (16/17) |
| Event-state accuracy | 0.89 (23/26) | 0.88 (22/25) |
| Direction accuracy (when stated) | 1.00 (11/11) | 1.00 (9/9) |
| Direction missing where one is labelled | 0.08 (1/12) | 0.25 (3/12) |
| Materiality accuracy | 0.00 (0/3) | 0.00 (0/3) |
| Timeframe agreement (common vocabulary) | 0.12 (3/24) | 0.06 (1/16) |
| Abstention precision | 1.00 (14/14) | 0.94 (33/35) |
| Abstention recall (non-events only) | 0.36 (14/39) | 0.85 (33/39) |

Abstained by basis (Build 45): {"filing_no_content": {"abstained": 17, "of": 17}, "immaterial_event": {"abstained": 0, "of": 3}, "non_event": {"abstained": 16, "of": 22}}

## TUNING-ERA SETS (233)

| Metric | Build 44 | Build 45 |
|---|---|---|
| Entity precision | 0.59 (186/314) | 0.79 (179/227) |
| Entity recall | 0.95 (186/196) | 0.89 (179/201) |
| False-company-match rate | 0.41 (128/314) | 0.21 (48/227) |
| Role accuracy | 0.90 (167/186) | 0.87 (155/179) |
| Event-family accuracy (when a family is named) | 0.71 (73/103) | 0.81 (75/93) |
| Event-state accuracy | 0.90 (99/110) | 0.90 (99/110) |
| Direction accuracy (when stated) | 0.95 (58/61) | 0.94 (59/63) |
| Direction missing where one is labelled | 0.08 (5/66) | 0.04 (3/66) |
| Materiality accuracy | 0.34 (17/50) | 0.40 (19/47) |
| Timeframe agreement (common vocabulary) | 0.13 (13/98) | 0.14 (12/88) |
| Abstention precision | 1.00 (81/81) | 1.00 (123/123) |
| Abstention recall (non-events only) | 0.45 (81/182) | 0.68 (123/182) |

Abstained by basis (Build 45): {"filing_no_content": {"abstained": 69, "of": 75}, "immaterial_event": {"abstained": 0, "of": 4}, "non_event": {"abstained": 54, "of": 107}}

## Split: news stories (n=194)

| Metric | Build 44 | Build 45 |
|---|---|---|
| entity_precision | 0.70 (151/217) | 0.73 (158/216) |
| entity_recall | 0.94 (151/161) | 0.98 (158/161) |
| event_family_accuracy | 0.69 (90/130) | 0.83 (91/110) |
| abstention_precision | 1.00 (50/50) | 0.97 (70/72) |
| abstention_recall | 0.39 (50/129) | 0.54 (70/129) |

## Split: filing-index entries (n=92)

| Metric | Build 44 | Build 45 |
|---|---|---|
| entity_precision | 0.47 (78/165) | 0.98 (62/63) |
| entity_recall | 0.97 (78/80) | 0.72 (62/86) |
| event_family_accuracy | n/a | n/a |
| abstention_precision | 1.00 (45/45) | 1.00 (86/86) |
| abstention_recall | 0.49 (45/92) | 0.94 (86/92) |

Filing-index entries: recall falls because filings by a different legal entity (UBS AG, Morgan Stanley Finance LLC, Honeywell Aerospace Inc.) are no longer attributed to the listed parent by name similarity; the registrant's own CIK decides, and subsidiary-to-parent knowledge is not yet in the knowledge base. All such filings are routine and abstained.

## What this does NOT show

Software correctness on small, single-labeller, headline-only sets. **Nothing here shows that trading improves**: the export has no resolved outcomes, and the relationship, regime, expiry, materiality and red-team measurements have none yet.
