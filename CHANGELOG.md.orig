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
