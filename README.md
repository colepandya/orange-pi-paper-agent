# Paper Agent v5: Market Intelligence OS (virtual money only)

A consolidated rewrite of the v1-v4 Orange Pi paper trader. It never touches a broker or real money.
It models the world as **events → evidence → probabilistic theses → expected value → risk-checked, sized virtual trades**, and learns from outcomes.

* Pure Python 3.9+ standard library, SQLite, systemd timers. Designed for an Orange Pi Zero 3.
* Runs **24/7**: news, theses, regime and monitoring update around the clock; only *execution* waits for the US regular session and a verified quote.
* Every decision is auditable: the dashboard's *Decisions* tab shows the forecast scenarios, EV, sizing steps, gates and quote used.

## Install / update (on the Pi): one line
```bash
curl -fsSL https://raw.githubusercontent.com/colepandya/orange-pi-paper-agent/main/scripts/update.sh | sudo bash
```
That downloads the latest code from GitHub, runs the tests, backs up your database, migrates it, installs keys + services, runs a first cycle and prints a health report.
After that the Pi **checks GitHub every 3 hours and updates itself** (tests gate every update; unchanged code is a no-op). To force it now: `sudo FORCE=1 /opt/paper-agent/scripts/update.sh`.

Health report any time (paste it to Claude if anything is red): `sudo /opt/paper-agent/scripts/doctor.sh`
Add `--fix` to let it restart stopped services and rebuild a missing universe.

**Private repo?** The plain download only works for public repos. For a private one, create a fine-grained token (GitHub → Settings → Developer settings → Fine-grained tokens → this repo only → Contents: Read-only) and put `GITHUB_TOKEN=...` in `/etc/paper-agent/update.conf` on the Pi; the update script then uses it automatically. The first-time one-liner must also send the token: `curl -fsSL -H "Authorization: Bearer TOKEN" https://raw.githubusercontent.com/colepandya/orange-pi-paper-agent/main/scripts/update.sh | sudo bash`.

Fallback without GitHub: `unzip` the package on the Pi and run `sudo bash scripts/install.sh`.
Keys: `config/market-data.env` in the repo is installed to `/etc/paper-agent/market-data.env`; if the repo has none, an existing Pi file is kept.

Dashboard: `http://<pi-ip>:8088`. Rollback: `sudo scripts/rollback.sh /var/backups/paper-agent/v5-<timestamp>`.

Your existing database is **preserved**: ledger, positions, decisions and plans carry over; the legacy per-decision `theses` table is renamed `decision_theses`.
Open positions without a plan get an EV-derived one on the first cycle so the executor can manage them.

## V6: LLM analyst (optional, off by default)
`sudo /opt/paper-agent/scripts/enable-llm.sh` switches on structured event reading with causal chains, SEC-filing company knowledge, a red-team
challenge before every entry, tripwires, a forecast revision ledger and per-company dossiers (dashboard -> Dossiers). Hard dollar caps apply.
See `docs/INTELLIGENCE.md` for design, cost and honest limits.

## Commands
`python3 -m paper_agent` + `doctor [--fix]` · `llm-test` · `llm-eval` · `cycle` (full pipeline, every 10 min) · `guard` (exit checks, every 2 min, regular hours only) · `serve` · `status` · `probe` · `quote T` · `keys` · `learn` · `universe` · `backup` · `migrate`

## Tests
`python3 -m unittest discover -s tests -t .` (~115 tests, fully offline; `soak_twenty_days.py` is the slow 20-day simulation, run by the installer only with FULL_TESTS=1). Includes a 20-day randomised soak test asserting the ledger reconciles after every cycle.

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
