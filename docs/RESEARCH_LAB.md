# The research lab (Build 54)

The bot used to judge its own assumptions on about a year of prices and a few dozen trades. A year is one market regime: almost any pattern can be found in it by accident. The lab tests an idea on MORE history, honestly, BEFORE a trading rule is changed to use it. **It never changes a trade.**

## What runs
* **History backfill** (`history.*` settings): `history.years` (5) of daily bars for the stocks the bot tracks, `history.symbols_per_cycle` (3) at a time, so the first full backfill takes about a day. 5 years of 400 stocks is about 40 MB of database. A rate-limit error stops the cycle's fetching; a failing symbol backs off 6, 12, 24, 48 hours, then retries weekly. Closes are split-adjusted, not dividend-adjusted.
* **Studies** (`research.*` settings): once a week while the US market is closed, ONE study per cycle, each with a time limit (`research.max_seconds`). A study too slow for the Pi is retried, and after three tries left to the command line.

## The studies
| Study | Question | Why it matters |
|---|---|---|
| post_move | What do stocks do after a 5-day move of each size? | The forecast subtracts 80% of the prior move (capped at 16%) as "already priced in". |
| earnings | After an earnings reaction of each size, does the stock continue or reverse? | Earnings events are a large share of the bot's ideas. |
| big_days | After a 5%+ move on twice normal volume with no earnings, does it continue or reverse? | The same question for other news. |
| calibration | When the forecast says 70%, does it come true 70% of the time? | Confidence drives size and the minimum-confidence gate. |
| exit_plans | What does a stop-and-target plan earn with NO signal? | It is the bar a signal must clear: `signal must add` is the gross edge per trade needed to break even. |

## How the numbers are kept honest
* Every return is the stock's minus SPY's over the same days.
* Stocks on the same day move together, so the observations of a day are averaged first and the t-statistic is taken across DAYS.
* The final 365 days are held out. The train/held-out split appears only when both halves have at least 30 days (with one year of data it does not).
* Each table says how many comparisons it contains: about one in twenty will look significant by chance.
* Exit simulation on daily bars: if the stop and the target are both reachable in one day the STOP is assumed first; a gap through a stop fills at the open (a loss bigger than the stop) and a gap over a target fills at the open (a gain bigger than the target). Costs are `shadow.cost_bps` round trip.

## Using it
```
PA="sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent"
$PA research status          # history coverage and the last run
$PA research report          # the last run
$PA research run             # run every study now (about a second on a year of data; longer with 5 years)
$PA research run --study calibration
$PA research history --limit 50   # fetch up to 50 pending stocks now, 2 seconds apart (needs the network)
```
The Intelligence tab shows the same tables. **A finding is a reason to test an idea going forward in the shadow book, never a reason to trade it.**
