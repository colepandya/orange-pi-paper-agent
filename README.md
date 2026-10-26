# Orange Pi News-Driven Virtual Paper Agent

> **Authoritative source:** This private repository is the single source of truth for the project. Before proposing or deploying changes, read `README.md`, `CURRENT_STATE.md`, `DECISIONS.md`, `CHANGELOG.md`, the latest release tag, and the relevant source and tests. Work from the latest deployed commit, not an old ZIP file or remembered chat context.

## Future-session prompt

Use the private `colepandya/orange-pi-paper-agent` repository as the authoritative source. Read `README.md`, `CURRENT_STATE.md`, `DECISIONS.md`, and `CHANGELOG.md` first. Preserve virtual-only operation, Pi-hole availability, the verified US-equity universe, entry eligibility score 65, bounded exposure, fresh-quote requirements, append-only research history, port 8088, and Pacific/Auckland dashboard times. Run the complete offline test suite and define target-device probes before providing deployment commands.

## Purpose

Paper Agent is a fully virtual, news-driven US-equity research platform running on an Orange Pi Zero 3. It collects broad public information, maps events to verified listed companies, records decision evidence before outcomes are known, creates virtual positions only in SQLite, forecasts event impact, continuously reassesses open-position plans, measures outcomes, and compares strategy variants.

**There is no brokerage connection, no order routing, and no real money.** All purchases, exits, P&L, allocations, and strategy plans are virtual research records.

## Current release

**Release package:** `v3.6.3` (deployed state must be verified after installation)

V3.6.3 adds an end-to-end virtual lifecycle executor on top of live open-position monitoring on top of V3.6 event-impact forecasting and the V3.6.1 single-page dashboard hotfix.

The platform now:

- separates trade eligibility, expected impact, and forecast confidence;
- derives target, stop, holding period, partial-exit, runner, and allocation proposals from event evidence rather than fixed percentages;
- records bear, base, and bull scenarios;
- adjusts for estimated information already priced into the stock;
- stores immutable forecast inputs, derivations, invalidation conditions, and hashes;
- displays forecasts inside each Decision Ledger item;
- applies the latest forecast plan to every open virtual position;
- reviews open positions after every collector cycle for new company, theme, sector, macro, and world events;
- creates a new versioned position plan only when evidence changes the thesis materially;
- preserves plan history and one active plan per open position;
- keeps collector and position-review timing visible at the top-right of the dashboard.

## Current deployed environment

| Item | Value |
|---|---|
| Device | Orange Pi Zero 3 |
| OS | Orange Pi Bullseye, Linux 6.1.31-sun50iw9 |
| LAN address | `192.168.1.91` |
| RAM | Approximately 730 MB |
| Shared critical service | Pi-hole |
| Application | `/opt/paper-agent` |
| Configuration | `/etc/paper-agent` |
| SQLite database | `/var/lib/paper-agent/paper-agent.db` |
| Backups | `/var/backups/paper-agent/` |
| Dashboard | `http://192.168.1.91:8088` |
| Health endpoint | `http://127.0.0.1:8088/health` |
| Dashboard timezone | `Pacific/Auckland` |
| Repository | `colepandya/orange-pi-paper-agent` |
| Operation mode | Virtual-only |

## Active services and timers

| Unit | Purpose |
|---|---|
| `pihole-FTL` | Shared Pi-hole DNS service and deployment gate |
| `paper-agent-dashboard-v362.service` | V3.6.2 single-page dashboard on port 8088 |
| `paper-agent-broad.service` | Broad public-information collector |
| `paper-agent-broad.timer` | Collector every ten minutes |
| `paper-agent-v36.service` | V3.6 forecast and exposure worker |
| `paper-agent-v36.timer` | Forecast worker after collector cycles |
| `paper-agent-position-monitor-v362.service` | Reassesses open positions using newly collected events |
| `paper-agent-position-monitor-v362.timer` | Position review after each collector/forecast cycle |
| `paper-agent-calibration-v36.service` | Forecast calibration worker |
| `paper-agent-calibration-v36.timer` | Weekly calibration schedule |
| `paper-agent-v34.service` / `paper-agent-v34.timer` | Existing V3.4 reconciliation and supporting research jobs |
| `paper-agent-research-v34.service` / timer | Existing bounded research comparison |
| `paper-agent-backup-v34.service` / timer | Online SQLite backup and retention |

A oneshot service such as the position monitor normally returns to `inactive (dead)` after `status=0/SUCCESS`. Its timer must remain active.

## Processing sequence

The intended ten-minute sequence is:

```text
Broad collector:   :00, :10, :20, :30, :40, :50
V3.6 forecast:     :05, :15, :25, :35, :45, :55
Position monitor:  :06, :16, :26, :36, :46, :56
```

The position monitor intentionally runs after collection and forecasting so preceding database writes can complete first.

## Strategy architecture

### 1. Eligibility

Eligibility answers whether an event is credible and tradable. Inputs include source quality, entity certainty, novelty, freshness, duplicate detection, quote availability, and hard-risk gates. The baseline entry eligibility threshold remains 65.

### 2. Event-impact forecast

The forecast estimates the economic and market impact of the specific event. It uses, where available:

- event subtype;
- disclosed monetary value;
- revenue, backlog, market value, and other point-in-time fundamentals;
- historical event analogues;
- source credibility;
- entity certainty;
- novelty;
- amount already priced into the stock;
- uncertainty and estimated execution costs.

The forecast records:

- economic impact;
- priced-in adjustment;
- residual expected return;
- bear, base, and bull returns;
- probability-weighted expected return;
- dynamic target and stop;
- maximum holding period;
- target-before-stop probability;
- proposed allocation;
- partial-exit and runner plan;
- invalidation conditions;
- confidence and data completeness;
- immutable forecast hash.

The score does not directly dictate a fixed target percentage.

### 3. Dynamic open-position plan

Every open virtual lot has one active versioned plan in `position_forecast_plans`. A plan includes target price, stop price, maximum holding period, estimated exit time, confidence, probability, partial-exit structure, runner structure, rationale, and forecast hash.

Previous plans remain in the database for audit. Reassessment marks the prior plan inactive and creates a new active plan only when the evidence change is material.

### 4. Continuous reassessment

After each collector cycle, V3.6.2 checks newly ingested events against all open positions.

Relevance is considered at three levels:

- **Company:** direct ticker/company-specific event.
- **Theme or sector:** event linked to the position's recorded theme.
- **World or macro:** tariffs, sanctions, interest rates, inflation, conflict, oil prices, government shutdowns, recession risk, export controls, and currency movements.

Per-cycle revisions are bounded to avoid overreacting to one ambiguous headline. Changes are classified as `EXPANDED`, `TIGHTENED`, `REVISED`, or `UNCHANGED`.

### 5. Shadow research policies

V3.6 retains three research policies for comparison:

- legacy fixed champion: +15% target, -10% stop, ten-session expiry;
- fast fixed challenger: +5% target, -3% stop, three-session expiry;
- dynamic V3.6 event-impact plan.

No challenger promotes itself automatically.

## Important execution boundary

V3.6.2 **updates and displays dynamic research plans but does not yet execute those plans automatically**.

The existing legacy virtual-exit executor still needs a separately tested V3.6-aware replacement covering:

- target and stop triggers;
- first-trigger ordering;
- partial virtual exits;
- runner-share accounting;
- stale-quote refusal;
- atomic position and ledger updates;
- idempotency and restart recovery;
- reconciliation and rollback.

Do not describe dynamic exits as operational until that executor is implemented and tested.

## Dashboard

The dashboard is a single-page trader command centre on port 8088.

The top-right status area shows:

- last collector time in NZST/NZDT;
- next collector time in NZST/NZDT;
- dashboard refresh cadence;
- latest position-review time and event/change counts.

The page includes:

- portfolio equity, cash, capital at acquisition cost, market value, unrealised P&L, open-position count, database/mode status;
- open virtual positions with entry price, total capital buy price, quote source/time, market value, P&L, dynamic forecast target/stop, forecast exit, probability, and confidence;
- Decision Ledger with 50/100/250/500 controls, default 100, filters, evidence, reasoning, and inline impact forecast;
- strategy, learning, reconciliation, readiness, and operational status.

Raw forecast JSON remains in SQLite for reproducibility but is rendered as plain-language summaries, labelled metrics, and bullet lists.

## Core database tables

Existing operational tables remain authoritative. V3.6 adds or uses:

- `v36_meta`
- `event_impact_forecasts`
- `shadow_plans`
- `forecast_observations`
- `forecast_calibration`
- `issuer_exposure_snapshots`
- `position_forecast_plans`
- `position_monitor_runs`
- `position_event_links`
- `position_plan_changes`

The database uses SQLite WAL mode and a 60-second busy timeout where configured. Database, WAL, SHM, configuration, watchlist, secrets, caches, and runtime state must never be committed.

## Safety and exposure controls

- Virtual-only operation is mandatory.
- Pi-hole availability is a deployment gate.
- Entry eligibility threshold remains 65.
- V3.6 proposed allocation is forecast-derived and capped at 20% by the forecast layer.
- Aggregate issuer and theme exposure must be checked across all open lots.
- No entry or exit should rely on stale quotes.
- No automatic strategy promotion is permitted.
- Human approval, Git commit, release tag, deployment record, and rollback path are required for strategy changes.

## Verification

```bash
curl -fsS --max-time 10 http://127.0.0.1:8088/health
echo
```

Expected release identity:

```json
{"ok": true, "version": "3.6.2", "mode": "virtual-only", "port": 8088}
```

Check active units:

```bash
for unit in \
  pihole-FTL \
  paper-agent-dashboard-v362.service \
  paper-agent-broad.timer \
  paper-agent-v36.timer \
  paper-agent-position-monitor-v362.timer \
  paper-agent-calibration-v36.timer \
  paper-agent-v34.timer \
  paper-agent-research-v34.timer \
  paper-agent-backup-v34.timer
do
  printf '%-52s ' "$unit"
  sudo systemctl is-active "$unit" || true
done
```

Check database integrity and active-plan coverage:

```bash
sudo -u paperagent python3 - <<'PY'
import sqlite3

db = sqlite3.connect('/var/lib/paper-agent/paper-agent.db', timeout=60)
db.execute('PRAGMA busy_timeout=60000')
print('Integrity:', db.execute('PRAGMA integrity_check').fetchone()[0])
open_positions = db.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
active_plans = db.execute("""
    SELECT COUNT(*)
    FROM position_forecast_plans fp
    JOIN positions p ON p.id=fp.position_id
    WHERE p.status='OPEN' AND fp.active=1
""").fetchone()[0]
print('Open positions:', open_positions)
print('Active plans:', active_plans)
print('Coverage:', 'PASS' if open_positions == active_plans else 'FAIL')
db.close()
PY
```

## Testing and deployment standard

Before deployment:

1. Compile all Python using Orange Pi Python 3.9.
2. Validate all shell scripts with `bash -n`.
3. Run the full offline test suite with `ResourceWarning` treated as an error.
4. Validate relevant systemd units.
5. Create and integrity-check an online SQLite backup.
6. Confirm port 8088 is released before starting a replacement dashboard.
7. Confirm Pi-hole and collector timers remain active.
8. Verify the health endpoint and required dashboard content.
9. Verify database integrity and one active plan per open position.
10. Preserve a tested rollback path.

## Repository exclusions

Never commit:

```text
*.db
*.db-wal
*.db-shm
*.sqlite
*.sqlite3
config.json
watchlist.json
.env
*.pem
*.key
*.p12
*.pfx
__pycache__/
*.py[cod]
```

## Development priorities

1. Implement the V3.6-aware virtual exit executor.
2. Add complete 1/3/5/10/15-session outcome observations for traded and non-traded decisions.
3. Add benchmark and sector-adjusted returns, MFE, and MAE.
4. Improve point-in-time fundamentals and economic-materiality coverage.
5. Improve entity, theme, sector, and macro relevance beyond keyword matching.
6. Calibrate forecast probabilities and uncertainty against realised outcomes.
7. Preserve performance and memory limits suitable for the Orange Pi Zero 3.


## V3.6.3 end-to-end lifecycle

After the position monitor, the executor evaluates fresh quotes and the frozen active plan. It handles thesis invalidation, stops, targets, partial exits, runners, and time expiry. If a target is reached unusually early, the system reforecasts first and may extend the target only when remaining bull-case upside, confidence, velocity, and evidence support continued holding. Otherwise, the plan is executed. All virtual cash, cost basis, realised P&L, position phase, and action history updates are atomic and idempotent.

The executor runs after the monitor at minute 07 of each ten-minute cycle. Quotes older than 180 seconds block execution. If target and stop both crossed but ordering is unknown, the conservative stop-first policy applies.
