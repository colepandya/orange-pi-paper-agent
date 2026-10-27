#!/bin/bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]]||{ echo 'Run with sudo';exit 1; }
R="$(cd "$(dirname "$0")/.."&&pwd)";S="$(date -u +%Y%m%dT%H%M%SZ)";B="/var/backups/paper-agent/v401-$S";mkdir -p "$B"
BACKUP_PATH="$B/paper-agent.db" python3 - <<'PY'
import os,sqlite3
s=sqlite3.connect('/var/lib/paper-agent/paper-agent.db',timeout=60);s.execute('pragma busy_timeout=60000');d=sqlite3.connect(os.environ['BACKUP_PATH']);s.backup(d);assert d.execute('pragma integrity_check').fetchone()[0]=='ok';d.close();s.close()
PY
python3 -m py_compile "$R"/app/*.py "$R"/tests/*.py
PYTHONPATH="$R" PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s "$R/tests" -t "$R" -v
install -m0755 "$R/app/"*.py /opt/paper-agent/app/
install -m0644 "$R/systemd/paper-agent-v4.service" "$R/systemd/paper-agent-v4.timer" "$R/systemd/paper-agent-quotes-v401.service" /etc/systemd/system/
systemctl disable --now paper-agent.service paper-agent-broad.timer paper-agent-v36.timer paper-agent-position-monitor-v362.timer paper-agent-executor-v363.timer 2>/dev/null||true
systemctl daemon-reload
systemctl enable --now paper-agent-quotes-v401.service paper-agent-v4.timer
systemctl is-active --quiet pihole-FTL
runuser -u paperagent -- env PYTHONPATH=/opt/paper-agent/app python3 - <<'PY'
import quote_refresh_v401 as q
d=q.connect();q.init(d);assert d.execute('pragma integrity_check').fetchone()[0]=='ok';print({'quote_schema':'ok','positions':len(q.tickers(d))});d.close()
PY
echo "PASS: cumulative V4.0.1 installed; quote daemon and authoritative V4 cycle enabled; backup $B"
