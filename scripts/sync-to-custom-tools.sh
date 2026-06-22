#!/bin/bash
# Sync from ~/.hermes/scripts/ to hermes-custom-tools repo
# Run weekly via cron. Exit 0 silently if no changes.
set -euo pipefail

REPO="$HOME/hermes-custom-tools"
SRC="$HOME/.hermes/scripts"

cd "$REPO"

# ─── Runtime scripts → scripts/ ─────────────────────────────────
# All .py and .sh files (except this sync script itself)
for f in "$SRC"/*.py "$SRC"/*.sh; do
    base=$(basename "$f")
    [[ "$base" == "sync-to-custom-tools.sh" ]] && continue
    cp "$f" scripts/
done

# ─── Tool files → custom_tools/ ─────────────────────────────────
# Subset of scripts that are also hermes-agent tool modules
cp "$SRC/daily-report.py"              custom_tools/
cp "$SRC/daily-briefing-fallback.py"   custom_tools/
cp "$SRC/daily-maintenance-check.py"   custom_tools/
cp "$SRC/daily-cve-report.py"          custom_tools/
cp "$SRC/severe-weather-watchdog.py"   custom_tools/

# ─── Commit only if changes ─────────────────────────────────────
if git diff --quiet && git diff --cached --quiet; then
    exit 0
fi

git add -A
git commit -m "chore: sync scripts $(date +%Y-%m-%d)"
git push -u myfork HEAD 2>/dev/null || git push
