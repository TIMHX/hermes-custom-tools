#!/bin/bash
# 系统运维周报 — 每周全面健康检查（含安全审计）
# 检查：服务、证书、连接、资源、安全加固、攻击面

set -e

echo "# 🖥️ 系统运维周报 — $(date '+%Y-%m-%d %H:%M %Z')"
echo ""

# ═══════════════════════════════════════
# 1. 核心服务
# ═══════════════════════════════════════
echo "## 📋 核心服务"
for svc in caddy derper ssh-2222 tailscaled fail2ban; do
    state=$(systemctl is-active $svc 2>/dev/null || echo "缺失")
    if [ "$state" = "active" ]; then
        echo "✅ $svc"
    else
        echo "❌ $svc: $state"
    fi
done
echo ""

# ═══════════════════════════════════════
# 2. DERP 健康
# ═══════════════════════════════════════
echo "## 🌐 DERP"
# HTTPS 可达性
status=$(curl -sk -o /dev/null -w "%{http_code}" --connect-timeout 5 https://nn.xing-self-project.xyz/ 2>/dev/null || echo "000")
if [ "$status" = "200" ]; then
    echo "✅ HTTPS 正常 (HTTP $status)"
else
    echo "❌ HTTPS 异常 (HTTP $status)"
fi

# derper 绑定范围（必须 127.0.0.1）
bind_check=$(ss -tlnp 2>/dev/null | grep ":444" | grep -v "127.0.0.1:444" || true)
if [ -z "$bind_check" ]; then
    echo "✅ derper 仅绑 localhost:444"
else
    echo "❌ derper 暴露到 $bind_check"
fi

# verify-clients 标志
if systemctl cat derper 2>/dev/null | grep -q "\-\-verify-clients"; then
    echo "✅ --verify-clients 已启用"
else
    echo "❌ --verify-clients 缺失"
fi
echo ""

# ═══════════════════════════════════════
# 3. TLS 证书
# ═══════════════════════════════════════
echo "## 🔒 TLS 证书"
cert_info=$(echo | openssl s_client -servername nn.xing-self-project.xyz -connect nn.xing-self-project.xyz:443 2>/dev/null | openssl x509 -noout -dates 2>/dev/null)
not_after=$(echo "$cert_info" | grep "notAfter" | cut -d= -f2)
if [ -n "$not_after" ]; then
    echo "✅ 有效期至 $not_after"
    # 计算剩余天数
    expiry=$(date -d "$not_after" +%s 2>/dev/null)
    now=$(date +%s)
    days_left=$(( ($expiry - $now) / 86400 ))
    if [ $days_left -lt 14 ]; then
        echo "⚠️ 仅剩 $days_left 天，检查自动续期！"
    fi
else
    echo "❌ 证书获取失败"
fi
echo ""

# ═══════════════════════════════════════
# 4. Caddy 安全（响应头审计）
# ═══════════════════════════════════════
echo "## 🌐 Caddy 响应头"
resp_headers=$(curl -skI --connect-timeout 5 https://nn.xing-self-project.xyz/ 2>/dev/null)
# via 头不得泄露
if echo "$resp_headers" | grep -qi "via:"; then
    echo "❌ via 头泄露服务器类型"
else
    echo "✅ via 头已隐藏"
fi
# HSTS 必须有
if echo "$resp_headers" | grep -qi "strict-transport-security"; then
    echo "✅ HSTS 已启用"
else
    echo "❌ HSTS 缺失"
fi
echo ""

# ═══════════════════════════════════════
# 5. 防火墙
# ═══════════════════════════════════════
echo "## 🛡️ 防火墙"
# 检查 ufw 是否被锁定不卸载
if apt-mark showhold 2>/dev/null | grep -q ufw; then
    echo "✅ ufw 已锁定 (apt-mark hold)"
else
    echo "⚠️ ufw 未锁定，可能被自动移除"
fi

ufw_state=$(sudo /usr/sbin/ufw status 2>&1 | head -1)
echo "$ufw_state"

# 必需端口
for port_spec in "80/tcp" "443/tcp" "3478/udp" "2222/tcp"; do
    if sudo /usr/sbin/ufw status 2>&1 | grep -q "$port_spec.*ALLOW"; then
        echo "✅ $port_spec"
    else
        echo "❌ $port_spec 未开放"
    fi
done

# iptables 速率限制
if sudo iptables -L INPUT -n 2>/dev/null | grep -q "HTTPS_RATE"; then
    echo "✅ 443 速率限制 (5 req/s)"
else
    echo "❌ 443 速率限制规则缺失"
fi
echo ""

# ═══════════════════════════════════════
# 6. SSH 安全审计
# ═══════════════════════════════════════
echo "## 🔑 SSH 安全"

# 主配置审计
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

# 2222 端口
echo "--- 后备 SSH (2222/公网) ---"
if [ -f /etc/ssh/sshd_config_2222 ]; then
    for key in "PasswordAuthentication" "PubkeyAuthentication" "PermitRootLogin"; do
        val=$(sudo grep "^$key " /etc/ssh/sshd_config_2222 2>/dev/null | awk '{print $NF}' || echo "?")
        case "$key:$val" in
            PasswordAuthentication:no) echo "  ✅ $key: $val" ;;
            PubkeyAuthentication:yes) echo "  ✅ $key: $val" ;;
            PermitRootLogin:prohibit-password|PermitRootLogin:no) echo "  ✅ $key: $val" ;;
            *) echo "  ⚠️ $key: $val" ;;
        esac
    done
else
    echo "  ❌ sshd_config_2222 缺失"
fi

# 失败登录统计
echo "--- 失败登录 (24h) ---"
fail_ssh=$(sudo journalctl -u ssh-2222.service --since "24 hours ago" 2>/dev/null | grep -ci "Failed\|Invalid" || echo "0")
fail_ssh=${fail_ssh:-0}
echo "  ssh-2222: $fail_ssh 次"
echo ""

# ═══════════════════════════════════════
# 7. fail2ban
# ═══════════════════════════════════════
echo "## 🚫 fail2ban"
for jail in sshd ssh-2222; do
    status=$(sudo fail2ban-client status $jail 2>&1)
    if echo "$status" | grep -q "Status for"; then
        banned=$(echo "$status" | grep "Total banned" | awk '{print $NF}')
        echo "✅ $jail (封禁: $banned IP)"
    else
        echo "❌ $jail: 未运行"
    fi
done
echo ""

# ═══════════════════════════════════════
# 8. 系统资源
# ═══════════════════════════════════════
echo "## 💻 系统资源"
echo "CPU: $(top -bn1 | grep "Cpu(s)" | awk '{print $2+$4"%"}')"
echo "内存: $(free -h | awk '/^Mem:/ {print $3"/"$2}')"
swap_info=$(free -h | awk '/^Swap:/ {print $3"/"$2}')
if [ -n "$swap_info" ] && [ "$swap_info" != "0B/0B" ]; then
    echo "Swap: $swap_info"
fi
echo "磁盘: $(df -h / | awk 'NR==2 {print $3"/"$2 " ("$5")"}')"
echo "Inode: $(df -i / | awk 'NR==2 {print $3"/"$2 " ("$5")"}')"
echo "负载: $(uptime | awk -F'load average:' '{print $2}')"

# 僵尸进程
zombie=$(ps aux | awk '{if($8=="Z") count++} END{print count+0}')
if [ "$zombie" -gt 0 ]; then
    echo "⚠️ 僵尸进程: $zombie 个"
fi

# 打开文件描述符
fd_count=$(cat /proc/sys/fs/file-nr 2>/dev/null | awk '{print $1}')
fd_max=$(cat /proc/sys/fs/file-nr 2>/dev/null | awk '{print $3}')
if [ -n "$fd_count" ] && [ "$fd_max" -gt 0 ]; then
    fd_pct=$(( $fd_count * 100 / $fd_max ))
    if [ $fd_pct -gt 80 ]; then
        echo "⚠️ 文件描述符: $fd_count/$fd_max ($fd_pct%)"
    fi
fi
echo ""

# ═══════════════════════════════════════
# 9. 网络
# ═══════════════════════════════════════
echo "## 📡 网络"
echo -n "公网 IP: "; curl -s --connect-timeout 3 ifconfig.me 2>/dev/null || echo "超时"
echo -n "DNS (nn): "; dig +short nn.xing-self-project.xyz A @8.8.8.8 2>/dev/null || echo "解析失败"
echo -n "Tailscale: "; tailscale status 2>/dev/null | head -1 || echo "不可用"
echo ""

# ═══════════════════════════════════════
# 10. 系统健康
# ═══════════════════════════════════════
echo "## 🩺 系统健康"

# 需要重启？
if [ -f /var/run/reboot-required ]; then
    echo "⚠️ 需要重启！（内核或其他更新待生效）"
else
    echo "✅ 无需重启"
fi

# 失败的 systemd 单元
failed_units=$(systemctl --failed --no-pager 2>/dev/null | grep "loaded units listed" | grep -oP '\d+')
if [ "${failed_units:-0}" -gt 0 ]; then
    echo "❌ 失败 systemd 单元: $failed_units 个"
    systemctl --failed --no-legend 2>/dev/null | head -5 | sed 's/^/  /'
else
    echo "✅ 无失败 systemd 单元"
fi

# Docker（如安装）
if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    docker_ps=$(docker ps -q 2>/dev/null | wc -l)
    echo "🐳 Docker: $docker_ps 容器运行中"
fi

# /var/log 磁盘
log_size=$(du -sh /var/log 2>/dev/null | awk '{print $1}')
echo "/var/log: $log_size"

echo ""

# ═══════════════════════════════════════
# 11. 更新 & BBR
# ═══════════════════════════════════════
echo "## 🔄 系统更新"
pending=$(apt list --upgradable 2>/dev/null | grep -v "Listing" | wc -l)
if [ "$pending" -eq 0 ]; then
    echo "✅ APT 已最新"
else
    echo "⚠️ APT: $pending 个包待更新"
fi

# Homebrew（如安装）
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

echo "## 🟢 BBR"
sysctl net.ipv4.tcp_congestion_control 2>/dev/null
echo ""

echo "---"
echo "✅ 周报完毕 — 下次运行：下周一 9:00 EDT"
