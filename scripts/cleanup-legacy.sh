#!/bin/bash
# cleanup-legacy.sh  -  remove what old builds (v4 and earlier v5 builds) left behind that the current program no longer uses. Run ON THE PI.
#
#   sudo bash scripts/cleanup-legacy.sh            DRY RUN: shows everything it would do and changes NOTHING
#   sudo bash scripts/cleanup-legacy.sh --apply    does it (refuses during the US session; add --force to override)
#
# What it does, in order:  (1) `db-maintain`: archives then removes the legacy v4 tables and decisions, expires old news, VACUUMs (a verified backup is taken first)
#                          (2) `cleanup`: database leftovers (old re-forecasts, finished queue rows, stale caches), the old v4 app folder inside /opt/paper-agent (archived first), stale systemd unit copies
#                              (archived first), install backups beyond the newest 3, old unpacked builds and zips in your home folder (the newest is kept), old temporary files, and config.json keys the
#                              program does not know (the effective configuration is verified identical)
# It never touches: the live database file, secrets and keys, settings.json, the current install, the newest backups. Everything archived goes to /var/backups/paper-agent.
set -u
APPLY=0; FORCE=0
for a in "$@"; do case "$a" in --apply) APPLY=1;; --force) FORCE=1;; *) echo "unknown option: $a (use --apply, --force)"; exit 2;; esac; done
[ "$(id -u)" = 0 ] || { echo "Run this with sudo:  sudo bash scripts/cleanup-legacy.sh"; exit 2; }
APP="${APP_DIR:-/opt/paper-agent}"
PY="env PYTHONPATH=$APP python3 -m paper_agent"
AS_APP="${PA_AS_APP-runuser -u paperagent --}"          # the database belongs to the service account; files and config need root
WHO="${SUDO_USER:-}"
DB="${DB_PATH:-/var/lib/paper-agent/paper-agent.db}"
size() { du -sk "$1" 2>/dev/null | cut -f1; }
echo "database now: $(du -h "$DB" 2>/dev/null | cut -f1) | /opt/paper-agent: $(du -sh "$APP" 2>/dev/null | cut -f1) | backups: $(du -sh /var/backups/paper-agent 2>/dev/null | cut -f1) | disk free: $(df -h / | awk 'NR==2{print $4}')"
echo
echo "################ 1/2  legacy database tables and decisions (db-maintain)"
$AS_APP $PY db-maintain plan || { echo "STOPPED: db-maintain plan failed"; exit 1; }
echo
echo "################ 2/2  everything else the old builds left behind (cleanup)"
$AS_APP $PY cleanup plan --scope db || exit 1
$PY cleanup plan --scope files,config --user "$WHO" || exit 1
if [ "$APPLY" = 0 ]; then
  echo
  echo "DRY RUN: nothing was changed. To do it:   sudo bash scripts/cleanup-legacy.sh --apply"
  exit 0
fi
if [ "$FORCE" = 0 ] && $PY status >/dev/null 2>&1 && env PYTHONPATH="$APP" python3 -c "import sys; from paper_agent.timeutil import market_state, utcnow; sys.exit(0 if market_state(utcnow())['execution'] else 1)" 2>/dev/null; then
  echo "STOPPED: the US market is open. Run this outside the session (it locks the database briefly), or add --force."
  exit 3
fi
echo
echo "################ APPLYING"
echo "--- 1/3 db-maintain apply (backup, archive, remove, VACUUM)"
$AS_APP $PY db-maintain apply || { echo "STOPPED: db-maintain apply failed; nothing else was done"; exit 1; }
echo "--- 2/3 database leftovers"
$AS_APP $PY cleanup apply --scope db || exit 1
echo "--- 3/3 files and config"
$PY cleanup apply --scope files,config --user "$WHO" || exit 1
echo
echo "################ DONE"
echo "database now: $(du -h "$DB" 2>/dev/null | cut -f1) | /opt/paper-agent: $(du -sh "$APP" 2>/dev/null | cut -f1) | backups: $(du -sh /var/backups/paper-agent 2>/dev/null | cut -f1) | disk free: $(df -h / | awk 'NR==2{print $4}')"
echo "Check the bot is healthy:   sudo -u paperagent env PYTHONPATH=$APP python3 -m paper_agent status"
