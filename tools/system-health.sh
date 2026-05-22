#!/bin/bash
# 系统运维日报 — 每日全面健康检查（含安全审计）
# 检查：服务、证书、连接、资源、安全加固、攻击面

set -e

echo "# 🖥️ 系统运维日报 — $(date '+%Y-%m-%d %H:%M %Z')"
echo ""

# ═══════════════════════════════════════
# 1. 核心服务
# ═══════════════════════════════════════
echo "## 📋 核心服务"
for svc in caddy derper tailscaled fail2ban; do
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
for port_spec in "80/tcp" "443/tcp" "3478/udp"; do
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

echo ""

# ═══════════════════════════════════════
# 7. fail2ban
# ═══════════════════════════════════════
echo "## 🚫 fail2ban"
for jail in sshd; do
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

# 安全更新独立计数
sec_updates=$(apt list --upgradable 2>/dev/null | grep -i security | wc -l)
if [ "${sec_updates:-0}" -gt 0 ]; then
    echo "🔴 APT 安全更新: $sec_updates 个待处理！"
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

# ═══════════════════════════════════════
# 12. VPS 安全基线（版本 + CVE 风险）
# ═══════════════════════════════════════
echo "## 🔬 VPS 安全基线"

# Caddy 版本
caddy_ver=$(caddy version 2>/dev/null | head -1 | awk '{print $1}' | sed 's/v//')
echo "Caddy: v${caddy_ver:-?}"
# 检查是否低于 2.11.2（2026年8个CVE的修复版本）
caddy_major=$(echo "${caddy_ver:-0}" | cut -d. -f1)
caddy_minor=$(echo "${caddy_ver:-0}" | cut -d. -f2)
if [ "$caddy_major" -gt 2 ] || ([ "$caddy_major" -eq 2 ] && [ "${caddy_minor:-0}" -ge 12 ]); then
    echo "  ✅ Caddy >= 2.12，2026 CVE 已修复"
elif [ "$caddy_major" -eq 2 ] && [ "$caddy_minor" -eq 11 ] && [ "$(echo "$caddy_ver" | cut -d. -f3)" -ge 2 ]; then
    echo "  ✅ Caddy = 2.11.2，2026 CVE 已修复"
else
    echo "  ❌ Caddy < 2.11.2，存在已知 CVE 风险！"
fi

# Caddy 配置风险评估：不使用 vars_regexp / FastCGI / forward_auth
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

# OpenSSH 版本
ssh_ver=$(ssh -V 2>&1 | head -1 | grep -oP 'OpenSSH_\K[\d.p]+')
echo "OpenSSH: ${ssh_ver:-?}"
# regreSSHion (CVE-2024-6387) 检查
if dpkg -l openssh-server 2>/dev/null | grep -q "9.6p1-3ubuntu13"; then
    echo "  ✅ regreSSHion 补丁已安装"
fi

# Tailscale 版本
ts_ver=$(tailscale version 2>/dev/null | head -1)
echo "Tailscale: ${ts_ver:-?}"

# 监听端口审计
echo "--- 公网暴露端口 ---"
pub_ports=$(sudo ss -tlnp 2>/dev/null | grep -v "127.0.0.1\|::1\|tailscale0\|100\." | grep LISTEN)
if [ -z "$pub_ports" ]; then
    echo "  ✅ 无意外公网暴露"
else
    echo "$pub_ports" | while read line; do
        echo "  ℹ️ $line"
    done
fi

# 非预期服务检查（8644 不应公网可达）
if sudo ss -tlnp 2>/dev/null | grep 8644 | grep -q "0.0.0.0"; then
    echo "  ℹ️ Hermes:8644 绑 0.0.0.0（ufw 阻止公网，仅 Tailscale 可达）"
fi
echo ""

# ═══════════════════════════════════════
# 13a. Exit Node 出口管理
# ═══════════════════════════════════════
echo "## 🚪 Tailscale 出口管理"

# 谁在广告 exit node
ts_status=$(tailscale status 2>/dev/null)
echo "--- 广告 Exit Node ---"
echo "$ts_status" | grep "offers exit node" | while read line; do
    echo "✅ $(echo $line | awk '{print $2, $1}')"
done

# VPS 自己在用谁的出口
exit_host=$(tailscale status --json 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
for k,v in d.get('Peer',{}).items():
    if v.get('ExitNodeOption'):
        print(v.get('HostName',k))
        break
" 2>/dev/null)
if [ -n "$exit_host" ] && [ "$exit_host" != "No exit node" ]; then
    echo "⚠️ VPS 出口: $exit_host（非直连，如需切回: ~/bin/vps-exit-direct）"
    
    # 验证出口可用
    http_code=$(curl -sk -o /dev/null -w "%{http_code}" --connect-timeout 5 https://order.chownow.com/ 2>/dev/null || echo "000")
    if [ "$http_code" = "200" ]; then
        echo "   ✅ 出口验证: order.chownow.com → HTTP $http_code"
    else
        echo "   ❌ 出口验证: order.chownow.com → HTTP $http_code"
    fi
else
    echo "✅ VPS 出口: 直连（不依赖家庭网络）"
fi
echo ""

# ═══════════════════════════════════════
# 13b. Tailscale SSH 状态
# ═══════════════════════════════════════
echo "## 🔗 Tailscale SSH"
vps_ssh=$(tailscale debug prefs 2>/dev/null | grep -c '"RunSSH": true')
[ "$vps_ssh" -gt 0 ] && echo "✅ VPS: Tailscale SSH 已启用" || echo "⚠️ VPS: Tailscale SSH 未启用"

# Beryl AX
beryl_ssh=$(timeout 15 ssh -o ConnectTimeout=5 -o BatchMode=yes root@gl-mt3000 "echo OK" 2>&1) || true
if echo "$beryl_ssh" | grep -q "OK"; then
    echo "✅ Beryl AX: Tailscale SSH 可达"
else
    echo "⚠️ Beryl AX: SSH 异常 — $beryl_ssh"
fi
echo ""

# ═══════════════════════════════════════
# 13c. Windows SSH 安全审计
# ═══════════════════════════════════════
echo "## 🪟 Windows SSH 安全"

# Windows 检查
win_check=$(timeout 15 ssh -o ConnectTimeout=5 -o BatchMode=yes "Xing Hong@tim-pc" "sc query sshd 2>nul & findstr /i PasswordAuthentication C:\\ProgramData\\ssh\\sshd_config & findstr /i PubkeyAuthentication C:\\ProgramData\\ssh\\sshd_config & findstr /i KbdInteractive C:\\ProgramData\\ssh\\sshd_config" 2>&1) || true

if echo "$win_check" | grep -q "RUNNING"; then
    echo "✅ sshd: 运行中"
else
    echo "❌ sshd: 未运行"
fi

echo "$win_check" | grep -q "PasswordAuthentication no" && echo "✅ PasswordAuthentication: no" || echo "❌ PasswordAuthentication 不安全"
echo "$win_check" | grep -q "PubkeyAuthentication yes" && echo "✅ PubkeyAuthentication: yes" || echo "⚠️ PubkeyAuthentication 异常"
echo "$win_check" | grep -q "KbdInteractiveAuthentication no" && echo "✅ KbdInteractive: no" || echo "⚠️ KbdInteractive 未禁用"

# admins 密钥
win_keys=$(timeout 15 ssh -o ConnectTimeout=5 -o BatchMode=yes "Xing Hong@tim-pc" "type C:\\ProgramData\\ssh\\administrators_authorized_keys" 2>/dev/null | wc -l) || true
echo "  已授权密钥: $win_keys 把"

# Windows Tailscale exit node
echo "$ts_status" | grep "tim-pc" | grep -q "offers exit node" && echo "✅ Exit node: 广告中" || echo "⚠️ Exit node: 未广告"
echo ""

# ═══════════════════════════════════════
# 13d. Beryl AX 安全检查
# ═══════════════════════════════════════
echo "## 📡 Beryl AX 安全"

beryl_info=$(timeout 15 ssh -o ConnectTimeout=5 -o BatchMode=yes root@gl-mt3000 "iw dev 2>/dev/null | grep Interface | wc -l; echo '---'; cat /etc/config/tailscale 2>/dev/null | grep -c 'lan_enabled.*1'; echo '---'; cat /etc/config/tailscale 2>/dev/null | grep -c 'wan_enabled.*1'" 2>&1) || true

if echo "$beryl_info" | grep -q "^0$"; then
    wifi_count=$(echo "$beryl_info" | sed -n '1p')
    [ "$wifi_count" = "0" ] && echo "✅ WiFi: 关闭" || echo "⚠️ WiFi: $wifi_count 个接口活跃"
else
    echo "⚠️ WiFi: 检查失败"
fi

lan_en=$(echo "$beryl_info" | sed -n '3p')
wan_en=$(echo "$beryl_info" | sed -n '5p')
[ "$lan_en" = "1" ] && echo "✅ Tailscale LAN: 已启用" || echo "⚠️ Tailscale LAN: 未启用"
[ "$wan_en" = "1" ] && echo "✅ Tailscale WAN: 已启用" || echo "⚠️ Tailscale WAN: 未启用"

echo "$ts_status" | grep "gl-mt3000" | grep -qE "exit node" && echo "✅ Exit node: 广告/使用中" || echo "⚠️ Exit node: 未广告"
echo ""

# ═══════════════════════════════════════
# 13e. Tailscale ACL 隔离验证
# ═══════════════════════════════════════
echo "## 🔐 ACL 隔离验证"

# 从 Beryl AX 尝试 SSH 到 VPS（应该失败—— tag:router 不允许出站 SSH）
beryl_to_vps=$(timeout 10 ssh -o ConnectTimeout=5 -o BatchMode=yes root@gl-mt3000 "timeout 5 ssh -o ConnectTimeout=3 -o BatchMode=yes root@100.82.77.91 'echo OK' 2>&1" 2>&1) || true
if echo "$beryl_to_vps" | grep -q "OK"; then
    echo "❌ Beryl AX → VPS: SSH 可达（ACL 隔离失效！）"
else
    echo "✅ Beryl AX → VPS: SSH 阻断（tag:router 隔离生效）"
fi

# 从 Beryl AX 尝试访问其他 tailnet 节点
beryl_to_mother=$(timeout 10 ssh -o ConnectTimeout=5 -o BatchMode=yes root@gl-mt3000 "timeout 5 ssh -o ConnectTimeout=3 -o BatchMode=yes Xing\ Hong@100.70.105.128 'echo OK' 2>&1" 2>&1) || true
if echo "$beryl_to_mother" | grep -q "OK"; then
    echo "❌ Beryl AX → mother: SSH 可达（ACL 隔离失效！）"
else
    echo "✅ Beryl AX → mother: SSH 阻断（tag:router 隔离生效）"
fi
echo ""

# ═══════════════════════════════════════
# 13f. 跨节点 SSH 互通测试
# ═══════════════════════════════════════
echo "## 🔁 SSH 互通"

# VPS → Beryl AX
timeout 10 ssh -o ConnectTimeout=4 -o BatchMode=yes root@gl-mt3000 "echo OK" 2>/dev/null | grep -q OK && echo "✅ VPS → Beryl AX" || echo "❌ VPS → Beryl AX"

# VPS → Windows
timeout 10 ssh -o ConnectTimeout=4 -o BatchMode=yes "Xing Hong@tim-pc" "echo OK" 2>/dev/null | grep -q OK && echo "✅ VPS → Windows" || echo "❌ VPS → Windows"

echo ""

echo "---"
echo "✅ 日报完毕 — 下次运行：明天 9:00 EDT"
