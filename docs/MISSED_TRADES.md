# Missed-trade analysis: what would the ideas we did NOT take have done?

Every decision the bot makes carries a forecast with a plan (target, stop, hold limit). When it declines an idea, the question is whether the rule that stopped it was right. After the US close the bot now answers that
for every idea it looked at, using 5-minute prices, by replaying the idea through the REAL exit code (`executor.run`: stop ratchet, partial exit, runner, target extension, time stop) on a throw-away in-memory book.
It reads decisions and writes only its own tables (`intraday_bars`, `intraday_state`, `whatif_results`). It never changes a decision, and it never runs in the US session or in pre-market.

## Where to see it
* **Intelligence tab -> "Missed trades: what would have happened"**: results grouped by what stopped the idea (reward:risk, story quality, edge, liquidity, veto, expiry...), by origin (news / thesis) and by kind of news.
* `python3 -m paper_agent whatif` (add `--run` to fetch prices and simulate now; `--days N`).

## How to read it
* Averages use finished simulations of the strategy profile in force only; ideas made under the older multi-week profile are stored but excluded.
* A group says nothing until it has 30 finished ideas. After that: "would have MADE money: this rule may be too strict" (95% range above +0.2%), "would have LOST money: this rule earns its keep" (below -0.2%), else "inconclusive".
  This is a prompt to look, not an instruction: with a few dozen ideas per group a single volatile week can fake a result. Treat a change as a hypothesis to confirm on later weeks.
* **Engine accuracy**: trades we actually took are replayed from their real entry; the difference from what really happened (on price-rule exits) is shown, so the engine is measured, not assumed.

## The assumptions (version w1) and their limits
* Entry: open of the first 5-minute bar after the decision, plus the configured slippage; out-of-hours decisions enter at the next open; the last-look gap rule and the 24 h age rule apply as for a real BUY.
* Inside a bar the price is assumed to walk open -> low -> high -> close (worst case for a long): a stop and a target in the same bar means the stop.
* Fixed notional per idea, portfolio limits ignored: it measures the IDEA, not the book. Percentages only.
* Not simulated: exits driven by news (thesis broken, adverse evidence), the earnings-overnight rule, and bearish ideas (the book is long-only; they are counted, not simulated).
* Data: Yahoo's 5-minute chart (regular session only), fetched for the stocks of recent ideas (default 20 a cycle), kept for the log-retention period. If a symbol cannot be fetched it is retried after an hour.

## Settings (Settings -> Missed-trade analysis)
Enabled; look-back days (default 5); stocks fetched per cycle (default 20); time budget per run (default 40 s). These are measurement settings: they are not part of the strategy stamp.
