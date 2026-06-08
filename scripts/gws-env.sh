#!/bin/bash
# Google Workspace environment for Claude Code / subagents
# Source this before using gws or google-workspace skill scripts:
#   source ~/.hermes/scripts/gws-env.sh

export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export PATH="$PATH:/home/linuxbrew/.linuxbrew/lib/node_modules/@googleworkspace/cli/bin"

# Use hermes-agent's venv Python (has google-api-python-client installed)
export PYTHON="${HERMES_HOME}/hermes-agent/venv/bin/python3"

# Skill paths
export GWORKSPACE_SKILL_DIR="${HERMES_HOME}/skills/productivity/google-workspace"
export GAPI="$PYTHON ${GWORKSPACE_SKILL_DIR}/scripts/google_api.py"
export GBRIDGE="$PYTHON ${GWORKSPACE_SKILL_DIR}/scripts/gws_bridge.py"

# Quick test
echo "Google Workspace env loaded."
echo "HERMES_HOME=$HERMES_HOME"
echo "GAPI=$GAPI"
$gws --version 2>/dev/null && echo "gws: $(gws --version)" || echo "gws: not found"
