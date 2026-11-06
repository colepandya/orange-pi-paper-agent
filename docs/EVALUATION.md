# Build 47: evaluation on YOUR live universe

Measured with the Pi's own 7,169-name `watchlist.json` and its own common-words list (from the 6 Nov export), at the level of the COMPANY (several tickers of one issuer count once). Build 45's figure reproduces exactly what `eval` printed on your Pi (44/63 on validation), so the harness is faithful.

**Limits.** Labels are one author's, headline-only, written before the engine ran on each set. The matcher rules of Builds 46 and 47 were chosen after reading false matches from ALL sets, validation included: treat Build 46/47 gains as partly fitted. No combined accuracy figure is given on purpose. Nothing here measures trading.

## VALIDATION (53)

| Metric | Build 44 | Build 45 | Build 47 |
|---|---|---|---|
| Entity precision (issuer level) | 0.49 (46/94) | 0.70 (44/63) | 0.90 (44/49) |
| Entity recall | 0.92 (46/50) | 0.88 (44/50) | 0.90 (44/49) |
| False-company-match rate | 0.51 (48/94) | 0.30 (19/63) | 0.10 (5/49) |
| Event-family accuracy | 0.63 (17/27) | 0.94 (16/17) | 1.00 (14/14) |
| Direction accuracy when stated | 1.00 (11/11) | 1.00 (9/9) | 1.00 (9/9) |
| Abstention precision | 1.00 (17/17) | 0.94 (33/35) | 0.95 (36/38) |
| Abstention recall (non-events) | 0.44 (17/39) | 0.85 (33/39) | 0.92 (36/39) |

## EARLIER SETS (233)

| Metric | Build 44 | Build 45 | Build 47 |
|---|---|---|---|
| Entity precision (issuer level) | 0.47 (201/425) | 0.70 (187/266) | 0.90 (185/206) |
| Entity recall | 0.94 (201/215) | 0.87 (187/215) | 0.89 (185/209) |
| False-company-match rate | 0.53 (224/425) | 0.30 (79/266) | 0.10 (21/206) |
| Event-family accuracy | 0.71 (68/96) | 0.81 (70/86) | 0.80 (68/85) |
| Direction accuracy when stated | 0.96 (54/56) | 0.95 (55/58) | 0.93 (57/61) |
| Abstention precision | 0.96 (92/96) | 0.97 (124/128) | 0.99 (134/135) |
| Abstention recall (non-events) | 0.51 (92/182) | 0.68 (124/182) | 0.74 (134/182) |

## Why Build 44 looked fine and was not

On the old 5,223-name export Build 44's precision was about 0.63. On the live 7,169-name universe it is **0.47-0.49**: the larger universe adds funds, SPAC units and warrants, and bank ETNs that share an issuer name, and every one of them is a chance for a wrong match.
