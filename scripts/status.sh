#!/bin/bash
set -euo pipefail
systemctl status paper-agent-broad.timer --no-pager
journalctl -u paper-agent-broad.service --since '2 hours ago' --no-pager -l | tail -n 20
python3 - <<'PY'
import sqlite3
p='/var/lib/paper-agent/paper-agent.db'; d=sqlite3.connect(p); d.row_factory=sqlite3.Row
print('\nSOURCE HEALTH')
for r in d.execute('select source_id,last_success,last_status,failures,items_total,last_error from source_health order by source_id'): print(dict(r))
print('\nDECISIONS BY STATUS')
for r in d.execute('select action,status,count(*) n from decisions group by action,status order by n desc'): print(dict(r))
print('\nOPEN POSITIONS')
for r in d.execute("select ticker,cost,opened from positions where status='OPEN'"): print(dict(r))
PY
