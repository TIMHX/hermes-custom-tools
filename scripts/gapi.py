#!/usr/bin/env python3
"""Google Workspace CLI for subagents (ClClaude Code, etc.)
Usage:
  gapi.py calendar create --summary "Meeting" --start 2026-04-15T10:00:00-04:00 --end 2026-04-15T10:30:00-04:00
  gapi.py gmail search "is:unread" --max 5
  gapi.py drive search "report"
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
SKILL_DIR = HERMES_HOME / "skills/productivity/google-workspace"
GAPI_SCRIPT = SKILL_DIR / "scripts/google_api.py"
GBRIDGE_SCRIPT = SKILL_DIR / "scripts/gws_bridge.py"

# Ensure gws CLI is on PATH
GWS_CLI_BIN = "/home/linuxbrew/.linuxbrew/lib/node_modules/@googleworkspace/cli/bin"
os.environ["PATH"] = os.environ.get("PATH", "") + ":" + GWS_CLI_BIN

# Use hermes-agent's venv Python (has google-api-python-client)
VENV_PYTHON = HERMES_HOME / "hermes-agent/venv/bin/python3"
if VENV_PYTHON.exists():
    PYTHON = str(VENV_PYTHON)
else:
    PYTHON = sys.executable


def main():
    # Pass through all arguments to the real google_api.py
    result = subprocess.run(
        [PYTHON, str(GAPI_SCRIPT)] + sys.argv[1:],
        env={**os.environ, "HERMES_HOME": str(HERMES_HOME)},
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
