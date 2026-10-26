#!/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo';exit 1; }
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
install -m 0755 "$ROOT/app/strategy_v3.py" /opt/paper-agent/app/strategy_v3.py
install -m 0755 "$ROOT/scripts/strategy_report.py" /opt/paper-agent/scripts/strategy_report.py
install -m 0644 "$ROOT/config/sources.json" /etc/paper-agent/broad-sources.json
python3 - <<'PY'
import importlib.util,sqlite3
p='/opt/paper-agent/app/strategy_v3.py';s=importlib.util.spec_from_file_location('v3',p);v3=importlib.util.module_from_spec(s);s.loader.exec_module(v3);d=sqlite3.connect('/var/lib/paper-agent/paper-agent.db');v3.init(d);print('model',v3.MODEL_VERSION,'installed');d.close()
PY
systemctl start paper-agent-broad.service
systemctl enable --now paper-agent-broad.timer
systemctl is-active --quiet pihole-FTL
systemctl is-active --quiet paper-agent.service
systemctl is-active --quiet paper-agent-broad.timer
echo 'PASS: strategy v3.0 installed; 10% minimum preserved'
