#!/bin/bash
set -euo pipefail
DB=/var/lib/paper-agent/paper-agent.db
OUT=/var/backups/paper-agent/paper-agent-$(date -u +%Y%m%dT%H%M%SZ).db
[[ -f "$DB" ]] || exit 0
python3 - "$DB" "$OUT" <<'PY'
import sqlite3,sys
src=sqlite3.connect(sys.argv[1]); dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()
PY
chown paperagent:paperagent "$OUT"; chmod 0600 "$OUT"
find /var/backups/paper-agent -type f -name 'paper-agent-*.db' -mtime +35 -delete
echo "$OUT"
