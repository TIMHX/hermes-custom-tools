#!/bin/bash
# hermes-custom-tools — Deploy custom scripts to Hermes runtime
# Run after hermes update or any time you want to re-sync.
# 
# NOTE (2026-07-26): Custom toolsets (nws_weather, github_scouter) have been REMOVED.
# All cron jobs now use no_agent pure scripts. Only scripts are deployed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HERMES_SCRIPTS="$HOME/.hermes/scripts"

echo "==> Deploying scripts to $HERMES_SCRIPTS/"
cp "$SCRIPT_DIR/scripts/"* "$HERMES_SCRIPTS/"

echo
echo "==> Done. Scripts deployed:"
ls "$HERMES_SCRIPTS/"
