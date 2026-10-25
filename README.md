# Orange Pi News-Driven Virtual Paper Agent

> **Canonical context:** This private repository is the single source of truth. Future Copilot/AI sessions must read this README, `CURRENT_STATE.md`, `DECISIONS.md`, `CHANGELOG.md`, the latest release notes, and the relevant source/tests before proposing changes. Work from the latest deployed tag, not old ZIP files or remembered chat context.

## Purpose

A fully virtual, news-driven US equity trading experiment running on an Orange Pi Zero 3. The application monitors broad public information, maps events to verified US-listed companies, records a thesis before the outcome, makes purchases only inside its SQLite ledger, tracks prices for 1–15 trading days, virtually exits, measures profit/loss, and records lessons.

**There is no brokerage connection, no order routing and no real money.** The objective is to test whether the decision logic produces profitable virtual outcomes.

## Current deployed environment

| Item | Value |
|---|---|
| Device | Orange Pi Zero 3 |
| OS | Orange Pi Bullseye, Linux 6.1.31-sun50iw9 |
| LAN address | `192.168.1.91` |
| RAM | About 730 MB |
| Shared critical service | Pi-hole |
| App | `/opt/paper-agent` |
| Config | `/etc/paper-agent` |
| Database | `/var/lib/paper-agent/paper-agent.db` |
| Backups | `/var/backups/paper-agent/` |
| Dashboard | Port `8088` |
| Base service | `paper-agent.service` |
| Broad collector | `paper-agent-broad.service` |
| Timer | `paper-agent-broad.timer` |
| Timer cadence | Every 10 minutes |
| Stored timezone | UTC |
| Display timezone | `Pacific/Auckland` with automatic NZST/NZDT |
| Strategy baseline | v2.2.1 plus consolidated NZ dashboard display |

Health endpoint: `http://127.0.0.1:8088/health`

LAN dashboard: `http://192.168.1.91:8088`

## Non-negotiable requirements

1. Virtual trading only. Never add brokerage connectivity without an explicit new project decision.
2. Consider thousands of verified US-listed operating companies, including small and emerging companies. Never revert to a manually selected large-cap-only watchlist.
3. Preserve Pi-hole reliability and existing system resource limits.
4. Do not invent prices, companies, evidence or trade results.
5. Store timestamps in UTC; display all dashboard times in Auckland time.
6. Every release must pass offline tests and target-device live probes.
7. No production patch may exist only in chat. Every change starts from this repository, is tested, committed, tagged and documented.
8. Do not commit production databases, backups, logs, credentials, SSH keys, tokens or secrets.

## Company universe

The universe must represent verified active US exchange-listed common equity and ADR-style instruments across Nasdaq, NYSE, NYSE American and relevant US exchanges.

Include large, medium, small, micro-cap where a usable quote exists, newly listed, emerging, foreign ADR, biotech, defence, energy, industrial and technology companies.

Exclude by default:

- ETFs and mutual funds
- warrants, rights and units
- preferred shares
- notes, bonds and ETNs
- test issues
- clearly abnormal or non-operating instruments

The universe must refresh automatically and preserve the last valid snapshot if a refresh fails or produces a suspiciously small result.

## Virtual portfolio rules

Starting virtual capital: **USD 1,000,000**.

| Score | Action | Target allocation of current equity |
|---:|---|---:|
| Below 65 | Reject/watch | 0% |
| 65–79 | BUY | 20% |
| 80–89 | BUY | 25% |
| 90–94 | BUY | 35% |
| 95–100 | BUY | 50% |

Additional rules:

- Minimum executed BUY: **20% of current virtual equity**.
- Maximum one stock: **50%**.
- Maximum one theme: **80%**.
- Maximum total deployment: **90%**.
- Keep at least 10% virtual liquidity.
- Maximum holding period: **15 trading days**.
- Hard loss boundary: **12%**.
- Current profit target: **20%**, unless changed by a tested release.
- If less than 20% capacity is available, use `CAPACITY_DEFERRED`; never silently shrink a BUY below 20%.

## Decision statuses

| Status | Meaning |
|---|---|
| `REJECTED` | Event did not qualify |
| `PROPOSED` | Temporary internal state only; must not remain stranded |
| `AWAITING_PRICE` | Qualified decision waiting for a quote |
| `CAPACITY_DEFERRED` | Qualified decision waiting for at least 20% capacity |
| `NO_PRICE` | Legacy/final quote failure from older releases |
| `RISK_REJECTED` | Exposure rules prevented execution |
| `EXECUTION_ERROR` | Unexpected execution failure |
| `EXECUTED` | Virtual purchase completed in SQLite |

A qualifying decision without a price must become `AWAITING_PRICE` and be retried. No BUY should remain indefinitely in `PROPOSED`.

## Current public information sources

Baseline sources require no login, OAuth, subscription or API key.

Primary sources:

- SEC EDGAR latest filings
- Federal Register public-inspection documents
- USAspending federal awards
- FTC press releases
- openFDA drug enforcement
- openFDA device enforcement

Technology discovery sources:

- TechCrunch
- Ars Technica
- The Verge
- Techmeme
- Hacker News
- GeekWire
- NVIDIA Newsroom

Technology publications and Hacker News are discovery signals. Media-only claims should not receive the same confidence as SEC filings, government awards or first-party announcements without confirmation.

Previously disabled or rejected:

- GDELT: repeated HTTP 429 from the Orange Pi public IP
- Yahoo Finance RSS: repeated HTTP errors
- old FTC Competition RSS URL: HTTP 404
- old SEC suspension/litigation RSS URLs: HTTP 404
- Finnhub, Polygon, Benzinga, Alpaca and Alpha Vantage: excluded from the zero-credential baseline

### HTTP status handling

- `200`: successful retrieval
- `304 Not Modified`: healthy check, zero new items
- `404`: endpoint invalid; remove or replace after verification
- `429`: rate limited; apply exponential backoff

A 304 is not a failure.

Each source tracks last attempt, last success, HTTP status, consecutive failures, backoff time, item total and last error. One failed source or item must never stop other sources or items. Fetch, item and execution failures are separate metrics.

## Quote resolution

Current intended quote chain:

1. Yahoo Finance chart JSON, keyless but unofficial
2. Stooq public quote page
3. Google Finance page parser as a last resort
4. local cached quote no older than 24 hours
5. `AWAITING_PRICE` if all routes fail

Requirements:

- Never invent or estimate a price.
- Preserve quote source and quote timestamp.
- Preserve original decision time and actual virtual-entry time.
- Retry awaiting-price decisions.
- Mandatory AAPL and IBM live quote probes must pass on the Orange Pi before deploying quote changes.
- Google Finance has no supported API and its HTML parser is brittle; it remains a last resort.

## Decision logic and known limitations

The present engine is deterministic and rules-based, not a full causal reasoning model.

A decision record should preserve source, headline, URL, publication time, decision time, ticker, action, score, theme, reason, quote source/time, entry price, shares, allocation and final status.

Known limitations:

- Keyword scoring can overvalue words such as `award`, `contract`, `order` and `will`.
- Alias matching can map the wrong company.
- Financial materiality relative to revenue, market capitalisation or backlog is incomplete.
- Priced-in analysis is incomplete.
- Supplier, customer and competitor relationships are incomplete.

False company mapping is a harder failure than conservative scoring. Entity resolution must be improved before loosening mapping controls.

## Dashboard requirements

The existing dashboard is served on port 8088 and should display:

- equity
- cash
- deployed capital
- active/paused mode
- last broad refresh in NZ time
- next scheduled refresh in NZ time
- open virtual positions
- recent decisions
- decision timestamps in NZ time

The decision table should ultimately include source, headline, article link, status and recorded logic. Stored timestamps remain UTC.

## Services and timer

Base service:

```bash
sudo systemctl status paper-agent.service --no-pager -l
```

Broad timer:

```bash
sudo systemctl status paper-agent-broad.timer --no-pager -l
```

Bullseye-safe calendar timer:

```ini
[Timer]
OnCalendar=*-*-* *:0/10:00
AccuracySec=15s
RandomizedDelaySec=30
Persistent=true
Unit=paper-agent-broad.service
```

Exact last/next trigger:

```bash
sudo systemctl show paper-agent-broad.timer   -p ActiveState   -p LastTriggerUSec   -p NextElapseUSecRealtime   --no-pager
```

Completed cycles:

```bash
sudo journalctl -u paper-agent-broad.service   --since "24 hours ago"   --no-pager -o short-iso-precise   | grep '"broad_cycle"'
```

An unchanged dashboard does not prove collection stopped. A cycle may retrieve only duplicates.

## Data and backups

Database: `/var/lib/paper-agent/paper-agent.db`

Backups: `/var/backups/paper-agent/`

Create a backup before every upgrade:

```bash
sudo /opt/paper-agent/scripts/backup.sh
```

Integrity check:

```bash
sudo python3 -c "import sqlite3; d=sqlite3.connect('/var/lib/paper-agent/paper-agent.db'); print(d.execute('PRAGMA integrity_check').fetchone()[0]); d.close()"
```

Expected: `ok`.

## Required release tests

Every applicable release must test:

- Python compilation
- shell syntax
- JSON validation
- RSS and Atom parsing
- every custom source parser
- per-source intervals
- HTTP 304 behavior
- HTTP 429 backoff
- fetch/item/execution failure isolation
- SEC forms including 8-K, 10-K, 6-K, SC 13D, SC 13G, NT 10-Q and NT 10-K
- full-universe matching and ambiguous short-ticker protection
- score boundaries 64/65/79/80/89/90/94/95
- 20% minimum purchase
- 25/35/50% allocations
- exposure limits
- quote fallback order
- awaiting-price retry
- capacity-deferred retry
- virtual buy and sell
- realised P&L and lessons
- restart persistence
- backup/restore
- SQLite integrity
- timer scheduling
- NZST/NZDT dashboard conversion
- Pi-hole health and resource boundaries

Release gate:

1. Full suite passes.
2. Full suite passes repeatedly to detect state leakage/flakiness.
3. Orange Pi live-probes every configured endpoint.
4. Mandatory sources pass.
5. Mandatory quotes return numeric prices.
6. Backup succeeds.
7. Pi-hole, base agent and broad timer remain active.
8. Timer has a real future `NEXT` value.
9. Rollback is documented.

Mocked parser tests do not prove external endpoints work. Target-device probes are mandatory.

## Operational health check

```bash
sudo systemctl is-active pihole-FTL
sudo systemctl is-active paper-agent.service
sudo systemctl is-active paper-agent-broad.timer
curl -fsS --max-time 5 http://127.0.0.1:8088/health
echo
nslookup google.com 127.0.0.1
```

Decision/status summary:

```bash
sudo python3 -c "import sqlite3; d=sqlite3.connect('/var/lib/paper-agent/paper-agent.db'); [print(r) for r in d.execute('SELECT action,status,COUNT(*) FROM decisions GROUP BY action,status ORDER BY COUNT(*) DESC')]; d.close()"
```

Stranded proposals must be zero:

```bash
sudo python3 -c "import sqlite3; d=sqlite3.connect('/var/lib/paper-agent/paper-agent.db'); print(d.execute("SELECT COUNT(*) FROM decisions WHERE action='BUY' AND status='PROPOSED'").fetchone()[0]); d.close()"
```

## Security and repository hygiene

Never commit databases, WAL/SHM files, backups, `.env` files, passwords, tokens, API keys, SSH keys, production logs or unredacted secrets.

Recommended `.gitignore`:

```gitignore
__pycache__/
*.py[cod]
*.db
*.db-wal
*.db-shm
*.sqlite
*.sqlite3
*.zip
*.log
.env
.env.*
config/config.json
config/watchlist.json
backups/
build/
dist/
.DS_Store
```

Keep the repository private until security, licensing and public documentation are deliberately reviewed.

## Git workflow

- `main`: last release proven on the Orange Pi
- `develop`: combined tested changes
- `feature/*`: new functionality
- `fix/*`: defects

Every deployed baseline gets an annotated, immutable tag. Never force-push deployed tags.

Example:

```bash
git checkout -b fix/example
git add .
git commit -m "Describe tested change"
git push -u origin fix/example
```

## Instructions for future Copilot/AI chats

At the beginning of a new chat:

1. Provide access to this private repository or upload the relevant files.
2. State the latest deployed tag.
3. Ask the assistant to read this README, `CURRENT_STATE.md`, `DECISIONS.md`, `CHANGELOG.md`, source files, tests and release notes.

Required assistant behavior:

- Treat the repository as authoritative.
- Never recreate existing code from memory.
- Inspect the deployed tag before editing.
- Distinguish local/mock tests from Orange Pi live probes.
- Preserve virtual-only operation.
- Preserve Pi-hole protections.
- Do not assume scope, universe, sizing, sources or deployment rules.
- Update tests for every behavior change.
- Provide rollback instructions.
- Do not claim success after a failed release gate.
- Do not silently change strategy parameters.
- Never commit secrets or production databases.

Suggested opening prompt:

```text
Use the private orange-pi-paper-agent repository as the authoritative source.
Read README.md, CURRENT_STATE.md, DECISIONS.md and CHANGELOG.md first.
Work only from the latest deployed tag. Preserve virtual-only operation,
Pi-hole protections, the full US equity universe, minimum entry score 65,
minimum 20% virtual purchase, quote retries and Auckland dashboard time.
Run the complete offline suite and define Orange Pi live probes before giving
any deployment commands.
```

## Immediate follow-ups

1. Confirm the current Git tag exactly matches `/opt/paper-agent`.
2. Add GitHub Actions for compilation, tests, JSON validation and shell syntax.
3. Add one clean idempotent upgrade script generated from repository state.
4. Add an automated rollback script.
5. Display source/article links and reasoning on the dashboard if absent.
6. Display last-cycle counts and source health on the dashboard.
7. Improve entity resolution and subsidiary/parent mapping.
8. Add event materiality and priced-in analysis.
9. Measure returns by source, event type and score band.
10. Maintain a complete `CHANGELOG.md` for every deployed release.

## Project principle

> Make virtual decisions from public information, record the thesis before the result, obtain a real observed price or wait for one, preserve immutable history, and measure whether the logic actually produces gains.
