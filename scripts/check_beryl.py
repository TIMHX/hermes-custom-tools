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
BERYL_IP_FALLBACK = "100.92.132.104"
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

    # ---- 13c2. opkg security classification ----
    # Categorize upgradable packages by risk level and whether the
    # corresponding service is running or disabled.
    _SECURITY_CRITICAL = {
        # Patterns that match security-critical package names
        "libopenssl", "openssl-util", "curl", "libcurl", "ca-bundle", "ca-certificates",
        "libexpat", "libsqlite", "dnsmasq", "bind-libs", "dbus", "libdbus",
        "dropbear", "nginx", "wpad", "hostapd", "firewall",
    }
    _DISABLED_SERVICES = {
        # Service names whose packages are irrelevant (service disabled)
        "tor", "openvpn", "zerotier", "vsftpd", "minidlna", "smstools3",
        "usbmuxd", "adguardhome", "avahi", "dnscrypt-proxy", "samba4",
        "odhcpd", "gl-black_white", "gl-portal", "gl-tertf", "gl_ipv6",
        "kmwan", "mpflow", "mpifd", "mptun", "carrier-monitor", "edgerouter",
        "gl-cloud", "gl_s2s", "gl_dns", "gl_dpi", "netifyd", "gl_eqos",
        "gl_clients", "gl_fan", "gl_cellular", "gl_tethering",
        "gl_nas", "disk_manage", "modem_signal", "sms_manager", "plugins",
        "parental_control", "port_forward", "radius", "repeater", "sip_alg",
        "usbmode", "vpn-client", "webdav", "openssl(init)",
    }
    # Build all upgradable names for the full-list SSH call
    # (opkg list-upgradable returns all, we just use head -10 for compactness)
    _rc_full, _full_out, _ = _ssh_beryl(
        "opkg list-upgradable 2>/dev/null | awk '{print $1}' | sort",
        timeout=15,
    )
    all_upgradable_names = [l.strip() for l in _full_out.split("\n") if l.strip()]

    security_critical: list[dict] = []
    security_upgrades: list[dict] = []
    disabled_svc_pkgs: list[dict] = []
    other_upgrades: list[dict] = []

    for pkg in opkg_upgradable + [
        {"name": n, "old_version": "?", "new_version": "?"}
        for n in all_upgradable_names[len(opkg_upgradable):]
    ]:
        name = pkg["name"]
        # Check security-critical
        is_critical = any(name.startswith(pat) for pat in _SECURITY_CRITICAL)
        # Check disabled-service
        is_disabled_svc = any(
            name.startswith(pat) or pat in name
            for pat in _DISABLED_SERVICES
        )
        entry = {
            "name": name,
            "old_version": pkg.get("old_version", "?"),
            "new_version": pkg.get("new_version", "?"),
        }
        if is_critical:
            security_critical.append(entry)
        elif is_disabled_svc:
            disabled_svc_pkgs.append(entry)
        elif any(name.startswith(p) for p in ("luci", "lua", "lib", "bind-", "zoneinfo", "wireless", "ip-", "tc-", "libudev", "procd", "usb", "unzip", "iperf", "sqlite", "ffmpeg", "nginx", "openssl")):
            # Libraries, LuCI, system tools — may affect running services
            security_upgrades.append(entry)
        else:
            other_upgrades.append(entry)

    result["opkg_security"] = {
        "security_critical_count": len(security_critical),
        "security_critical": security_critical,
        "security_upgrade_count": len(security_upgrades),
        "security_upgrades": security_upgrades[:5],
        "disabled_svc_count": len(disabled_svc_pkgs),
        "disabled_svc": disabled_svc_pkgs[:5],
        "other_count": len(other_upgrades),
    }

    # ---- 13c3. System performance thresholds ----
    perf_warnings: list[str] = []
    # Parse memory percentage
    mem_str = result.get("memory", "")
    mem_match = re.search(r'\(([\d.]+)%\)', mem_str)
    mem_pct = float(mem_match.group(1)) if mem_match else 0.0
    if mem_pct > 80:
        perf_warnings.append(f"memory {mem_pct:.0f}% (threshold 80%)")

    # Parse disk percentage
    disk_str = result.get("disk", "")
    disk_match = re.search(r'\((\d+)%\)', disk_str)
    disk_pct = int(disk_match.group(1)) if disk_match else 0
    if disk_pct > 80:
        perf_warnings.append(f"disk {disk_pct}% (threshold 80%)")

    # Parse load
    load_str = result.get("load", "0,0,0")
    try:
        loads = [float(x) for x in load_str.split(",")]
        if len(loads) >= 3 and loads[2] > 2.0:
            perf_warnings.append(f"load_15min {loads[2]:.1f} (threshold 2.0)")
    except (ValueError, IndexError):
        pass

    # Service count deviation from baseline.
    # Update BERYL_EXPECTED_SERVICE_COUNT when a new service is intentionally added/removed.
    BERYL_EXPECTED_SERVICE_COUNT = 52  # 2026-07-03: 51, 2026-07-12: +etherwake → 52
    svc_count = result.get("enabled_service_count")
    if svc_count is not None and svc_count != BERYL_EXPECTED_SERVICE_COUNT:
        perf_warnings.append(f"service_count {svc_count} (baseline {BERYL_EXPECTED_SERVICE_COUNT})")

    result["performance"] = {
        "memory_pct": mem_pct,
        "disk_pct": disk_pct,
        "warnings": perf_warnings,
        "ok": len(perf_warnings) == 0,
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
    # Monitored services — these were previously disabled and should stay dead.
    # Baseline: 51 services (2026-07-03; 104→51, 53 disabled).
    # Excluded from monitor (allowed to run): carrier-monitor, gl_timer.
    rc_svc, svc_out, _ = _ssh_beryl(
        "for svc in tor vsftpd minidlna zerotier smstools3 usbmuxd adguardhome samba4 openvpn "
        "gl-black_white_list gl-portal gl-tertf gl_ipv6 kmwan mpflow mpifd mptun "
        "edgerouter init_new_provider.sh gl-cloud gl_ddns gl_s2s "
        "gl_dns gl_dpi gl_dpi_flow_statistics netifyd gl_eqos sqm gl_clients gl_fan "
        "gl_cellular_manager gl_tethering gl_nas_diskmanager gl_nas_sys "
        "gl_nas_sys_dl gl_nas_sys_up disk_manage modem_signal sms_manager plugins "
        "parental_control avahi-daemon port_forward radius repeater sip_alg sudo "
        "usbmode vpn-client webdav_ser dnscrypt-proxy dnsproxy openssl; do "
        "/etc/init.d/$svc enabled 2>/dev/null && echo \"ENABLED:$svc\" || echo \"disabled:$svc\"; done",
        timeout=10,
    )
    re_enabled = [l.split(":", 1)[1] for l in svc_out.split("\n") if l.startswith("ENABLED:")]
    result["services"] = {
        "re_enabled": re_enabled,
        "all_disabled": len(re_enabled) == 0,
    }

    # ---- 13h2. OP24 baseline: OpenSSL, kernel, firewall integrity ----
    # Four separate SSH calls to avoid quote-nesting hell in BusyBox ash.
    openssl_ver = ""
    kernel_ver = ""
    openwrt_rel = ""
    ts_lan_fw = 0

    rc1, out1, _ = _ssh_beryl("openssl version 2>/dev/null | awk '{print $2}'", timeout=10)
    if rc1 == 0 and out1:
        openssl_ver = out1.strip()

    rc2, out2, _ = _ssh_beryl("uname -r", timeout=10)
    if rc2 == 0 and out2:
        kernel_ver = out2.strip()

    rc3, out3, _ = _ssh_beryl(
        "grep DISTRIB_RELEASE /etc/openwrt_release 2>/dev/null | grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+'",
        timeout=10,
    )
    if rc3 == 0 and out3:
        openwrt_rel = out3.strip()

    rc4, out4, _ = _ssh_beryl(
        "nft list chain inet fw4 forward_tailscale0 2>/dev/null | grep -c br-lan || echo 0",
        timeout=10,
    )
    if rc4 == 0 and out4:
        try:
            ts_lan_fw = int(out4.strip())
        except ValueError:
            pass

    result["op24_baseline"] = {
        "openssl_version": openssl_ver or "?",
        "kernel_full": kernel_ver or "?",
        "kernel_6x": kernel_ver.startswith("6."),
        "openwrt_release": openwrt_rel or "?",
        "ts_lan_fw_rule": ts_lan_fw > 0,
    }

    # ---- 13i. Tailscale online status (from VPS perspective) ----
    rc_vps, ts_line, _ = run_cmd(
        f"tailscale status 2>/dev/null | grep 'gl-mt3000'",
        shell=True, timeout=8,
    )
    result["tailscale_online"] = bool(ts_line.strip())
    result["offers_exit_node"] = "offers exit node" in ts_line

    # ---- 13j. TS ip-rule watchdog ----
    rc_wd, wd_out, _ = _ssh_beryl(
        "echo 'SCRIPT_EXISTS='$(test -x /root/ts-iprule-watchdog.sh && echo yes || echo no); "
        "echo 'IN_CRONTAB='$(crontab -l 2>/dev/null | grep -c ts-iprule-watchdog || echo 0); "
        "echo 'CRON_RUNNING='$(ps | grep -c '[c]rond' || echo 0); "
        "echo 'IPRULE_PRESENT='$(ip rule show 2>/dev/null | grep -c 'from 100.92.132.104 lookup 52' || echo 0); "
        "echo 'STATUS='$(cat /tmp/ts-watchdog.status 2>/dev/null || echo 'missing'); "
        "echo 'FIXES='$(cat /tmp/ts-watchdog.fixes 2>/dev/null || echo 0); "
        "echo 'LASTLOG='$(tail -3 /tmp/ts-watchdog.log 2>/dev/null || echo '')",
        timeout=10,
    )
    wd = {
        "script_exists": False,
        "in_crontab": False,
        "cron_running": False,
        "iprule_present": False,
        "status_file": None,
        "fixes": 0,
        "last_fix_log": None,
    }
    for line in wd_out.split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            if k == "SCRIPT_EXISTS":
                wd["script_exists"] = v.strip() == "yes"
            elif k == "IN_CRONTAB":
                try:
                    wd["in_crontab"] = int(v) > 0
                except ValueError:
                    pass
            elif k == "CRON_RUNNING":
                try:
                    wd["cron_running"] = int(v) > 0
                except ValueError:
                    pass
            elif k == "IPRULE_PRESENT":
                try:
                    wd["iprule_present"] = int(v) > 0
                except ValueError:
                    pass
            elif k == "STATUS":
                wd["status_file"] = v.strip()
            elif k == "FIXES":
                try:
                    wd["fixes"] = int(v)
                except ValueError:
                    pass
            elif k == "LASTLOG":
                val = v.strip()
                if val:
                    wd["last_fix_log"] = val
    wd["healthy"] = (
        wd["script_exists"] and wd["in_crontab"] and
        wd["cron_running"] and wd["iprule_present"] and
        (wd["status_file"] or "").startswith("ok")
    )
    result["ts_watchdog"] = wd

    return result
