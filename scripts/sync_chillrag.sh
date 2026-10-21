#!/usr/bin/env bash
# Re-sync the vendored chillrag/ package from the upstream sibling repo.
#
# Upstream of truth: ../ChillRAG (development happens there). The copy inside
# this repo is a deployment snapshot — migrate the intranet with a
# self-contained build context. Re-run this after upstream changes, then
# review the diff before committing.
set -euo pipefail

cd "$(dirname "$0")/.."

UPSTREAM="${1:-../ChillRAG}"
SRC="$UPSTREAM/chillrag"
DST="chillrag"

if [ ! -d "$SRC" ]; then
    echo "upstream not found: $SRC (pass the ChillRAG repo root as arg 1)" >&2
    exit 1
fi

rm -rf "$DST"
cp -R "$SRC" "$DST"
find "$DST" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

echo "synced $SRC → $DST"
echo "upstream HEAD: $(git -C "$UPSTREAM" rev-parse --short HEAD 2>/dev/null || echo '?')"
git diff --stat -- "$DST" | tail -3
