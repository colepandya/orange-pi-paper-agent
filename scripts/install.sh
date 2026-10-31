#!/bin/bash
# Paper Agent v5 installer. Backs up first, tests before touching anything, migrates the live DB non-destructively.
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
R="$(cd "$(dirname "$0")/.." && pwd)"; TS="$(date -u +%Y%m%dT%H%M%SZ)"; B="/var/backups/paper-agent/v5-$TS"
DB=/var/lib/paper-agent/paper-agent.db

# ---- only one install at a time (the auto-update timer and a manual run must never overlap)
mkdir -p /var/lock; exec 9>/var/lock/paper-agent-install.lock
flock -n 9 || { echo "Another Paper Agent install/update is already running; exiting."; [[ "${AUTO:-0}" == "1" ]] && exit 0 || exit 1; }

# ---- a DEDICATED temp directory on DISK (never RAM): /var/lib/paper-agent/tmp, verified not to be tmpfs/ramfs
TMPROOT=/var/lib/paper-agent/tmp
mkdir -p "$TMPROOT" 2>/dev/null || TMPROOT=/var/tmp/paper-agent-tmp
mkdir -p "$TMPROOT"
case "$(stat -f -c %T "$TMPROOT" 2>/dev/null || echo unknown)" in
  tmpfs|ramfs|devtmpfs) echo "WARN: $TMPROOT is RAM-backed; using /var/tmp instead"; TMPROOT=/var/tmp/paper-agent-tmp; mkdir -p "$TMPROOT" ;;
esac
chmod 0700 "$TMPROOT"
TEST_TMPDIR=""
cleanup() {
  [[ -z "$TEST_TMPDIR" ]] || rm -rf -- "$TEST_TMPDIR"
  rm -rf -- "$TMPROOT/pycache-$$"
}
trap cleanup EXIT HUP INT TERM
export PYTHONPYCACHEPREFIX="$TMPROOT/pycache-$$" PYTHONDONTWRITEBYTECODE=1   # keep bytecode (and root-owned __pycache__) OUT of the source tree and out of RAM

# ---- janitor: free leftovers from earlier runs (old versions put test files in RAM-backed /dev/shm and never removed them)
find "$TMPROOT" -mindepth 1 -maxdepth 1 -mmin +30 -exec rm -rf -- {} + 2>/dev/null || true
bash "$R/scripts/cleanup-ram.sh" >/dev/null 2>&1 || true
avail_mb() { awk '/MemAvailable:/ {printf "%d", $2/1024}' /proc/meminfo; }
echo "memory available: $(avail_mb) MB  |  RAM-disk in use: $(du -sm /dev/shm 2>/dev/null | cut -f1) MB"

echo "Installing Paper Agent BUILD $(cat "$R/BUILD" 2>/dev/null || echo '?')   (currently installed: BUILD $(cat /opt/paper-agent/BUILD 2>/dev/null || echo none))"
# ---- refuse a HYBRID tree (leftovers of a failed `patch`, or files that differ from what was packaged): this is how a half-patched build once got installed
bash "$R/scripts/verify-package.sh" "$R" || exit 1
# ---- completeness check: GitHub's web uploader can silently drop files; detect that BEFORE changing anything
if [[ -f "$R/MANIFEST.txt" ]]; then
  MISSING="$(cd "$R" && while read -r f; do [[ -f "$f" ]] || echo "  $f"; done < MANIFEST.txt)"
  if [[ -n "$MISSING" ]]; then
    echo "ERROR: the package is INCOMPLETE (a GitHub upload dropped files). Missing:"; echo "$MISSING"
    echo "Re-upload those files (or the whole folder contents) and run again. Nothing on this Pi was changed."; exit 1
  fi
  echo "package complete: $(wc -l < "$R/MANIFEST.txt") files verified"
else
  echo "WARN: no MANIFEST.txt found; cannot verify the package is complete"
fi
mkdir -p "$B" /opt/paper-agent /etc/paper-agent /var/lib/paper-agent /var/log/paper-agent
id paperagent >/dev/null 2>&1 || useradd --system --home /var/lib/paper-agent --shell /usr/sbin/nologin paperagent

# ---- low memory: an unattended update must not run the (memory-hungry) tests and risk starving the live bot
if [[ "${AUTO:-0}" == "1" && "$(avail_mb)" -lt 250 ]]; then
  echo "Only $(avail_mb) MB RAM available: postponing this unattended update (the timer will retry later)."; exit 0
fi

echo "[1/7] Pre-flight tests (nothing is changed if these fail)..."
python3 - <<'PY'
import sys; assert sys.version_info >= (3, 9), "Python 3.9+ required"
PY
python3 -m compileall -q "$R/paper_agent"
if [[ "${SKIP_TESTS:-0}" == "1" ]]; then
  echo "SKIP_TESTS=1: skipping tests"
else
  START=$(date +%s)
  TEST_TMPDIR="$(mktemp -d "$TMPROOT/pa-tests.XXXXXX")"        # disk-backed, removed on exit (even on error / Ctrl-C)
  TD="$TEST_TMPDIR"
  # hermetic: tests must never read this Pi's real config, dashboard settings, keys or admin file
  HERM="PAPER_AGENT_CONFIG_DIR=$TD/pa-t-etc PAPER_AGENT_STATE_DIR=$TD/pa-t-state PAPER_AGENT_SETTINGS=$TD/pa-t-state/settings.json PAPER_AGENT_SECRETS=$TD/pa-t-state/secrets.env PAPER_AGENT_ADMIN=$TD/pa-t-state/admin.json PAPER_AGENT_CONTROL=$TD/pa-t-state/control PAPER_AGENT_SYSTEMD_DIR=$TD/pa-t-systemd PYTHONWARNINGS=ignore::ResourceWarning"
  if [[ "${FULL_TESTS:-0}" == "1" ]]; then
    MODS=(discover -s tests -t . -p "test_*.py"); LABEL="full suite"
  else
    MODS=(tests.test_regressions tests.test_envmerge); LABEL="quick checks (full suite: FULL_TESTS=1; it is run before every release and takes ~2 min on a Pi)"
  fi
  if ! OUT=$(cd "$R" && env $HERM TMPDIR="$TD" PYTHONPATH="$R" python3 -m unittest "${MODS[@]}" 2>&1); then
    echo "$OUT" | tail -40; echo "FAIL: tests failed; aborting before any change"; exit 1
  fi
  echo "[$LABEL]"
  if [[ "${SOAK:-0}" == "1" ]]; then
    (cd "$R" && env $HERM TMPDIR="$TD" PYTHONPATH="$R" python3 -m unittest discover -s tests -t . -p "soak_*.py" 2>&1 | tail -3) || { echo "FAIL: soak test failed"; exit 1; }
  fi
  echo "$OUT" | grep -E "^(Ran|OK)" ; echo "tests passed in $(( $(date +%s) - START ))s"
  rm -rf -- "$TEST_TMPDIR"; TEST_TMPDIR=""                       # free the disk space immediately
fi

echo "[2/7] Backup -> $B"
if [[ -f $DB ]]; then
  BACKUP_PATH="$B/paper-agent.db" DB="$DB" python3 - <<'PY'
import os, sqlite3
s = sqlite3.connect(os.environ["DB"], timeout=60); d = sqlite3.connect(os.environ["BACKUP_PATH"]); s.backup(d)
assert d.execute("pragma integrity_check").fetchone()[0] == "ok"; print("db backup verified")
PY
fi
cp -a /etc/paper-agent "$B/etc-paper-agent" 2>/dev/null || true
[[ -d /opt/paper-agent/app ]] && cp -a /opt/paper-agent "$B/opt-paper-agent" || true
mkdir -p "$B/units"; cp -a /etc/systemd/system/paper-agent* "$B/units/" 2>/dev/null || true
systemctl list-units 'paper-agent*' --all --no-legend 2>/dev/null | awk '{print $1}' > "$B/active-units.txt" || true

echo "[3/7] Removing legacy units (never the current ones)"
for u in /etc/systemd/system/paper-agent*.service /etc/systemd/system/paper-agent*.timer /etc/systemd/system/paper-agent*.path; do
  [[ -e "$u" ]] || continue
  u="$(basename "$u")"
  case "$u" in
    paper-agent-cycle.service|paper-agent-cycle.timer|\
    paper-agent-guard.service|paper-agent-guard.timer|\
    paper-agent-dashboard.service|\
    paper-agent-backup.service|paper-agent-backup.timer|\
    paper-agent-universe.service|paper-agent-universe.timer|\
    paper-agent-update.service|paper-agent-update.timer|\
    paper-agent-open.timer|\
    paper-agent-report.service|paper-agent-report.timer|\
    paper-agent-maintain.service|paper-agent-maintain.timer|\
    paper-agent-control.service|paper-agent-control.path)
      ;;
    *)
      echo "Removing legacy unit: $u"
      systemctl disable --now "$u" 2>/dev/null || true
      rm -f "/etc/systemd/system/$u"
      ;;
  esac
done

echo "[4/7] Installing code and config"
[[ -d /opt/paper-agent/app ]] && mv /opt/paper-agent/app "/opt/paper-agent/app.legacy-$TS"
rm -rf /opt/paper-agent/paper_agent /opt/paper-agent/scripts && cp -a "$R/paper_agent" /opt/paper-agent/paper_agent && cp -a "$R/scripts" /opt/paper-agent/scripts && chmod +x /opt/paper-agent/scripts/*.sh
[[ -f /etc/paper-agent/config.json ]] || install -m0644 "$R/config/config.example.json" /etc/paper-agent/config.json
[[ -f /etc/paper-agent/sources.json ]] && cp /etc/paper-agent/sources.json "$B/sources.json.old"
install -m0644 "$R/config/sources.json" /etc/paper-agent/sources.json
[[ -f /etc/paper-agent/relations.json ]] || install -m0644 "$R/config/relations.json" /etc/paper-agent/relations.json       # YOUR edits to the relationship map are kept; the shipped one is always alongside as relations.default.json
install -m0644 "$R/config/relations.json" /etc/paper-agent/relations.default.json
if [[ -f "$R/config/market-data.env" ]]; then
  [[ -f /etc/paper-agent/market-data.env ]] && cp /etc/paper-agent/market-data.env "$B/market-data.env.old"
  # MERGE (never overwrite): keys already on the Pi survive unless the repo supplies a non-empty value
  if python3 "$R/paper_agent/envmerge.py" /etc/paper-agent/market-data.env "$R/config/market-data.env" /etc/paper-agent/market-data.env.new /etc/paper-agent/.forced-ids; then
    install -m0640 -o root -g paperagent /etc/paper-agent/market-data.env.new /etc/paper-agent/market-data.env
  else
    echo "WARN: key merge failed; keeping the existing keys file"
    [[ -f /etc/paper-agent/market-data.env ]] || install -m0640 -o root -g paperagent "$R/config/market-data.env" /etc/paper-agent/market-data.env
  fi
  rm -f /etc/paper-agent/market-data.env.new
elif [[ ! -f /etc/paper-agent/market-data.env ]]; then
  install -m0640 -o root -g paperagent "$R/config/market-data.env.example" /etc/paper-agent/market-data.env
fi
chown -R paperagent:paperagent /var/lib/paper-agent /var/log/paper-agent
install -d -o paperagent -g paperagent -m0755 /var/lib/paper-agent/control        # dashboard -> root-helper request/result files
chmod 0640 /etc/paper-agent/market-data.env; chgrp paperagent /etc/paper-agent/market-data.env

echo "[5/7] Migrating database (non-destructive)"
runuser -u paperagent -- env PYTHONPATH=/opt/paper-agent python3 -m paper_agent migrate
[[ -f /etc/paper-agent/watchlist.json ]] || { echo "building universe (needs network)"; env PYTHONPATH=/opt/paper-agent python3 -m paper_agent universe || echo "WARN: universe build failed; run: sudo systemctl start paper-agent-universe"; }

echo "[5b] Release settings entries (your own changes are never overwritten; see docs/SETTINGS.md)"
[[ -f "$R/config/deploy-settings.json" ]] && install -m0644 "$R/config/deploy-settings.json" /opt/paper-agent/deploy-settings.json
runuser -u paperagent -- env PYTHONPATH=/opt/paper-agent python3 -m paper_agent apply-deploy /opt/paper-agent/deploy-settings.json || echo "WARN: could not process release settings (continuing)"

runuser -u paperagent -- env PYTHONPATH=/opt/paper-agent python3 -m paper_agent common-words || echo "WARN: could not learn common words (continuing)"
runuser -u paperagent -- env PYTHONPATH=/opt/paper-agent python3 -m paper_agent recover --hours 24 || echo "WARN: could not recover recent events (continuing)"

echo "[6/7] Installing units"
for f in cycle.service cycle.timer guard.service guard.timer dashboard.service backup.service backup.timer universe.service universe.timer update.service update.timer open.timer report.service report.timer maintain.service maintain.timer control.service control.path; do install -m0644 "$R/systemd/paper-agent-$f" /etc/systemd/system/; done
systemctl daemon-reload
PYTHONPATH=/opt/paper-agent python3 -m paper_agent.control reconcile || echo "WARN: could not reconcile timer schedule with saved settings"
systemctl enable paper-agent-dashboard.service paper-agent-cycle.timer paper-agent-guard.timer paper-agent-backup.timer paper-agent-universe.timer paper-agent-update.timer paper-agent-open.timer paper-agent-report.timer paper-agent-maintain.timer paper-agent-control.path
systemctl restart paper-agent-dashboard.service
systemctl start paper-agent-control.path
systemctl start paper-agent-cycle.timer paper-agent-guard.timer paper-agent-backup.timer paper-agent-universe.timer paper-agent-update.timer paper-agent-open.timer paper-agent-report.timer paper-agent-maintain.timer

echo "[7/7] First cycle + health report"
ls -1dt /var/backups/paper-agent/v5-* 2>/dev/null | tail -n +4 | xargs -r rm -rf      # keep the 3 newest backups (each is a full uncompressed copy of the database)
PA=(runuser -u paperagent -- env PYTHONPATH=/opt/paper-agent python3 -m paper_agent)
timeout 600 "${PA[@]}" cycle || echo "(first cycle reported problems; see doctor below)"
"${PA[@]}" doctor --fix || true
echo
[[ -f "$R/BUILD" ]] && cp "$R/BUILD" /opt/paper-agent/BUILD
[[ -n "${VERSION_ID:-}" ]] && echo "$VERSION_ID" > /opt/paper-agent/.version   # only now: a failed install is retried by the next update
echo "DONE. Backup: $B   Dashboard: http://$(hostname -I | awk '{print $1}'):8088"
echo "Settings: open the dashboard -> Settings tab (no password by default). ALL settings, API keys (incl. Anthropic), refresh times, feeds and service restarts are editable there."
echo "Updates: this Pi now checks GitHub every 3 hours by itself. Health report any time:  sudo /opt/paper-agent/scripts/doctor.sh"
