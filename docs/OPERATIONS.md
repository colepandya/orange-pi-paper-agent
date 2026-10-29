# Operations guide

## Is it ready for market open?
`sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent preflight` (or Settings -> Test connections -> **Pre-open checklist**).
It checks services, timer schedule, memory/disk, that cycles and the exit guard are actually running, that news is flowing, that company recognition works on the REAL
watchlist, that >= 2 quote providers return data, and (while the market is open) that two providers agree on a fresh price. Output ends with **READY** or the exact blockers.
US regular session = 9:30-16:00 New York = about 2:30-9:00 am NZ time (3:30-10:00 am after the US clocks change). Freshness can only be proven while the market is open:
re-run it ~5 minutes after the open.

## "Why are there no decisions?"
`python3 -m paper_agent funnel --hours 24` (or the dashboard **Funnel** tab) shows news -> events -> candidates -> decisions -> trades, and for every event the **reason** it
did or did not become a candidate (duplicate story, macro event, no company recognised, direction unclear, merger side unknown, ...), plus findings such as *skipped cycles*,
*no cycle in 2h*, *events flowing but nothing qualifies*. Candidates beyond the per-cycle cap are recorded as `AWAITING_CAPACITY` and retried, never dropped.
`python3 -m paper_agent recall-check` runs 36 realistic headlines through company recognition + qualification on the real watchlist and lists any misses.

## Installing a release (and proving you installed the right one)
1. Download the zip; its name contains the build (e.g. `paper-agent-build28.zip`). 2. `scp` that exact file to the Pi. 3. BEFORE installing, check which build is inside:
   `unzip -p /tmp/paper-agent-build28.zip paper-agent-v5/BUILD`  4. Unpack into a NEW empty folder, then `sudo bash scripts/install.sh`: it prints `Installing Paper Agent BUILD n` and refuses a half-patched folder.
5. Afterwards `cat /opt/paper-agent/BUILD` must show that number. Do not apply patches onto an older install; always install the full zip.

## Database clean-up
See docs/DATABASE.md. `db-maintain plan` is always safe; `db-maintain apply` backs up, archives and verifies before it removes anything.

## Quote providers table (System tab)
Checked every 10 minutes (Settings -> Execution), market open or closed. "Answering; prices are old" is normal when the market is closed. "Last fresh price" is the last time a provider's price was recent enough to trade on.

## Performance tab
Every closed trade, analysed: results by how it ended, by kind of news, by hold time, by time of day and by stock, with the verdict in plain words. It will not judge fewer than ~20 trades. Click "Why?" on any trade to see the full reasoning.

## Earnings, duplicates, recaps
Swing trades stay out of earnings reports and are sold before the close if one would land overnight (needs the Finnhub key; the checklist warns if the calendar is empty). A stock already held is not bought twice.
After-the-fact articles ("Why X stock dropped...") are ignored as catalysts.

## Swing strategy
In and out within ~1-3 sessions: tight stops, small repeatable targets, profits protected (break-even lock after +1%, then a trailing stop). Every number is a dashboard setting (Settings -> Swing strategy).
This is a risk profile, not a promise: there is no guarantee of 1-2% a day. Judge it by the closed positions and their exit reasons.

## Last look, world events, prices after hours
A queued BUY is re-checked at the open against later news about the company (cancel), market-wide shocks (hold) and overnight gaps (cancel) - Settings -> Risk limits.
Pre-market / after-hours prices refresh on your schedule (Settings -> Refresh times) for valuation and early warning only; trades still need a fresh verified quote (one source is enough).
Decisions -> Why? explains any decision in plain English.

## Unattended reports (you do not have to be awake)
Four times per trading day (08:45 / 09:45 / 12:30 / 16:10 New York time) a report is written: **VERDICT: OK / ATTENTION / FAILED**, what opened and closed, BUYs still waiting and exactly why, errors with file and line,
then the full checklist and funnel. Read it on the dashboard **Reports** tab (http://<pi>:8088/?tab=reports), or `cat /var/lib/paper-agent/reports/latest.txt`.
For a phone notification: install the free **ntfy** app, subscribe to a long random topic name, and enter it at Settings -> System -> "Phone push". FAILED reports are sent as urgent.
Write one on demand: `python3 -m paper_agent report`.

## Old errors and false flags
Failures from before the running build (or that you acknowledge with `acknowledge-errors`) are history: kept in the database, shown as an INFO line, never flagged again. `errors` prints the stored traceback
(file, line, function) of the latest failed stages; the funnel shows the same location on each error line.

## What the Decisions statuses mean
| Status | Meaning |
|---|---|
| `REJECTED` / `NO_TRADE` | evaluated and not worth trading (low expected value, low confidence, illiquid, ...). The "why" shows the exact gates. |
| `AWAITING_MARKET` | a BUY that passed every check but the US market is closed. It is re-evaluated by the first cycle after the open (a cycle runs at 09:35 New York) and executed if still good. |
| `AWAITING_QUOTE` | the market IS open but two providers could not confirm a fresh, agreeing price. The reason names each provider (stale / error / no key). It retries every cycle while the news is under 24 h old. |
| `AWAITING_CAPACITY` | more candidates than the per-cycle limit allows, or waiting for room in the portfolio. Retried next cycle. |
| `EXECUTED` | a virtual position was opened (see Positions). |
| `RISK_REJECTED` / `CAPACITY_DEFERRED` / `PAUSED` | blocked by a risk limit / position too small / trading paused. |
| `EXPIRED` | the news got too old to trade (24 h). |
| `ERROR` | an internal error on this one candidate (it never blocks the others). |
**Selling:** exits run every 2 minutes during regular hours (guard) and in every cycle, only on a verified fresh price, never outside regular hours. A position exits on its stop, its target (partial sale, the rest runs
with a trailing exit - or the system re-forecasts and *extends the hold*, which is expected and shown as HOLD_EXTENDED), a time stop, or a broken thesis.

## "Hundreds of events but no decisions" - the cycle was killed
The cycle has a time limit (10 min) and a memory cap. Before build 15, trade candidates lived only in memory between the evidence stage and the entries stage, so a cycle that
was killed in between (slow network calls during company refresh, out-of-memory, a restart) kept the events but **lost every candidate**, silently.
Now: every candidate is written to a durable queue the moment it is created (same transaction as its event); slow enrichment stops early after ~40% of the time budget
(Settings -> System -> Cycle time budget); each candidate and each headline is isolated so one bad item cannot block the rest; every stage is checkpointed so a killed cycle shows
the stage it died in; and cycles that never finished are labelled KILLED. `funnel` / the Funnel tab / `preflight` report all of this.
If a build older than 15 was running when it happened, run `python3 -m paper_agent recover --hours 24`: it re-evaluates the unrecorded events from that period with the current rules and
queues the candidates that are still inside the 24h trade window (the installer does this automatically).

## Memory (730 MB machine)
* A cycle is **skipped** (no news processed, no decisions) when free RAM drops below `runtime.min_available_memory_mb` (default 120). The doctor / checklist / funnel all flag this.
* The installer and tests use a dedicated on-disk folder (`/var/lib/paper-agent/tmp`), never `/dev/shm`. `sudo bash scripts/cleanup-ram.sh` frees leftovers from older versions.
* Heavy jobs (update, backup, company list) are refused when RAM is short and run with low CPU/IO priority and a memory cap.

## Timers and restarts
* Timers start relative to **when they are started** (`OnActiveSec`), not boot, so restarting one can never trigger an instant stampede of jobs.
* "Restart guard + cycle + dashboard" restarts only those three, in a safe order. update / backup / company-list are **jobs**: use their *Run now* buttons.
* Dashboard schedule settings are applied to systemd by a small root helper; the Services panel shows **configured vs. running** for every timer, and each cycle auto-heals drift.

## Settings survive deployments
Every setting records when and by whom it was changed. A release can ship `config/deploy-settings.json` entries:
`{"id": "...", "key": "risk.max_position_pct", "value": 0.07, "released": "2026-10-30T00:00:00Z", "force": false, "note": "..."}`
* setting you never changed -> applied; * you changed it -> **not applied**, shown as a suggestion (Use release value / Keep mine); if your change is newer than the release it is dropped;
* `force: true` -> applied exactly once per entry `id` (a later redeploy never re-forces it).
API keys: a deploy only fills **missing** keys; it can override one only via `FORCE_KEYS=...` + a new `FORCE_ID=...` in the repo's `config/market-data.env`, once. Keys saved in the dashboard always win.

## Useful commands
`status` · `doctor [--fix]` · `preflight` · `funnel` · `recover` · `recall-check` · `probe` · `quote SPY` · `apply-deploy` · `llm-test` · `llm-eval` · `backup`
(all via `sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent <command>`)
