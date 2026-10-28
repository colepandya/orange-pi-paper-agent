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
`status` · `doctor [--fix]` · `preflight` · `funnel` · `recall-check` · `probe` · `quote SPY` · `apply-deploy` · `llm-test` · `llm-eval` · `backup`
(all via `sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent <command>`)
