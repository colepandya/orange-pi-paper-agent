#!/bin/bash
# Publish THIS package to GitHub from the Pi (uses your existing deploy key). Run as your normal user, NOT sudo:
#     bash scripts/publish.sh
# Afterwards the Pi's auto-update (every 3 h) or `sudo FORCE=1 /opt/paper-agent/scripts/update.sh` picks it up.
set -Eeuo pipefail
[[ $EUID -ne 0 || "${ALLOW_ROOT:-0}" == "1" ]] || { echo "Run as your normal user (not sudo) so your SSH key is used"; exit 1; }
command -v git >/dev/null || { echo "git is not installed: sudo apt install -y git"; exit 1; }
R="$(cd "$(dirname "$0")/.." && pwd)"
if find "$R" \( -name '*.rej' -o -name '*.orig' \) -not -path '*/.git/*' | grep -q .; then echo "Refusing to publish: this folder has failed-patch leftovers (*.rej / *.orig). Use a fresh unzip."; exit 1; fi
REPO_DIR="${REPO_DIR:-$HOME/paper-agent-repo}"
REPO_URL="${REPO_URL:-git@github.com:colepandya/orange-pi-paper-agent.git}"
KEY="${SSH_KEY:-$HOME/.ssh/orangepi-paper-agent}"
[[ -f "$KEY" ]] && export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
[[ -d "$REPO_DIR/.git" ]] || git clone "$REPO_URL" "$REPO_DIR"
cd "$REPO_DIR"
git fetch -q origin || true; git checkout -q main 2>/dev/null || git checkout -q -b main
git reset -q --hard origin/main 2>/dev/null || true
# mirror the package into the repo (keeping .git), without bytecode
find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -a "$R"/. .
find . -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf .git/index.lock 2>/dev/null || true
git add -A
if git diff --cached --quiet; then echo "GitHub already has this version (build $(cat BUILD))."; exit 0; fi
git -c user.name="${GIT_NAME:-paper-agent}" -c user.email="${GIT_EMAIL:-paper-agent@users.noreply.github.com}" commit -qm "Update to build $(cat BUILD)"
git push origin HEAD:main
echo "Published build $(cat BUILD) to GitHub ($(git rev-parse --short HEAD)). Files: $(git ls-files | wc -l)"
