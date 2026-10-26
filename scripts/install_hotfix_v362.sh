#!/bin/bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]]||{ echo 'Run with sudo';exit 1; }
R="$(cd "$(dirname "$0")/.."&&pwd)";S="$(date -u +%Y%m%dT%H%M%SZ)";B="/var/backups/paper-agent/v362-$S";mkdir -p "$B";cp -a /opt/paper-agent/app/dashboard_v361.py "$B/" 2>/dev/null||true;cp -a /etc/systemd/system/paper-agent-dashboard-v361.service "$B/" 2>/dev/null||true
BACKUP_PATH="$B/paper-agent.db" python3 - <<'PY'
import os,sqlite3
s=sqlite3.connect('/var/lib/paper-agent/paper-agent.db',timeout=60);s.execute('pragma busy_timeout=60000');d=sqlite3.connect(os.environ['BACKUP_PATH']);s.backup(d);assert d.execute('pragma integrity_check').fetchone()[0]=='ok';d.close();s.close()
PY
python3 -m py_compile "$R"/app/*.py
PYTHONPATH="$R/app" python3 -m unittest discover -s "$R/tests" -t "$R" -v
install -m0755 "$R/app/"*.py /opt/paper-agent/app/
install -m0644 "$R/systemd-paper-agent-position-monitor-v362.service" /etc/systemd/system/paper-agent-position-monitor-v362.service
install -m0644 "$R/systemd-paper-agent-position-monitor-v362.timer" /etc/systemd/system/paper-agent-position-monitor-v362.timer
runuser -u paperagent -- env PYTHONPATH=/opt/paper-agent/app /usr/bin/python3 /opt/paper-agent/app/position_monitor_v362.py
sed 's#dashboard_v361.py#dashboard_v361.py#g;s#V3.6.1#V3.6.2#g' /etc/systemd/system/paper-agent-dashboard-v361.service >/etc/systemd/system/paper-agent-dashboard-v362.service
systemctl stop paper-agent-dashboard-v361.service;systemctl disable paper-agent-dashboard-v361.service 2>/dev/null||true
systemctl daemon-reload;systemctl enable --now paper-agent-dashboard-v362.service paper-agent-position-monitor-v362.timer;sleep 3
curl -fsS http://127.0.0.1:8088/health -o /tmp/v362-health;grep -q '3.6.2' /tmp/v362-health
curl -fsS http://127.0.0.1:8088/ -o /tmp/v362-page;grep -q 'Last collector' /tmp/v362-page;grep -q 'Next collector' /tmp/v362-page;grep -q 'Position review' /tmp/v362-page
systemctl is-active --quiet paper-agent-broad.timer;systemctl is-active --quiet pihole-FTL
echo "PASS: V3.6.2 live position monitor installed; backup $B"
