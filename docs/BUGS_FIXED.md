# Defects found in v1-v4 and how v5 addresses them
Each has a regression test in `tests/test_regressions.py` (mutation-checked: re-introducing the bug fails the test).

| # | Defect | Impact | Fix |
|---|---|---|---|
| 1 | `execute_virtual_buy` used undefined `cash` | `NameError` on every buy with a quote → no trade ever executed | rewritten (`trader.execute_buy`) |
| 2 | `install_v402.sh` disabled collector, monitor, executor and the V4 timer | nothing ran after V4.0.2 except the quote daemon | single set of timers; installer removes legacy units |
| 3 | README's "V3.6 forecast worker" never existed; `generate_missing()` never called; `forecast_observations` never written | learning loop had no data | forecasts + predictions recorded at decision time; `learning.py` scores them |
| 4 | Substring keyword scoring (`ban`⊂bank, `order`⊂border, `sec`⊂second, `will`) | random trades from noise | word-boundary phrases + negation + hedging |
| 5 | RFC-822 parse used `%Z` so `+0000`/`-0400` failed → age 0 | **old news scored as fresh** | robust `parse_ts` everywhere |
| 6 | Alpaca nanosecond timestamps failed to parse → age `None` → accepted | stale quotes executable | robust parser; timestamp required for execution |
| 7 | Alpha Vantage quotes have no timestamp but counted toward quorum | unverifiable quote could execute | valuation-only |
| 8 | `forecast_gate()` called without portfolio state | issuer/theme/gross/drawdown limits never applied | sizing + gates use live holdings and stress tests |
| 9 | Readiness required *all* positions execution-eligible | monitor/thesis updates never ran overnight/weekends | collection, thesis, monitor run 24/7; only execution is gated |
| 10 | `book_equity()` summed cost | unrealised P&L and drawdown invisible | mark-to-market |
| 11 | Every company `theme='Unclassified'` | theme limits meaningless | thesis membership + Finnhub/SEC sector |
| 12 | Short tickers (`ON`, `ALL`, `IT`) and common-word names (`Target`) matched anywhere | wrong-company trades | `entities.py` (exchange tags, cashtags, CIK, capitalisation, blacklist) |
| 13 | Config pointed at env var literally named `REDACTED` | Finnhub never worked | real env names |
| 14 | `quote_cache` created by a different module than the one writing it | fresh-install crash | one schema |
| 15 | Dashboard called `resolve_quote(d, watch, ticker)` with swapped args and fetched quotes while rendering | quotes silently never refreshed; slow pages | read-only render, no network |
| 16 | 2026-only holiday table | market clock wrong from 1 Jan 2027 | rule-based NYSE calendar (matches 2026 table exactly) |
| 17 | Hard-coded `/opt/paper-agent/app` paths + importlib loading + 30 version-suffixed modules | untestable, fragile | one package, injectable I/O |
| 18 | Free-tier provider limits ignored (Alpha Vantage 25/day…) | quota exhaustion | per-provider budgets and 429/401 backoff |
| 19 | 304 handling absent | wasted requests | conditional GET; 304 is healthy |
| 20 | Risk limits 50/80/90% | ignores "survival first" | safer defaults, configurable |
