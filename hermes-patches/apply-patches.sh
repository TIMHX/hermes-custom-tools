#!/usr/bin/env bash
# Apply custom patches to hermes-agent after updates
# Usage: ./apply-patches.sh [--dry-run]

set -e

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/hermes-agent}"
PATCH_DIR="$HOME/hermes-custom-tools/hermes-patches"

DRY_RUN=""
if [[ "$1" == "--dry-run" ]]; then
    DRY_RUN="echo"
fi

echo "=== Hermes Agent Patch Applicator ==="
echo "Hermes home: $HERMES_HOME"
echo ""

if [[ ! -d "$HERMES_HOME" ]]; then
    echo "ERROR: hermes-agent not found at $HERMES_HOME"
    exit 1
fi

OLD_COMMIT=$(cd "$HERMES_HOME" && git rev-parse HEAD 2>/dev/null || echo "unknown")
echo "Current hermes-agent commit: $OLD_COMMIT"
echo ""

# Apply each .diff patch
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
if $DRY_RUN python3 -c "import sys; sys.path.insert(0, '$HERMES_HOME'); import toolsets" 2>&1; then
    echo "  OK: toolsets.py imports cleanly"
else
    echo "  FAIL: toolsets.py has syntax errors!"
fi

echo ""
echo "=== Verifying openviking plugin syntax ==="
if $DRY_RUN python3 -c "import sys; sys.path.insert(0, '$HERMES_HOME'); from plugins.memory.openviking import OpenVikingMemoryProvider; print('OK')" 2>&1; then
    echo "  OK: openviking plugin imports cleanly"
else
    echo "  FAIL: openviking plugin has syntax errors!"
fi

echo ""
NEW_COMMIT=$(cd "$HERMES_HOME" && git rev-parse HEAD 2>/dev/null || echo "unknown")
echo "Done. hermes-agent commit: $OLD_COMMIT → $NEW_COMMIT"
echo "Restart hermes gateway with: hermes gateway restart"
