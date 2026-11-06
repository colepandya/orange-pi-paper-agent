# The shadow book (Build 49)

Every decision the bot takes, traded or not, is followed to a COSTED outcome. The point is sample size: the bot trades about 26 times a week but decides on more than a thousand ideas, so a gate or a threshold can be judged on hundreds of outcomes instead of a handful. **It is observation only: it reads the decisions table and the stored daily bars, writes its own two tables, and nothing it computes is ever fed back into a trade.** A test runs a full simulated morning with it on and off and compares every decision, position and dollar.

## Conventions (fixed before any result was looked at)
* **Side:** the sign of the idea's direction. Ideas weaker than `shadow.min_abs_direction` (0.15) carry no view and are not followed.
* **Entry:** the decision-time reference price if the regular session was open; the same day's open if it was decided before the open on a trading day; otherwise the next trading day's open.
* **Horizons:** 1, 3, 5 and 10 sessions. Horizon h is the close of the h-th session, counting the entry day as session 1.
* **Cost:** `shadow.cost_bps` (30) round trip comes off every outcome. It matches the 0.3% your real round trips showed.
* **Market adjustment:** every outcome is also measured against SPY over the same window ("excess"). A long beats the market by rising more than it; a short by falling more. **The verdict on a gate uses the market-adjusted outcome**, because one week's market drift otherwise looks like skill.
* **Completed days only:** a day counts once it is past 16:30 New York time.
* **One observation per stock, entry day and side:** several ideas about one stock on one day are one bet.

## What it reports (Intelligence tab, and `python3 -m paper_agent shadow`)
* Traded vs not traded; LONG vs SHORT, with the market's own move over the same windows.
* **By gate:** what each gate blocked, had it been taken: mean net, mean vs market, a bootstrap 95% interval, an evidence label (insufficient < 20 observations, informational < 50, preliminary < 100, review candidate >= 100) and a reading. A reading other than "insufficient" or "no evidence either way" needs at least 30 observations and an interval that excludes zero.
* **EV calibration:** does a higher estimated EV earn more? Quintiles, a rank correlation, and a permutation p-value.
* **By event type**, with a shrunk mean that pulls a small sample toward the overall mean.

## What it cannot tell you
Daily bars only: an intraday stop or target is not simulated. A flat cost, no market impact. Outcomes are hypothetical. Many gates and event types are compared at once, so some will look special by chance and the intervals are NOT corrected for that. The first weeks are one market regime. **Treat "review candidate" as a question to investigate, not a verdict, and change a gate only after the same finding survives a different period.**

## Commands
```
PA="sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent"
$PA shadow                 # the report (3-session outcomes)
$PA shadow --horizon 5     # 1, 3, 5 or 10 sessions
$PA shadow status          # coverage only
$PA shadow sync            # capture new decisions now (the cycle does this)
$PA shadow measure         # measure everything measurable now
```
Settings: Settings -> Shadow book (`shadow.enabled`, `shadow.cost_bps`, `shadow.min_abs_direction`, `shadow.measure_per_cycle`, `shadow.max_seconds`). It needs no new network traffic: it uses the daily bars the bot already stores.
