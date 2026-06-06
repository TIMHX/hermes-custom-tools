#!/bin/bash
# ============================================================================
# 系统运维日报 — 基础设施健康检查
# ============================================================================
# 职责：基础设施层级（VPS 系统、网络、安全、Beryl AX、Windows、Tailscale）
# 不负责：应用软件（OpenViking/GitNexus/npm/CVE等→见 daily-maintenance-check.py）
#
# 运行：每天 9:00 EDT，cron job 8d3f8fcbfcc5，no_agent=true
# 交付：discord:1507855717254828152
#
# 检查项目：
#   1.  VPS 核心服务       caddy, derper, tailscaled, fail2ban
#   2.  DERP 健康          HTTPS可达性, 绑定范围, verify-clients
#   3.  TLS 证书           有效期, 剩余天数
#   4.  Caddy 响应头       via 隐藏, HSTS
#   5.  防火墙             ufw 状态, 端口, 速率限制
#   6.  SSH 安全           主配置审计
#   7.  fail2ban           封禁状态
#   8.  系统资源           CPU, 内存, 磁盘, 负载, 僵尸进程, 文件描述符
#   9.  网络               公网IP, DNS, Tailscale 状态
#   10. 系统健康           重启需求, 失败单元, Docker, /var/log
#   11. 更新 & BBR         APT, Brew, BBR 拥塞控制
#   12. VPS 安全基线       Caddy 版本 + CVE 风险, OpenSSH, Tailscale 版本, 端口审计
#
#   13. Beryl AX (100.100.114.49, gl-mt3000, NJ住宅):
#       a. 系统健康        SSH可达, uptime, 负载, 内存, 磁盘, kernel
#       b. 服务状态        sockd (SOCKS5代理), tailscaled
#       c. SOCKS5 代理     端口可达性, 端到端代理测试 (curl), 延迟
#       d. 包更新          opkg upgradable 计数 + 列表
#       e. 安全配置        WiFi状态, Tailscale LAN/WAN, Exit Node
#
#   14. SearXNG 搜索引擎   被封/异常引擎统计 (通过住宅IP出站)
#
#   15. Windows (100.117.102.69, tim-pc):
#       a. SSH 安全审计    sshd运行, PasswordAuth, PubkeyAuth, 授权密钥数
#       b. Exit Node 状态
#
#   16. Tailscale 出口管理  VPS出口状态, 出口验证
#   17. ACL 隔离验证       Beryl AX→VPS, Beryl AX→mother
#   18. 跨节点 SSH 互通    VPS→Beryl AX, VPS→Windows
# ============================================================================

set -e

TITLE_DATE=$(date '+%Y-%m-%d %H:%M %Z')
BERYL_HOST="gl-mt3000"
BERYL_PORT=1080

# --- Dynamic Beryl AX IP resolution (3-tier fallback) ---
resolve_beryl_ip() {
    # Tier 1: tailscale status (most accurate — reflects actual tailnet IP)
    local ip
    ip=$(tailscale status 2>/dev/null | grep 'gl-mt3000' | awk '{print $1}' | head -1)
    if [ -n "$ip" ]; then
        echo "$ip"
        return 0
    fi

    # Tier 2: DNS resolution
    ip=$(host gl-mt3000 2>/dev/null | grep 'has address' | awk '{print $NF}' | head -1)
    if [ -n "$ip" ]; then
        echo "$ip"
        return 0
    fi

    # Tier 3: hardcoded fallback with stderr warning
    echo "⚠️  WARNING: Beryl AX IP resolution failed (tailscale+DNS), using hardcoded fallback 100.100.114.49" >&2
    echo "100.100.114.49"
    return 0
}
BERYL_IP=$(resolve_beryl_ip)
WIN_HOST="tim-pc"
SSH_TIMEOUT="ConnectTimeout=5"
SSH_OPTS="-o $SSH_TIMEOUT -o BatchMode=yes"
HAS_PYTHON3=$(command -v python3 &>/dev/null && echo 1 || echo 0)

# Helper: print section header with blank line before
section() {
    echo ""
    echo "## $1"
}

echo "# 🖥️ 系统运维日报 — $TITLE_DATE"
echo ""

# ═══════════════════════════════════════
# 1. 核心服务
# ═══════════════════════════════════════
section "📋 核心服务"
for svc in caddy derper tailscaled fail2ban; do
    state=$(systemctl is-active $svc 2>/dev/null || echo "缺失")
    if [ "$state" = "active" ]; then
        echo "✅ $svc"
    else
        echo "❌ $svc: $state"
    fi
done

# ═══════════════════════════════════════
# 2. DERP 健康
# ═══════════════════════════════════════
section "🌐 DERP"
status=$(curl -sk -o /dev/null -w "%{http_code}" --connect-timeout 5 https://nn.xing-self-project.xyz/ 2>/dev/null || echo "000")
if [ "$status" = "200" ]; then
    echo "✅ HTTPS 正常 (HTTP $status)"
else
    echo "❌ HTTPS 异常 (HTTP $status)"
fi

bind_check=$(ss -tlnp 2>/dev/null | grep ":444" | grep -v "127.0.0.1:444" || true)
bind_any=$(ss -tlnp 2>/dev/null | grep -c ":444" || true)
if [ "${bind_any:-0}" -eq 0 ]; then
    echo "⚠️ derper 未监听 :444（服务可能未启动）"
elif [ -z "$bind_check" ]; then
    echo "✅ derper 仅绑 localhost:444"
else
    echo "❌ derper 暴露到 $bind_check"
fi

if systemctl cat derper 2>/dev/null | grep -q "\-\-verify-clients"; then
    echo "✅ --verify-clients 已启用"
else
    echo "❌ --verify-clients 缺失"
fi

# ═══════════════════════════════════════
# 3. TLS 证书
# ═══════════════════════════════════════
section "🔒 TLS 证书"
cert_info=$(echo | openssl s_client -servername nn.xing-self-project.xyz -connect nn.xing-self-project.xyz:443 2>/dev/null | openssl x509 -noout -dates 2>/dev/null)
not_after=$(echo "$cert_info" | grep "notAfter" | cut -d= -f2) || true
if [ -n "$not_after" ]; then
    echo "✅ 有效期至 $not_after"
    expiry=$(date -d "$not_after" +%s 2>/dev/null)
    now=$(date +%s)
    days_left=$(( ($expiry - $now) / 86400 ))
    if [ $days_left -lt 14 ]; then
        echo "⚠️ 仅剩 $days_left 天，检查自动续期！"
    fi
else
    echo "❌ 证书获取失败"
fi

# ═══════════════════════════════════════
# 4. Caddy 安全（响应头审计）
# ═══════════════════════════════════════
section "🌐 Caddy 响应头"
resp_headers=$(curl -skI --connect-timeout 5 https://nn.xing-self-project.xyz/ 2>/dev/null)
if echo "$resp_headers" | grep -qi "via:"; then
    echo "❌ via 头泄露服务器类型"
else
    echo "✅ via 头已隐藏"
fi
if echo "$resp_headers" | grep -qi "strict-transport-security"; then
    echo "✅ HSTS 已启用"
else
    echo "❌ HSTS 缺失"
fi

# ═══════════════════════════════════════
# 5. 防火墙
# ═══════════════════════════════════════
section "🛡️ 防火墙"
if apt-mark showhold 2>/dev/null | grep -q ufw; then
    echo "✅ ufw 已锁定 (apt-mark hold)"
else
    echo "⚠️ ufw 未锁定，可能被自动移除"
fi

ufw_state=$(sudo /usr/sbin/ufw status 2>&1 | head -1)
echo "$ufw_state"

for port_spec in "80/tcp" "443/tcp" "3478/udp"; do
    if sudo /usr/sbin/ufw status 2>&1 | grep -q "$port_spec.*ALLOW"; then
        echo "✅ $port_spec"
    else
        echo "❌ $port_spec 未开放"
    fi
done

if sudo iptables -L INPUT -n 2>/dev/null | grep -q "HTTPS_RATE"; then
    echo "✅ 443 速率限制 (5 req/s)"
else
    echo "❌ 443 速率限制规则缺失"
fi

# ═══════════════════════════════════════
# 6. SSH 安全审计
# ═══════════════════════════════════════
section "🔑 SSH 安全"
echo "--- 主 SSH (22/Tailscale) ---"
if [ -f /etc/ssh/sshd_config ]; then
    for key in "PasswordAuthentication" "PubkeyAuthentication" "PermitRootLogin" "GatewayPorts" "X11Forwarding"; do
        val=$(sudo grep "^$key " /etc/ssh/sshd_config 2>/dev/null | awk '{print $NF}' || echo "?")
        case "$key:$val" in
            PasswordAuthentication:no) echo "  ✅ $key: $val" ;;
            PubkeyAuthentication:yes) echo "  ✅ $key: $val" ;;
            PermitRootLogin:prohibit-password|PermitRootLogin:no) echo "  ✅ $key: $val" ;;
            GatewayPorts:no) echo "  ✅ $key: $val" ;;
            X11Forwarding:no) echo "  ✅ $key: $val" ;;
            *) echo "  ⚠️ $key: $val" ;;
        esac
    done
else
    echo "  ❌ sshd_config 缺失"
fi

# ═══════════════════════════════════════
# 7. fail2ban
# ═══════════════════════════════════════
section "🚫 fail2ban"
for jail in sshd; do
    status=$(sudo fail2ban-client status $jail 2>&1)
    if echo "$status" | grep -q "Status for"; then
        banned=$(echo "$status" | grep "Total banned" | awk '{print $NF}') || true
        echo "✅ $jail (封禁: $banned IP)"
    else
        echo "❌ $jail: 未运行"
    fi
done

# ═══════════════════════════════════════
# 8. 系统资源
# ═══════════════════════════════════════
section "💻 系统资源"
echo "CPU: $(top -bn1 | grep "Cpu(s)" | awk '{print $2+$4"%"}')"
echo "内存: $(free -h | awk '/^Mem:/ {print $3"/"$2}')"
swap_info=$(free -h | awk '/^Swap:/ {print $3"/"$2}')
if [ -n "$swap_info" ] && [ "$swap_info" != "0B/0B" ]; then
    echo "Swap: $swap_info"
fi
echo "磁盘: $(df -h / | awk 'NR==2 {print $3"/"$2 " ("$5")"}')"
echo "Inode: $(df -i / | awk 'NR==2 {print $3"/"$2 " ("$5")"}')"
echo "负载: $(uptime | awk -F'load average:' '{print $2}')"

zombie=$(ps aux | awk '{if($8 ~ /^Z/) count++} END{print count+0}')
if [ "${zombie:-0}" -gt 0 ]; then
    echo "⚠️ 僵尸进程: $zombie 个"
fi

fd_count=$(cat /proc/sys/fs/file-nr 2>/dev/null | awk '{print $1}')
fd_max=$(cat /proc/sys/fs/file-nr 2>/dev/null | awk '{print $3}')
if [ -n "$fd_count" ] && [ "${fd_count:-0}" -gt 0 ] && [ "${fd_max:-0}" -gt 0 ]; then
    fd_pct=$(( $fd_count * 100 / $fd_max ))
    if [ $fd_pct -gt 80 ]; then
        echo "⚠️ 文件描述符: $fd_count/$fd_max ($fd_pct%)"
    fi
fi

# ═══════════════════════════════════════
# 9. 网络
# ═══════════════════════════════════════
section "📡 网络"
echo -n "公网 IP: "; curl -s --connect-timeout 3 ifconfig.me 2>/dev/null || echo "超时"
echo -n "DNS (nn): "; dig +short nn.xing-self-project.xyz A @8.8.8.8 2>/dev/null || echo "解析失败"
echo -n "Tailscale: "; tailscale status 2>/dev/null | head -1 || echo "不可用"

# ═══════════════════════════════════════
# 10. 系统健康
# ═══════════════════════════════════════
section "🩺 系统健康"
if [ -f /var/run/reboot-required ]; then
    echo "⚠️ 需要重启！（内核或其他更新待生效）"
else
    echo "✅ 无需重启"
fi

failed_units=$(systemctl --failed --no-pager 2>/dev/null | grep "loaded units listed" | grep -oE '[0-9]+')
if [ "${failed_units:-0}" -gt 0 ]; then
    echo "❌ 失败 systemd 单元: $failed_units 个"
    systemctl --failed --no-legend 2>/dev/null | head -5 | sed 's/^/  /'
else
    echo "✅ 无失败 systemd 单元"
fi

if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    docker_ps=$(docker ps -q 2>/dev/null | wc -l)
    echo "🐳 Docker: $docker_ps 容器运行中"
fi

log_size=$(du -sh /var/log 2>/dev/null | awk '{print $1}')
echo "/var/log: $log_size"

# ═══════════════════════════════════════
# 11. 更新 & BBR
# ═══════════════════════════════════════
section "🔄 系统更新"
pending=$(apt list --upgradable 2>/dev/null | grep -v "Listing" | wc -l)
if [ "$pending" -eq 0 ]; then
    echo "✅ APT 已最新"
else
    echo "⚠️ APT: $pending 个包待更新"
fi

sec_updates=$(apt list --upgradable 2>/dev/null | grep -i security | wc -l)
if [ "${sec_updates:-0}" -gt 0 ]; then
    echo "🔴 APT 安全更新: $sec_updates 个待处理！"
fi

BREW_PATH="/home/linuxbrew/.linuxbrew/bin/brew"
if [ -x "$BREW_PATH" ]; then
    export PATH="$PATH:/home/linuxbrew/.linuxbrew/bin"
    brew_outdated=$(brew outdated 2>/dev/null | wc -l)
    if [ "${brew_outdated:-0}" -eq 0 ]; then
        echo "✅ Brew 已最新"
    else
        echo "⚠️ Brew: $brew_outdated 个包过时"
    fi
fi

section "🟢 BBR"
sysctl net.ipv4.tcp_congestion_control 2>/dev/null

# ═══════════════════════════════════════
# 12. VPS 安全基线（版本 + CVE 风险）
# ═══════════════════════════════════════
section "🔬 VPS 安全基线"
caddy_ver=$(caddy version 2>/dev/null | head -1 | awk '{print $1}' | sed 's/v//')
echo "Caddy: v${caddy_ver:-?}"
caddy_major=$(echo "${caddy_ver:-0}" | cut -d. -f1)
caddy_minor=$(echo "${caddy_ver:-0}" | cut -d. -f2)
if [ "$caddy_major" -gt 2 ] || ([ "$caddy_major" -eq 2 ] && [ "${caddy_minor:-0}" -ge 12 ]); then
    echo "  ✅ Caddy >= 2.12，2026 CVE 已修复"
elif [ "$caddy_major" -eq 2 ] && [ "$caddy_minor" -eq 11 ] && [ "$(echo "$caddy_ver" | cut -d. -f3)" -ge 2 ]; then
    echo "  ✅ Caddy = 2.11.2，2026 CVE 已修复"
else
    echo "  ❌ Caddy < 2.11.2，存在已知 CVE 风险！"
fi

caddyfile="/etc/caddy/Caddyfile"
if [ -f "$caddyfile" ]; then
    if grep -q "vars_regexp" "$caddyfile" 2>/dev/null; then
        echo "  ⚠️ Caddy 使用了 vars_regexp (CVE-2026-30852 相关)"
    else
        echo "  ✅ 未使用 vars_regexp (CVE-2026-30852 不适用)"
    fi
    if grep -qi "fastcgi\|php_fastcgi" "$caddyfile" 2>/dev/null; then
        echo "  ⚠️ Caddy 配置了 FastCGI (CVE-2026-27590 相关)"
    else
        echo "  ✅ 未使用 FastCGI (CVE-2026-27590 不适用)"
    fi
    if grep -qi "forward_auth" "$caddyfile" 2>/dev/null; then
        echo "  ⚠️ Caddy 配置了 forward_auth (CVE-2026-30851 相关)"
    else
        echo "  ✅ 未使用 forward_auth (CVE-2026-30851 不适用)"
    fi
fi

ssh_ver=$(ssh -V 2>&1 | head -1 | sed -n 's/.*OpenSSH_\([0-9.p]*\).*/\1/p')
echo "OpenSSH: ${ssh_ver:-?}"
if dpkg -l openssh-server 2>/dev/null | grep -q "9.6p1-3ubuntu13"; then
    echo "  ✅ regreSSHion 补丁已安装"
fi

ts_ver=$(tailscale version 2>/dev/null | head -1)
echo "Tailscale: ${ts_ver:-?}"

echo "--- 公网暴露端口 ---"
pub_ports=$(sudo ss -tlnp 2>/dev/null | grep -vE '127\.0\.0\.1|::1|tailscale0|100\.' | grep LISTEN) || true
if [ -z "$pub_ports" ]; then
    echo "  ✅ 无意外公网暴露"
else
    echo "$pub_ports" | while read line; do
        echo "  ℹ️ $line"
    done
fi

if sudo ss -tlnp 2>/dev/null | grep 8644 | grep -q "0.0.0.0"; then
    echo "  ℹ️ Hermes:8644 绑 0.0.0.0（ufw 阻止公网，仅 Tailscale 可达）"
fi

# ═══════════════════════════════════════
# 13. Beryl AX (NJ 住宅路由器)
# ═══════════════════════════════════════
section "📡 Beryl AX — 系统健康"

# --- 13a. 基本信息 + 系统状态 ---
# SSH 进 Beryl AX 获取系统信息（单次连接，避免多次 SSH 开销）
beryl_sys=$(timeout 20 ssh $SSH_OPTS root@$BERYL_HOST "
    echo 'UPTIME='\$(awk '{printf \"%.0f\", \$1}' /proc/uptime)
    echo 'LOAD='\$(awk '{print \$1,\$2,\$3}' /proc/loadavg)
    echo 'KERNEL='\$(uname -r)
    echo 'MEM='\$(free | awk '/^Mem:/ {printf \"%d/%d (%.1f%%)\", \$3/1024, \$2/1024, \$3/\$2*100}')
    echo 'DISK='\$(df -h /overlay | awk 'NR==2 {print \$3\"/\"\$2 \" (\"\$5\")\"}')
    echo 'SOCKD='\$(ps | grep -c '[s]ockd')
    echo 'TAILSCALED='\$(ps | grep -c '[t]ailscaled')
    echo 'TAILSCALE_VER='\$(tailscale version 2>/dev/null | head -1)
    echo 'WIFI='\$(iwinfo 2>/dev/null | grep -c 'ESSID' || echo 0)
" 2>&1) || true

if echo "$beryl_sys" | grep -q "UPTIME="; then
    # 解析各字段
    b_uptime=$(echo "$beryl_sys" | grep '^UPTIME=' | cut -d= -f2)
    b_load=$(echo "$beryl_sys" | grep '^LOAD=' | cut -d= -f2)
    b_kernel=$(echo "$beryl_sys" | grep '^KERNEL=' | cut -d= -f2)
    b_mem=$(echo "$beryl_sys" | grep '^MEM=' | cut -d= -f2)
    b_disk=$(echo "$beryl_sys" | grep '^DISK=' | cut -d= -f2)
    b_sockd=$(echo "$beryl_sys" | grep '^SOCKD=' | cut -d= -f2)
    b_ts=$(echo "$beryl_sys" | grep '^TAILSCALED=' | cut -d= -f2)
    b_ts_ver=$(echo "$beryl_sys" | grep '^TAILSCALE_VER=' | cut -d= -f2-)
    b_wifi=$(echo "$beryl_sys" | grep '^WIFI=' | cut -d= -f2)

    # 格式化输出
    [ -n "$b_uptime" ] && echo "Uptime: $(($b_uptime / 86400))天 $(($b_uptime % 86400 / 3600))时"
    [ -n "$b_load" ] && echo "负载: $b_load"
    [ -n "$b_mem" ] && echo "内存: ${b_mem}MB"
    [ -n "$b_disk" ] && echo "磁盘(/overlay): $b_disk"
    [ -n "$b_kernel" ] && echo "内核: $b_kernel"

    # 服务状态
    [ "${b_sockd:-0}" -gt 0 ] && echo "✅ sockd (SOCKS5:1080)" || echo "❌ sockd: 未运行"
    [ "${b_ts:-0}" -gt 0 ] && echo "✅ tailscaled (${b_ts_ver:-?})" || echo "❌ tailscaled: 未运行"

    # WiFi 安全
    if [ "${b_wifi:-0}" -gt 0 ] 2>/dev/null; then
        echo "⚠️ WiFi: ${b_wifi} 个活跃网络"
    else
        echo "✅ WiFi: 关闭"
    fi
else
    echo "❌ SSH 连接失败 — Beryl AX 不可达"
fi

# --- 13b. SOCKS5 代理功能测试 ---
echo ""
echo "--- SOCKS5 代理测试 ---"
source ~/.hermes/secrets/beryl-socks 2>/dev/null || { echo "❌ SOCKS5 凭证文件缺失"; }

# TCP 端口可达性（从 VPS）
if nc -z -w5 $BERYL_IP $BERYL_PORT 2>/dev/null; then
    echo "✅ sockd 端口 $BERYL_PORT 可达"
    
    # 端到端代理测试（带认证）
    proxy_result=$(curl -x socks5h://${BERYL_SOCKS_USER}:${BERYL_SOCKS_PASS}@$BERYL_IP:$BERYL_PORT \
        -s -o /dev/null -w "%{http_code} %{time_total}" \
        --max-time 10 https://www.google.com 2>/dev/null || echo "000 0")
    proxy_code=$(echo "$proxy_result" | awk '{print $1}')
    proxy_latency=$(echo "$proxy_result" | awk '{print $2}')
    
    if [ "$proxy_code" = "200" ]; then
        echo "✅ Google 可达 (${proxy_latency}s, 经住宅IP)"
    else
        echo "❌ 代理测试失败 (HTTP $proxy_code)"
    fi
else
    echo "❌ sockd 端口 $BERYL_PORT 不可达"
fi

# --- 13c. 包更新 ---
echo ""
beryl_pkg=$(timeout 15 ssh $SSH_OPTS root@$BERYL_HOST "
    opkg update 2>/dev/null >/dev/null
    opkg list-upgradable 2>/dev/null | grep -v '^$\|^Downloading\|^Updated' | head -10
    echo '---COUNT---'
    opkg list-upgradable 2>/dev/null | grep -v '^$\|^Downloading\|^Updated' | wc -l
" 2>&1) || true

upgradable_count=$(echo "$beryl_pkg" | grep -E '^[0-9]+$' | tail -1) || true
upgradable_list=$(echo "$beryl_pkg" | grep -vE '^---COUNT---$|^[0-9]+$') || true

if [ -n "$upgradable_count" ] && [ "$upgradable_count" -gt 0 ] 2>/dev/null; then
    echo "📦 opkg: $upgradable_count 个包可更新"
    if [ -n "$upgradable_list" ]; then
        echo "$upgradable_list" | head -10 | sed 's/^/  /'
    fi
else
    echo "✅ opkg 已最新"
fi

# --- 13d. Tailscale 配置 ---
echo ""
echo "--- Tailscale 配置 ---"
ts_status=$(tailscale status 2>/dev/null) || true

beryl_conf=$(timeout 10 ssh $SSH_OPTS root@$BERYL_HOST "
    echo \"LAN=\$(cat /etc/config/tailscale 2>/dev/null | grep -c 'lan_enabled.*1')\"
    echo \"WAN=\$(cat /etc/config/tailscale 2>/dev/null | grep -c 'wan_enabled.*1')\"
" 2>/dev/null) || true

lan_en=$(echo "$beryl_conf" | grep '^LAN=' | cut -d= -f2) || true
wan_en=$(echo "$beryl_conf" | grep '^WAN=' | cut -d= -f2) || true
[ "$lan_en" = "1" ] && echo "✅ Tailscale LAN: 已启用" || echo "⚠️ Tailscale LAN: 未启用"
[ "$wan_en" = "1" ] && echo "✅ Tailscale WAN: 已启用" || echo "⚠️ Tailscale WAN: 未启用"

echo "$ts_status" | grep "gl-mt3000" | grep -qE "offers exit node" && echo "✅ Exit Node: 广告中" || echo "⚠️ Exit Node: 未广告"

# --- 13e. SSH 安全审计 ---
echo ""
echo "--- SSH 安全审计 ---"
ssh_audit=$(timeout 10 ssh $SSH_OPTS root@$BERYL_HOST "
    uci get dropbear.main.PasswordAuth 2>/dev/null || echo 'N/A'
    uci get dropbear.main.RootPasswordAuth 2>/dev/null || echo 'N/A'
    grep -c ssh- ~/.ssh/authorized_keys /etc/dropbear/authorized_keys 2>/dev/null | tail -1 || echo '0'
    echo \"DROPBEAR=\$(ps | grep -c dropbear)\"
" 2>/dev/null) || true

ssh_passauth=$(echo "$ssh_audit" | sed -n '1p')
ssh_rootpw=$(echo "$ssh_audit" | sed -n '2p')
ssh_keycnt=$(echo "$ssh_audit" | sed -n '3p')
ssh_dropbear=$(echo "$ssh_audit" | grep '^DROPBEAR=' | cut -d= -f2) || true
[ "$ssh_passauth" = "0" ] && echo "  ✅ PasswordAuthentication: no" || echo "  ❌ PasswordAuthentication: yes"
[ "$ssh_rootpw" = "0" ] && echo "  ✅ RootPasswordAuth: no" || echo "  ❌ RootPasswordAuth: yes"
[ "${ssh_keycnt:-0}" -ge 1 ] 2>/dev/null && echo "  ✅ 已授权密钥: $ssh_keycnt 把" || echo "  ❌ 无授权密钥"
[ "${ssh_dropbear:-0}" -gt 0 ] && echo "  ✅ sshd: 运行中" || echo "  ❌ sshd: 未运行"

# --- 13f. 防火墙/端口审计 ---
echo ""
echo "--- 防火墙审计 ---"
fw_audit=$(timeout 10 ssh $SSH_OPTS root@$BERYL_HOST "
    # sockd 绑定检查
    echo \"BIND=\$(netstat -tlnp 2>/dev/null | grep 1080 | awk '{print \$4}' | head -1)\"
    # tailscale0 在 lan zone
    echo \"LANZONE=\$(uci get firewall.@zone[0].network 2>/dev/null)\"
    # WAN input policy
    echo \"WANINPUT=\$(uci get firewall.@zone[1].input 2>/dev/null)\"
" 2>/dev/null) || true

sockd_bind=$(echo "$fw_audit" | grep '^BIND=' | cut -d= -f2) || true
lan_zone=$(echo "$fw_audit" | grep '^LANZONE=' | cut -d= -f2) || true
wan_input=$(echo "$fw_audit" | grep '^WANINPUT=' | cut -d= -f2) || true

echo "$sockd_bind" | grep -q "0.0.0.0:1080" && echo "  ❌ sockd 绑 0.0.0.0:1080" || echo "  ✅ sockd 仅绑 Tailscale IP"
echo "$lan_zone" | grep -q "tailscale" && echo "  ✅ tailscale0 在 lan zone" || echo "  ❌ tailscale0 不在 lan zone"
[ "$wan_input" = "DROP" ] && echo "  ✅ WAN input: DROP" || echo "  ❌ WAN input: $wan_input"

# --- 13g. DNS 泄露检查 ---
echo ""
echo "--- DNS 泄露检查 ---"
dns_ts=$(timeout 8 ssh $SSH_OPTS root@$BERYL_HOST "
    netstat -tlnp 2>/dev/null | grep dnsmasq | grep -c '100\.'
" 2>&1) || true
[ "${dns_ts:-0}" -eq 0 ] 2>/dev/null && echo "  ✅ DNS 不监听 Tailscale IP" || echo "  ❌ DNS 泄露到 Tailscale ($dns_ts 绑定)"

# --- 13h. 服务完整性检查 ---
echo ""
echo "--- 服务完整性 ---"
svc_check=$(timeout 10 ssh $SSH_OPTS root@$BERYL_HOST "
    for svc in tor vsftpd minidlna zerotier smstools3 usbmuxd; do
        /etc/init.d/\$svc enabled 2>/dev/null && echo \"ENABLED:\$svc\" || echo \"disabled:\$svc\"
    done
" 2>&1) || true

re_enabled=$(echo "$svc_check" | grep "^ENABLED:") || true
if [ -z "$re_enabled" ]; then
    echo "  ✅ 冗余服务均保持禁用"
else
    echo "  ❌ 以下服务被意外启用："
    echo "$re_enabled" | while IFS=: read -r _ svc; do echo "     - $svc"; done
fi

# --- 13i. Tailscale 版本 + 在线状态 ---
echo ""
echo "--- Tailscale 状态 ---"
ts_version=$(timeout 8 ssh $SSH_OPTS root@$BERYL_HOST "tailscale version 2>/dev/null | head -1 | awk '{print \$2}'" 2>/dev/null) || true
ts_online=$(echo "$ts_status" | grep -oE 'gl-mt3000.*' | grep -oE 'active|idle|offline' | head -1) || true
ts_subnets=$(echo "$ts_status" | grep -oE 'gl-mt3000.*' | grep -oE 'routes: [^,]+' || echo "none")

[ -n "$ts_version" ] && echo "  ℹ️ Tailscale $ts_version" || echo "  ❌ Tailscale 版本获取失败"
[ "$ts_online" = "active" ] || [ "$ts_online" = "idle" ] && echo "  ✅ 在线状态: $ts_online" || echo "  ❌ 离线"
echo "$ts_subnets" | grep -qv "none" && echo "  ⚠️ Subnet routes 广告中: $ts_subnets" || echo "  ✅ 无 subnet routes 广告"

# ═══════════════════════════════════════
# 14. SearXNG 搜索引擎健康
# ═══════════════════════════════════════
section "🔍 SearXNG 搜索引擎"

# 查询 SearXNG API，统计被封/异常引擎
engines_json=$(curl -s --max-time 8 'http://127.0.0.1:8888/search?q=test&format=json&limit=1' 2>/dev/null)
if [ -n "$engines_json" ] && [ "$HAS_PYTHON3" = "1" ]; then
    engines_blocked=$(echo "$engines_json" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    blocked=[e for e in d.get('unresponsive_engines',[]) 
             if any(kw in str(e).lower() for kw in ['captcha','suspended','too many','blocked','rate limit'])]
    working=[e for e in d.get('unresponsive_engines',[]) if e not in blocked]
    print(f'{len(blocked)}')  # line1: blocked count
    print(f'{len(working)}')  # line2: other unresponsive
    for b in blocked:
        print(f'BLOCKED: {b[0]} — {b[1]}' if isinstance(b,list) else f'BLOCKED: {b}')
    for w in working:
        print(f'OTHER: {w[0]} — {w[1]}' if isinstance(w,list) else f'OTHER: {w}')
except Exception: pass
" 2>/dev/null)

    blocked_count=$(echo "$engines_blocked" | head -1)
    if [ "${blocked_count:-0}" -eq 0 ] 2>/dev/null; then
        echo "✅ 无搜索引擎被封"
    else
        echo "🔴 $blocked_count 个引擎被封："
        echo "$engines_blocked" | grep "^BLOCKED:" | sed 's/BLOCKED: /  ❌ /'
    fi
    
    # Show other unresponsive (non-blocked)
    other_lines=$(echo "$engines_blocked" | grep "^OTHER:" | sed 's/OTHER: /  ⚠️ /') || true
    [ -n "$other_lines" ] && echo "$other_lines"
elif [ -n "$engines_json" ]; then
    echo "⚠️ Python3 不可用，跳过 SearXNG 引擎分析"
else
    echo "⚠️ SearXNG API 无响应"
fi

# ═══════════════════════════════════════
# 15. Windows (tim-pc) 安全审计
# ═══════════════════════════════════════
section "🪟 Windows SSH 安全"

win_check=$(timeout 15 ssh $SSH_OPTS "Xing Hong@$WIN_HOST" "
    sc query sshd 2>nul
    findstr /i PasswordAuthentication C:\\ProgramData\\ssh\\sshd_config
    findstr /i PubkeyAuthentication C:\\ProgramData\\ssh\\sshd_config
    findstr /i KbdInteractive C:\\ProgramData\\ssh\\sshd_config
" 2>&1) || true

if echo "$win_check" | grep -q "RUNNING"; then
    echo "✅ sshd: 运行中"
else
    echo "❌ sshd: 未运行"
fi

echo "$win_check" | grep -q "PasswordAuthentication no" && echo "✅ PasswordAuthentication: no" || echo "❌ PasswordAuthentication 不安全"
echo "$win_check" | grep -q "PubkeyAuthentication yes" && echo "✅ PubkeyAuthentication: yes" || echo "⚠️ PubkeyAuthentication 异常"
echo "$win_check" | grep -q "KbdInteractiveAuthentication no" && echo "✅ KbdInteractive: no" || echo "⚠️ KbdInteractive 未禁用"

win_keys=$(timeout 10 ssh $SSH_OPTS "Xing Hong@$WIN_HOST" "type C:\\ProgramData\\ssh\\administrators_authorized_keys" 2>/dev/null | wc -l) || true
echo "  已授权密钥: $win_keys 把"

echo "$ts_status" | grep -w "tim-pc" | grep -q "offers exit node" && echo "✅ Exit Node: 广告中" || echo "⚠️ Exit Node: 未广告"

# ═══════════════════════════════════════
# 16. Tailscale 出口管理
# ═══════════════════════════════════════
section "🚪 Tailscale 出口管理"

echo "--- 广告 Exit Node ---"
echo "$ts_status" | grep "offers exit node" | while read line; do
    echo "✅ $(echo $line | awk '{print $2, $1}')"
done

if [ "$HAS_PYTHON3" = "1" ]; then
exit_node_id=$(tailscale debug prefs 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
eid=d.get('ExitNodeID','')
print(eid)
" 2>/dev/null)
fi
if [ -n "$exit_node_id" ]; then
    exit_host=$(tailscale status --json 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
eid='$exit_node_id'
for k,v in d.get('Peer',{}).items():
    if v.get('ID','') == eid:
        print(v.get('HostName',k))
        break
" 2>/dev/null)
    echo "⚠️ VPS 出口: ${exit_host:-$exit_node_id}（非直连，如需切回: ~/bin/vps-exit-direct）"
    
    http_code=$(curl -sk -o /dev/null -w "%{http_code}" --connect-timeout 5 https://order.chownow.com/ 2>/dev/null || echo "000")
    if [ "$http_code" = "200" ]; then
        echo "   ✅ 出口验证: order.chownow.com → HTTP $http_code"
    else
        echo "   ❌ 出口验证: order.chownow.com → HTTP $http_code"
    fi
else
    echo "✅ VPS 出口: 直连（不依赖家庭网络）"
fi

# ═══════════════════════════════════════
# 17. ACL 隔离验证
# ═══════════════════════════════════════
section "🔐 ACL 隔离验证"

# Beryl AX → VPS（应该被阻断 — tag:router 不允许出站 SSH）
vps_tailscale_ip=$(echo "$ts_status" | awk '/racknerd-c3b1d83/{print $1}' | head -1)
beryl_to_vps=$(timeout 15 ssh $SSH_OPTS root@$BERYL_HOST "
    timeout 5 ssh -o ConnectTimeout=3 -o BatchMode=yes root@${vps_tailscale_ip:-100.82.77.91} 'echo OK' 2>&1
" 2>&1) || true
if echo "$beryl_to_vps" | grep -q "OK"; then
    echo "❌ Beryl AX → VPS: SSH 可达（ACL 隔离失效！）"
else
    echo "✅ Beryl AX → VPS: SSH 阻断（tag:router 隔离生效）"
fi

# Beryl AX → mother
mother_tailscale_ip=$(echo "$ts_status" | awk '/mother/{print $1}' | head -1)
beryl_to_mother=$(timeout 15 ssh $SSH_OPTS root@$BERYL_HOST "
    timeout 5 ssh -o ConnectTimeout=3 -o BatchMode=yes Xing\\ Hong@${mother_tailscale_ip:-100.70.105.128} 'echo OK' 2>&1
" 2>&1) || true
if echo "$beryl_to_mother" | grep -q "OK"; then
    echo "❌ Beryl AX → mother: SSH 可达（ACL 隔离失效！）"
else
    echo "✅ Beryl AX → mother: SSH 阻断（tag:router 隔离生效）"
fi

# ═══════════════════════════════════════
# 18. 跨节点 SSH 互通
# ═══════════════════════════════════════
section "🔁 SSH 互通"

# VPS → Beryl AX
timeout 10 ssh $SSH_OPTS root@$BERYL_HOST "echo OK" 2>/dev/null | grep -q OK && echo "✅ VPS → Beryl AX" || echo "❌ VPS → Beryl AX"

# VPS → Windows
timeout 10 ssh $SSH_OPTS "Xing Hong@$WIN_HOST" "echo OK" 2>/dev/null | grep -q OK && echo "✅ VPS → Windows" || echo "❌ VPS → Windows"

# ═══════════════════════════════════════
# 19. 妈妈设备 (tag:mother) 状态
# ═══════════════════════════════════════
section "👩 妈妈设备状态"

mother_status=$(echo "$ts_status" | grep "mother") || true
if echo "$mother_status" | grep -qE "active|idle"; then
    echo "✅ mother 在线"
    mother_ip=$(echo "$mother_status" | awk '{print $1}')
    echo "  ℹ️ IP: $mother_ip"
elif echo "$mother_status" | grep -q "offline"; then
    last_seen=$(echo "$mother_status" | grep -oE 'last seen [^ ]+' || echo "未知")
    echo "❌ mother 离线 ($last_seen)"
else
    echo "❌ mother 未在 tailnet 中"
fi

# ═══════════════════════════════════════
# 20. DERP 链路健康 (从 Beryl AX 视角)
# ═══════════════════════════════════════
section "🔄 DERP 中继链路"

derp_http=$(timeout 10 ssh $SSH_OPTS root@$BERYL_HOST "
    curl -so /dev/null -w '%{http_code}' --max-time 5 https://nn.xing-self-project.xyz:443/ 2>&1
" 2>&1) || derp_http="000"

if [ "$derp_http" = "200" ]; then
    echo "✅ Beryl AX → DERP HTTPS: $derp_http"
else
    echo "❌ Beryl AX → DERP HTTPS: $derp_http (不可达)"
fi

# DERP netcheck 延迟
derp_latency=$(timeout 15 ssh $SSH_OPTS root@$BERYL_HOST "
    tailscale netcheck 2>&1 | grep 'myderp' | grep -oE '[0-9]+\.?[0-9]*ms' || echo '无数据'
" 2>&1) || derp_latency="无数据"

if echo "$derp_latency" | grep -q "^[0-9]"; then
    echo "  ℹ️ DERP 延迟: $derp_latency"
else
    echo "  ⚠️ DERP 延迟: 无数据（不影响功能）"
fi

# ═══════════════════════════════════════
# 21. Exit Node 功能验证
# ═══════════════════════════════════════
section "🚪 Exit Node 功能"

# 检查 Beryl AX 是否广告 exit node
beryl_exit_ok=$(echo "$ts_status" | grep "gl-mt3000" | grep -c "offers exit node" || echo "0")
beryl_exit_ip=$(echo "$ts_status" | grep "gl-mt3000" | grep -oE 'offers exit node(, routes: [^, ]+)?' || echo "")

if [ "$beryl_exit_ok" -ge 1 ] 2>/dev/null; then
    echo "✅ Beryl AX 广告 exit node"
else
    echo "❌ Beryl AX 未广告 exit node"
fi

# 统计可用 exit node 数量
exit_count=$(echo "$ts_status" | grep -c "offers exit node" || echo "0")
echo "  ℹ️ 可用 exit node: $exit_count 个"
echo "$ts_status" | grep "offers exit node" | while read line; do
    echo "     - $(echo $line | awk '{print $2, $1}')"
done

# 出口验证：如果 VPS 当前走 Beryl AX 出口
if [ -n "$exit_node_id" ]; then
    exit_ip=$(curl -s --max-time 5 https://ifconfig.me 2>/dev/null || echo "无法获取")
    echo "  ℹ️ 当前出口 IP: $exit_ip"
fi

# ═══════════════════════════════════════
# 22. VPN 链路汇总
# ═══════════════════════════════════════
section "📊 VPN 链路总览"

if [ "$derp_http" = "200" ] && [ "$beryl_exit_ok" -ge 1 ] 2>/dev/null; then
    echo "✅ 链路完整: DERP ✅ → Beryl AX ✅ → 互联网"
else
    [ "$derp_http" != "200" ] && echo "🔴 DERP 中断: Beryl AX 无法访问 DERP 中继"
    [ "$beryl_exit_ok" = "0" ] 2>/dev/null && echo "🔴 Exit Node 缺失: Beryl AX 未广告出口"
fi

if echo "$mother_status" | grep -qE "active|idle"; then
    echo "✅ 妈妈链路: 南宁 → DERP ✅ → Beryl AX ✅"
else
    echo "❌ 妈妈设备离线，VPN 链路中断"
fi

echo ""
echo "---"
echo "✅ 日报完毕 — 下次运行：明天 9:00 EDT"
