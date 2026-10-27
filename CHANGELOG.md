# Changelog

All notable changes to the Orange Pi Paper Agent are recorded here. The platform remains virtual-only.

## [3.6.2] - 2026-10-26

### Added

- Continuous open-position monitoring after collector and forecast cycles.
- Company-level, theme-level, and world/macro event relevance checks.
- `position_monitor_runs` for cycle history.
- `position_event_links` for auditable event-to-position relationships.
- `position_plan_changes` for material plan revisions.
- Bounded target, stop, and holding-period revisions.
- Change classifications: `EXPANDED`, `TIGHTENED`, `REVISED`, and `UNCHANGED`.
- Top-right dashboard status for last collector, next collector, dashboard refresh, and latest position review.
- Per-position plan-review status in the Open Virtual Positions table.
- New systemd position-monitor service and timer.

### Verified

- Dashboard active on port 8088.
- Four open positions and four active plans with coverage `PASS`.
- Manual position-monitor run completed with `status=0/SUCCESS`.
- Pi-hole and all required timers active.

### Limitations

- Revised plans are research records and dashboard inputs; they do not yet trigger automated dynamic exits.

## [3.6.1] - 2026-10-26

### Changed

- Moved event-impact forecasts into each Decision Ledger line item and expansion.
- Replaced raw JSON UI with trader-readable prose, labelled metrics, and bullet lists.
- Removed standalone Forecasts and Issuer Concentration dashboard sections.
- Reforecast all open positions using V3.6 event-impact logic.
- Replaced displayed legacy fixed exits with dynamic forecast target, stop, holding period, and expected exit.

### Added

- `position_forecast_plans` with versioned history and one active plan per position.
- Dynamic target price, stop price, expected exit, probability, confidence, partial-exit, runner, rationale, invalidation conditions, and forecast hash.

## [3.6.0] - 2026-10-26

### Added

- Event-impact forecasting engine.
- Separate eligibility, impact, confidence, and data-completeness measures.
- Economic-materiality analysis for contracts, product/customer events, earnings/guidance, regulatory events, M&A, dilution, cyber incidents, policy, ownership, and general news.
- Priced-in movement adjustment and residual-return estimation.
- Bear, base, and bull forecast scenarios.
- Dynamic target, stop, maximum hold, allocation, partial-exit, and runner proposals.
- Immutable forecast hashes and derivation records.
- Three shadow policies: legacy fixed, fast fixed, and dynamic V3.6.
- Issuer exposure snapshots for aggregated risk controls.
- Forecast and calibration services/timers.
- V3.6 single-page dashboard extension.

### Safety

- No brokerage code, order routing, or automatic model promotion.
- Dynamic plans initially operated as shadow research.

## [3.5.2] - 2026-10-26

### Added

- Rebuilt professional single-page trader dashboard.
- Restored complete open-position table.
- Highlighted total capital buy price.
- Restored Pacific/Auckland collector and quote timestamps.
- Added sticky in-page navigation, server-side decision filtering, and inline audit detail.

## [3.5.0] - 2026-10-26

### Added

- Append-only decision-lineage and strategy-registry foundation.
- Feature, logic, risk, configuration, watchlist, and Git metadata snapshots.
- Market-readiness scaffolding and expanded dashboard workspaces.

### Known issue

- Initial dashboard package contained a Python 3.9 f-string syntax defect that target-device validation detected before deployment. The multi-page design was subsequently rejected in favour of the V3.5.2 single-page design.

## [3.4.1] - 2026-10-26

### Added

- Decision count selector with 50/100/250/500 options and default 100.
- Decision score and status colour coding.

## [3.4.0] - 2026-10-26

### Added

- V3.4 research schema, reconciliation, backup, challenger comparison, and strategy recommendation scaffolding.
- Online SQLite backup using Python's `sqlite3` API after the target device was found not to include the `sqlite3` CLI.

## [3.2.0] - 2026-10-26

### Added

- Active V3 strategy and single-page trader dashboard.
- Position timestamps, quote source/time, P&L, targets, stops, and estimated exits.
- Fixed systemd collector timestamp parsing for Auckland display.

## Earlier releases

Earlier V2.x and V3.0/V3.1 work established broad-source collection, verified equity mapping, source-scoped UIDs, quote fallbacks, allocation ladders, duplicate/stale controls, event classifications, watermarking, exit-rule scaffolding, and outcome research foundations. Consult Git history and release tags for exact changes.

## [3.6.3] - 2026-10-26

### Added
- End-to-end virtual position lifecycle executor running after each position-monitor cycle.
- Fresh-quote gating, thesis invalidation, stop, target, maximum-hold, runner-target, runner-stop, and runner-time triggers.
- Rapid target-hit reforecast: extend and hold only when price velocity, remaining bull-case return, confidence, and evidence justify it; otherwise follow the planned exit.
- Atomic full and partial virtual exits, proportional cost-basis release, realised P&L, virtual cash ledger updates, runner plans, and final closure.
- Idempotent action identity, action reservations, restart-safe retry, writer locking, plan/evidence snapshots, and execution audit trail.
- Conservative stop-first handling when a price range crossed target and stop but ordering is unknown.
- Simulated adverse slippage and observed-price/fill-price separation.
- Position Lifecycle dashboard section and per-position lifecycle/reforecast status.
- V3.6.3 executor service and timer at minute 07 of each ten-minute cycle.

### [4.0.0] - 2026-10-27
- Consolidated one-writer V4 virtual cycle.
- Added synchronous forecast and independent risk gating before entry.
- Added strict event phrase, timestamp and quote controls.
- Added open-position quote refresh before monitoring/execution.
- Corrected stop-tightening and transaction-order defects.
- Removed unsafe quote parser fallback and immortal pending retries.
- Added V4 installer, rollback, systemd units and regression suite.

### [4.0.1] - 2026-10-27
- Added dedicated session-aware quote daemon.
- Added regular/pre-market/after-hours field selection.
- Added 60-second open/extended and five-minute closed cadence.
- Added identity, timestamp, price and spread validation.
- Added explicit quote quality and execution eligibility.
- Added forced refresh before monitoring and lifecycle execution.

### [4.0.2] - 2026-10-27
- Replaced single-provider quote dependency with seven documented adapters.
- Added concurrent quote retrieval, validation, consensus and outlier rejection.
- Added provider health, authentication/rate-limit state, readiness and provenance tables.
- Added hard full-position quote coverage gate and fail-closed V4 execution pause.
- Added plan repair workflow blocked until validated quote quorum passes.
