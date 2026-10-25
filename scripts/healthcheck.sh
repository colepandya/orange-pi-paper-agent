#!/bin/bash
set -u
echo "=== services ==="
systemctl is-active pihole-FTL paper-agent.service || true
echo "=== memory ==="
free -h
echo "=== paper agent cgroup ==="
systemctl show paper-agent.service -p MemoryCurrent -p CPUUsageNSec -p NRestarts
echo "=== dashboard ==="
python3 - <<'PY'
import urllib.request
try: print(urllib.request.urlopen('http://127.0.0.1:8088/health',timeout=3).read().decode())
except Exception as e: print('FAILED',e)
PY
echo "=== dns ==="
getent hosts pi.hole || true
