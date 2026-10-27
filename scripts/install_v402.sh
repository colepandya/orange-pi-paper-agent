#!/bin/bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]]||{ echo 'Run with sudo';exit 1; }
R="$(cd "$(dirname "$0")/.."&&pwd)";S="$(date -u +%Y%m%dT%H%M%SZ)";B="/var/backups/paper-agent/v402-$S";mkdir -p "$B"
BACKUP_PATH="$B/paper-agent.db" python3 - <<'PY'
import os,sqlite3
s=sqlite3.connect('/var/lib/paper-agent/paper-agent.db',timeout=60);s.execute('pragma busy_timeout=60000');d=sqlite3.connect(os.environ['BACKUP_PATH']);s.backup(d);assert d.execute('pragma integrity_check').fetchone()[0]=='ok';d.close();s.close()
PY
python3 -m py_compile "$R"/app/*.py "$R"/tests/*.py
PYTHONPATH="$R" PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s "$R/tests" -t "$R" -v
install -m0755 "$R/app/"*.py /opt/paper-agent/app/
install -m0644 "$R/systemd/paper-agent-v4.service" "$R/systemd/paper-agent-v4.timer" "$R/systemd/paper-agent-market-data-v402.service" /etc/systemd/system/
[ -f /etc/paper-agent/market-data.env ] || install -m0640 -o root -g paperagent "$R/config/market-data.env.example" /etc/paper-agent/market-data.env
systemctl disable --now paper-agent-quotes-v401.service paper-agent-executor-v363.timer paper-agent-v36.timer paper-agent-position-monitor-v362.timer paper-agent-broad.timer 2>/dev/null||true
systemctl daemon-reload;systemctl enable --now paper-agent-market-data-v402.service
systemctl disable --now paper-agent-v4.timer 2>/dev/null||true
systemctl is-active --quiet pihole-FTL
runuser -u paperagent -- env PYTHONPATH=/opt/paper-agent/app python3 - <<'PY'
import impact_v36 as i,market_data_fabric_v402 as m
d=i.connect();m.init(d);assert d.execute('pragma integrity_check').fetchone()[0]=='ok';print({'market_data_schema':'ok'});d.close()
PY
echo "PASS: V4.0.2 installed in quote-readiness mode; execution remains paused until quorum passes; backup $B"
