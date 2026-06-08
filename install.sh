#!/bin/bash
# hermes-custom-tools — Deploy custom tools and scripts to Hermes runtime
# Run after hermes update or any time you want to re-sync.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HERMES_AGENT="$HOME/.hermes/hermes-agent"
HERMES_SCRIPTS="$HOME/.hermes/scripts"

echo "==> Deploying custom tools to $HERMES_AGENT/tools/"
cp "$SCRIPT_DIR/custom_tools/github_scouter.py"   "$HERMES_AGENT/tools/"
cp "$SCRIPT_DIR/custom_tools/nws_weather_tool.py" "$HERMES_AGENT/tools/"

echo "==> Deploying scripts to $HERMES_SCRIPTS/"
cp "$SCRIPT_DIR/scripts/"* "$HERMES_SCRIPTS/"

echo "==> Checking toolsets.py registration..."
if grep -q '"nws_now"' "$HERMES_AGENT/toolsets.py" 2>/dev/null; then
    echo "  ✓ nws_weather already registered"
else
    echo "  → Applying toolsets.patch..."
    cd "$HERMES_AGENT"
    if patch -p1 --dry-run < "$SCRIPT_DIR/toolsets.patch" >/dev/null 2>&1; then
        patch -p1 < "$SCRIPT_DIR/toolsets.patch"
        echo "  ✓ toolsets.py patched"
    else
        echo "  ⚠ patch doesn't apply cleanly. Edit toolsets.py manually:"
        echo "    cat $SCRIPT_DIR/toolsets.patch"
    fi
fi

# Verify
echo "==> Verifying..."
python3 -c "
import sys; sys.path.insert(0, '$HERMES_AGENT')
import toolsets
for t in ['nws_weather', 'github_scouter']:
    status = '✅' if t in toolsets.TOOLSETS else '❌'
    print(f'  {t}: {status}')
"

echo
echo "==> Done. Restart gateway if Hermes is running:"
echo "    hermes gateway restart"
