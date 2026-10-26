#!/bin/bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]]||{ echo 'Run with sudo';exit 1; }
B="${1:-$(find /var/backups/paper-agent -maxdepth 1 -type d -name 'v363-*'|sort|tail -1)}";[[ -n "$B" ]]||{ echo 'No V3.6.3 backup';exit 1; }
systemctl disable --now paper-agent-executor-v363.timer paper-agent-dashboard-v363.service 2>/dev/null||true
if [[ -f "$B/paper-agent-dashboard-v362.service" ]];then install -m0644 "$B/paper-agent-dashboard-v362.service" /etc/systemd/system/;fi
systemctl daemon-reload;systemctl enable --now paper-agent-dashboard-v362.service;sleep 3
curl -fsS http://127.0.0.1:8088/health | grep -q '3.6.2'
echo 'PASS: V3.6.3 services rolled back. Committed virtual exit transactions, if any, are not reversed automatically.'
