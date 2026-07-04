#!/bin/sh
# Beryl AX TS ip-rule watchdog — deployed 2026-07-03
# Runs via crontab every 5 minutes.
# Monitors the Tailscale ip rule (critical: without it, TCP goes dead).
# Auto-fixes when missing + logs to /tmp/ts-watchdog.log
#
# Tailscale IP may change over time; update TS_IP below when it does.

TS_IP="100.92.132.104"
LOGFILE="/tmp/ts-watchdog.log"
STATUSFILE="/tmp/ts-watchdog.status"
FIXCOUNT="/tmp/ts-watchdog.fixes"
RULE_PREF="5210"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOGFILE"
}

# Check if ip rule exists
if ip rule show 2>/dev/null | grep -q "from ${TS_IP} lookup 52"; then
    echo "ok $(date +%s)" > "$STATUSFILE"
    exit 0
fi

# Rule missing — attempt fix
log "ALERT: ip rule missing for ${TS_IP}, adding..."
ip rule add from "${TS_IP}" lookup 52 pref "${RULE_PREF}" 2>> "$LOGFILE"
rc=$?

if [ $rc -eq 0 ]; then
    # Flush conntrack to force new sessions through correct route
    conntrack -F 2>/dev/null || true
    log "FIXED: ip rule added (pref=${RULE_PREF}), conntrack flushed"

    # Increment fix counter
    fixes=$(cat "$FIXCOUNT" 2>/dev/null || echo 0)
    echo $((fixes + 1)) > "$FIXCOUNT"

    echo "ok $(date +%s)" > "$STATUSFILE"
else
    log "FAILED: ip rule add returned $rc"
    echo "fail $(date +%s)" > "$STATUSFILE"
fi
