#!/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
systemctl disable --now paper-agent.service paper-agent-backup.timer 2>/dev/null || true
rm -f /etc/systemd/system/paper-agent.service /etc/systemd/system/paper-agent-backup.service /etc/systemd/system/paper-agent-backup.timer
systemctl daemon-reload
rm -rf /opt/paper-agent /etc/paper-agent
if [[ "${1:-}" == "--purge-data" ]]; then rm -rf /var/lib/paper-agent /var/log/paper-agent /var/backups/paper-agent; userdel paperagent 2>/dev/null || true; fi
echo "Removed. Data retained unless --purge-data was used."
