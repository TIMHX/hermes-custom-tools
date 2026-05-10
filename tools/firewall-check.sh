#!/bin/bash
# Firewall health check — weekly report
# Checks: ufw status, open ports, SSH config, unusual changes

set -e

echo "# 🔒 防火墙周报 — $(date '+%Y-%m-%d %H:%M %Z')"
echo ""

echo "## UFW 状态"
sudo ufw status verbose 2>&1
echo ""

echo "## 公网监听端口"
sudo ss -tlnp 2>&1 | grep -v "127.0.0.1" | grep -v "::1" | grep -v "100."
echo ""

echo "## iptables INPUT 丢包统计 (最近)"
sudo iptables -L INPUT -n -v 2>&1 | grep "DROP\|REJECT" | head -5
echo ""

echo "## SSH 认证方式"
echo "--- 主 SSH ---"
sudo grep -E "^(PasswordAuthentication|PubkeyAuthentication|PermitRootLogin)" /etc/ssh/sshd_config 2>/dev/null
echo "--- 2222 端口 ---"
sudo grep -E "^(PasswordAuthentication|PubkeyAuthentication|PermitRootLogin)" /etc/ssh/sshd_config_2222 2>/dev/null
echo ""

echo "## BBR 状态"
sysctl net.ipv4.tcp_congestion_control 2>&1
echo ""

echo "## 异常登录尝试 (最近 24h 失败登录)"
sudo journalctl -u ssh-2222.service --since "24 hours ago" 2>/dev/null | grep -i "failed\|invalid\|error" | tail -5 || echo "无"
echo ""

echo "---"
echo "✅ 报告完毕"
