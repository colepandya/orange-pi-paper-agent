#!/bin/bash
# Frees RAM-disk (/dev/shm) leftovers from older Paper Agent test runs. Safe to run any time:  sudo bash scripts/cleanup-ram.sh
set -u
before=$(du -sm /dev/shm 2>/dev/null | cut -f1)
find /dev/shm -maxdepth 1 \( -name 'pa-t-*' -o -name 'pa-pycache-*' -o -name 'pa-tests.*' \) -exec rm -rf -- {} + 2>/dev/null
find /dev/shm -maxdepth 1 -user root \( -name 'tmp????????.db' -o -name 'tmp????????.db-wal' -o -name 'tmp????????.db-shm' \) -exec rm -f -- {} + 2>/dev/null
for d in /dev/shm/tmp????????; do
  [[ -d "$d" && -O "$d" ]] || continue
  if ls "$d" 2>/dev/null | grep -qE '^(t\.db|settings\.json|secrets\.env|admin\.json|control|etc|PAUSE|pa-t-)'; then rm -rf -- "$d"; fi
done
after=$(du -sm /dev/shm 2>/dev/null | cut -f1)
echo "RAM-disk: ${before:-0} MB -> ${after:-0} MB  |  memory available now: $(awk '/MemAvailable:/ {printf "%d", $2/1024}' /proc/meminfo) MB"
