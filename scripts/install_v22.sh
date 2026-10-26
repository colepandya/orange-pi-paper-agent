#!/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]] || exit 1
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
systemctl is-active --quiet pihole-FTL
systemctl is-active --quiet paper-agent.service
systemctl disable --now paper-agent-broad.timer 2>/dev/null || true
python3 -m unittest discover -s "$ROOT/tests" -v
python3 -m py_compile "$ROOT"/app/*.py "$ROOT"/scripts/*.py
python3 "$ROOT/scripts/probe_sources.py" "$ROOT/config/sources.json"
python3 "$ROOT/scripts/probe_quotes.py"
/opt/paper-agent/scripts/backup.sh
install -m 0755 "$ROOT/app/broad_collector.py" /opt/paper-agent/app/broad_collector.py
install -m 0755 "$ROOT/app/experiment_execution.py" /opt/paper-agent/app/experiment_execution.py
install -m 0644 "$ROOT/config/sources.json" /etc/paper-agent/broad-sources.json
python3 "$ROOT/scripts/install_experiment.py"
python3 - <<'PY'
import json
p='/etc/paper-agent/config.json';c=json.load(open(p));c['portfolio']['entry_score']=65;c['portfolio']['standard_position_pct']=.10;c['portfolio']['high_confidence_position_pct']=.20
with open(p,'w') as f:json.dump(c,f,indent=2);f.write('\n')
PY
systemctl restart paper-agent.service
systemctl start paper-agent-broad.service
systemctl enable --now paper-agent-broad.timer
systemctl is-active --quiet pihole-FTL
systemctl is-active --quiet paper-agent.service
systemctl is-active --quiet paper-agent-broad.timer
echo 'PASS v2.2.1 installed'
