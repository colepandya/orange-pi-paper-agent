# Instruments: long, short, calls, puts (build 39, strategy S4)

All trading is paper money. The bot now judges every idea from the POSITION's point of view and can express it four ways.

| Instrument | When | Entry | Exits | Real data? |
|---|---|---|---|---|
| Stock, long | a bullish idea that passes every gate | buy at the quote + slippage | stop below, target above, ratchet up, time | real quotes |
| Stock, short | a bearish idea that passes every gate AND the shortability rules | sell at the quote - slippage; proceeds credited, 150% held back as collateral | stop ABOVE, target BELOW, ratchet DOWN, time; covering pays the quote + slippage | real quotes; borrow fee is a flat rate |
| Call | a strong (edge >= 3%), short (<= 5 sessions), liquid (>= $100M/day, >= $20) bullish idea | model price at the ask | stock stop/target, premium -50% / +100%, 5 days left, time, expiry settlement | **MODEL-priced** |
| Put | the same, bearish | model price at the ask | mirrored | **MODEL-priced** |

## What a short must pass (instruments.*)
price >= $5, market cap >= $2B, volume >= $20M a day, not already up 20% in 5 days (squeeze), sized at 60% of an equal long, total shorts <= 25% of equity, borrow fee 0.5% a year charged daily, 150% of each short reserved so proceeds cannot be spent.
A market-wide shock does NOT hold a short (it helps it); good news about the company after the decision vetoes it, mirroring the long rule.

## Options are an experiment
There is no options data feed. Prices come from Black-Scholes with the stock's recent volatility x 1.15 (a little more around earnings), a spread of 4% (at least 3 cents) each side and $0.65 a contract. Real chains have skew, an implied volatility that collapses after news and wider spreads in a fast market that this ignores. The sleeve is capped at 5% of equity in premiums (1.2% a trade), the most a bought option can lose is its premium, and results are shown on their own row. Writing options and spreads are NOT supported.

## The edge is what the plan can collect (strategy.plan_consistent_ev)
EV = sum(P(scenario) x clip(return, -stop x 1.5, +target)) - costs. The old forecast could claim +6% for a plan that sold at +4%. On the owner's data 40 of 42 buys claimed more than their own target; none do now.

## Where to see it
Positions (Side column, options table), Results -> "By instrument" (trades, win rate, P&L, average, hold; borrow fees), Decisions -> "related companies".
Switch any instrument off in Settings -> Instruments; doing so starts a new measurement period (it is part of the strategy stamp).
Rollback of the whole build to the old behaviour: `shorts_enabled=false, options_enabled=false, graph.enabled=false, plan_consistent_ev=false, open_delay_minutes=0, corr_block=1.01` (tested: identical to build 38).
