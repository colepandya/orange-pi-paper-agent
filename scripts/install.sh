#!/bin/bash
set -euo pipefail
DRY=0; [[ "${1:-}" == "--dry-run" ]] && DRY=1
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "Preflight: architecture=$(uname -m), memory=$(awk '/MemTotal/{printf "%.0f MB",$2/1024}' /proc/meminfo), root=$ROOT"
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required"; exit 1; }
python3 -m unittest discover -s "$ROOT/tests" -v
python3 -m py_compile "$ROOT"/app/*.py "$ROOT"/scripts/probe_sources.py
python3 -m json.tool "$ROOT/config/config.json" >/dev/null
python3 -m json.tool "$ROOT/config/watchlist.json" >/dev/null
if [[ $DRY -eq 1 ]]; then echo "DRY RUN OK: no changes made"; exit 0; fi
if [[ -f /var/lib/paper-agent/paper-agent.db && -x /opt/paper-agent/scripts/backup.sh ]]; then /opt/paper-agent/scripts/backup.sh; fi
getent group paperagent >/dev/null || groupadd --system paperagent
id paperagent >/dev/null 2>&1 || useradd --system --gid paperagent --home /var/lib/paper-agent --shell /usr/sbin/nologin paperagent
install -d -o root -g root -m 0755 /opt/paper-agent /etc/paper-agent
install -d -o paperagent -g paperagent -m 0750 /var/lib/paper-agent /var/log/paper-agent /var/backups/paper-agent
cp -a "$ROOT/app" "$ROOT/scripts" /opt/paper-agent/
install -m 0644 "$ROOT/config/config.json" /etc/paper-agent/config.json
install -m 0644 "$ROOT/config/watchlist.json" /etc/paper-agent/watchlist.json
cat > /etc/paper-agent/paper-agent.env <<ENV
PYTHONUNBUFFERED=1
ENV
install -m 0644 "$ROOT/systemd/paper-agent.service" /etc/systemd/system/paper-agent.service
install -m 0644 "$ROOT/systemd/paper-agent-backup.service" /etc/systemd/system/paper-agent-backup.service
install -m 0644 "$ROOT/systemd/paper-agent-backup.timer" /etc/systemd/system/paper-agent-backup.timer
chown -R paperagent:paperagent /var/lib/paper-agent /var/log/paper-agent /var/backups/paper-agent
systemctl daemon-reload
systemctl enable --now paper-agent-backup.timer
systemctl enable paper-agent.service
systemctl restart paper-agent.service
sleep 2
systemctl --no-pager --full status paper-agent.service || { journalctl -u paper-agent.service -n 80 --no-pager; exit 1; }
echo "Installed. Dashboard: http://$(hostname -I | awk '{print $1}'):8088"
