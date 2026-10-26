# Current Deployment State

**Last verified:** 26 October 2026 21:00 NZDT  
**Deployed release:** `v3.6.2`  
**Mode:** Virtual-only

## Host

- Device: Orange Pi Zero 3
- OS: Orange Pi Bullseye, Linux 6.1.31-sun50iw9
- LAN address: `192.168.1.91`
- RAM: approximately 730 MB
- Application: `/opt/paper-agent`
- Configuration: `/etc/paper-agent`
- Database: `/var/lib/paper-agent/paper-agent.db`
- Backups: `/var/backups/paper-agent/`
- Dashboard: `http://192.168.1.91:8088`
- Dashboard timezone: `Pacific/Auckland`

## Verified health

```json
{"ok": true, "version": "3.6.2", "mode": "virtual-only", "port": 8088}
```

## Verified active units

- `pihole-FTL`
- `paper-agent-dashboard-v362.service`
- `paper-agent-broad.timer`
- `paper-agent-v36.timer`
- `paper-agent-position-monitor-v362.timer`
- `paper-agent-calibration-v36.timer`
- `paper-agent-v34.timer`
- `paper-agent-research-v34.timer`
- `paper-agent-backup-v34.timer`

`paper-agent-position-monitor-v362.service` is a oneshot service. `inactive (dead)` after `status=0/SUCCESS` is normal; the timer must be active.

## Verified processing cadence

```text
Broad collector:   every ten minutes near :00/:10/:20/:30/:40/:50
V3.6 forecast:     approximately five minutes after each boundary
Position monitor:  approximately six minutes after each boundary
Dashboard refresh: every 60 seconds
```

## Verified position-monitor state

The first verified V3.6.2 review reported:

```json
{
  "version": "3.6.2",
  "open_positions": 4,
  "events_reviewed": 0,
  "plans_changed": 0,
  "details": []
}
```

This is a valid idempotent result: all open positions were checked, but no newly ingested relevant events existed after the active plans were created.

## Verified open-plan coverage

- Database integrity: `ok`
- Open positions: `4`
- Active position plans: `4`
- Coverage: `PASS`

At verification time, the open lots were SPCX, VINP, and two MSFT lots. Each had one active V3.6.1-origin forecast plan. V3.6.2 continuously monitors those plans and creates a new version only when subsequent evidence changes the thesis materially.

## Current operational capabilities

- Broad multi-source collection and source-health handling
- Verified US-equity mapping
- Virtual decision and position ledger
- Single-page dashboard on port 8088
- NZST/NZDT display while storing timestamps in UTC
- Event-impact forecasts with bear/base/bull scenarios
- Priced-in adjustment and residual-return estimation
- Dynamic target, stop, maximum hold, partial-exit, runner, probability, confidence, and allocation proposals
- Inline forecast detail in the Decision Ledger
- One active versioned plan for each open position
- Continuous company/theme/world-event reassessment after collector cycles
- Append-only plan history and event-to-position links
- Shadow strategy comparison and weekly calibration scaffolding
- Online backup, reconciliation, and research timers

## Current limitations

- Dynamic V3.6 plans are not yet consumed by an automated V3.6-aware exit executor.
- No broker or real-money connection exists or is permitted.
- Position monitoring relevance currently relies on bounded ticker, theme-token, and macro-keyword logic; this must mature toward richer entity/sector/macro modelling.
- Outcome and calibration coverage remains incomplete until sufficient 1/3/5/10/15-session observations accumulate.
- Point-in-time fundamentals are incomplete for many events.
- Displayed quote freshness must be reviewed before interpreting P&L or an exit level as executable.

## Current source-control state

- Canonical repository: private `colepandya/orange-pi-paper-agent`
- Intended branch: `main`
- Intended release tag: `v3.6.2`
- Runtime database, WAL/SHM files, configuration, watchlist, secrets, caches, and transient state are excluded from Git.

## V3.6.3 target state

V3.6.3 adds `paper-agent-executor-v363.service` and `paper-agent-executor-v363.timer`. The timer runs at approximately `:07/:17/:27/:37/:47/:57`, after collection, forecasting, and position monitoring. New tables are `position_lifecycle`, `virtual_exit_actions`, `target_reforecasts`, `executor_runs`, and `v363_price_ranges`. The dashboard health identity becomes `3.6.3` and includes lifecycle and virtual-exit history. This state becomes current only after target-device installation and verification.
