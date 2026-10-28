#!/bin/bash
# Usage: sudo scripts/rollback.sh /var/backups/paper-agent/v5-<timestamp>
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
B="${1:?backup dir}"; [[ -d $B ]] || { echo "no such backup $B"; exit 1; }
for u in $(ls /etc/systemd/system | grep -E '^paper-agent.*\.(service|timer)$' || true); do systemctl disable --now "$u" 2>/dev/null || true; rm -f "/etc/systemd/system/$u"; done
rm -rf /opt/paper-agent/paper_agent
[[ -d $B/opt-paper-agent ]] && { rm -rf /opt/paper-agent; cp -a "$B/opt-paper-agent" /opt/paper-agent; }
[[ -f $B/paper-agent.db ]] && { rm -f /var/lib/paper-agent/paper-agent.db-wal /var/lib/paper-agent/paper-agent.db-shm; install -m0640 -o paperagent -g paperagent "$B/paper-agent.db" /var/lib/paper-agent/paper-agent.db; }
[[ -d $B/etc-paper-agent ]] && cp -a "$B/etc-paper-agent/." /etc/paper-agent/
[[ -d $B/units ]] && cp -a "$B/units/." /etc/systemd/system/
systemctl daemon-reload
echo "Restored database, code, config and unit files from $B. Units are NOT re-enabled automatically:"
echo "  re-enable the ones you want, listed in $B/active-units.txt"
