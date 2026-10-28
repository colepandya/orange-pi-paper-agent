#!/bin/bash
# Paper Agent v5 installer. Backs up first, tests before touching anything, migrates the live DB non-destructively.
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
R="$(cd "$(dirname "$0")/.." && pwd)"; TS="$(date -u +%Y%m%dT%H%M%SZ)"; B="/var/backups/paper-agent/v5-$TS"
DB=/var/lib/paper-agent/paper-agent.db
mkdir -p "$B" /opt/paper-agent /etc/paper-agent /var/lib/paper-agent /var/log/paper-agent
id paperagent >/dev/null 2>&1 || useradd --system --home /var/lib/paper-agent --shell /usr/sbin/nologin paperagent

echo "[1/7] Pre-flight tests (nothing is changed if these fail)..."
python3 - <<'PY'
import sys; assert sys.version_info >= (3, 9), "Python 3.9+ required"
PY
python3 -m compileall -q "$R/paper_agent"
if [[ "${SKIP_TESTS:-0}" == "1" ]]; then
  echo "SKIP_TESTS=1: skipping tests"
else
  START=$(date +%s)
  PAT="test_[re]*.py"; [[ "${FULL_TESTS:-0}" == "1" ]] && PAT="test_*.py"     # default skips the slow soak test
  if ! OUT=$(cd "$R" && PYTHONPATH="$R" python3 -m unittest discover -s tests -t . -p "$PAT" 2>&1); then
    echo "$OUT" | tail -40; echo "FAIL: tests failed; aborting before any change"; exit 1
  fi
  echo "$OUT" | grep -E "^(Ran|OK)" ; echo "tests passed in $(( $(date +%s) - START ))s"
fi

echo "[2/7] Backup -> $B"
if [[ -f $DB ]]; then
  BACKUP_PATH="$B/paper-agent.db" DB="$DB" python3 - <<'PY'
import os, sqlite3
s = sqlite3.connect(os.environ["DB"], timeout=60); d = sqlite3.connect(os.environ["BACKUP_PATH"]); s.backup(d)
assert d.execute("pragma integrity_check").fetchone()[0] == "ok"; print("db backup verified")
PY
fi
cp -a /etc/paper-agent "$B/etc-paper-agent" 2>/dev/null || true
[[ -d /opt/paper-agent/app ]] && cp -a /opt/paper-agent "$B/opt-paper-agent" || true
mkdir -p "$B/units"; cp -a /etc/systemd/system/paper-agent* "$B/units/" 2>/dev/null || true
systemctl list-units 'paper-agent*' --all --no-legend 2>/dev/null | awk '{print $1}' > "$B/active-units.txt" || true

echo "[3/7] Stopping and disabling legacy units"
for u in $(ls /etc/systemd/system | grep -E '^paper-agent.*\.(service|timer)$' || true); do
  case "$u" in paper-agent-cycle.*|paper-agent-guard.*|paper-agent-dashboard.service|paper-agent-backup.*|paper-agent-universe.*) ;; *)
    systemctl disable --now "$u" 2>/dev/null || true; rm -f "/etc/systemd/system/$u";; esac
done

echo "[4/7] Installing code and config"
[[ -d /opt/paper-agent/app ]] && mv /opt/paper-agent/app "/opt/paper-agent/app.legacy-$TS"
rm -rf /opt/paper-agent/paper_agent /opt/paper-agent/scripts && cp -a "$R/paper_agent" /opt/paper-agent/paper_agent && cp -a "$R/scripts" /opt/paper-agent/scripts && chmod +x /opt/paper-agent/scripts/*.sh
[[ -n "${VERSION_ID:-}" ]] && echo "$VERSION_ID" > /opt/paper-agent/.version || true
[[ -f /etc/paper-agent/config.json ]] || install -m0644 "$R/config/config.example.json" /etc/paper-agent/config.json
[[ -f /etc/paper-agent/sources.json ]] && cp /etc/paper-agent/sources.json "$B/sources.json.old"
install -m0644 "$R/config/sources.json" /etc/paper-agent/sources.json
if [[ -f "$R/config/market-data.env" ]]; then
  [[ -f /etc/paper-agent/market-data.env ]] && cp /etc/paper-agent/market-data.env "$B/market-data.env.old"
  install -m0640 -o root -g paperagent "$R/config/market-data.env" /etc/paper-agent/market-data.env
elif [[ ! -f /etc/paper-agent/market-data.env ]]; then
  install -m0640 -o root -g paperagent "$R/config/market-data.env.example" /etc/paper-agent/market-data.env
fi
chown -R paperagent:paperagent /var/lib/paper-agent /var/log/paper-agent
chmod 0640 /etc/paper-agent/market-data.env; chgrp paperagent /etc/paper-agent/market-data.env

echo "[5/7] Migrating database (non-destructive)"
runuser -u paperagent -- env PYTHONPATH=/opt/paper-agent python3 -m paper_agent migrate
[[ -f /etc/paper-agent/watchlist.json ]] || { echo "building universe (needs network)"; env PYTHONPATH=/opt/paper-agent python3 -m paper_agent universe || echo "WARN: universe build failed; run: sudo systemctl start paper-agent-universe"; }

echo "[6/7] Installing units"
for f in cycle.service cycle.timer guard.service guard.timer dashboard.service backup.service backup.timer universe.service universe.timer update.service update.timer; do install -m0644 "$R/systemd/paper-agent-$f" /etc/systemd/system/; done
systemctl daemon-reload
systemctl enable paper-agent-dashboard.service paper-agent-cycle.timer paper-agent-guard.timer paper-agent-backup.timer paper-agent-universe.timer paper-agent-update.timer
systemctl restart paper-agent-dashboard.service
systemctl start paper-agent-cycle.timer paper-agent-guard.timer paper-agent-backup.timer paper-agent-universe.timer paper-agent-update.timer

echo "[7/7] First cycle + health report"
ls -1dt /var/backups/paper-agent/v5-* 2>/dev/null | tail -n +6 | xargs -r rm -rf      # keep the 5 newest backups
PA=(runuser -u paperagent -- env PYTHONPATH=/opt/paper-agent python3 -m paper_agent)
timeout 600 "${PA[@]}" cycle || echo "(first cycle reported problems; see doctor below)"
"${PA[@]}" doctor --fix || true
echo
echo "DONE. Backup: $B   Dashboard: http://$(hostname -I | awk '{print $1}'):8088"
echo "Updates: this Pi now checks GitHub every 3 hours by itself. Health report any time:  sudo /opt/paper-agent/scripts/doctor.sh"
