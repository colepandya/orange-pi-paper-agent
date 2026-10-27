#!/bin/bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]]||{ echo 'Run with sudo';exit 1; }
R="$(cd "$(dirname "$0")/.."&&pwd)";S="$(date -u +%Y%m%dT%H%M%SZ)";B="/var/backups/paper-agent/v4-$S";mkdir -p "$B"
BACKUP_PATH="$B/paper-agent.db" python3 - <<'PY'
import os,sqlite3
s=sqlite3.connect('/var/lib/paper-agent/paper-agent.db',timeout=60);s.execute('pragma busy_timeout=60000');d=sqlite3.connect(os.environ['BACKUP_PATH']);s.backup(d);assert d.execute('pragma integrity_check').fetchone()[0]=='ok';d.close();s.close()
PY
python3 -m py_compile "$R"/app/*.py "$R"/tests/*.py
PYTHONPATH="$R" PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s "$R/tests" -t "$R" -v
install -m0755 "$R/app/"*.py /opt/paper-agent/app/
install -m0644 "$R/systemd/paper-agent-v4.service" "$R/systemd/paper-agent-v4.timer" /etc/systemd/system/
# One V4 writer. Disable all legacy mutating schedules and the old combined daemon.
systemctl disable --now paper-agent.service paper-agent-broad.timer paper-agent-v36.timer paper-agent-position-monitor-v362.timer paper-agent-executor-v363.timer 2>/dev/null||true
systemctl daemon-reload
systemctl enable --now paper-agent-v4.timer
systemctl is-active --quiet pihole-FTL
systemctl is-active --quiet paper-agent-dashboard-v363.service || systemctl is-active --quiet paper-agent-dashboard-v362.service
runuser -u paperagent -- env PYTHONPATH=/opt/paper-agent/app PAPER_AGENT_V4=1 python3 - <<'PY'
import impact_v36 as i,position_plans_v361 as p,lifecycle_executor_v363 as e
d=i.connect();i.init(d);p.refresh(d);e.init(d);assert d.execute('pragma integrity_check').fetchone()[0]=='ok';print({'open':d.execute("select count(*) from positions where status='OPEN'").fetchone()[0]});d.close()
PY
echo "PASS: V4 installed; authoritative virtual cycle enabled; backup $B"
