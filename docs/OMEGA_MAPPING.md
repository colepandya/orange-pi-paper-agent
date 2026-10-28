# OMEGA brief → implementation

| Brief | Module | How |
|---|---|---|
| Data Collection | `sources.py`, `net.py` | 44 sources: RSS/Atom, SEC EDGAR, Federal Register, USAspending, openFDA, central banks, BLS/EIA/DoD/NRC/CISA, **GDELT geopolitical queries**, Finnhub. Conditional GET, per-source isolation, exponential backoff. |
| Event Engine | `nlp.py`, `entities.py`, `evidence.py` | 30 event types (company + macro/geopolitical). Word-boundary phrases, negation, hedging, amounts, regions. Entity mapping with confidence. Story clustering (duplicates don't stack). |
| Evidence Engine | `evidence.py` → `evidence` table | source, credibility, strength, novelty, predictive_power, affected theses/companies, expiry_date. Decay: earnings 30 d, contract 180 d, product 90 d, approval 365 d, structural 730 d (exponential, ~5% weight at expiry). |
| Thesis Engine + states | `thesis.py`, `theses_seed.py` | 21 theses (your 7 + sub-theses + macro/geo). Emerging/Forming/Active/Strong/Overcrowded/Weakening/Broken. |
| Bayesian Update Engine | `thesis.recompute` | prior → log-odds sum of clustered, saturating evidence → posterior + uncertainty + confidence quality; full history in `thesis_history`. |
| Thesis dependency graph | `thesis_links` | Datacentres/Networking/Semis/Power/Cooling → AI Infrastructure. Children lift parents; a broken parent drags children down. |
| Contradiction Engine | `contradiction.py` | Actively searches (GDELT) for counter-evidence to held/high-probability theses; Support / Contradiction / Net scores on every thesis. |
| Narrative Engine | `narrative.py` | strength, velocity, acceleration, crowding (volume share + price extension), exhaustion, decay. |
| Company Intelligence + DNA | `company.py` | 6 scored dimensions with data completeness (unknown ≠ guessed), DNA classes. |
| Capital Flow Engine | `flows.py` | Insider buy/sell (Finnhub), buybacks (events), sector-ETF dollar-volume proxy. **13F/institutional: not available free, a research task is raised.** |
| Regime Engine | `regime.py` | 8 regimes as probabilities from SPY/VIX/yields/credit/USD/oil/gold; tilts thesis style weights and the risk multiplier. |
| Expected Value Engine | `ev.py` | Bull/Base/Bear/Tail/Black Swan, probabilities from thesis, regime and calibration; net of costs; negative EV rejected. |
| Opportunity Cost Engine | `opportunity.py` | cash hurdle; rotation versus weakest holding raises SUPERIOR_OPPORTUNITY. |
| Portfolio Intelligence | `portfolio.py` | mark-to-market equity; sector/theme/country/currency/liquidity/factor exposure. |
| Portfolio Survival Engine | `portfolio.stress` | AI Crash, Market Crash, Recession, Rate Shock, China-Taiwan, Credit Crisis → survival probability, max drawdown, recovery time. Scenario probabilities are linked to thesis probabilities and regime. |
| Position sizing | `sizing.py` | quarter-Kelly from EV, then caps: position, risk-per-trade, liquidity, issuer, theme, gross, cash reserve, **stress survival**; thesis/regime/confidence/risk multipliers. |
| Self-Learning Engine | `learning.py` | Every claim is stored and scored (Brier). Learns source credibility, event predictive power, move-size scale, probability calibration, thesis priors; nothing changes before min samples, always shrunk to defaults. |
| Research Gap Engine | `gaps.py` | Missing deal value / guidance / customer / fundamentals / quotes / stale theses / failing sources → `research_tasks`; auto-resolves what it can. |
| Monitoring Engine | `monitor.py`, `executor.py` | Per position: thesis, supporting vs contradicting evidence, health, trend; bounded plan revisions; exit triggers Thesis Broken, Risk Escalation, Superior Opportunity, Target Achieved. |
| Explainability 100% | `decisions` + `forecasts` + `audit_log` | Every candidate gets a ledger row with gates, sizing steps, scenarios and the quote used. |
| Optional LLM analyst | `llm.py` (off by default) | Reads ambiguous/macro items, strictly schema-validated, adds *evidence only*, scored like any other source. |
