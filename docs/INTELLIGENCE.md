# V6 Intelligence layer: how it works, what it costs, and what it can't do

## Principle
**The LLM produces intelligence. Deterministic code allocates capital.** An LLM never sizes, places or cancels a trade. Everything it says is
data: validated against a schema, checked for hallucination, weighted by *learned* reliability, and then fed to the same Bayesian / EV / risk
engines as everything else. If the LLM is off, over budget or wrong, the system degrades to the V5 deterministic engine; it never stops.

## Pipeline (what changes per event)
1. **Event intelligence** (`event_intel.py`): news item -> structured analysis: what happened, why, certainty, surprise, horizon, magnitude,
   beneficiaries/losers with mechanism, **first/second/third-order causal chain**, narrative support/contradiction, bull/bear case, verbatim quotes.
2. **Grounding** (`llm_client.ground_quotes`): every claim must quote the source verbatim. Fabricated quotes -> analysis rejected.
3. **Evidence with hop decay**: each hop's weight = confidence x 0.55^(order-1) x certainty x *learned reliability of that hop order*
   (`LLM-analyst:o1/o2/o3` are scored separately against real price outcomes, so unreliable hops fade on their own).
   Only order-1 beneficiaries can become trade candidates (`max_trade_order`); order 2-3 are evidence only until proven.
4. **Company knowledge base** (`filings.py`): SEC 10-K / 10-Q / earnings 8-K read by the LLM into revenue & cost drivers, customers, suppliers,
   competitors, risks, guidance, results vs expectations, tone, "what changed". Quote-grounded; each filing processed once.
5. **Red team** (`redteam.py`): before entry a separate pass argues AGAINST the trade -> bear/bull points, valuation view, fatal-flaw flag, conviction.
   A deterministic gate blocks fatal flaws / low conviction and shrinks size when valuation is rich. Its **falsifiers become tripwires**
   (price levels, specific adverse event types) checked every cycle by the monitor -> automatic exit signal.
6. **Decomposed confidence** (`confidence_log`): thesis / forecast / target / position confidence tracked independently, plus valuation view.
7. **Forecast revision ledger** (`forecast_revisions`): every target/stop/hold/thesis-probability change with the reason and confidence.
8. **Dossier** (dashboard -> Dossiers): one page per company: thesis, confidence parts, forecast, evidence for AND against, red-team case,
   tripwires, causal chain, filings knowledge, revision history.

## Cost control (hard limits, enforced in code)
`llm_daily_budget_usd` (default $0.75), `llm_monthly_budget_usd` ($15), per-tier daily call caps (triage 150, analyst 40), per-cycle cap (6 items),
response cache (identical requests are free), filings read once. Over budget -> LLM silently skipped. Spend is on the dashboard System tab.
Rough cost at published prices (Haiku 5.5 $0.10/$0.50, Sonnet 5.5 $2/$10 per million tokens; verify on Anthropic's pricing page):
triage ~ $0.0005/call; analyst ~ $0.015/call. Typical month with the default caps: **a few dollars to ~$15**.

## Providers
Any tier can use `anthropic` (Messages API) or any OpenAI-compatible endpoint (`github` = GitHub Models free tier, Groq, OpenRouter...).
Edit `intelligence.llm_tiers` in `/etc/paper-agent/config.json`. GitHub Copilot itself has no public API for third-party apps.
GitHub Models' free tier is reported (third-party sources, unverified) to allow ~10-15 requests/min, 50-150/day and an 8K input cap,
which is enough for headline triage but too small for filings and weaker at strict-JSON/grounded reasoning; use it for triage at most.

## Turn it on (one command)
`sudo /opt/paper-agent/scripts/enable-llm.sh` (asks for the key, stores it in /etc/paper-agent/market-data.env - preserved across auto-updates -
sets `llm_enabled`, runs a live test). Then `python3 -m paper_agent llm-eval` scores the analyst on a golden set of cases with known answers.

## Honest limits
* **Prompts are untested against a live model** (no network in the build environment). The machinery (validation, budgets, grounding,
  gating, tripwires) is fully tested with a scripted fake provider; the *quality* of real model output must be measured with `llm-eval`
  and then by the learning loop. Expect to iterate prompts after the first real runs.
* LLMs hallucinate, anchor on narratives and cannot be backtested (training data leaks the past). The only valid test is **forward paper trading**.
* "Surprise vs expectations" needs consensus estimates, which free data lacks; the model reports `null`/`unknown` rather than guessing.
* Company IR websites are not scraped (JS-heavy, ToS/robots issues, mostly duplicates of SEC filings). SEC EDGAR is the authoritative source.
* Smarter reading does not by itself create alpha. Success is measured by the learning metrics (Brier score vs baseline, per-hop hit rate), not by how convincing the dossiers read.

## Before any real money (suggested gates, not advice)
At least 3-6 months of forward paper trading; >=100 scored forecasts; Brier score clearly better than the naive baseline; positive net-of-cost
excess return vs SPY with drawdown inside limits across at least two regimes; LLM-sourced evidence (`LLM-analyst:*`) showing hit rate above chance.
Then start tiny, with hard loss limits, and keep the deterministic gates in charge.
