# Build 30
* **A "shock" must move the market.** After build 29 a war story ("At least 14 people killed in attack on Ukraine train station": war_escalation, many outlets) still froze every BUY for 6 hours. A headline of a dangerous kind reported by 2+ outlets now only freezes new BUYs
  if SPY is ALSO down at least 1% versus its previous close (live quote from the 10-minute probe, else today's bar). If the market's move is unknown, a headline alone is not proof. Settings -> Risk limits: "Market must also be down at least" (0 = headlines alone suffice).
* **US states, countries and cities can no longer be company names** ("...over Alaska campaign investigation" was a trade candidate for Alaska Air Group). The full names still match ("Alaska Air Group", "Texas Instruments").
* **A failing retry is never silent.** If retrying a waiting BUY threw an exception, the decision was left exactly as it was (still "waiting", old note showing) and a NEW `ERROR` decision was added every cycle: from the Funnel it looked stuck, with no reason.
  The failure is now written onto the waiting decision itself ("retry failed (2 in a row) at 08:42 UTC: <error>"), which the Funnel and Decisions pages show as what it is waiting for; no new rows are created; five failures in a row turn it into an `ERROR`;
  and the note disappears as soon as a retry works. A brand-new candidate that fails is still recorded as an `ERROR` decision, as before.

# Build 29
* **Why NO queued BUY executed at the open** (found by replaying the owner's real database: 68 queued BUYs, 0 executed; after the fixes the same open buys 8 stocks, ~$538k, sized by edge). Four rules were rejecting almost everything for the wrong reasons:
  1. **News aged on the wall clock.** A story from just after the close lost ~14 of 20 eligibility points overnight while the market was shut (27 rejections, all 52-65 vs a 65 threshold). News now ages on the MARKET clock
     (regular session = 1h/h, pre-market and after-hours = a quarter, nights/weekends/holidays = 0); nothing is traded on news over 3 days old by the wall clock either.
  2. **"Market-wide shock" holds fired on non-shocks** ("Bitcoin weakens as hawkish Fed", an SEC filing header, a video-game row; 7 holds). A shock is now a dangerous KIND of event (crash, war escalation, emergency policy, financial stress, exchange outage)
     confirmed by at least 2 outlets (Settings -> Risk limits -> "Outlets needed to confirm a market-wide shock").
  3. **The "bad news since" veto fired on any story that merely mentioned the ticker** (market wraps, roundups, "Alphabet slides, Microsoft firms" cancelled MICROSOFT; 13 cancellations). A story now cancels a BUY only if it is ABOUT that company (the first company its headline names);
     roundups, commentary and recaps never cancel.
  4. Swing minimum reward:risk 1.2 -> 1.0 (still editable). The remaining rejections of that kind have targets on the 1.2% floor against 1.5% stops: roughly zero expectancy after costs, correctly skipped.
* **SEC filing headers are half of everything ingested** (4,721 of 9,389 events) and were classified from the company's NAME ("Acquisition Corp" = merger, "Item 5.07: vote" = election, "Private Credit Fund" = credit stress; 94 junk election events, 29 junk mergers).
  They are now read for what they say: neutral by default, and a clear adverse signal for the 8-K items that really are bad news (1.03 bankruptcy, 3.01 delisting, 4.02 non-reliance, 2.04, 2.06, 1.02), which the veto uses.
* `python3 -m paper_agent requeue [--apply]`: re-opens recent BUYs that were turned down by the now-corrected rules (eligibility-by-age, old-style vetoes, non-shock holds) so they are judged afresh against current prices and news. Real rejections (a real gap, poor reward:risk, wrong company, duplicates) stay final.

# Build 28
* **Database clean-up** (from analysing the owner's real database: 30 MB, 103 tables, only 58 used by this version; 95% of `decisions` written by the retired v4 app; news and events growing ~130 MB a month with nothing ever deleted).
  `python3 -m paper_agent db-maintain plan` shows exactly what would happen (changes nothing); `... apply` takes a verified backup, archives the legacy tables and the 4,962 v4 decisions to a separate file (row-counted on disk BEFORE
  anything is dropped), expires old news/events/run logs that nothing live points at, then VACUUMs. On a copy of the real database: 30.3 MB -> 19.4 MB, 103 -> 60 tables, the 250 current decisions and every table the app uses byte-for-byte identical.
  Refuses to run during the US session. Undo instructions: docs/DATABASE.md.
* **Weekly automatic retention** (Saturdays, US market closed; Settings -> Database: on/off, days to keep news and logs). It only ever expires old data; it never archives or drops legacy tables.
* **System tab "Database" card** and a `[database]` line in the doctor: size, what is left over from the old app, when it was last cleaned.
* Schema: removed two tables nothing ever used (`event_impact_forecasts`, `position_event_links`); added the two indexes the query plans asked for (`forecasts.decision_id`, `provider_calls.ts`).

# Build 27
* **A queued BUY must still be about that company.** BUYs queued by older builds could rest on a looser company match (a company named only in the body of someone else's story) and would still have executed at the open.
  The last look now re-checks the stored headline: if it names companies and this ticker is not one of them, the BUY is cancelled ("the headline is about STX, not AVGO"). Supply-chain / thesis candidates (which carry a reason) are left alone.
* **Commentary is not news.** Questions, listicles, "I'd buy", "stocks to buy", earnings-call recaps and fund-holdings posts are neutral (new reason: `commentary_not_news`). Found in the live funnel:
  "Is Earnings Beat Altering The Investment Case For Equinix (EQIX)?" had scored +0.78 and "2 Reasons I'd Buy Marvell Stock" +0.33; "Best AI Stock to Buy in November" had been classified as a merger.
* **Pre-market gaps now say what will happen:** "[trading -18.4% vs previous close, pre-market, 8 min ago -> WILL BE CANCELLED at the open if it holds (gap down)]" in the funnel and the morning report, using YOUR gap thresholds.

# Build 26
* **Wrong-company attribution fixed at the source** (an outside review found it; all eight examples reproduced): the headline now decides what a story is about (a company named only in the body, or after "with / from / on", counts 60%);
  "NASDAQ" / "listed on the Nasdaq" no longer means Nasdaq, Inc.; first names, common surnames, AI product names and generic legal names ("Bancorp, Inc.") can no longer BE a company; "Fed Chair Powell" is a person.
* **Headlines that contradict themselves are not trades:** "GOOGL Stock Slumps 11% ... Earnings Beat" scored +0.85. Good news with a falling stock (or the reverse) is now neutral; a headline move of 10%+ is "already priced in"; a smaller aligned move is discounted.
* **Position size now follows the edge.** Kelly saturated at 100% for every idea, so a 0.7% edge was sized like a 4% one. Size now scales with conviction (edge / typical move; Settings -> Risk limits -> "Edge needed for a FULL-size position").
* Queued BUYs expire by the NEWS's age (age when decided + time waiting), not just the decision's age. The eligibility message shows two decimals ("64.96 < 65.00"). Full review verdicts: docs/REVIEW_FINDINGS.md.

# Build 25
* **Quote providers table is now true.** It used to change only when a trade needed a price, counted a provider that answered with an OLD price (market closed) as a failure, kept alarming leftovers from the old app
  (UNKNOWN, TIINGO, EODHD "DEGRADED, never"), and showed no "last checked" time. Now a quick SPY probe checks every configured provider every 10 minutes (Settings -> Execution; 0 = off), market open or closed.
  Plain-English statuses (working / answering but prices old / not answering / key rejected / rate limited), "Last checked" and "Last fresh price" columns, and old-app rows listed quietly as "not in use".
  The probe only observes: it never pauses a provider, never overwrites a newer after-hours price, and protects scarce daily quotas (Alpha Vantage ~20/day is checked at most every 3 hours).
* **Fixed: a rate-limited Twelve Data (and Alpha Vantage) was never paused.** Errors were stored under the wrong name ("TWELVE"), so the pause lookup never found it. Pauses also used the real clock instead of the cycle clock.
* **Fixed: the Swing Strategy settings were not shown on the Settings page** (18 settings existed and were validated, but the page lists its groups explicitly and the new group was missing). New test: every setting must be on the page and the page must round-trip.
* Dashboard pages use the full window width (they were capped at 1300px and centred); an empty Amount shows "-" (amounts exist only once a BUY executes).

# Build 24
* **Fixed: BUYs queued by an older build had no overnight-gap protection.** They stored no "previous close", so the gap check silently skipped them (in testing one executed straight into a -9% gap). While the market is
  closed, every waiting BUY that lacks a reference now gets the last close filled in (never during the session, never over an existing reference, never from stale data).

# Build 23
* **Performance tab (new):** insights on every trade: realized P&L, win rate, profit factor, average win/loss, average hold, cumulative and daily P&L charts, and breakdowns by how trades ended, kind of news, hold time,
  time of day and stock; biggest wins/losses and every closed trade with a "Why?" link; what happened to every BUY idea. A plain-English verdict that refuses to judge fewer than ~20 trades and points at specific
  problems (too many stop-outs, trades going nowhere). Window: 7 days / 30 days / all. The Overview now shows "Realized today".
* **Never stack a position:** a second BUY in a stock already held is skipped (DUPLICATE), strongest idea first; Settings -> Risk limits to allow adding.
* **After-the-fact articles are not catalysts:** "Why Garmin Stock Dropped After Record Results", "Is X Stock a Buy?", "Here's why X jumped this week" are recognised as recaps and ignored (that Garmin article had become a +0.77 BUY candidate).
* **Earnings awareness:** a swing trade is not opened when an earnings report falls inside its holding window (unless the news IS the release), and a position is sold before the close when a report would land overnight.
  Data: Finnhub earnings calendar (refreshed ~every 11 h, fails soft). The pre-open checklist warns loudly if no calendar is loaded.
* The after-close report now states the day's realized P&L.
* Verification note: mutation checks are now run with bytecode caching disabled (a stale compiled file had been able to hide a result).

# Build 22
* **Swing profile (default ON, all editable in Settings -> Swing strategy):** holds capped at 3 sessions (a runner may run 1 more), stops 1.2-3% (1x the stock's daily move), targets 1.2-4%, reward:risk >= 1.2, stocks that move < 0.8%/day skipped,
  60% sold at the target and the rest runs behind a protected stop, **break-even lock after +1% and a 1% trailing stop** (the stop only ever moves up), optional "sell before the close" day-trade mode. Swing mode counts only the part of a
  story's expected move that fits the shorter window, so EVs are lower and fewer BUYs pass. Turn it off to restore the old multi-week behaviour.
* **Last look before money moves:** a BUY queued before the open is re-checked against everything since it was decided. Bad news about that company cancels it; a market-wide shock holds it (default 6 h); a gap beyond -3% / +5% cancels it.
* **World events are no longer invisible:** crashes, war, emergency central-bank action, bank failures and exchange outages are now recognised as risk-off market shocks (before: "China launches missile strikes on Taiwan" was a BULLISH product launch).
* **Quotes:** one fresh source is enough by default; agreement is judged among fresh quotes only; a lone price must be within 20% of the last close.
* **Prices refresh while the market is closed** (pre-market / after-hours; Settings -> Refresh times). Positions show "after-hours, 4 min ago"; waiting BUYs show where they trade versus the price they were decided against.
* **Decisions -> Why is now plain English:** what happened, the trigger, the checks in words (tick / cross), the numbers, the plan, what would change it, and a timeline. The table shows a one-line headline.
* Unlimited BUYs per cycle by default (funds and risk limits are the real limits).

# Build 21
* **Unattended reports** (nobody has to be awake): at 08:45, 09:45, 12:30 and 16:10 New York time on trading days (about 01:45, 02:45, 05:30 and 09:10 NZ time; daylight-saving automatic) the system runs the full
  pre-open checklist (live) + decision funnel and writes a plain-English report with a VERDICT (OK / ATTENTION / FAILED): what opened, what closed, what is still waiting and why, and any errors with their location.
  Read it on the dashboard **Reports** tab or in `/var/lib/paper-agent/reports/latest.txt`. A report is ALWAYS written, even if building it crashes.
* **Optional phone push** (Settings -> System -> "Phone push: ntfy.sh topic"): the verdict and key lines are sent to your phone, with urgent priority when FAILED.
* Report labelling looks forward to the next open (a pre-open report is no longer mislabelled "close").

# Build 20
* **No per-cycle position limit by default.** `risk.max_new_per_cycle` now defaults to 0 = unlimited (it was 3, a pacing guard, not a funds check). Settings -> Risk limits accepts 0-200.
  The real limits still always apply: 20% cash reserve, 80% max invested, per-company 15%, per-theme 30%, stress-loss cap, risk-per-trade sizing, and the cycle time budget.

# Build 19
* **Old errors no longer raise false flags** (and nothing is deleted): errors from cycles that started before the current build, or that you acknowledge (`acknowledge-errors`), become history. The funnel,
  doctor and pre-open checklist count only errors since then; a NEW error always shows. History is still mentioned and kept in the database.
* **Every stage error now records exactly where it happened** (file:line function) and the funnel shows it; new `errors` command prints the stored tracebacks.
* Fixed the order in which baseline markers are chosen.

# Build 18
* **Fixed (the real cause of the 64 failed cycles and zero trades): `entries: OperationalError: no such column: backoff_until`.** The database began as an older install whose `provider_health` table lacked
  that column; `CREATE TABLE IF NOT EXISTS` never adds columns, so every real quote lookup failed - but only while the market was open (the only time quotes are fetched). Migration now compares the REAL
  database with the intended schema and adds every missing column to every existing table (data preserved), then creates indexes. Runs automatically on install and on every start.
* **New checks that would have caught it:** `[schema]` line + HIGH issue in the doctor; "Database schema matches the code" and "Recent cycles free of stage errors" in the pre-open checklist (which previously
  said READY); the checklist's live quote check now goes through the database exactly like a real BUY.
* New tests start from LEGACY-shaped databases (every table missing every non-key column), including the complete buy -> wait -> open -> sell lifecycle.

# Build 17
* **Fixed (the cause of cycles ending in ERROR): numbers from providers arriving as text** ("28.4", "N/A", "", null) crashed the company scoring and the BUY path with `TypeError`. One strict `num()` is now
  used at every parse boundary, scoring defends itself, and numeric columns are neutralised on read. Found with the new "fake internet" tests (below).
* **Fixed: false company matches.** The first-word alias rule treated ordinary words as company names ("Stock" -> Stock Yards Bancorp/SYBT, "Here" -> HERE, "Work" -> WOK, "Context-Aware" -> AWRE).
  Now: a broad stoplist, 5-letter minimum, words learned as lowercase from the stored news (refreshed daily), compounds ("Context-Aware") ignored, generic two-word phrases ("Main Street") never aliases.
* **New: whole-system tests against a simulated internet** (`tests/fake_internet.py`): real network layer, parsers, company refresh and quote consensus; clean, degraded (empty, nulls, strings, HTML, truncated,
  HTTP 403/429/500) and malformed news items; buying AND selling through the full stack.
* One failing company refresh can no longer abort the rest of the company stage; failures are recorded.
* **The funnel / doctor now print the actual stage-error messages** (grouped, with counts), not just "N cycles had errors".
* **Installer refuses hybrid trees**: leftovers of a failed `patch` (`*.rej`/`*.orig`) or any file that differs from the packaged release (SHA-256 manifest) abort the install before anything is touched. It also prints
  the BUILD it is installing. `publish.sh` refuses to publish such a folder.
* Release zips are now named by build (`paper-agent-build17.zip`) so an older download can never be copied by mistake.

# Build 16
* **BUY -> position -> SELL is now tested end to end through the REAL quote-consensus code** (fake Twelve Data / Finnhub / Alpaca / Yahoo responses): overnight wait, execution at the open, stale / disagreeing / missing quotes,
  stop, target + runner, time stop, thesis exit, after-hours, stale-quote exits, idempotent guard, ledger reconciliation. Previously every test used a stub that always answered PASS.
* **Fixed: "open market but no verified price" looked identical to "market closed"** (both AWAITING_MARKET). New status `AWAITING_QUOTE`, with a per-provider reason (stale / error / no key / need 2 agreeing).
* **Fixed: waiting BUYs were fully re-evaluated (with market-data calls) every 10 minutes all night.** They now wait cheaply until the open.
* **New: a cycle runs at 09:35 New York time** (systemd handles daylight saving) so BUYs that waited overnight execute promptly after the open.
* **Visibility:** the Decisions table shows what each waiting BUY is waiting for; the Funnel tab, `funnel` and the pre-open checklist list waiting BUYs and flag any stuck without a verified price.
* **Fixed: quote freshness used the real clock while the cycle used its own** (now one clock; this is what made the path untestable).

# Build 15
* **Fixed (the "events but no decisions" bug): trade candidates were only held in memory** between the evidence and entries stages. A cycle killed in between (timeout, out-of-memory, restart)
  kept its events but lost every candidate, with no error. Candidates are now written to a durable queue in the same transaction as their event and removed only once a decision exists.
* **Cycle time budget**: slow company/filing refreshes stop early so decisions are never starved; entries have their own deadline; leftovers stay queued for the next cycle.
* **Isolation**: one malformed headline can no longer abort the whole evidence stage (it used to be retried and fail forever); one failing candidate becomes a visible `ERROR` decision instead of aborting the rest.
* **Traceability**: every stage is checkpointed before it starts; cycles that never finished are labelled `KILLED`; the funnel / checklist report them with the stage they died in and show the slowest stages.
* **New `recover` command** (run automatically at install): rebuilds the candidates lost before this fix from the events still in the database.
* Recent news is evaluated first (an old backlog can no longer starve fresh, tradable items). Classifier is ~3x faster (patterns compiled once).
* Fixed mixed real/injected clock bugs (`mark_run`, source scheduling, health stamps); the whole test suite now passes at any wall-clock date.

# Build 14
* **Fixed: news items silently dropped.** Company recognition used SEC legal names only ("UBER TECHNOLOGIES INC") so "Uber", "Ford", "Amazon", "Meta", "Disney", "JPMorgan", ... were never found; common headline wording
  ("beats subscriber estimates", "signs a contract", "recalls", "warns") was never classified. On a realistic benchmark candidates went from 38% to ~97%, with 0 false positives.
* **Fixed: candidates beyond the per-cycle cap vanished** (a `break` discarded them with no record). They are now recorded (`AWAITING_CAPACITY`) and retried.
* **Fixed: acquirer-only merger headlines produced no decision at all.** Now a visible no-trade decision.
* **New: decision funnel** (every event records why it did / did not qualify), `funnel` command + dashboard tab, `recall-check`, `preflight` pre-open checklist.
* **Fixed: "Restart everything" stampede.** Timers were anchored to boot (`OnBootSec`) so a restart fired cycle + guard + update (installer + tests) + backup + universe at once and ran the Pi out of RAM.
  Timers now use `OnActiveSec`; restart-all touches only guard/cycle/dashboard; heavy jobs are memory-guarded and resource-limited.
* **Fixed: installer/test temp files in RAM (`/dev/shm`).** Dedicated on-disk temp dir, janitor + `scripts/cleanup-ram.sh`, install lock, low-memory postponement; tests are hermetic and delete their temp files.
* **Fixed: auto-update could kill itself** (it disabled its own unit while running). Merged the legacy-unit cleanup fix.
* **New: dashboard schedule settings are verified against systemd** (configured vs running), re-applied on install, and auto-healed every cycle.
* **New: settings survive deployments** (modified-on stamps, release suggestions, one-time explicit force). Keys file no longer overwrites existing keys.
* **New: Finnhub 40-character (key+secret) value is split and the working half used automatically.**
