# Paper Agent v5: Market Intelligence OS (virtual money only)

A consolidated rewrite of the v1-v4 Orange Pi paper trader. It never touches a broker or real money.
It models the world as **events → evidence → probabilistic theses → expected value → risk-checked, sized virtual trades**, and learns from outcomes.

* Pure Python 3.9+ standard library, SQLite, systemd timers. Designed for an Orange Pi Zero 3.
* Runs **24/7**: news, theses, regime and monitoring update around the clock; only *execution* waits for the US regular session and a verified quote.
* Every decision is auditable: the dashboard's *Decisions* tab shows the forecast scenarios, EV, sizing steps, gates and quote used.

## Install (on the Pi)
```bash
sudo apt update && sudo apt install -y unzip python3 tzdata curl
cd /tmp && unzip -o paper-agent-v5.zip && cd paper-agent-v5
sudo bash scripts/install.sh      # tests first, backs up, migrates your live DB, installs keys + units
```
This package **includes your API keys** (`config/market-data.env`); the installer places them in `/etc/paper-agent/market-data.env` (0640 root:paperagent). Delete the unzipped copy afterwards:
`sudo shred -u /tmp/paper-agent-v5.zip; sudo rm -rf /tmp/paper-agent-v5`

Verify:
```bash
PA="sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent"
$PA keys          # which keys are loaded (names only)
$PA probe         # validate every news feed
$PA quote MSFT    # see each quote provider (run during US market hours for full results)
$PA status
```
Dashboard: `http://<pi-ip>:8088`. Rollback: `sudo scripts/rollback.sh /var/backups/paper-agent/v5-<timestamp>`.

Your existing database is **preserved**: ledger, positions, decisions and plans carry over; the legacy per-decision `theses` table is renamed `decision_theses`.
Open positions without a plan get an EV-derived one on the first cycle so the executor can manage them.

## Commands
`python3 -m paper_agent` + `cycle` (full pipeline, every 10 min) · `guard` (exit checks, every 2 min, regular hours only) · `serve` · `status` · `probe` · `quote T` · `keys` · `learn` · `universe` · `backup` · `migrate`

## Tests
`python3 -m unittest discover -s tests -t .` (74 tests, ~4 s, fully offline). Includes a 20-day randomised soak test asserting the ledger reconciles after every cycle.

## Key defaults you should review (`/etc/paper-agent/config.json`)
| Setting | Default | Legacy |
|---|---|---|
| Max single position / issuer / theme / gross | 10% / 15% / 30% / 80% | 50% / 50% / 80% / 90% |
| Min cash reserve | 20% | none |
| Risk per trade (to stop) | 1% of equity | none |
| Max worst-case stress loss | 20% | none |
| Drawdown pause / daily-loss pause | 12% / 3% | none |
| Quote quorum | 2 providers agreeing, timestamped, ≤180 s (≤90 s entry) | same |

Legacy limits were loosened to levels that ignore your own principle "survival is more important than returns"; change them if you disagree.

## Docs
`docs/OMEGA_MAPPING.md` (brief → modules) · `docs/BUGS_FIXED.md` · `docs/LIMITS.md` (what is *not* verified or not possible)
