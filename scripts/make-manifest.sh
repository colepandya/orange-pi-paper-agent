#!/bin/bash
# Maintainer tool: writes MANIFEST.txt (file list) and MANIFEST.sha256 (hash of every file) for the folder it lives in.
cd "$(dirname "$0")/.." || exit 1
find . -type f ! -name MANIFEST.txt ! -name MANIFEST.sha256 ! -path './.git/*' ! -path '*/__pycache__/*' ! -name '*.pyc' | sed 's#^\./##' | sort > MANIFEST.txt
xargs -d '\n' sha256sum < MANIFEST.txt > MANIFEST.sha256
echo "manifest: $(wc -l < MANIFEST.txt) files, build $(cat BUILD)"
