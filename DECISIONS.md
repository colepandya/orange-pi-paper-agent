# Architecture and Strategy Decisions

## Immutable decisions

1. **Virtual-only operation.** No broker integration, order routing, or real money.
2. **Canonical repository.** The private Git repository is the source of truth. Deployments originate from tested commits and tags.
3. **Pi-hole protection.** Pi-hole availability is a deployment gate. Paper Agent must not compromise the shared DNS service.
4. **Verified US-equity universe.** Consider verified US-listed equities rather than an arbitrary small watchlist.
5. **Entry eligibility threshold.** A decision must meet the baseline eligibility score of 65 before a BUY can be considered.
6. **Score is not target return.** Eligibility, economic impact, and forecast confidence are separate concepts.
7. **Dynamic event-derived plans.** Target, stop, maximum hold, partial exit, runner, and allocation proposals derive from the event and forecast, not a universal fixed percentage.
8. **Priced-in adjustment.** Forecast residual opportunity must subtract information already reflected in price where measurable.
9. **Point-in-time evidence.** Historical replay and research must use only information available at the decision time.
10. **Append-only evidence.** Original events, decisions, forecasts, plans, outcomes, and failed experiments remain auditable.
11. **One active plan per open position.** Older plans remain recorded but inactive.
12. **Continuous reassessment.** Every collector cycle is followed by open-position review for company, theme, sector, macro, and world events.
13. **Bounded revisions.** One new event cannot create an unlimited target or duration change. Materiality thresholds and per-cycle bounds reduce overreaction.
14. **No automatic strategy promotion.** Challengers and recommendations require human approval, a Git commit, release tag, deployment record, and rollback path.
15. **Fresh-quote discipline.** Missing quotes become `AWAITING_PRICE`; stale quotes must not trigger entries or exits.
16. **UTC storage, Auckland display.** Store timestamps in UTC and display user-facing times in `Pacific/Auckland`.
17. **Independent source health.** A source failure must not stop unrelated sources. Healthy HTTP 304 responses are treated as unchanged, not failed.
18. **SQLite safety.** Use WAL and long busy timeouts where configured; create online backups and integrity-check before upgrades.
19. **Resource limits.** New workers must remain suitable for the Orange Pi Zero 3 and protect Pi-hole.
20. **Port stability.** The trader dashboard remains on port 8088.

## V3.6 forecast decision

V3.6 separates:

- **Eligibility:** whether the event is credible, novel, mapped, fresh, and tradable.
- **Impact:** the event's estimated economic and market significance.
- **Confidence:** the reliability and completeness of that impact estimate.

The dynamic plan is based on economic materiality, historical analogues, expected market response, priced-in movement, uncertainty, and estimated costs. Fixed targets remain only as shadow comparators or safety bounds.

## V3.6.1 dashboard decision

Forecasts belong to their originating Decision Ledger entries. The dashboard must not expose raw JSON as the primary UI. JSON remains in SQLite for reproducibility, while the user interface presents labelled metrics, prose, and bullet lists.

A standalone Issuer Concentration dashboard panel is not required because open positions already show position-level capital. Aggregated issuer exposure may remain in the database for risk controls.

## V3.6.2 living-thesis decision

An open position's plan is not frozen at purchase. Newly collected company, theme, sector, macro, or geopolitical information can revise the target, stop, expected holding period, and thesis validity.

The monitor:

- runs after collector and forecast cycles;
- links relevant events to open positions;
- records relevance, scope, direction, and reason;
- creates a new plan only for material changes;
- classifies changes as `EXPANDED`, `TIGHTENED`, `REVISED`, or `UNCHANGED`;
- preserves all prior plans;
- does not silently execute the revised plan.

## Exposure decision

The older automatic minimum purchase rule is no longer an architectural requirement. V3.6 proposes allocation from expected residual return, probability, confidence, downside, and capacity, with a forecast-layer maximum of 20%. Aggregate issuer and correlated-theme exposure remain hard constraints.

## Exit-execution boundary

As of V3.6.2, the dashboard and database use dynamic research plans, but the legacy virtual-exit executor has not been replaced by a V3.6 plan-aware executor.

Automatic dynamic execution is prohibited until separately tested for:

- target and stop trigger ordering;
- partial sells and runner accounting;
- fresh-quote enforcement;
- atomic ledger/position updates;
- idempotency;
- restart recovery;
- reconciliation;
- rollback.

## Deployment decision

Every deployment must include:

1. Python 3.9 compilation on the Orange Pi.
2. Shell syntax validation.
3. Full offline tests with `ResourceWarning` treated as an error.
4. Systemd validation where applicable.
5. Online SQLite backup and integrity check.
6. Explicit service handover on port 8088.
7. Pi-hole and collector verification.
8. Health endpoint verification.
9. Database integrity and active-plan coverage checks.
10. A documented rollback path.
