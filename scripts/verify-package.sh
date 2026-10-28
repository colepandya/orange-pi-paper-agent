#!/bin/bash
# Refuses a HYBRID or damaged release folder. Usage: verify-package.sh <folder>      (exit 0 = fine, 1 = do not install)
R="${1:-.}"
JUNK="$(cd "$R" && find . \( -name '*.rej' -o -name '*.orig' \) -not -path './.git/*' | head -5)"
if [[ -n "$JUNK" ]]; then
  echo "ERROR: this folder contains leftovers of a FAILED patch:"; echo "$JUNK"
  echo "It is a half-patched mixture of two builds. Do NOT use it. Unpack a fresh copy of the zip into a new empty folder and run install.sh from there."; exit 1
fi
if [[ -f "$R/MANIFEST.sha256" ]]; then
  BAD="$(cd "$R" && sha256sum -c --quiet MANIFEST.sha256 2>&1)"; RC=$?        # the exit status of sha256sum itself (NOT of a pipeline's last command)
  if [[ $RC -ne 0 ]]; then
    echo "ERROR: files in this folder do not match the packaged release (modified, patched, missing or corrupted):"; echo "$BAD" | head -6
    echo "Unpack a fresh copy of the zip into a new empty folder and run install.sh from there. Nothing on this Pi was changed."; exit 1
  fi
  echo "integrity verified: every file matches the release"
else
  echo "WARN: no MANIFEST.sha256; cannot prove this folder matches a release"
fi
exit 0
