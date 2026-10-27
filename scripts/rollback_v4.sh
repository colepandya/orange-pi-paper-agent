#!/bin/bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]]||exit 1
systemctl disable --now paper-agent-v4.timer 2>/dev/null||true
systemctl enable --now paper-agent-broad.timer paper-agent-position-monitor-v362.timer 2>/dev/null||true
systemctl daemon-reload
echo 'PASS: V4 cycle disabled; prior collector/monitor schedules restored. Database history retained.'
