#!/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo'; exit 1; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
systemctl is-active --quiet pihole-FTL
systemctl is-active --quiet paper-agent.service
systemctl disable --now paper-agent-broad.timer 2>/dev/null || true
python3 -m unittest discover -s "$ROOT/tests" -v
python3 -m py_compile "$ROOT/app/broad_collector.py"
python3 -m json.tool "$ROOT/config/sources.json" >/dev/null
python3 "$ROOT/scripts/probe_sources.py" "$ROOT/config/sources.json"
/opt/paper-agent/scripts/backup.sh
install -m 0755 "$ROOT/app/broad_collector.py" /opt/paper-agent/app/broad_collector.py
install -m 0644 "$ROOT/config/sources.json" /etc/paper-agent/broad-sources.json
python3 - <<'PY'
import sqlite3
p='/var/lib/paper-agent/paper-agent.db'; d=sqlite3.connect(p)
n=d.execute("update decisions set status='EXECUTION_ERROR',reason=reason||'; finalized by v2.1.1 hotfix from stranded PROPOSED' where action='BUY' and status='PROPOSED'").rowcount
d.commit(); d.close(); print('Finalized stranded proposals:',n)
PY
systemctl daemon-reload
systemctl start paper-agent-broad.service
systemctl enable --now paper-agent-broad.timer
systemctl is-active --quiet pihole-FTL
systemctl is-active --quiet paper-agent.service
systemctl is-active --quiet paper-agent-broad.timer
echo 'PASS: v2.1.1 hotfix installed'
