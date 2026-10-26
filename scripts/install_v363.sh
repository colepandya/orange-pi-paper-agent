#!/bin/bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]]||{ echo 'Run with sudo';exit 1; }
R="$(cd "$(dirname "$0")/.."&&pwd)";S="$(date -u +%Y%m%dT%H%M%SZ)";B="/var/backups/paper-agent/v363-$S";mkdir -p "$B"
cp -a /opt/paper-agent/app/dashboard_v361.py "$B/" 2>/dev/null||true
cp -a /etc/systemd/system/paper-agent-dashboard-v362.service "$B/" 2>/dev/null||true
BACKUP_PATH="$B/paper-agent.db" python3 - <<'PY'
import os,sqlite3
s=sqlite3.connect('/var/lib/paper-agent/paper-agent.db',timeout=60);s.execute('pragma busy_timeout=60000');d=sqlite3.connect(os.environ['BACKUP_PATH']);s.backup(d);assert d.execute('pragma integrity_check').fetchone()[0]=='ok';d.close();s.close()
PY
python3 -m py_compile "$R"/app/*.py "$R"/tests/*.py
bash -n "$R"/scripts/*.sh
PYTHONPATH="$R" PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s "$R/tests" -t "$R" -v
install -m0755 "$R/app/"*.py /opt/paper-agent/app/
install -m0644 "$R/systemd-paper-agent-dashboard-v363.service" /etc/systemd/system/paper-agent-dashboard-v363.service
install -m0644 "$R/systemd-paper-agent-executor-v363.service" /etc/systemd/system/paper-agent-executor-v363.service
install -m0644 "$R/systemd-paper-agent-executor-v363.timer" /etc/systemd/system/paper-agent-executor-v363.timer
runuser -u paperagent -- env PYTHONPATH=/opt/paper-agent/app PYTHONPYCACHEPREFIX=/tmp/paper-agent-pycache python3 - <<'PY'
import impact_v36 as i,lifecycle_executor_v363 as e
d=i.connect();e.init(d);assert d.execute('pragma integrity_check').fetchone()[0]=='ok';print({'open_lifecycle_rows':d.execute("select count(*) from position_lifecycle l join positions p on p.id=l.position_id where p.status='OPEN'").fetchone()[0]});d.close()
PY
systemctl stop paper-agent-dashboard-v362.service 2>/dev/null||true
systemctl disable paper-agent-dashboard-v362.service 2>/dev/null||true
systemctl daemon-reload
systemctl enable --now paper-agent-dashboard-v363.service paper-agent-executor-v363.timer
sleep 3
curl -fsS --max-time 10 http://127.0.0.1:8088/health -o /tmp/v363-health.json
grep -q '3.6.3' /tmp/v363-health.json
curl -fsS --max-time 30 http://127.0.0.1:8088/ -o /tmp/v363-dashboard.html
grep -q 'Position lifecycle and virtual exits' /tmp/v363-dashboard.html
grep -q 'Last collector' /tmp/v363-dashboard.html
grep -q 'Position review' /tmp/v363-dashboard.html
systemctl is-active --quiet pihole-FTL
systemctl is-active --quiet paper-agent-broad.timer
systemctl is-active --quiet paper-agent-position-monitor-v362.timer
echo "PASS: V3.6.3 installed; executor timer enabled; no install-time exits executed; backup $B"
