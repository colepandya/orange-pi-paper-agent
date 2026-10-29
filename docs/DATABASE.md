# The database: what is in it, what is cleaned, how to undo it

Found when the owner's real database was analysed (30 Oct 2026): 30 MB, 103 tables of which the current version uses 58; **95% of the `decisions` table (4,962 of 5,212 rows) was written by the retired v4 app**;
`events` and `news` grew ~4,700 rows/day each (~130 MB a month, nothing ever deleted). Zero orphaned rows, zero column drift.

## See what would happen (changes nothing)
    sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent db-maintain plan

## Do it (one-off: archives the legacy data, then expires old news)
    sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent db-maintain apply

It takes a verified full backup first (`/var/backups/paper-agent/pre-maintain-*.db`), copies every legacy table and the v4 decisions into `/var/lib/paper-agent/archive/legacy-v4-*.db` and re-counts the rows on disk
BEFORE dropping anything, refuses to run while the US market is open (`--force` overrides), waits for the cycle and exit guard to finish, and ends with ANALYZE, VACUUM and an integrity check.

## What is never touched
Positions, the ledger, the current app's decisions, forecasts, evidence, portfolio snapshots (the drawdown peak is read from them), companies, market data, theses. A table the current code still mentions is kept.
Events/news that any decision, queue row, evidence or forecast points at are kept regardless of age.

## Weekly (automatic, Saturday 10:00 New York time, Settings -> Database)
Only retention: news/events older than 30 days (nothing live points at them), run logs and narrative history older than 30 days (the newest state of every thesis is kept), successful-cycle reports older than 14 days shrunk
to a summary (failed cycles are kept in full). It NEVER archives or drops legacy tables: that is a deliberate, one-off decision.

## Undo
    sudo systemctl stop paper-agent-cycle.timer paper-agent-guard.timer paper-agent-report.timer paper-agent-update.timer paper-agent-open.timer paper-agent-dashboard.service
    sudo cp /var/backups/paper-agent/pre-maintain-<the one you want>.db /var/lib/paper-agent/paper-agent.db
    sudo rm -f /var/lib/paper-agent/paper-agent.db-wal /var/lib/paper-agent/paper-agent.db-shm
    sudo chown paperagent:paperagent /var/lib/paper-agent/paper-agent.db
    sudo systemctl start paper-agent-cycle.timer paper-agent-guard.timer paper-agent-report.timer paper-agent-update.timer paper-agent-open.timer paper-agent-dashboard.service
The archive file is an ordinary SQLite file: `sqlite3 legacy-v4-*.db` (or Python's sqlite3) opens it; each table keeps its original CREATE statement in the `_schema` table.
