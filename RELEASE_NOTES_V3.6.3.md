# Paper Agent V3.6.3 release notes

V3.6.3 completes the virtual position lifecycle from event collection through forecast, purchase-plan monitoring, target/stop evaluation, partial or full exit, runner management, virtual ledger settlement, realised P&L, audit history, and dashboard reporting.

The installer does not execute exits immediately. It initializes lifecycle state and enables the executor timer. The first action-capable cycle occurs after a subsequent collector, forecast, monitor, and executor sequence.

This remains a virtual research system with no broker integration and no real money.
