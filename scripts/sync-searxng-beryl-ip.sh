#!/usr/bin/env bash
# sync-searxng-beryl-ip.sh
# Checks if the SearXNG container's --add-host beryl-ax mapping matches
# the current Beryl AX Tailscale IP. Prints fix commands if mismatch.
# Does NOT auto-execute changes - user prefers manual review.
#
# Usage: ./sync-searxng-beryl-ip.sh
#        ./sync-searxng-beryl-ip.sh --status  (brief status only)

set -euo pipefail

STATUS_ONLY=false
if [[ "${1:-}" == "--status" ]]; then
    STATUS_ONLY=true
fi

# --- Get current Beryl AX IP from tailscale status ---
CURRENT_IP=""
if command -v tailscale &>/dev/null; then
    # Look for beryl/gl-mt3000 node in tailscale status
    CURRENT_IP=$(tailscale status 2>/dev/null | awk '/gl-mt3000|beryl/ {print $1; exit}')
fi

if [[ -z "$CURRENT_IP" ]]; then
    echo "ERROR: Could not determine Beryl AX IP from tailscale status."
    echo "       Make sure tailscale is running and the Beryl AX node is online."
    exit 1
fi

# --- Get current --add-host mapping on the searxng container ---
CONTAINER_IP=""
if docker ps --filter name=searxng --format '{{.Names}}' | grep -q searxng; then
    CONTAINER_IP=$(docker inspect searxng --format '{{range .HostConfig.ExtraHosts}}{{.}}{{"\n"}}{{end}}' 2>/dev/null | grep 'beryl-ax:' | cut -d: -f2-)
fi

if [[ -z "$CONTAINER_IP" ]]; then
    echo "WARNING: searxng container does not have a beryl-ax --add-host entry."
    echo "         Container needs to be recreated with --add-host."
    CONTAINER_IP="MISSING"
fi

# --- Status output ---
echo "============================================"
echo "SearXNG Beryl AX IP Sync Check"
echo "============================================"
echo "Current Beryl AX Tailscale IP:  $CURRENT_IP"
echo "Container --add-host beryl-ax:  $CONTAINER_IP"
echo "============================================"

if [[ "$CURRENT_IP" == "$CONTAINER_IP" ]]; then
    echo "✅ IPs MATCH - No action needed."
    exit 0
fi

if $STATUS_ONLY; then
    echo "❌ IPs MISMATCH - Run without --status to see fix commands."
    exit 1
fi

# --- Mismatch: print fix commands (do NOT auto-execute) ---
echo "⚠️  IPs MISMATCH - To fix, run these commands MANUALLY:"
echo ""
echo "  # 1. Stop & remove the current searxng container"
echo "  docker stop searxng && docker rm searxng"
echo ""
echo "  # 2. Recreate with updated --add-host"
echo "  docker run -d \\"
echo "    --name searxng \\"
echo "    --network searxng-net \\"
echo "    --add-host beryl-ax:${CURRENT_IP} \\"
echo "    -p 127.0.0.1:8888:8080 \\"
echo "    -v /etc/searxng:/etc/searxng \\"
echo "    --restart unless-stopped \\"
echo "    searxng/searxng:latest"
echo ""
echo "  # 3. Verify it's working"
echo "  docker ps --filter name=searxng"
echo "  curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8888/"
echo ""

# Also update settings.yml if it has hardcoded IP (safety check)
if grep -q 'socks5h://.*@[0-9]\+\.[0-9]\+\.[0-9]\+\.[0-9]\+:1080' /etc/searxng/settings.yml 2>/dev/null; then
    echo "  ⚠️  ALSO: /etc/searxng/settings.yml still has hardcoded IP in proxy URL!"
    echo "  Fix with:"
    echo "  sudo sed -i 's|socks5h://socksproxy:.*@[0-9.]*:1080|socks5h://socksproxy:PASSWORD_HERE@beryl-ax:1080|' /etc/searxng/settings.yml"
    echo ""
fi

exit 1
