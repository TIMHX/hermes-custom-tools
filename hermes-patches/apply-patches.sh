#!/usr/bin/env bash
# Apply custom patches to hermes-agent after updates
# Usage: ./apply-patches.sh [--dry-run]

set -e

HERMES_REPO="${HERMES_REPO:-$HOME/.hermes/hermes-agent}"
PATCH_DIR="$HOME/hermes-custom-tools/hermes-patches"

DRY_RUN=""
if [[ "$1" == "--dry-run" ]]; then
    DRY_RUN="echo"
fi

echo "=== Hermes Agent Patch Applicator ==="
echo "Hermes home: $HERMES_REPO"
echo ""

if [[ ! -d "$HERMES_REPO" ]]; then
    echo "ERROR: hermes-agent not found at $HERMES_REPO"
    exit 1
fi

OLD_COMMIT=$(cd "$HERMES_REPO" && git rev-parse HEAD 2>/dev/null || echo "unknown")
echo "Current hermes-agent commit: $OLD_COMMIT"
echo ""

# Apply each .diff patch (must run from HERMES_REPO for paths to resolve)
cd "$HERMES_REPO"
for diff in "$PATCH_DIR"/*.diff; do
    [[ -e "$diff" ]] || { echo "No patches found in $PATCH_DIR"; exit 0; }
    name=$(basename "$diff")
    echo "--- Applying: $name ---"
    if $DRY_RUN patch -p1 --no-backup-if-mismatch < "$diff"; then
        echo "  OK: $name"
    else
        echo "  FAIL: $name (may already be applied or conflicts)"
    fi
done

# Verify syntax
echo ""
echo "=== Verifying toolsets.py syntax ==="
if $DRY_RUN python3 -c "import sys; sys.path.insert(0, '$HERMES_REPO'); import toolsets" 2>&1; then
    echo "  OK: toolsets.py imports cleanly"
else
    echo "  FAIL: toolsets.py has syntax errors!"
fi

echo ""
echo "=== Verifying openviking plugin syntax ==="
if $DRY_RUN python3 -c "import sys; sys.path.insert(0, '$HERMES_REPO'); from plugins.memory.openviking import OpenVikingMemoryProvider; print('OK')" 2>&1; then
    echo "  OK: openviking plugin imports cleanly"
else
    echo "  FAIL: openviking plugin has syntax errors!"
fi

echo ""
NEW_COMMIT=$(cd "$HERMES_REPO" && git rev-parse HEAD 2>/dev/null || echo "unknown")
echo "Done. hermes-agent commit: $OLD_COMMIT → $NEW_COMMIT"
echo "Restart hermes gateway with: hermes gateway restart"
