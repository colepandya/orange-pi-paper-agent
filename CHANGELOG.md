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
