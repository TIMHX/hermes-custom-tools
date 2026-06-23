#!/usr/bin/env python3
"""Beryl AX health + security check module.

Extracted from daily-report.py.
Single entry point: check_beryl_ax() → dict[str, Any].
Also exports _ssh_beryl and _resolve_beryl_ip for other daily-report consumers.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from typing import Any

# ═══════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════

BERYL_HOST = "gl-mt3000"
BERYL_IP_FALLBACK = "100.100.114.49"
BERYL_PORT = 1080
CMD_TIMEOUT = 30
SSH_TIMEOUT_OPTS = "-o ConnectTimeout=5 -o BatchMode=yes"


def run_cmd(cmd, timeout=CMD_TIMEOUT, cwd=None, shell=False, env=None, input_text=None):
    """Run a command, return (exit_code, stdout, stderr). Handles timeouts and missing executables."""
    try:
        if shell:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             shell=True, cwd=cwd, env=env, input=input_text)
        else:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             cwd=cwd, env=env, input=input_text)
        return p.returncode, p.stdout.strip() if p.stdout else "", p.stderr.strip() if p.stderr else ""
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return -2, "", str(e)
    except Exception as e:
        return -3, "", str(e)


# ═══════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════

def _resolve_beryl_ip() -> str:
    """Resolve Beryl AX IP with 3-tier fallback: tailscale status → DNS → hardcoded+warning."""
    # Tier 1: tailscale status (most accurate — reflects actual tailnet IP)
    try:
        _rc, ts_line, _ = run_cmd(
            "tailscale status 2>/dev/null | grep 'gl-mt3000' | awk '{print $1}' || true",
            shell=True, timeout=8,
        )
        if ts_line.strip():
            return ts_line.strip()
    except Exception:
        pass

    # Tier 2: DNS resolution
    try:
        return socket.getaddrinfo(BERYL_HOST, 22, family=socket.AF_INET)[0][4][0]
    except Exception:
        pass

    # Tier 3: hardcoded fallback with stderr warning
    print(
        f"⚠️  WARNING: Beryl AX IP resolution failed (tailscale+DNS), "
        f"using hardcoded fallback {BERYL_IP_FALLBACK}",
        file=sys.stderr,
    )
    return BERYL_IP_FALLBACK


def _ssh_beryl(command: str, timeout: int = 20) -> tuple[int, str, str]:
    """SSH into Beryl AX with timeout wrapper. Uses shell=False to avoid quote mangling."""
    ssh_args = ["timeout", str(timeout), "ssh"]
    for opt in SSH_TIMEOUT_OPTS.split():
        ssh_args.append(opt)
    ssh_args += ["-o", "StrictHostKeyChecking=accept-new", f"root@{BERYL_HOST}", command]
    return run_cmd(ssh_args, timeout=timeout + 5, shell=False)


# ═══════════════════════════════════════════
# Main check
# ═══════════════════════════════════════════

def check_beryl_ax() -> dict[str, Any]:
    """Complete Beryl AX health + security check. Single entry point."""
    ip = _resolve_beryl_ip()

    # ---- 13a. System info (single SSH connection) ----
    sys_cmd = (
        "echo 'UPTIME='$(awk '{printf \"%.0f\", $1}' /proc/uptime); "
        "echo 'LOAD='$(awk '{print $1\",\"$2\",\"$3}' /proc/loadavg); "
        "echo 'KERNEL='$(uname -r); "
        "echo 'MEM='$(free | awk '/^Mem:/ {printf \"%d/%d (%.1f%%)\", $3/1024, $2/1024, $3/$2*100}'); "
        "echo 'DISK='$(df -h /overlay | awk 'NR==2 {print $3\"/\"$2 \" (\"$5\")\"}'); "
        "echo 'SOCKD='$(ps | grep -c '[s]ockd'); "
        "echo 'TAILSCALED='$(ps | grep -c '[t]ailscaled'); "
        "echo 'TAILSCALE_VER='$(tailscale version 2>/dev/null | head -1); "
        "echo 'WIFI='$(iwinfo 2>/dev/null | grep -c 'ESSID' || echo 0); "
        "echo 'ENABLED_SVC='$(ls /etc/rc.d/S* 2>/dev/null | wc -l); "
        "echo 'TS_ZONE='$(iptables -L INPUT -n 2>/dev/null | grep -c zone_tailscale0_input || echo 0)"
    )
    rc_sys, sys_out, _ = _ssh_beryl(sys_cmd, timeout=20)

    result: dict[str, Any] = {"reachable": False, "ip": ip, "hostname": BERYL_HOST}

    if rc_sys != 0 or "UPTIME=" not in sys_out:
        result["error"] = f"SSH failed (rc={rc_sys})" if rc_sys != 0 else "No system data"
        return result

    result["reachable"] = True
    for line in sys_out.split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k == "UPTIME":
                try:
                    ut = int(v)
                    result["uptime_days"] = ut // 86400
                    result["uptime_hours"] = (ut % 86400) // 3600
                except ValueError:
                    result["uptime_raw"] = v
            elif k == "LOAD":
                result["load"] = v
            elif k == "KERNEL":
                result["kernel"] = v
            elif k == "MEM":
                result["memory"] = v
            elif k == "DISK":
                result["disk"] = v
            elif k == "SOCKD":
                try:
                    result["sockd_running"] = int(v) > 0
                except ValueError:
                    result["sockd_running"] = None
            elif k == "TAILSCALED":
                try:
                    result["tailscaled_running"] = int(v) > 0
                except ValueError:
                    result["tailscaled_running"] = None
            elif k == "TAILSCALE_VER":
                result["tailscale_version"] = v
            elif k == "WIFI":
                try:
                    result["wifi_networks"] = int(v)
                except ValueError:
                    result["wifi_networks"] = 0
            elif k == "ENABLED_SVC":
                try:
                    result["enabled_service_count"] = int(v)
                except ValueError:
                    result["enabled_service_count"] = None
            elif k == "TS_ZONE":
                try:
                    result["tailscale_zone_ok"] = int(v) > 0
                except ValueError:
                    result["tailscale_zone_ok"] = None


                    result["wifi_networks"] = 0

    # ---- 13b. SOCKS5 proxy test ----
    # Port reachability
    rc_nc, nc_out, _ = run_cmd(f"nc -z -w5 {ip} {BERYL_PORT} 2>/dev/null && echo 'open' || echo 'closed'",
                              shell=True, timeout=8)
    socks5_port_open = "open" in nc_out

    socks5_proxy_ok = None
    socks5_latency = None
    socks5_cred_ok = os.path.isfile(os.path.expanduser("~/.hermes/secrets/beryl-socks"))
    if socks5_port_open and socks5_cred_ok:
        # Use '.' not 'source' — POSIX sh compat (shell=True uses /bin/sh)
        rc_src, src_out, _ = run_cmd(
            f". ~/.hermes/secrets/beryl-socks 2>/dev/null && "
            f"curl -x socks5h://${{BERYL_SOCKS_USER}}:${{BERYL_SOCKS_PASS}}@{ip}:{BERYL_PORT} "
            f"-s -o /dev/null -w '%{{http_code}} %{{time_total}}' --max-time 10 https://www.google.com 2>/dev/null "
            f"|| echo '000 0'",
            shell=True, timeout=15,
        )
        if src_out:
            parts = src_out.strip().split()
            socks5_proxy_ok = parts[0] == "200"
            try:
                socks5_latency = float(parts[1]) if len(parts) > 1 else None
            except ValueError:
                pass

    result["socks5"] = {
        "port_open": socks5_port_open,
        "proxy_ok": socks5_proxy_ok,
        "latency": socks5_latency,
        "cred_file_exists": socks5_cred_ok,
    }

    # ---- 13c. opkg updates ----
    rc_pkg, pkg_out, _ = _ssh_beryl(
        "opkg update 2>/dev/null >/dev/null; "
        "opkg list-upgradable 2>/dev/null | grep -v '^$\\|^Downloading\\|^Updated' | head -10; "
        "echo '---COUNT---'; "
        "opkg list-upgradable 2>/dev/null | grep -v '^$\\|^Downloading\\|^Updated' | wc -l",
        timeout=20,
    )
    opkg_upgradable: list[dict[str, str]] = []
    opkg_count = 0
    count_seen = False
    for line in pkg_out.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line == "---COUNT---":
            count_seen = True
            continue
        if count_seen:
            try:
                opkg_count = int(line)
            except ValueError:
                pass
            count_seen = False  # only one count line
        else:
            m = re.match(r'^(\S+)\s+-\s+(\S+)\s+-\s+(\S+)$', line)
            if m:
                opkg_upgradable.append({
                    "name": m.group(1),
                    "old_version": m.group(2),
                    "new_version": m.group(3),
                })
    result["opkg"] = {
        "upgradable_count": opkg_count,
        "upgradable": opkg_upgradable[:10],
    }

    # ---- 13d. Tailscale config ----
    rc_ts, ts_conf, _ = _ssh_beryl(
        "echo \"LAN=$(cat /etc/config/tailscale 2>/dev/null | grep -c 'lan_enabled.*1')\"; "
        "echo \"WAN=$(cat /etc/config/tailscale 2>/dev/null | grep -c 'wan_enabled.*1')\"",
        timeout=10,
    )
    lan_en = wan_en = False
    for line in ts_conf.split("\n"):
        if line.startswith("LAN="):
            lan_en = line.split("=", 1)[1] == "1"
        elif line.startswith("WAN="):
            wan_en = line.split("=", 1)[1] == "1"
    result["tailscale_config"] = {"lan_enabled": lan_en, "wan_enabled": wan_en}

    # ---- 13e. SSH security audit ----
    rc_ssh, ssh_out, _ = _ssh_beryl(
        "uci get dropbear.main.PasswordAuth 2>/dev/null || echo 'N/A'; "
        "uci get dropbear.main.RootPasswordAuth 2>/dev/null || echo 'N/A'; "
        "grep -c ssh- ~/.ssh/authorized_keys /etc/dropbear/authorized_keys 2>/dev/null | tail -1 || echo '0'; "
        "echo 'DROPBEAR='$(ps | grep -c dropbear)",
        timeout=10,
    )
    ssh_lines = ssh_out.split("\n")
    pass_auth = ssh_lines[0].strip() if len(ssh_lines) > 0 else "N/A"
    root_pw = ssh_lines[1].strip() if len(ssh_lines) > 1 else "N/A"
    key_cnt = ssh_lines[2].strip() if len(ssh_lines) > 2 else "0"
    dropbear_running = False
    for line in ssh_lines:
        if line.startswith("DROPBEAR="):
            try:
                dropbear_running = int(line.split("=", 1)[1]) > 0
            except ValueError:
                pass
    # Extract count from "filename:2" format (grep -c with multiple files)
    if ":" in key_cnt:
        key_cnt = key_cnt.rsplit(":", 1)[-1]
    try:
        key_count = int(key_cnt)
    except ValueError:
        key_count = 0
    # pass_auth="0" means password DISABLED (secure) → password_auth should be False
    # "N/A" means option not set → treat as secure (default is key-only for root)
    result["ssh_audit"] = {
        "password_auth": pass_auth not in ("0", "off", "no"),
        "root_password_auth": root_pw not in ("0", "off", "no", "N/A"),
        "ssh_key_count": key_count,
        "sshd_running": dropbear_running,
    }

    # ---- 13f. Firewall/port audit ----
    rc_fw, fw_out, _ = _ssh_beryl(
        "echo \"BIND=$(netstat -tlnp 2>/dev/null | grep 1080 | awk '{print $4}' | head -1)\"; "
        "echo \"LANZONE=$(uci get firewall.@zone[0].network 2>/dev/null)\"; "
        "echo \"WANINPUT=$(uci get firewall.@zone[1].input 2>/dev/null)\"",
        timeout=10,
    )
    sockd_bind = ""
    lan_zone = ""
    wan_input = ""
    for line in fw_out.split("\n"):
        if line.startswith("BIND="):
            sockd_bind = line.split("=", 1)[1]
        elif line.startswith("LANZONE="):
            lan_zone = line.split("=", 1)[1]
        elif line.startswith("WANINPUT="):
            wan_input = line.split("=", 1)[1]
    result["firewall"] = {
        "sockd_binds_public": "0.0.0.0" in sockd_bind,
        "sockd_bind": sockd_bind,
        "tailscale_in_lan_zone": "tailscale" in lan_zone,
        "wan_input": wan_input,
        "wan_input_drop": wan_input == "DROP",
    }

    # ---- 13g. DNS leak check ----
    rc_dns, dns_out, _ = _ssh_beryl(
        "netstat -tlnp 2>/dev/null | grep dnsmasq | grep -c '100\\.' || echo '0'",
        timeout=10,
    )
    try:
        dns_on_tailscale = int(dns_out.strip()) > 0
    except ValueError:
        dns_on_tailscale = False
    result["dns_leak"] = {"dns_on_tailscale_ip": dns_on_tailscale}

    # ---- 13h. Service integrity ----
    rc_svc, svc_out, _ = _ssh_beryl(
        "for svc in tor vsftpd minidlna zerotier smstools3 usbmuxd; do "
        "/etc/init.d/$svc enabled 2>/dev/null && echo \"ENABLED:$svc\" || echo \"disabled:$svc\"; done",
        timeout=10,
    )
    re_enabled = [l.split(":", 1)[1] for l in svc_out.split("\n") if l.startswith("ENABLED:")]
    result["services"] = {
        "re_enabled": re_enabled,
        "all_disabled": len(re_enabled) == 0,
    }

    # ---- 13i. Tailscale online status (from VPS perspective) ----
    rc_vps, ts_line, _ = run_cmd(
        f"tailscale status 2>/dev/null | grep 'gl-mt3000'",
        shell=True, timeout=8,
    )
    result["tailscale_online"] = bool(ts_line.strip())
    result["offers_exit_node"] = "offers exit node" in ts_line

    return result
