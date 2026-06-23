#!/usr/bin/env python3
"""
统一日报数据采集脚本。
合并：system-health.sh + daily-cve-report.py + daily-maintenance-check.py
输出：结构化 JSON 供 LLM 分析，生成综合日报。

运行时间：< 120s（cron 默认超时）
SSH 超时：所有远程调用均 wrap timeout
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from check_beryl import _resolve_beryl_ip, _ssh_beryl, check_beryl_ax

# ═══════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════

DISTRO = "noble"
DISTRO_NAME = "Ubuntu 24.04"
USN_LOOKBACK_DAYS = 7
KEV_LOOKBACK_DAYS = 14
SKIP_PACKAGE_PATTERNS = ["nvidia-tegra"]

HERMES_REPO = "/home/xing/.hermes/hermes-agent"
HERMES_DOTVENV = f"{HERMES_REPO}/.venv"
OV_HEALTH_URL = "http://127.0.0.1:1933/health"
OV_PYPI_URL = "https://pypi.org/pypi/openviking/json"
SEARXNG_URL = "http://127.0.0.1:8888"
SEARXNG_IMAGE = "searxng/searxng:latest"
CMD_TIMEOUT = 30
GIT_FETCH_TIMEOUT = 60
SOCKET_TOKEN_FILE = "/home/xing/.hermes/.socket-token"
BWS_BIN = "/home/xing/.hermes/bin/bws"

BERYL_HOST = "gl-mt3000"
BERYL_IP_FALLBACK = "100.100.114.49"
BERYL_PORT = 1080
WIN_HOST = "tim-pc"
SSH_TIMEOUT_OPTS = "-o ConnectTimeout=5 -o BatchMode=yes"

KNOWN_MITIGATIONS: dict[str, dict[str, Any]] = {
    "/etc/modprobe.d/disable-algif_aead.conf": {
        "cve": "CVE-2026-31431",
        "name": "Copy Fail",
        "module": "algif_aead",
    },
    "/etc/modprobe.d/dirty-frag.conf": {
        "cve": "CVE-2026-43284 / CVE-2026-43500",
        "name": "Dirty Frag",
        "modules": ["esp4", "esp6", "rxrpc"],
    },
}

# ═══════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════

def _which(cmd: str) -> str | None:
    """Resolve command in PATH via shutil.which. Returns path or None."""
    return shutil.which(cmd)


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


def _parse_version(v: str) -> tuple[int, ...]:
    """Extract numeric version components for comparison."""
    m = re.search(r'(\d+(?:\.\d+)*)', v)
    if not m:
        return (0,)
    parts = re.findall(r'\d+', m.group(1))
    return tuple(int(x) for x in parts) if parts else (0,)

def _should_skip_pkg(pkg_name: str) -> bool:
    return any(pat in pkg_name for pat in SKIP_PACKAGE_PATTERNS)


def _safe_check(name: str, fn, *args) -> dict[str, Any]:
    """Wrap a check function; on exception return error dict instead of crashing."""
    try:
        return fn(*args)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ═══════════════════════════════════════════
# Shared cache (avoid redundant apt calls)
# ═══════════════════════════════════════════

_APT_UPGRADABLE_CACHE: dict[str, Any] | None = None


def _get_apt_upgradable() -> dict[str, Any]:
    """Get apt list --upgradable (cached, run once). NO sudo apt update — read-only."""
    global _APT_UPGRADABLE_CACHE
    if _APT_UPGRADABLE_CACHE is not None:
        return _APT_UPGRADABLE_CACHE

    rc, stdout, stderr = run_cmd(
        "apt list --upgradable 2>/dev/null | grep -v '^Listing\\|^WARNING\\|^$' || true",
        shell=True, timeout=30,
    )
    if rc not in (0, 1):  # apt list exits 1 when there ARE upgradable packages
        _APT_UPGRADABLE_CACHE = {"error": stderr or f"exit {rc}", "lines": []}
        return _APT_UPGRADABLE_CACHE

    lines = [l.strip() for l in stdout.split("\n") if l.strip()]
    _APT_UPGRADABLE_CACHE = {"error": None, "lines": lines}
    return _APT_UPGRADABLE_CACHE


# ═══════════════════════════════════════════
# 1. VPS Infrastructure
# ═══════════════════════════════════════════

def check_vps_services() -> dict[str, Any]:
    """Check core services: caddy, derper, tailscaled, fail2ban."""
    services = {}
    for svc in ["caddy", "derper", "tailscaled", "fail2ban"]:
        rc, stdout, _ = run_cmd(["systemctl", "is-active", svc], timeout=10)
        services[svc] = {"active": rc == 0, "state": stdout if rc == 0 else "missing"}
    return {"ok": all(s["active"] for s in services.values()), "services": services}


def check_derp_health() -> dict[str, Any]:
    """DERP health: HTTPS reachability, bind scope, verify-clients."""
    rc, http_code, _ = run_cmd(
        "curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 https://nn.xing-self-project.xyz/ 2>/dev/null || echo '000'",
        shell=True, timeout=10,
    )
    https_ok = http_code == "200"

    # Bind check
    rc2, bind_out, _ = run_cmd("ss -tlnp 2>/dev/null | grep ':444' || true", shell=True, timeout=5)
    bind_lines = [l for l in bind_out.split("\n") if l.strip()] if bind_out else []
    public_bind = [l for l in bind_lines if "127.0.0.1:444" not in l]
    binds_public = len(public_bind) > 0

    # verify-clients
    rc3, svc_cat, _ = run_cmd(
        ["systemctl", "cat", "derper"], timeout=5,
    )
    has_verify = "verify-clients" in svc_cat

    return {
        "https_ok": https_ok,
        "http_code": http_code,
        "bind_count": len(bind_lines),
        "binds_public": binds_public,
        "verify_clients": has_verify,
    }


def check_tls_cert() -> dict[str, Any]:
    """TLS certificate expiry check."""
    rc, stdout, _ = run_cmd(
        "echo | openssl s_client -servername nn.xing-self-project.xyz "
        "-connect nn.xing-self-project.xyz:443 2>/dev/null | "
        "openssl x509 -noout -dates 2>/dev/null",
        shell=True, timeout=10,
    )
    not_after = None
    days_left = None
    for line in stdout.split("\n"):
        if "notAfter=" in line:
            not_after = line.split("=", 1)[1].strip()
    if not_after:
        try:
            expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
            days_left = (expiry - datetime.now()).days
        except ValueError:
            pass
    return {
        "ok": not_after is not None,
        "not_after": not_after,
        "days_left": days_left,
        "warning": days_left is not None and days_left < 14,
    }


def check_caddy_headers() -> dict[str, Any]:
    """Caddy response headers: via hiding, HSTS."""
    rc, headers, _ = run_cmd(
        "curl -skI --connect-timeout 5 https://nn.xing-self-project.xyz/ 2>/dev/null",
        shell=True, timeout=10,
    )
    via_hidden = "via:" not in headers.lower()
    hsts = "strict-transport-security" in headers.lower()
    return {"via_hidden": via_hidden, "hsts": hsts}


def check_firewall() -> dict[str, Any]:
    """UFW firewall status and rules."""
    # ufw hold check
    rc, hold_out, _ = run_cmd("apt-mark showhold 2>/dev/null | grep ufw || true", shell=True, timeout=5)
    ufw_held = "ufw" in hold_out

    # ufw status
    rc2, ufw_status, _ = run_cmd(["sudo", "/usr/sbin/ufw", "status"], timeout=10)
    status_line = ufw_status.split("\n")[0] if ufw_status else "unknown"

    # port checks
    ports_ok = {}
    for port_spec in ["80/tcp", "443/tcp", "3478/udp"]:
        ports_ok[port_spec] = f"{port_spec}.*ALLOW" in ufw_status

    # rate limiting
    rc3, ipt_out, _ = run_cmd("sudo iptables -L INPUT -n 2>/dev/null | grep HTTPS_RATE || true",
                              shell=True, timeout=5)
    rate_limited = "HTTPS_RATE" in ipt_out

    return {
        "ufw_held": ufw_held,
        "status": status_line,
        "ports": ports_ok,
        "rate_limit_443": rate_limited,
    }


def check_ssh_security() -> dict[str, Any]:
    """SSH server configuration audit."""
    cfg: dict[str, str] = {}
    keys = ["PasswordAuthentication", "PubkeyAuthentication", "PermitRootLogin",
            "GatewayPorts", "X11Forwarding"]
    for key in keys:
        rc, val, _ = run_cmd(
            f"sudo grep '^{key} ' /etc/ssh/sshd_config 2>/dev/null | awk '{{print $NF}}' || echo '?'",
            shell=True, timeout=5,
        )
        cfg[key] = val.strip() if val else "?"

    issues = []
    if cfg.get("PasswordAuthentication") != "no":
        issues.append("PasswordAuthentication enabled")
    if cfg.get("PubkeyAuthentication") != "yes":
        issues.append("PubkeyAuthentication disabled")
    if cfg.get("PermitRootLogin") not in ("no", "prohibit-password"):
        issues.append("PermitRootLogin insecure")
    if cfg.get("GatewayPorts") != "no":
        issues.append("GatewayPorts enabled")
    if cfg.get("X11Forwarding") != "no":
        issues.append("X11Forwarding enabled")

    return {"config": cfg, "issues": issues, "secure": len(issues) == 0}


def check_fail2ban() -> dict[str, Any]:
    """fail2ban jail status."""
    rc, stdout, _ = run_cmd(["sudo", "fail2ban-client", "status", "sshd"], timeout=10)
    ok = "Status for" in stdout
    banned = 0
    if ok:
        m = re.search(r"Total banned:\s*(\d+)", stdout)
        if m:
            banned = int(m.group(1))
    return {"ok": ok, "banned_ips": banned}


def check_system_resources() -> dict[str, Any]:
    """CPU, memory, disk, load, zombies, file descriptors."""
    # CPU
    rc, cpu, _ = run_cmd(
        "top -bn1 | grep 'Cpu(s)' | awk '{print $2+$4\"%\"}'", shell=True, timeout=10,
    )
    # Memory
    rc2, mem, _ = run_cmd("free -h | awk '/^Mem:/ {print $3\"/\"$2}'", shell=True, timeout=5)
    # Disk
    rc3, disk, _ = run_cmd("df -h / | awk 'NR==2 {print $3\"/\"$2 \" (\"$5\")\"}'", shell=True, timeout=5)
    # Inode
    rc4, inode, _ = run_cmd("df -i / | awk 'NR==2 {print $3\"/\"$2 \" (\"$5\")\"}'", shell=True, timeout=5)
    # Load
    rc5, load, _ = run_cmd("uptime | awk -F'load average:' '{print $2}'", shell=True, timeout=5)
    # Zombies
    rc6, zombies_str, _ = run_cmd(
        "ps aux | awk '{if($8 ~ /^Z/) count++} END{print count+0}'", shell=True, timeout=5,
    )
    zombies = int(zombies_str.strip()) if zombies_str.strip().isdigit() else 0
    # File descriptors
    fd_count = fd_max = fd_pct = None
    rc7, fd_out, _ = run_cmd("cat /proc/sys/fs/file-nr 2>/dev/null", shell=True, timeout=5)
    if fd_out:
        parts = fd_out.split()
        if len(parts) >= 3:
            try:
                fd_count = int(parts[0])
                fd_max = int(parts[2])
                fd_pct = round(fd_count * 100 / fd_max, 1) if fd_max else None
            except ValueError:
                pass

    # Disk usage percentage
    disk_pct = None
    m = re.search(r'(\d+)%', disk) if disk else None
    if m:
        disk_pct = int(m.group(1))

    return {
        "cpu": cpu.strip(),
        "memory": mem.strip(),
        "disk": disk.strip(),
        "disk_pct": disk_pct,
        "inode": inode.strip(),
        "load": load.strip(),
        "zombies": zombies,
        "fd_count": fd_count,
        "fd_max": fd_max,
        "fd_pct": fd_pct,
    }


def check_network() -> dict[str, Any]:
    """Public IP, DNS resolution, Tailscale status."""
    rc, pub_ip, _ = run_cmd("curl -s --connect-timeout 3 ifconfig.me 2>/dev/null || echo 'timeout'",
                            shell=True, timeout=8)
    rc2, dns, _ = run_cmd("dig +short nn.xing-self-project.xyz A @8.8.8.8 2>/dev/null || echo 'failed'",
                          shell=True, timeout=8)
    rc3, ts_status, _ = run_cmd("tailscale status 2>/dev/null | head -1 || echo 'unavailable'",
                               shell=True, timeout=8)
    return {
        "public_ip": pub_ip.strip(),
        "dns_ok": "failed" not in dns and bool(dns.strip()),
        "dns_result": dns.strip(),
        "tailscale": ts_status.strip(),
    }


def check_system_health() -> dict[str, Any]:
    """Reboot required, failed units, Docker, /var/log size."""
    rc, _, _ = run_cmd("test -f /var/run/reboot-required", shell=True, timeout=5)
    needs_reboot = rc == 0

    rc2, failed_out, _ = run_cmd(
        "systemctl --failed --no-pager 2>/dev/null | grep 'loaded units listed' | grep -oE '[0-9]+' || echo '0'",
        shell=True, timeout=5,
    )
    failed_units = int(failed_out.strip()) if failed_out.strip().isdigit() else 0

    docker_containers = 0
    rc3, _, _ = run_cmd("docker info &>/dev/null", shell=True, timeout=8)
    if rc3 == 0:
        rc4, dc_out, _ = run_cmd("docker ps -q 2>/dev/null | wc -l", shell=True, timeout=5)
        docker_containers = int(dc_out.strip()) if dc_out.strip().isdigit() else 0

    rc5, log_size, _ = run_cmd("du -sh /var/log 2>/dev/null | awk '{print $1}'", shell=True, timeout=8)

    return {
        "needs_reboot": needs_reboot,
        "failed_units": failed_units,
        "docker_containers": docker_containers,
        "var_log_size": log_size.strip(),
    }


def check_apt_updates() -> dict[str, Any]:
    """APT upgradable packages (read-only, no sudo apt update)."""
    apt_data = _get_apt_upgradable()
    if apt_data.get("error"):
        return {"error": apt_data["error"], "total": 0, "security_count": 0, "security": [], "all": []}

    all_pkgs = []
    security_pkgs = []
    for line in apt_data["lines"]:
        parts = line.split()
        if not parts:
            continue
        pkg_full = parts[0]
        pkg_name = pkg_full.split("/")[0]
        repo_info = pkg_full.split("/", 1)[1] if "/" in pkg_full else ""

        if _should_skip_pkg(pkg_name):
            continue
        if "dbgsym" in pkg_name or "unsigned" in pkg_name:
            continue

        is_security = "security" in repo_info.lower()
        entry = {"package": pkg_name, "repo": repo_info}
        all_pkgs.append(entry)
        if is_security:
            security_pkgs.append(entry)

    return {
        "total": len(all_pkgs),
        "security_count": len(security_pkgs),
        "security": security_pkgs,
        "all": all_pkgs,
    }


def check_bbr() -> dict[str, Any]:
    """BBR congestion control check."""
    rc, stdout, _ = run_cmd("sysctl net.ipv4.tcp_congestion_control 2>/dev/null", shell=True, timeout=5)
    return {"output": stdout.strip()}


def check_vps_security_baseline() -> dict[str, Any]:
    """VPS security baseline: Caddy, OpenSSH, Tailscale versions + exposed ports."""
    # Caddy version
    rc, caddy_out, _ = run_cmd("caddy version 2>/dev/null | head -1 | awk '{print $1}' | sed 's/v//'",
                              shell=True, timeout=5)
    caddy_ver = caddy_out.strip()

    # Instead of hard-coding version thresholds per-CVE, check if an apt update is
    # available. Caddy is installed from Cloudsmith repo — any new release (with CVE
    # patches) shows up as an apt update automatically. No manual threshold bumps needed.
    caddy_update_available = False
    if caddy_ver:
        rc_pol, pol_out, _ = run_cmd(
            "apt-cache policy caddy 2>/dev/null",
            shell=True, timeout=5)
        # Parse "Candidate: X.Y.Z" from apt-cache policy output
        candidate_match = re.search(r'Candidate:\s*(\S+)', pol_out)
        if candidate_match:
            caddy_update_available = caddy_ver != candidate_match.group(1)

    # Caddy config CVEs — check for directives associated with known CVEs.
    # These remain manual because they detect *usage* of vulnerable features,
    # not just version. Keep the list updated when new Caddy CVEs involve config.
    caddyfile = "/etc/caddy/Caddyfile"
    caddy_cves = {}
    if os.path.isfile(caddyfile):
        with open(caddyfile) as f:
            cf_content = f.read()
        caddy_cves["vars_regexp"] = "vars_regexp" in cf_content
        caddy_cves["fastcgi"] = bool(re.search(r'fastcgi|php_fastcgi', cf_content, re.I))
        caddy_cves["forward_auth"] = bool(re.search(r'forward_auth', cf_content, re.I))

    # cve_safe: True only if latest version is installed AND no dangerous directives are used
    caddy_cve_safe = (not caddy_update_available) and (not any(caddy_cves.values()))

    # OpenSSH
    rc2, ssh_out, _ = run_cmd("ssh -V 2>&1 | head -1 | sed -n 's/.*OpenSSH_\\([0-9.p]*\\).*/\\1/p'",
                             shell=True, timeout=5)
    openssh_ver = ssh_out.strip()

    rc3, dpkg_out, _ = run_cmd(
        "dpkg -l openssh-server 2>/dev/null | grep -q '9.6p1-3ubuntu13' && echo 'patched' || echo 'unknown'",
        shell=True, timeout=5,
    )
    regreSSHion_patched = "patched" in dpkg_out

    # Tailscale
    rc4, ts_ver, _ = run_cmd("tailscale version 2>/dev/null | head -1", shell=True, timeout=5)

    # Exposed ports
    rc5, ports_out, _ = run_cmd(
        "sudo ss -tlnp 2>/dev/null | grep -vE '127\\.0\\.0\\.1|::1|tailscale0|100\\.' | grep LISTEN || true",
        shell=True, timeout=8,
    )
    exposed_ports = [l.strip() for l in ports_out.split("\n") if l.strip()] if ports_out else []

    # Hermes port check
    rc6, hermes_port, _ = run_cmd(
        "sudo ss -tlnp 2>/dev/null | grep 8644 | grep -q '0.0.0.0' && echo 'public' || echo 'ok'",
        shell=True, timeout=5,
    )
    hermes_binds_public = "public" in hermes_port

    return {
        "caddy": {
            "version": caddy_ver,
            "cve_safe": caddy_cve_safe,
            "cve_checks": caddy_cves,
        },
        "openssh": {
            "version": openssh_ver,
            "regreSSHion_patched": regreSSHion_patched,
        },
        "tailscale": ts_ver.strip(),
        "exposed_ports": exposed_ports,
        "hermes_binds_public": hermes_binds_public,
    }


def check_searxng_engines() -> dict[str, Any]:
    """SearXNG engine blocked/unresponsive stats (via curl + Python).

    Uses /config when /search?format=json is blocked (public_instance=false).
    """
    # Try search API first (needs public_instance=true or Referer bypass)
    rc, result_json, _ = run_cmd(
        f"curl -s -w '\\n%{{http_code}}' --max-time 25 "
        f"'{SEARXNG_URL}/search?q=test&format=json&limit=1' 2>/dev/null",
        shell=True, timeout=30,
    )
    if not result_json:
        return {"ok": False, "error": "SearXNG API no response", "blocked": [], "blocked_count": 0}

    # Split body from trailing HTTP status code
    lines = result_json.rsplit("\n", 1)
    body = lines[0]
    http_code = lines[1].strip() if len(lines) > 1 else ""

    if http_code == "403":
        return {"ok": False, "error": "API blocked (public_instance=false, returns 403)",
                "blocked": [], "blocked_count": 0}

    try:
        data = json.loads(body)
        unresponsive = data.get("unresponsive_engines", [])
    except json.JSONDecodeError:
        return {"ok": False, "error": f"JSON parse error (HTTP {http_code})", "blocked": [], "blocked_count": 0}

    blocked_kw = ["captcha", "suspended", "too many", "blocked", "rate limit"]
    blocked = []
    other = []
    for item in unresponsive:
        if isinstance(item, list) and len(item) >= 2:
            engine, reason = item[0], str(item[1])
        elif isinstance(item, str):
            engine, reason = item, "unknown"
        else:
            continue
        if any(kw in reason.lower() for kw in blocked_kw):
            blocked.append({"engine": engine, "reason": reason})
        else:
            other.append({"engine": engine, "reason": reason})

    return {
        "ok": len(blocked) == 0,
        "blocked_count": len(blocked),
        "blocked": blocked,
        "other_unresponsive": other,
        "total_unresponsive": len(unresponsive),
    }


def check_beryl_ip_mismatch() -> dict[str, Any]:
    """Cross-validate Beryl AX IP: SearXNG proxy config vs actual Tailscale IP.

    SearXNG uses a SOCKS5 proxy through Beryl AX. If the Beryl AX Tailscale
    IP changes (e.g. after a tailnet rekey or node rejoin), the hardcoded IP
    in /etc/searxng/settings.yml becomes stale and SearXNG proxy breaks.
    This check catches that mismatch before it causes silent failures.
    """
    # 1. Get actual Beryl AX IP from tailscale status (most accurate)
    rc, ts_line, _ = run_cmd(
        "tailscale status 2>/dev/null | grep 'gl-mt3000' | awk '{print $1}' || true",
        shell=True, timeout=8,
    )
    actual_ip = ts_line.strip() if ts_line.strip() else _resolve_beryl_ip()

    # 2. Extract SearXNG-configured Beryl IP from settings.yml
    # Matches lines like: socks5h://socksproxy:PASSWORD@100.100.114.49:1080
    searxng_configured_ip = None
    rc2, settings_out, _ = run_cmd(
        "sudo grep -oP 'socks5h://[^@]+@\\K[\\d.]+' /etc/searxng/settings.yml 2>/dev/null || true",
        shell=True, timeout=5,
    )
    if settings_out.strip():
        searxng_configured_ip = settings_out.strip()

    # 3. Compare and build result
    mismatch = bool(
        actual_ip and searxng_configured_ip and actual_ip != searxng_configured_ip
    )

    fix_hint = ""
    if mismatch:
        fix_hint = (
            f"SearXNG proxy IP ({searxng_configured_ip}) differs from actual "
            f"Beryl AX Tailscale IP ({actual_ip}). Update the proxies section in "
            f"/etc/searxng/settings.yml to use socks5h://...@{actual_ip}:1080, "
            f"then run: sudo docker restart searxng"
        )

    return {
        "actual_ip": actual_ip,
        "searxng_configured_ip": searxng_configured_ip,
        "mismatch": mismatch,
        "fix_hint": fix_hint,
    }


# ═══════════════════════════════════════════
# 3. Windows
# ═══════════════════════════════════════════

_WIN_SSH_OPTS = f"-o ConnectTimeout=5 -o BatchMode=yes"


def check_windows() -> dict[str, Any]:
    """Windows SSH security + Exit Node status.

    When the host is unreachable (SSH fails entirely), the SSH audit fields
    are set to None to indicate "unverifiable" — not "insecure". The LLM
    prompt should check `reachable` before interpreting ssh audit data.
    """
    # SSH audit
    rc_win, win_out, _ = run_cmd(
        f"timeout 15 ssh {_WIN_SSH_OPTS} 'Xing Hong@{WIN_HOST}' "
        "\"sc query sshd 2>nul & "
        "findstr /i PasswordAuthentication C:\\\\\\\\ProgramData\\\\\\\\ssh\\\\\\\\sshd_config & "
        "findstr /i PubkeyAuthentication C:\\\\\\\\ProgramData\\\\\\\\ssh\\\\\\\\sshd_config & "
        "findstr /i KbdInteractive C:\\\\\\\\ProgramData\\\\\\\\ssh\\\\\\\\sshd_config\" 2>&1",
        shell=True, timeout=20,
    )

    # Detect whether SSH actually connected (not timeout/connection-refused)
    ssh_connected = rc_win == 0

    sshd_running = "RUNNING" in win_out
    pass_auth_secure = "PasswordAuthentication no" in win_out
    pubkey_ok = "PubkeyAuthentication yes" in win_out
    kbd_secure = "KbdInteractiveAuthentication no" in win_out

    # Key count (only meaningful if SSH connected)
    if ssh_connected:
        rc_keys, key_out, _ = run_cmd(
            f"timeout 10 ssh {_WIN_SSH_OPTS} 'Xing Hong@{WIN_HOST}' "
            "\"type C:\\\\\\\\ProgramData\\\\\\\\ssh\\\\\\\\administrators_authorized_keys\" 2>/dev/null | wc -l",
            shell=True, timeout=15,
        )
        try:
            key_count = int(key_out.strip()) if key_out.strip().isdigit() else 0
        except ValueError:
            key_count = 0
    else:
        key_count = None

    # Exit node status
    rc_ts, ts_line, _ = run_cmd(
        f"tailscale status 2>/dev/null | grep -w 'tim-pc'",
        shell=True, timeout=8,
    )
    offers_exit = "offers exit node" in ts_line

    reachable = ssh_connected  # actual SSH connectivity, not derived

    # When unreachable, SSH audit values are unverifiable (None), not "insecure".
    # The LLM prompt must check reachable before interpreting.
    return {
        "reachable": reachable,
        "sshd_running": sshd_running if reachable else None,
        "ssh": {
            "password_auth_secure": pass_auth_secure if reachable else None,
            "pubkey_ok": pubkey_ok if reachable else None,
            "kbd_interactive_secure": kbd_secure if reachable else None,
            "ssh_key_count": key_count if reachable else None,
        },
        "offers_exit_node": offers_exit,
    }


# ═══════════════════════════════════════════
# 4. Tailscale / VPN Infrastructure
# ═══════════════════════════════════════════

def _get_ts_status() -> str:
    """Get Tailscale status output (cached in main)."""
    rc, ts_status, _ = run_cmd("tailscale status 2>/dev/null || true", shell=True, timeout=8)
    return ts_status


def _get_ts_status_json() -> dict[str, Any]:
    """Get Tailscale status as JSON."""
    rc, ts_json, _ = run_cmd("tailscale status --json 2>/dev/null || echo '{}'", shell=True, timeout=8)
    try:
        return json.loads(ts_json)
    except json.JSONDecodeError:
        return {}


def check_tailscale_exit(ts_status: str) -> dict[str, Any]:
    """Tailscale exit node management."""
    result: dict[str, Any] = {}

    # Advertised exit nodes
    exit_lines = [l for l in ts_status.split("\n") if "offers exit node" in l]
    result["advertised_exit_nodes"] = len(exit_lines)

    # Current VPS exit node
    ts_json = _get_ts_status_json()
    self_data = ts_json.get("Self", {})
    exit_node_id = self_data.get("ExitNodeID", "")
    result["using_exit_node"] = bool(exit_node_id)

    if exit_node_id:
        # Find hostname
        for peer_id, peer in ts_json.get("Peer", {}).items():
            if peer.get("ID") == exit_node_id:
                result["exit_node_hostname"] = peer.get("HostName", peer_id[:8])
                break
        # Verify connectivity
        rc, http_code, _ = run_cmd(
            "curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 https://order.chownow.com/ 2>/dev/null || echo '000'",
            shell=True, timeout=10,
        )
        result["exit_verification"] = {"url": "order.chownow.com", "http_code": http_code, "ok": http_code == "200"}
        result["current_exit_ip"] = run_cmd("curl -s --max-time 5 https://ifconfig.me 2>/dev/null || echo 'unknown'",
                                           shell=True, timeout=8)[1].strip()
    else:
        result["exit_node_hostname"] = None
        result["current_exit_ip"] = run_cmd("curl -s --max-time 5 https://ifconfig.me 2>/dev/null || echo 'unknown'",
                                           shell=True, timeout=8)[1].strip()

    return result


def check_acl_isolation(ts_status: str) -> dict[str, Any]:
    """ACL isolation: Beryl AX → VPS, Beryl AX → mother."""
    vps_tailscale_ip = ""
    mother_tailscale_ip = ""
    for line in ts_status.split("\n"):
        if "racknerd-c3b1d83" in line:
            vps_tailscale_ip = line.split()[0] if line.split() else ""
        elif "mother" in line:
            mother_tailscale_ip = line.split()[0] if line.split() else ""

    # Beryl → VPS (should be blocked)
    rc1, beryl2vps, _ = _ssh_beryl(
        f"timeout 5 ssh -o ConnectTimeout=3 -o BatchMode=yes root@{vps_tailscale_ip or '100.82.77.91'} 'echo OK' 2>&1 || true",
        timeout=15,
    )
    beryl_to_vps_blocked = "OK" not in beryl2vps

    # Beryl → mother (should be blocked)
    mother_ip = mother_tailscale_ip or "100.70.105.128"
    mother_cmd = f"timeout 5 ssh -o ConnectTimeout=3 -o BatchMode=yes 'Xing Hong@{mother_ip}' 'echo OK' 2>&1 || true"
    rc2, beryl2mother, _ = _ssh_beryl(mother_cmd, timeout=15)
    beryl_to_mother_blocked = "OK" not in beryl2mother

    return {
        "beryl_to_vps_blocked": beryl_to_vps_blocked,
        "beryl_to_mother_blocked": beryl_to_mother_blocked,
        "acl_effective": beryl_to_vps_blocked and beryl_to_mother_blocked,
    }


def check_cross_node_ssh() -> dict[str, Any]:
    """Cross-node SSH: VPS → Beryl AX, VPS → Windows."""
    rc1, beryl_out, _ = run_cmd(
        f"timeout 10 ssh {SSH_TIMEOUT_OPTS} root@{BERYL_HOST} 'echo OK' 2>/dev/null | grep -q OK && echo 'ok' || echo 'fail'",
        shell=True, timeout=15,
    )
    vps_to_beryl = "ok" in beryl_out

    rc2, win_out, _ = run_cmd(
        f"timeout 10 ssh {_WIN_SSH_OPTS} 'Xing Hong@{WIN_HOST}' 'echo OK' 2>/dev/null | grep -q OK && echo 'ok' || echo 'fail'",
        shell=True, timeout=15,
    )
    vps_to_windows = "ok" in win_out

    return {"vps_to_beryl": vps_to_beryl, "vps_to_windows": vps_to_windows}


def check_mother_device(ts_status: str) -> dict[str, Any]:
    """Mother device (tag:mother) online status."""
    mother_lines = [l for l in ts_status.split("\n") if "mother" in l.lower()]
    if not mother_lines:
        return {"online": False, "in_tailnet": False, "ip": None, "status": "not_in_tailnet"}

    line = mother_lines[0]
    online = "active" in line or "idle" in line
    ip = line.split()[0] if line.split() else None
    status = "online" if online else "offline"

    last_seen = None
    m = re.search(r'last seen ([^\s,]+)', line)
    if m:
        last_seen = m.group(1)

    return {"online": online, "in_tailnet": True, "ip": ip, "status": status, "last_seen": last_seen}


def check_derp_chain() -> dict[str, Any]:
    """DERP chain health from Beryl AX perspective."""
    rc, http_code, _ = _ssh_beryl(
        "curl -so /dev/null -w '%{http_code}' --max-time 5 https://nn.xing-self-project.xyz:443/ 2>&1 || echo '000'",
        timeout=15,
    )
    derp_reachable = http_code.strip() == "200"

    rc2, latency_out, _ = _ssh_beryl(
        "tailscale netcheck 2>&1 | grep 'myderp' | grep -oE '[0-9]+\\.?[0-9]*ms' || echo 'N/A'",
        timeout=20,
    )
    derp_latency = latency_out.strip() if latency_out.strip() else "N/A"

    return {"derp_reachable": derp_reachable, "derp_latency": derp_latency}


def check_exit_node(ts_status: str) -> dict[str, Any]:
    """Exit Node functional verification."""
    exit_count = ts_status.count("offers exit node")
    exit_nodes = []
    for line in ts_status.split("\n"):
        if "offers exit node" in line:
            parts = line.split()
            hostname = parts[1] if len(parts) > 1 else parts[0]
            ip = parts[0]
            exit_nodes.append({"hostname": hostname, "ip": ip})

    beryl_exit = any("gl-mt3000" in line and "offers exit node" in line for line in ts_status.split("\n"))

    return {
        "available_count": exit_count,
        "exit_nodes": exit_nodes,
        "beryl_advertising": beryl_exit,
    }


def check_vpn_summary(derp_ok: bool, beryl_exit: bool, mother_online: bool) -> dict[str, Any]:
    """VPN chain summary."""
    chain_ok = derp_ok and beryl_exit
    mother_chain_ok = mother_online
    issues = []
    if not derp_ok:
        issues.append("DERP unreachable from Beryl AX")
    if not beryl_exit:
        issues.append("Beryl AX not advertising exit node")
    if not mother_online:
        issues.append("Mother device offline")

    return {
        "vpn_chain_ok": chain_ok and mother_chain_ok,
        "derp_ok": derp_ok,
        "beryl_exit_ok": beryl_exit,
        "mother_chain_ok": mother_chain_ok,
        "issues": issues,
    }


# ═══════════════════════════════════════════
# 5. Security
# ═══════════════════════════════════════════

def fetch_usn() -> list[dict[str, Any]]:
    """Fetch Ubuntu Security Notices from last 7 days."""
    import requests as req
    notices: list[dict[str, Any]] = []
    offset = 0
    limit = 20
    cutoff = datetime.now(timezone.utc) - timedelta(days=USN_LOOKBACK_DAYS)

    while True:
        url = f"https://ubuntu.com/security/notices.json?limit={limit}&offset={offset}"
        try:
            r = req.get(url, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"USN_API_ERROR (offset={offset}): {e}", file=sys.stderr)
            break

        batch = data.get("notices", [])
        if not batch:
            break

        for n in batch:
            pub_str = n.get("published", "")
            try:
                pub_date = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                if pub_date.tzinfo is None:
                    pub_date = pub_date.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            if pub_date < cutoff:
                return notices

            if DISTRO in n.get("release_packages", {}):
                pkgs = n["release_packages"].get(DISTRO)
                if not pkgs or not isinstance(pkgs, list):
                    continue
                visible = [
                    p["name"] for p in pkgs
                    if not p.get("is_source") and p.get("is_visible", True) and not _should_skip_pkg(p["name"])
                ]
                if not visible:
                    continue
                notices.append({
                    "id": n.get("id", ""),
                    "title": n.get("title", "")[:200],
                    "published": pub_str[:10],
                    "summary": n.get("summary", "")[:500],
                    "packages": visible,
                    "url": f"https://ubuntu.com/security/{n.get('id', '')}",
                })

        offset += limit
        if offset >= data.get("total_results", 0):
            break

    return notices


def _get_installed_packages() -> dict[str, str]:
    """Get all installed packages: {pkg_name: version}."""
    rc, stdout, _ = run_cmd(
        ["dpkg-query", "-W", "-f=${Package}\\t${Version}\\n"],
        timeout=60,
    )
    if rc != 0:
        return {}
    installed: dict[str, str] = {}
    for line in stdout.split("\n"):
        if "\t" in line:
            name, version = line.split("\t", 1)
            installed[name] = version
    return installed


def _check_apt_policy(pkg_name: str) -> str:
    """Return: 'up_to_date', 'update_available', 'not_installed', 'unknown'."""
    rc, stdout, _ = run_cmd(["apt-cache", "policy", pkg_name], timeout=30)
    if rc != 0:
        return "unknown"

    installed = None
    candidate = None
    for line in stdout.split("\n"):
        line = line.strip()
        if line.startswith("Installed:"):
            installed = line.split(":", 1)[1].strip()
            if installed == "(none)":
                installed = None
        elif line.startswith("Candidate:"):
            candidate = line.split(":", 1)[1].strip()

    if installed is None:
        return "not_installed"
    if installed and candidate and candidate != "(none)":
        rc_cmp = run_cmd(["dpkg", "--compare-versions", installed, "ge", candidate], timeout=5)[0]
        return "up_to_date" if rc_cmp == 0 else "update_available"
    return "unknown"


def classify_usn(notices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pre-compute status field for each USN based on actual system state."""
    installed_pkgs = _get_installed_packages()
    if not installed_pkgs:
        for notice in notices:
            notice["status"] = "unknown"
        return notices

    for notice in notices:
        statuses: list[str] = []
        for pkg in notice.get("packages", []):
            if pkg not in installed_pkgs:
                statuses.append("not_installed")
                continue
            policy = _check_apt_policy(pkg)
            status_map = {
                "up_to_date": "patched",
                "update_available": "needs_update",
                "not_installed": "not_installed",
            }
            statuses.append(status_map.get(policy, "unknown"))

        if not statuses:
            notice["status"] = "unknown"
        elif "needs_update" in statuses:
            notice["status"] = "needs_update"
        elif "unknown" in statuses:
            notice["status"] = "unknown"
        elif "patched" in statuses:
            notice["status"] = "patched"
        else:
            notice["status"] = "not_installed"

    return notices


def fetch_kev() -> list[dict[str, str]]:
    """Fetch CISA KEV catalog (14-day lookback, Linux-filtered)."""
    import requests as req
    try:
        r = req.get(
            "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"KEV_API_ERROR: {e}", file=sys.stderr)
        return []

    vulns = r.json().get("vulnerabilities", [])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEV_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    LINUX_KEYWORDS = [
        "linux", "ubuntu", "debian", "kernel", "apache", "nginx",
        "openssl", "drupal", "wordpress", "php", "python", "node",
        "redis", "postgres", "mysql", "kubernetes", "docker",
        "git", "curl", "openssh", "dns", "npm", "rust", "golang",
    ]

    recent: list[dict[str, str]] = []
    for v in vulns:
        if v.get("dateAdded", "") >= cutoff:
            product = (v.get("product", "") + " " + v.get("vendorProject", "")).lower()
            desc = v.get("shortDescription", "").lower()
            combined = product + " " + desc
            if not any(kw in combined for kw in LINUX_KEYWORDS):
                continue
            recent.append({
                "cve": v.get("cveID", ""),
                "product": v.get("product", "N/A"),
                "vendor": v.get("vendorProject", "N/A"),
                "description": v.get("shortDescription", "")[:300],
                "date_added": v.get("dateAdded", ""),
            })

    return sorted(recent, key=lambda x: x["date_added"], reverse=True)


def check_security_upgrades() -> dict[str, Any]:
    """Security-related APT upgrades."""
    apt_data = _get_apt_upgradable()
    if apt_data.get("error"):
        return {"error": apt_data["error"], "total": 0, "security": [], "all": []}

    all_pkgs = []
    security_pkgs = []
    for line in apt_data["lines"]:
        parts = line.split()
        if not parts:
            continue
        pkg_full = parts[0]
        pkg_name = pkg_full.split("/")[0]
        repo_info = pkg_full.split("/", 1)[1] if "/" in pkg_full else ""

        if _should_skip_pkg(pkg_name):
            continue
        if "dbgsym" in pkg_name or "unsigned" in pkg_name:
            continue

        is_security = "security" in repo_info.lower()
        entry = {"package": pkg_name, "repo": repo_info}
        all_pkgs.append(entry)
        if is_security:
            security_pkgs.append(entry)

    return {
        "total": len(all_pkgs),
        "security_count": len(security_pkgs),
        "security": security_pkgs,
        "all": all_pkgs,
    }


def check_kernel_updates() -> dict[str, Any]:
    """Check for available kernel updates."""
    apt_data = _get_apt_upgradable()
    if apt_data.get("error"):
        return {"error": apt_data["error"], "updates": []}

    current_kernel = run_cmd(["uname", "-r"], timeout=5)[1].strip()

    updates: list[str] = []
    for line in apt_data["lines"]:
        if "linux-image" in line:
            parts = line.split()
            if len(parts) >= 2:
                pkg_name = parts[0].split("/")[0]
                if "dbgsym" not in pkg_name and "unsigned" not in pkg_name and not _should_skip_pkg(pkg_name):
                    updates.append(pkg_name)

    needs_reboot = False
    if updates and "-" in current_kernel:
        flavor = current_kernel.rsplit("-", 1)[-1]
        needs_reboot = any(pkg.endswith("-" + flavor) for pkg in updates)
    elif updates:
        needs_reboot = True

    return {
        "current_kernel": current_kernel,
        "available_updates": updates,
        "needs_reboot": needs_reboot,
    }


def check_mitigations() -> dict[str, dict[str, Any]]:
    """Check known security mitigation configs and module loading."""
    results: dict[str, dict[str, Any]] = {}
    for conf_path, info in KNOWN_MITIGATIONS.items():
        exists = os.path.isfile(conf_path)
        modules_loaded: list[str] = []
        lsmod_ok = False
        rc, lsmod_out, _ = run_cmd(["lsmod"], timeout=5)
        if rc == 0:
            lsmod_ok = True
            targets = info.get("modules", [info.get("module", "")])
            for mod in targets:
                if mod and re.search(rf'^{re.escape(mod)}\s', lsmod_out, re.MULTILINE):
                    modules_loaded.append(mod)

        results[conf_path] = {
            "exists": exists,
            "cve": info["cve"],
            "name": info["name"],
            "modules_loaded": modules_loaded,
            "safe": (exists and not modules_loaded) if lsmod_ok else None,
        }
    return results


# ═══════════════════════════════════════════
# 6. Applications
# ═══════════════════════════════════════════

def check_openviking_health() -> dict[str, Any]:
    """OpenViking health check via HTTP."""
    rc, stdout, stderr = run_cmd(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10", OV_HEALTH_URL],
        timeout=15,
    )
    ok = rc == 0 and stdout == "200"
    return {"ok": ok, "http_code": stdout if rc == 0 else None, "error": stderr if not ok else None}


def check_openviking_logs() -> dict[str, Any]:
    """OpenViking logs - WARNING/ERROR from last 200 lines."""
    log_file = "/home/xing/.openviking/data/log/openviking.log"
    if not os.path.isfile(log_file):
        return {"source": log_file, "warnings_errors": [], "count": 0, "file_found": False}

    rc, stdout, _ = run_cmd(
        f"tail -200 {log_file} 2>/dev/null | grep -iE 'WARNING|ERROR' || true",
        shell=True, timeout=10,
    )
    lines = [l for l in stdout.split("\n") if l.strip()] if stdout else []
    return {"source": f"{log_file} (last 200 lines)", "warnings_errors": lines, "count": len(lines), "file_found": True}


def check_openviking_version() -> dict[str, Any]:
    """OpenViking version — compare installed vs PyPI latest."""
    # Installed version
    rc, installed, _ = run_cmd(
        [f"{HERMES_DOTVENV}/bin/python3", "-c",
         "import openviking; print(getattr(openviking, '__version__', 'UNKNOWN'))"],
        timeout=CMD_TIMEOUT,
    )
    installed = installed.strip() if rc == 0 else None
    if installed and installed.upper() == "UNKNOWN":
        installed = None

    # Latest from PyPI
    rc2, pypi_out, _ = run_cmd(
        ["curl", "-s", "--max-time", "10", OV_PYPI_URL],
        timeout=15,
    )
    latest = None
    if rc2 == 0 and pypi_out:
        try:
            data = json.loads(pypi_out)
            latest = data.get("info", {}).get("version")
        except json.JSONDecodeError:
            pass

    update_available = False
    if installed and latest:
        try:
            update_available = _parse_version(latest) > _parse_version(installed)
        except (ValueError, TypeError):
            update_available = latest != installed

    return {
        "installed_version": installed,
        "latest_version": latest,
        "update_available": update_available,
        "upgrade_command": f"cd {HERMES_REPO} && VIRTUAL_ENV=.venv uv pip install --upgrade openviking" if update_available else None,
    }


def check_hermes_version() -> dict[str, Any]:
    """Hermes version via git describe."""
    rc, stdout, stderr = run_cmd(
        ["git", "describe", "--tags", "--always", "--dirty"],
        cwd=HERMES_REPO, timeout=CMD_TIMEOUT,
    )
    return {"version": stdout if rc == 0 else None, "error": stderr if rc != 0 else None}


def check_hermes_updates() -> dict[str, Any]:
    """Fetch myfork and list pending commits."""
    rc_fetch, _, stderr_fetch = run_cmd(
        ["git", "fetch", "myfork"],
        cwd=HERMES_REPO, timeout=GIT_FETCH_TIMEOUT,
    )
    if rc_fetch != 0:
        return {"fetch_ok": False, "fetch_error": stderr_fetch, "pending_commits": [], "count": 0}

    rc_log, stdout_log, stderr_log = run_cmd(
        ["git", "log", "HEAD..myfork/main", "--oneline", "-20"],
        cwd=HERMES_REPO, timeout=CMD_TIMEOUT,
    )
    if rc_log != 0:
        return {"fetch_ok": True, "log_ok": False, "log_error": stderr_log, "pending_commits": [], "count": 0}

    commits = [l for l in stdout_log.split("\n") if l.strip()] if stdout_log else []
    return {"fetch_ok": True, "log_ok": True, "pending_commits": commits, "count": len(commits)}


def _get_npm_outdated() -> tuple[int, dict[str, Any], str]:
    """Helper: run npm outdated -g --json once."""
    rc, stdout, stderr = run_cmd(
        ["npm", "outdated", "-g", "--json"],
        timeout=CMD_TIMEOUT,
    )
    try:
        data = json.loads(stdout) if stdout else {}
        if not isinstance(data, dict):
            data = {}
    except json.JSONDecodeError:
        data = {"_parse_error": stdout[:500] if stdout else "empty"}
    return rc, data, stderr


def check_npm_outdated(outdated_cache=None) -> dict[str, Any]:
    """npm global outdated packages."""
    if outdated_cache is None:
        rc, data, stderr = _get_npm_outdated()
    else:
        rc, data, stderr = outdated_cache
    return {"ok": rc in (0, 1), "outdated": data, "error": stderr if rc > 1 else None}


def check_npm_audit() -> dict[str, Any]:
    """npm audit — known CVEs in global dependency tree."""
    rc, stdout, stderr = run_cmd(
        ["npm", "audit", "-g", "--json"],
        timeout=CMD_TIMEOUT * 2,
    )
    try:
        data = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        data = {"_parse_error": stdout[:500] if stdout else "empty"}

    vulns = data.get("vulnerabilities", {}) if isinstance(data, dict) else {}
    summary = {"info": 0, "low": 0, "moderate": 0, "high": 0, "critical": 0}
    for pkg_info in vulns.values():
        if isinstance(pkg_info, dict):
            sev = pkg_info.get("severity", "info")
            if sev in summary:
                summary[sev] += 1

    return {
        "total_vulnerabilities": sum(summary.values()),
        "summary": summary,
        "error": stderr if rc > 1 else None,
    }


def check_npm_freshness(outdated_cache=None) -> dict[str, Any]:
    """Check if recently published (<7d) packages exist in outdated list."""
    if outdated_cache is None:
        _, outdated, _ = _get_npm_outdated()
    else:
        _, outdated, _ = outdated_cache

    fresh_packages = []
    for pkg_name, info in outdated.items():
        if not isinstance(info, dict) or pkg_name.startswith("_"):
            continue
        latest_ver = info.get("latest", "")
        if not latest_ver:
            continue

        rc, reg_stdout, _ = run_cmd(
            ["npm", "view", "--json", f"{pkg_name}@latest", "time"],
            timeout=CMD_TIMEOUT,
        )
        if rc != 0:
            continue
        try:
            time_data = json.loads(reg_stdout) if reg_stdout else {}
        except json.JSONDecodeError:
            continue

        publish_time = time_data.get(latest_ver) if isinstance(time_data, dict) else None
        if publish_time:
            try:
                pub_dt = datetime.fromisoformat(publish_time.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600
                if age_hours < 168:
                    fresh_packages.append({
                        "package": pkg_name,
                        "latest_version": latest_ver,
                        "published": publish_time,
                        "age_hours": round(age_hours, 1),
                    })
            except (ValueError, TypeError):
                pass

    return {"ok": True, "fresh_packages": fresh_packages, "count": len(fresh_packages)}


def check_socket_scan(outdated_cache=None) -> dict[str, Any]:
    """Socket.dev supply-chain scan on outdated global npm packages."""
    try:
        with open(SOCKET_TOKEN_FILE) as f:
            token = f.read().strip()
    except (FileNotFoundError, PermissionError):
        return {"ok": False, "skipped": True, "reason": "no token file"}

    socket_bin = _which("socket")
    if not socket_bin:
        return {"ok": False, "skipped": True, "reason": "socket CLI not found"}

    if outdated_cache is None:
        _, outdated, _ = _get_npm_outdated()
    else:
        _, outdated, _ = outdated_cache

    if not outdated or (isinstance(outdated, dict) and "_parse_error" in outdated):
        return {"ok": True, "alerts": [], "scanned": 0}

    alerts = []
    scanned = 0
    merged_env = os.environ.copy()
    merged_env["SOCKET_SECURITY_API_TOKEN"] = token

    for pkg_name in outdated:
        if pkg_name.startswith("_"):
            continue
        scanned += 1
        try:
            p = subprocess.run(
                [socket_bin, "package", "score", "npm", pkg_name, "--json"],
                capture_output=True, text=True, timeout=20,
                env=merged_env,
            )
            if p.returncode != 0:
                continue
            data = json.loads(p.stdout) if p.stdout.strip() else {}
            if not isinstance(data, dict):
                continue
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            continue

        pkg_alerts = data.get("data", {}).get("alerts", {})
        if not pkg_alerts:
            continue

        pkg_summary = {"package": pkg_name, "severity_counts": {}}
        for sev in ["critical", "high", "medium", "low"]:
            items = pkg_alerts.get(sev, [])
            if items:
                pkg_summary["severity_counts"][sev] = len(items)
                if "top_alert" not in pkg_summary:
                    pkg_summary["top_alert"] = items[0].get("type", "unknown")
        if pkg_summary.get("severity_counts"):
            alerts.append(pkg_summary)

    return {"ok": True, "alerts": alerts, "scanned": scanned, "has_alerts": len(alerts) > 0}


def check_searxng_health() -> dict[str, Any]:
    """SearXNG Docker container health + JSON API check."""
    rc, stdout, stderr = run_cmd(
        ["sudo", "docker", "inspect", "-f", "{{.State.Running}}", "searxng"],
        timeout=CMD_TIMEOUT,
    )
    container_running = rc == 0 and stdout.strip() == "true"

    api_ok = False
    api_result_count = 0
    api_error = None
    if container_running:
        rc_api, stdout_api, _ = run_cmd(
            ["curl", "-s", "-w", "\n%{http_code}", "--max-time", "25",
             f"{SEARXNG_URL}/search?q=test&format=json&limit=1"],
            timeout=30,
        )
        if stdout_api:
            # Split body from trailing HTTP status code
            lines = stdout_api.rsplit("\n", 1)
            body = lines[0]
            http_code = lines[1].strip() if len(lines) > 1 else ""
            if http_code == "403":
                api_error = "API blocked (public_instance=false)"
            else:
                try:
                    data = json.loads(body) if body else {}
                    api_ok = "results" in data
                    api_result_count = len(data.get("results", []))
                except json.JSONDecodeError:
                    api_error = f"JSON parse error (HTTP {http_code})"

    return {
        "container_running": container_running,
        "api_ok": api_ok,
        "api_result_count": api_result_count,
        "api_error": api_error,
        "error": stderr if rc != 0 else None,
    }


def check_searxng_update() -> dict[str, Any]:
    """SearXNG Docker image update check."""
    # Local digest
    rc_loc, stdout_loc, _ = run_cmd(
        ["sudo", "docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", SEARXNG_IMAGE],
        timeout=CMD_TIMEOUT,
    )
    local_digest = None
    if rc_loc == 0 and stdout_loc:
        m = re.search(r"sha256:[a-f0-9]+", stdout_loc)
        if m:
            local_digest = m.group(0)

    # Remote digest from Docker Hub
    rc_remote, stdout_remote, stderr_remote = run_cmd(
        ["curl", "-s", "--max-time", "10",
         "https://hub.docker.com/v2/repositories/searxng/searxng/tags/latest"],
        timeout=15,
    )
    remote_digest = None
    update_available = None
    if rc_remote == 0 and stdout_remote:
        try:
            data = json.loads(stdout_remote)
            images = data.get("images", [])
            for img in images:
                d = img.get("digest") if isinstance(img, dict) else None
                if d:
                    if remote_digest is None:
                        remote_digest = d
                    if local_digest and d == local_digest:
                        update_available = False
                        break
            if update_available is None:
                update_available = local_digest is not None and remote_digest is not None and local_digest != remote_digest
        except json.JSONDecodeError:
            update_available = None

    return {
        "local_digest": local_digest[:19] + "…" if local_digest and len(local_digest) > 20 else local_digest,
        "remote_digest": remote_digest[:19] + "…" if remote_digest and len(remote_digest) > 20 else remote_digest,
        "update_available": update_available,
        "upgrade_command": "sudo docker pull searxng/searxng:latest && sudo docker restart searxng" if update_available else None,
    }


def check_gitnexus() -> dict[str, Any]:
    """GitNexus health — gitnexus list exits 0 on success."""
    gitnexus_bin = _which("gitnexus")
    if not gitnexus_bin:
        return {"ok": False, "exit_code": -2, "repo_count": 0, "error": "gitnexus not found in PATH"}
    rc, stdout, stderr = run_cmd([gitnexus_bin, "list"], timeout=CMD_TIMEOUT)
    ok = rc == 0
    repo_count = 0
    if ok and stdout:
        repos = re.findall(r"^\s{2}(\S+)", stdout, re.MULTILINE)
        repo_count = len(repos)
    return {"ok": ok, "exit_code": rc, "repo_count": repo_count, "error": stderr if not ok else None}


def check_gitnexus_version() -> dict[str, Any]:
    """GitNexus version — compare installed vs npm latest."""
    gitnexus_bin = _which("gitnexus")
    if not gitnexus_bin:
        return {"installed_version": None, "latest_version": None, "update_available": False, "upgrade_command": None, "error": "gitnexus not found in PATH"}
    rc_inst, installed_v, _ = run_cmd([gitnexus_bin, "--version"], timeout=CMD_TIMEOUT)
    installed = installed_v.strip() if rc_inst == 0 else None

    rc_npm, latest_v, _ = run_cmd(["npm", "view", "gitnexus", "version"], timeout=CMD_TIMEOUT)
    latest = latest_v.strip() if rc_npm == 0 else None

    update_available = False
    if installed and latest:
        try:
            update_available = _parse_version(latest) > _parse_version(installed)
        except (ValueError, TypeError):
            update_available = latest != installed

    return {
        "installed_version": installed,
        "latest_version": latest,
        "update_available": update_available,
        "upgrade_command": "npm install -g gitnexus@latest" if update_available else None,
    }


def check_context7() -> dict[str, Any]:
    """Context7 MCP health — process alive, version, wrapper detection."""
    import subprocess as _sp

    # 1. Process check — pgrep then filter by /proc/<pid>/exe to only count
    #    processes running under the node binary (excludes pgrep, bash, etc.)
    p = _sp.run(
        ["pgrep", "-f", "context7-mcp"],
        capture_output=True, text=True, timeout=5,
    )
    proc_count = 0
    if p.stdout.strip():
        for pid_str in p.stdout.strip().split():
            try:
                exe = os.readlink(f"/proc/{pid_str}/exe")
                if "node" in exe:
                    proc_count += 1
            except (FileNotFoundError, PermissionError, OSError):
                pass

    # 2. Wrapper detection — pgrep for npm+context7, filter by /proc/<pid>/exe
    p2 = _sp.run(
        ["pgrep", "-f", "npm exec.*context7"],
        capture_output=True, text=True, timeout=5,
    )
    npm_wrapper_count = 0
    if p2.stdout.strip():
        for pid_str in p2.stdout.strip().split():
            try:
                exe = os.readlink(f"/proc/{pid_str}/exe")
                if "npm" in exe or "node" in exe:
                    npm_wrapper_count += 1
            except (FileNotFoundError, PermissionError, OSError):
                pass
    using_npm_wrapper = npm_wrapper_count > 0

    # 3. Installed version (global)
    rc3, installed_out, _ = run_cmd(
        ["npm", "list", "-g", "@upstash/context7-mcp", "--depth=0"],
        timeout=CMD_TIMEOUT,
    )
    installed_version = None
    if rc3 == 0 and installed_out:
        m = re.search(r'@upstash/context7-mcp@(\S+)', installed_out)
        if m:
            installed_version = m.group(1)

    # 4. Latest version from npm registry
    rc4, latest_v, _ = run_cmd(
        ["npm", "view", "@upstash/context7-mcp", "version"],
        timeout=CMD_TIMEOUT,
    )
    latest_version = latest_v.strip() if rc4 == 0 else None

    update_available = False
    if installed_version and latest_version:
        try:
            update_available = _parse_version(latest_version) > _parse_version(installed_version)
        except (ValueError, TypeError):
            update_available = latest_version != installed_version

    return {
        "ok": proc_count > 0,
        "process_count": proc_count,
        "using_npm_wrapper": using_npm_wrapper,
        "installed_version": installed_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "upgrade_command": "npm install -g @upstash/context7-mcp@latest" if update_available else None,
    }


def check_bitwarden_sm() -> dict[str, Any]:
    """Bitwarden Secrets Manager health check."""
    rc, stdout, stderr = run_cmd([BWS_BIN, "project", "list"], timeout=CMD_TIMEOUT)
    ok = rc == 0
    project_count = 0
    if ok and stdout:
        try:
            projects = json.loads(stdout)
            if isinstance(projects, list):
                project_count = len(projects)
        except json.JSONDecodeError:
            pass
    return {"ok": ok, "exit_code": rc, "project_count": project_count, "error": stderr if not ok else None}


def check_hermes_errors() -> dict[str, Any]:
    """Hermes error log analysis — detect persistent config/service errors."""
    log_file = "/home/xing/.hermes/logs/errors.log"
    if not os.path.isfile(log_file):
        return {"ok": False, "issues": [], "error": f"log file not found: {log_file}"}

    today = datetime.now().strftime("%Y-%m-%d")
    rc, stdout, _ = run_cmd(
        f"grep '{today}' {log_file} 2>/dev/null | grep -iE 'WARNING|ERROR' || true",
        shell=True, timeout=CMD_TIMEOUT,
    )

    if not stdout.strip():
        return {"ok": True, "issues": []}

    lines = [l for l in stdout.strip().split("\n") if l.strip()]

    NOISE = [
        r"QQBot.*WebSocket closed.*Session timed out",
        r"rate limited.*o9cq800v",
        r"agent\.stream_diag.*Stream drop",
        r"agent\.conversation_loop.*Retrying API call",
        r"agent\.conversation_loop.*API call failed.*attempt \d",
        r"agent\.tool_executor:.*returned error",
        r"execute_code timed out",
        r"Tool.*returned error.*exit_code",
        r"Gateway.*draining",
        r"Gateway drain timed out",
        r"background task.*did not exit",
        r"origin has thread_id.*delivery target lost",
    ]

    CATEGORIES = [
        ("mcp_connect", [
            r"Failed to connect to MCP server",
            r"MCP server.*initial connection failed.*giving up",
            r"missing executable.*MCP|MCP.*missing executable",
        ]),
        ("mcp_config", [
            r"missing executable.*gitnexus|gitnexus.*missing executable",
            r"No such file or directory.*gitnexus",
        ]),
        ("provider_error", [
            r"API call failed after.*retries.*provider=",
            r"root:.*API call failed after.*retries",
            r"cron\.scheduler.*Job.*failed.*RuntimeError",
        ]),
        ("config_warning", [
            r"WEIXIN_GROUP_POLICY",
            r"iLink.*group.*delivery",
        ]),
    ]

    def _matches_any(line, patterns):
        return any(re.search(p, line) for p in patterns)

    def _normalize(line):
        s = re.sub(r'\b\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+\b', 'TS', line)
        s = re.sub(r'\battempt \d\b', 'attempt N', s)
        s = re.sub(r'\bretrying in [\d.]+s\b', 'retrying in Xs', s)
        s = re.sub(r'\bThreadPoolExecutor-\d+_\d+:\d+\b', 'THREAD', s)
        s = re.sub(r'\btokens=~[\d,]+\b', 'tokens=~N', s)
        s = re.sub(r'\bmsgs=\d+\b', 'msgs=N', s)
        s = re.sub(r'\[cron_[a-f0-9]+_\d+_\d+\]', '[cron_ID_SESSION]', s)
        s = re.sub(r"Job '[^']+' failed", "Job 'NAME' failed", s)
        return s

    signal_lines = [l for l in lines if not _matches_any(l, NOISE)]

    issues = []
    classified = set()
    for cat_name, patterns in CATEGORIES:
        matches = [(i, l) for i, l in enumerate(signal_lines) if _matches_any(l, patterns)]
        if matches:
            msg_counts = defaultdict(int)
            for idx, m in matches:
                msg_counts[_normalize(m)] += 1
                classified.add(idx)
            grouped = [{"message": dedup, "count": cnt}
                      for dedup, cnt in sorted(msg_counts.items(), key=lambda x: -x[1])]
            issues.append({
                "category": cat_name,
                "total_occurrences": sum(g["count"] for g in grouped),
                "unique_messages": len(grouped),
                "details": grouped,
            })

    unclassified = [l for i, l in enumerate(signal_lines) if i not in classified]
    if unclassified:
        msg_counts = defaultdict(int)
        for m in unclassified:
            msg_counts[_normalize(m)] += 1
        grouped = [{"message": dedup, "count": cnt}
                  for dedup, cnt in sorted(msg_counts.items(), key=lambda x: -x[1])]
        issues.append({
            "category": "unclassified",
            "total_occurrences": sum(g["count"] for g in grouped),
            "unique_messages": len(grouped),
            "details": grouped,
        })

    return {
        "ok": len(issues) == 0,
        "total_signals": len(signal_lines),
        "total_filtered": len(lines) - len(signal_lines),
        "issues": issues,
    }


def check_local_bin() -> dict[str, Any]:
    """Check ~/.local/bin binaries for outdated versions via GitHub releases.

    Covers standalone binaries that aren't managed by apt/npm/pip/docker.
    Each tool maps to its GitHub repo for release-version comparison.
    Tools without a repo (claude, x-cli) use their own update mechanism —
    we just report the current version.
    """
    LOCAL_BIN = os.path.expanduser("~/.local/bin")

    # (name, version_flag, github_repo, update_command)
    # version_flag=None → use "uv tool list" for uv-managed tools
    # github_repo=None → skip GitHub release check (self-updating tool)
    TOOLS: list[tuple[str, str | None, str | None, str]] = [
        ("uv", "--version", "astral-sh/uv", "uv self update"),
        ("sesh", "--version", "joshmedeski/sesh", "download from GitHub releases"),
        ("himalaya", "--version", "pimalaya/himalaya", "download from GitHub releases"),
        ("zoxide", "--version", "ajeetdsouza/zoxide", "download from GitHub releases"),
        ("claude", "--version", None, "claude update"),
        ("x-cli", None, None, "uv tool upgrade x-cli"),  # version from "uv tool list"
    ]

    binaries = []
    for name, ver_flag, repo, update_cmd in TOOLS:
        bin_path = os.path.join(LOCAL_BIN, name)
        if not os.path.isfile(bin_path) and name != "x-cli":
            continue

        current_ver = None
        if ver_flag:
            rc, out, _ = run_cmd(
                f"{shlex.quote(bin_path)} {ver_flag} 2>&1",
                shell=True, timeout=CMD_TIMEOUT,
            )
            if out.strip():
                # Extract version number from noisy output like "uv 0.11.18 (x86_64...)"
                ver_match = re.search(r'(\d+\.\d+\.\d+)', out)
                current_ver = ver_match.group(1) if ver_match else out.strip()
        elif name == "x-cli":
            # x-cli has no --version flag; get version from "uv tool list"
            rc, out, _ = run_cmd(
                "uv tool list 2>/dev/null",
                shell=True, timeout=CMD_TIMEOUT,
            )
            if out.strip():
                ver_match = re.search(r'x-cli\s+v([\d.]+)', out)
                current_ver = ver_match.group(1) if ver_match else None

        latest_ver = None
        if repo:
            rc, out, _ = run_cmd(
                f"curl -sL https://api.github.com/repos/{repo}/releases/latest 2>/dev/null | "
                f"python3 -c \"import sys,json; d=json.load(sys.stdin); print(d['tag_name'].lstrip('v'))\"",
                shell=True, timeout=CMD_TIMEOUT,
            )
            if out.strip():
                latest_ver = out.strip()

        update_available = (
            current_ver is not None
            and latest_ver is not None
            and current_ver != latest_ver
        )

        binaries.append({
            "name": name,
            "current_version": current_ver,
            "latest_version": latest_ver,
            "update_available": update_available,
            "update_command": update_cmd,
        })

    return {
        "binaries": binaries,
        "total": len(binaries),
        "updates_count": sum(1 for b in binaries if b["update_available"]),
    }


# ═══════════════════════════════════════════
# Main
# ═══════════════════════════════════════════

def main() -> None:
    timestamp = datetime.now(timezone.utc).isoformat()

    # Get Tailscale status once
    ts_status = _get_ts_status()

    # Get apt data once (shared across apt checks)
    # _get_apt_upgradable()

    # Get npm outdated once (shared across 3 consumers)
    npm_cache = _get_npm_outdated()

    # ═══ Infrastructure ═══
    infrastructure: dict[str, Any] = {}

    # VPS
    vps: dict[str, Any] = {}
    for name, fn in [
        ("services", check_vps_services),
        ("derp", check_derp_health),
        ("tls", check_tls_cert),
        ("caddy_headers", check_caddy_headers),
        ("firewall", check_firewall),
        ("ssh_security", check_ssh_security),
        ("fail2ban", check_fail2ban),
        ("resources", check_system_resources),
        ("network", check_network),
        ("system_health", check_system_health),
        ("apt_updates", check_apt_updates),
        ("bbr", check_bbr),
        ("security_baseline", check_vps_security_baseline),
    ]:
        vps[name] = _safe_check(f"vps.{name}", fn)
    infrastructure["vps"] = vps

    # Beryl AX
    infrastructure["beryl_ax"] = _safe_check("beryl_ax", check_beryl_ax)

    # SearXNG engines — call once, shared by infrastructure + applications
    searxng_engines_result = _safe_check("searxng_engines", check_searxng_engines)
    infrastructure["searxng_engines"] = searxng_engines_result

    # Beryl AX ↔ SearXNG IP cross-validation
    infrastructure["beryl_ip_mismatch"] = _safe_check("beryl_ip_mismatch", check_beryl_ip_mismatch)

    # Windows
    infrastructure["windows"] = _safe_check("windows", check_windows)

    # Annotate SearXNG engine timeouts with Beryl AX causality.
    # When Beryl AX is unreachable, SearXNG's outbound proxy (socks5h via Beryl)
    # is unavailable — public engine timeouts are likely caused by this, not
    # by the engines themselves.
    beryl_reachable = infrastructure.get("beryl_ax", {}).get("reachable", True)
    searxng_engines = infrastructure.get("searxng_engines", {})
    if not beryl_reachable and searxng_engines.get("total_unresponsive", 0) > 0:
        infrastructure["searxng_engines"]["_beryl_causal"] = True
        infrastructure["searxng_engines"]["_beryl_causal_note"] = (
            "Beryl AX is unreachable. SearXNG routes outbound traffic through "
            "Beryl AX socks5 proxy — engine timeouts may be caused by proxy "
            "unavailability, not by the search engines themselves."
        )
    else:
        infrastructure["searxng_engines"]["_beryl_causal"] = False

    # Tailscale
    infrastructure["tailscale_exit"] = _safe_check("tailscale_exit", check_tailscale_exit, ts_status)
    infrastructure["acl"] = _safe_check("acl", check_acl_isolation, ts_status)
    infrastructure["cross_node_ssh"] = _safe_check("cross_node_ssh", check_cross_node_ssh)
    infrastructure["mother_device"] = _safe_check("mother_device", check_mother_device, ts_status)

    # DERP + VPN
    infrastructure["derp_chain"] = _safe_check("derp_chain", check_derp_chain)
    infrastructure["exit_node"] = _safe_check("exit_node", check_exit_node, ts_status)

    # VPN summary (depends on earlier checks)
    derp_ok = infrastructure.get("derp_chain", {}).get("derp_reachable", False)
    beryl_exit = infrastructure.get("exit_node", {}).get("beryl_advertising", False)
    mother_online = infrastructure.get("mother_device", {}).get("online", False)
    infrastructure["vpn_summary"] = check_vpn_summary(derp_ok, beryl_exit, mother_online)

    # ═══ Security ═══
    security: dict[str, Any] = {}
    security["usn_notices"] = _safe_check("usn", lambda: classify_usn(fetch_usn()))
    security["cisa_kev"] = _safe_check("kev", fetch_kev)
    security["pending_upgrades"] = _safe_check("security_upgrades", check_security_upgrades)
    security["kernel_updates"] = _safe_check("kernel_updates", check_kernel_updates)
    security["mitigations"] = _safe_check("mitigations", check_mitigations)

    # ═══ Applications ═══
    applications: dict[str, Any] = {}
    applications["openviking"] = {
        "health": _safe_check("openviking_health", check_openviking_health),
        "logs": _safe_check("openviking_logs", check_openviking_logs),
        "version": _safe_check("openviking_version", check_openviking_version),
    }
    applications["hermes"] = {
        "version": _safe_check("hermes_version", check_hermes_version),
        "updates": _safe_check("hermes_updates", check_hermes_updates),
        "errors": _safe_check("hermes_errors", check_hermes_errors),
    }
    applications["npm"] = {
        "outdated": _safe_check("npm_outdated", check_npm_outdated, npm_cache),
        "audit": _safe_check("npm_audit", check_npm_audit),
        "freshness": _safe_check("npm_freshness", check_npm_freshness, npm_cache),
    }
    applications["socket_scan"] = _safe_check("socket_scan", check_socket_scan, npm_cache)
    applications["searxng"] = {
        "health": _safe_check("searxng_health", check_searxng_health),
        "engines": searxng_engines_result,
        "update": _safe_check("searxng_update", check_searxng_update),
    }
    applications["gitnexus"] = {
        "health": _safe_check("gitnexus_health", check_gitnexus),
        "version": _safe_check("gitnexus_version", check_gitnexus_version),
    }
    applications["context7"] = _safe_check("context7", check_context7)
    applications["bitwarden_sm"] = _safe_check("bitwarden_sm", check_bitwarden_sm)
    applications["local_bin"] = _safe_check("local_bin", check_local_bin)

    # ═══ Output ═══
    report = {
        "timestamp": timestamp,
        "infrastructure": infrastructure,
        "security": security,
        "applications": applications,
    }

    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
