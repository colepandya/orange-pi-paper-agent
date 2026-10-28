#!/bin/bash
# Pulls the latest code from GitHub and installs it if it changed. Safe to run any time; also runs from a systemd timer.
#   First install on a fresh Pi (one line):
#     curl -fsSL https://raw.githubusercontent.com/colepandya/orange-pi-paper-agent/main/scripts/update.sh | sudo bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
[[ -f /etc/paper-agent/update.conf ]] && source /etc/paper-agent/update.conf
REPO="${REPO:-colepandya/orange-pi-paper-agent}"; BRANCH="${BRANCH:-main}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
echo "Fetching https://github.com/$REPO ($BRANCH)"
# Public repo: plain download. Private repo: set GITHUB_TOKEN (read-only, "Contents: read") in /etc/paper-agent/update.conf
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  URL="https://api.github.com/repos/$REPO/tarball/$BRANCH"; AUTH=(-H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json")
else
  URL="https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"; AUTH=()
fi
curl -fsSL --retry 3 --retry-delay 5 ${AUTH[@]+"${AUTH[@]}"} -o "$W/src.tar.gz" "$URL" || { echo "Download failed. If the repo is private you need GITHUB_TOKEN in /etc/paper-agent/update.conf (see README)."; exit 1; }
ID="$(sha256sum "$W/src.tar.gz" | cut -d' ' -f1)"
if [[ "${FORCE:-0}" != "1" && -f /opt/paper-agent/.version && "$(cat /opt/paper-agent/.version)" == "$ID" ]]; then echo "Already up to date."; exit 0; fi
tar xzf "$W/src.tar.gz" -C "$W"
SRC="$(find "$W" -maxdepth 3 -name install.sh -path '*scripts*' | head -1 | xargs -r dirname | xargs -r dirname)"
[[ -n "$SRC" && -d "$SRC/paper_agent" ]] || { echo "Archive does not look like Paper Agent; aborting"; exit 1; }
VERSION_ID="$ID" AUTO=1 bash "$SRC/scripts/install.sh"
