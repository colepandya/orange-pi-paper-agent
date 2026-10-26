#!/bin/bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]]||{ echo 'Run with sudo';exit 1; }
R="$(cd "$(dirname "$0")/.."&&pwd)";STAMP="$(date -u +%Y%m%dT%H%M%SZ)";B="/var/backups/paper-agent/v361-hotfix-$STAMP";mkdir -p "$B"
cp -a /opt/paper-agent/app/dashboard_v36.py "$B/" 2>/dev/null||true;cp -a /etc/systemd/system/paper-agent-dashboard-v36.service "$B/" 2>/dev/null||true
BACKUP_PATH="$B/paper-agent.db" python3 - <<'PY'
import os,sqlite3
s=sqlite3.connect('/var/lib/paper-agent/paper-agent.db',timeout=60);s.execute('pragma busy_timeout=60000');d=sqlite3.connect(os.environ['BACKUP_PATH']);s.backup(d);assert d.execute('pragma integrity_check').fetchone()[0]=='ok';d.close();s.close()
PY
python3 -m py_compile "$R"/app/*.py
PYTHONPATH="$R/app" PAPER_AGENT_BASE_DASHBOARD=/opt/paper-agent/app/dashboard_single_v352.py python3 -m unittest discover -s "$R/tests" -t "$R" -v
install -m0755 "$R/app/impact_v36.py" "$R/app/position_plans_v361.py" "$R/app/dashboard_v361.py" /opt/paper-agent/app/
runuser -u paperagent -- env PYTHONPATH=/opt/paper-agent/app /usr/bin/python3 /opt/paper-agent/app/position_plans_v361.py
sed 's#dashboard_v36.py#dashboard_v361.py#g;s#V3.6 #V3.6.1 hotfix #g' /etc/systemd/system/paper-agent-dashboard-v36.service >/etc/systemd/system/paper-agent-dashboard-v361.service
systemctl stop paper-agent-dashboard-v36.service
systemctl disable paper-agent-dashboard-v36.service 2>/dev/null||true
systemctl daemon-reload;systemctl enable --now paper-agent-dashboard-v361.service;sleep 3
curl -fsS --max-time 10 http://127.0.0.1:8088/health -o /tmp/v361-health.json
grep -q '3.6.1' /tmp/v361-health.json
curl -fsS --max-time 30 http://127.0.0.1:8088/ -o /tmp/v361-dashboard.html
grep -q 'impact forecast included in every line item' /tmp/v361-dashboard.html
grep -q 'V3.6.1 active research plan' /tmp/v361-dashboard.html
! grep -q 'Issuer concentration' /tmp/v361-dashboard.html
! grep -q 'V3.6 event-impact forecasts</h2>' /tmp/v361-dashboard.html
! grep -q '&quot;allocation_formula&quot;' /tmp/v361-dashboard.html
systemctl is-active --quiet pihole-FTL;systemctl is-active --quiet paper-agent-broad.timer
echo "PASS: V3.6.1 inline forecast dashboard hotfix installed; backup $B"
