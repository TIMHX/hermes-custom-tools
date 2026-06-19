#!/usr/bin/env python3
"""Daily maintenance data-collection script.

Runs 18 checks, outputs JSON to stdout.
Designed for cron: `no_agent=false` LLM-driven pattern — the downstream
LLM agent processes the JSON and produces the report.

职责：应用软件层级（OpenViking, SearXNG, GitNexus, npm, Hermes, 包管理）
基础设施检查 → system-health.sh (cron: 8d3f8fcbfcc5)

检查项目：
  1. OpenViking health            — GET http://127.0.0.1:1933/health
  2. OpenViking logs              — tail ~/.openviking/data/log/openviking.log
  3. OpenViking version           — compare installed PyPI version with latest release
  4. Hermes version               — git describe --tags
  5. Hermes pending updates       — git fetch myfork; git log HEAD..myfork/main
  6. npm global outdated          — npm outdated -g --json
  7. npm audit (global)           — npm audit --json (CVE + dependency vulnerabilities)
  8. npm publish freshness        — latest versions published <7d ago
  9. APT security updates         — apt list --upgradable | grep -i security
 10. Homebrew outdated            — brew outdated (graceful skip if brew absent)
 11. Socket.dev scan              — supply-chain scan on outdated packages
 12. SearXNG health               — Docker container running + JSON API
 13. SearXNG image update         — check Docker Hub for newer image
 14. SearXNG unresponsive engines — count blocked/errored search engines
 15. Hermes error log analysis    — detect persistent config/service errors
 16. GitNexus health              — gitnexus list exit 0
 17. GitNexus version             — compare installed vs latest npm release
 18. Bitwarden SM health          — bws project list exit 0

"""

import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

HERMES_REPO = "/home/xing/.hermes/hermes-agent"
HERMES_DOTVENV = "/home/xing/.hermes/hermes-agent/.venv"  # OpenViking lives here
OV_HEALTH_URL = "http://127.0.0.1:1933/health"
OV_PYPI_URL = "https://pypi.org/pypi/openviking/json"
SEARXNG_URL = "http://127.0.0.1:8888"
SEARXNG_IMAGE = "searxng/searxng:latest"
CMD_TIMEOUT = 30  # seconds for most commands
GIT_FETCH_TIMEOUT = 60  # git fetch can be slower
BREW_BIN = "/home/linuxbrew/.linuxbrew/bin/brew"
SOCKET_BIN = "/home/xing/.nvm/versions/node/v24.15.0/bin/socket"
SOCKET_TOKEN_FILE = "/home/xing/.hermes/.socket-token"
GITNEXUS_BIN = "/home/xing/.nvm/versions/node/v24.15.0/bin/gitnexus"
BWS_BIN = "/home/xing/.hermes/bin/bws"


def _parse_version(v: str):
    """Extract numeric version components for comparison.

    Strips pre-release suffixes: 2.0.0-rc1, 2.0.0rc1, 2.0.0+build,
    2.0.0.rc1 → (2,0,0).  Also handles git-describe output:
    v1.2.3-5-gabcdef → (1,2,3).
    """
    m = re.search(r'(\d+(?:\.\d+)*)', v)
    if not m:
        return (0,)
    parts = re.findall(r'\d+', m.group(1))
    return tuple(int(x) for x in parts) if parts else (0,)


def run(cmd, timeout=CMD_TIMEOUT, cwd=None, shell=False, env=None):
    """Run a command, return (exit_code, stdout, stderr)."""
    try:
        if shell:
            p = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                shell=True, cwd=cwd, env=env,
            )
        else:
            p = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                env=env,
            )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return -2, "", str(e)
    except Exception as e:
        return -3, "", str(e)


def check_openviking_health():
    """OpenViking health check."""
    rc, stdout, stderr = run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10", OV_HEALTH_URL])
    ok = rc == 0 and stdout == "200"
    return {
        "ok": ok,
        "http_code": stdout if rc == 0 else None,
        "error": stderr if not ok else None,
    }


def check_openviking_logs():
    """OpenViking logs from ~/.openviking/data/log/openviking.log. Grep WARNING/ERROR."""
    log_file = "/home/xing/.openviking/data/log/openviking.log"
    if not os.path.isfile(log_file):
        return {
            "source": log_file,
            "warnings_errors": [],
            "count": 0,
            "file_found": False,
        }
    rc, stdout, stderr = run(
        f"tail -200 {log_file} 2>/dev/null | grep -iE 'WARNING|ERROR' || true",
        shell=True,
    )
    lines = [l for l in stdout.split("\n") if l.strip()] if stdout else []
    return {
        "source": f"{log_file} (last 200 lines)",
        "warnings_errors": lines,
        "count": len(lines),
        "file_found": True,
    }


def check_hermes_version():
    """Hermes version via git describe --tags."""
    rc, stdout, stderr = run(
        ["git", "describe", "--tags", "--always", "--dirty"],
        cwd=HERMES_REPO,
    )
    return {
        "version": stdout if rc == 0 else None,
        "error": stderr if rc != 0 else None,
    }


def check_hermes_updates():
    """Fetch myfork and list pending commits."""
    # Fetch
    rc_fetch, _, stderr_fetch = run(
        ["git", "fetch", "myfork"],
        cwd=HERMES_REPO,
        timeout=GIT_FETCH_TIMEOUT,
    )
    if rc_fetch != 0:
        return {
            "fetch_ok": False,
            "fetch_error": stderr_fetch,
            "pending_commits": [],
            "count": 0,
        }

    # Log pending
    rc_log, stdout_log, stderr_log = run(
        ["git", "log", "HEAD..myfork/main", "--oneline", "-20"],
        cwd=HERMES_REPO,
    )
    if rc_log != 0:
        return {
            "fetch_ok": True,
            "log_ok": False,
            "log_error": stderr_log or f"git log failed: rc={rc_log}",
            "pending_commits": [],
            "count": 0,
        }
    commits = [l for l in stdout_log.split("\n") if l.strip()] if stdout_log else []
    return {
        "fetch_ok": True,
        "log_ok": True,
        "pending_commits": commits,
        "count": len(commits),
    }


def _get_npm_outdated():
    """Helper: run npm outdated -g --json once, return (exit_code, parsed_dict, stderr)."""
    rc, stdout, stderr = run(
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


def check_npm_outdated(outdated_cache=None):
    """npm global outdated packages."""
    if outdated_cache is None:
        rc, data, stderr = _get_npm_outdated()
    else:
        rc, data, stderr = outdated_cache
    return {
        "ok": rc == 0 or rc == 1,  # npm outdated exits 1 when packages ARE outdated
        "outdated": data,
        "error": stderr if rc > 1 else None,
    }


def check_npm_audit():
    """npm audit — known CVEs in global dependency tree."""
    rc, stdout, stderr = run(
        ["npm", "audit", "-g", "--json"],
        timeout=CMD_TIMEOUT * 2,  # audit can be slower
    )
    try:
        data = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        data = {"_parse_error": stdout[:500] if stdout else "empty"}
    # Extract summary — count packages by severity (not advisory instances)
    vulns = data.get("vulnerabilities", {}) if isinstance(data, dict) else {}
    summary = {
        "info": 0,
        "low": 0,
        "moderate": 0,
        "high": 0,
        "critical": 0,
    }
    for pkg_name, pkg_info in vulns.items():
        if isinstance(pkg_info, dict):
            sev = pkg_info.get("severity", "info")
            if sev in summary:
                summary[sev] += 1
    return {
        "total_vulnerabilities": sum(summary.values()),
        "summary": summary,
        "full_report": data if isinstance(data, dict) else {"raw": str(data)[:2000]},
        "error": stderr if rc > 1 else None,
    }


def check_npm_freshness(outdated_cache=None):
    """Check if any global package's 'latest' version was published recently (<7d)."""
    if outdated_cache is None:
        _, outdated, _ = _get_npm_outdated()
    else:
        _, outdated, _ = outdated_cache

    fresh_packages = []
    packages_to_check = []
    for pkg_name, info in outdated.items():
        if not isinstance(info, dict):
            continue
        latest_ver = info.get("latest", "")
        if latest_ver:
            packages_to_check.append((pkg_name, latest_ver))

    if not packages_to_check:
        return {"ok": True, "fresh_packages": [], "count": 0}

    # Call npm view per package (list-form, no shell=True — no batching since
    # npm view does not support multiple package specs with --json)
    for pkg_name, latest_ver in packages_to_check:
        rc_reg, reg_stdout, reg_stderr = run(
            ["npm", "view", "--json", f"{pkg_name}@latest", "time"],
            timeout=CMD_TIMEOUT,
        )
        if rc_reg != 0:
            continue
        try:
            time_data = json.loads(reg_stdout) if reg_stdout else {}
        except json.JSONDecodeError:
            continue

        publish_time = None
        if isinstance(time_data, dict):
            publish_time = time_data.get(latest_ver)  # only exact version match

        if publish_time:
            try:
                pub_dt = datetime.fromisoformat(publish_time.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600
                if age_hours < 168:  # 7 days
                    fresh_packages.append({
                        "package": pkg_name,
                        "latest_version": latest_ver,
                        "published": publish_time,
                        "age_hours": round(age_hours, 1),
                    })
            except (ValueError, TypeError):
                pass

    return {
        "ok": True,
        "fresh_packages": fresh_packages,
        "count": len(fresh_packages),
        "note": "Packages with latest version published < 7 days ago — consider waiting before updating",
    }


def check_apt_upgradable():
    """APT upgradable packages (all).

    NOTE: runs `sudo apt update -qq` first (updates local APT cache as side effect).
    """
    # First run apt update to refresh package lists
    rc_upd, stdout_upd, stderr_upd = run(
        ["sudo", "apt", "update", "-qq"],
        timeout=60,
    )
    rc, stdout, stderr = run(
        "apt list --upgradable 2>/dev/null | grep -v '^Listing\\|^WARNING\\|^$' || true",
        shell=True,
        timeout=CMD_TIMEOUT,
    )
    lines = [l for l in stdout.split("\n") if l.strip()] if stdout else []
    # Count security updates separately for highlighting
    security_lines = [l for l in lines if "security" in l.lower()]
    return {
        "source": "apt list --upgradable (after apt update)",
        "packages": lines,
        "count": len(lines),
        "security_count": len(security_lines),
        "apt_update_ok": rc_upd == 0,
        "apt_update_error": stderr_upd[:500] if rc_upd != 0 else None,
        "error": stderr if rc != 0 else None,
    }


def check_socket_scan(outdated_cache=None):
    """Socket.dev supply-chain scan on outdated global npm packages."""
    try:
        with open(SOCKET_TOKEN_FILE) as f:
            token = f.read().strip()
    except (FileNotFoundError, PermissionError):
        return {"ok": False, "skipped": True, "reason": "no token file"}

    if not os.path.isfile(SOCKET_BIN):
        return {"ok": False, "skipped": True, "reason": "socket CLI not found"}

    # Get outdated packages from cache or fresh call
    if outdated_cache is None:
        _, outdated, _ = _get_npm_outdated()
    else:
        _, outdated, _ = outdated_cache

    if not outdated or isinstance(outdated, dict) and "_parse_error" in outdated:
        return {"ok": True, "alerts": [], "scanned": 0}
    
    # Filter out non-package keys (e.g. _parse_error from failed parse)

    alerts = []
    scanned = 0
    SOCKET_TIMEOUT = 20
    merged_env = os.environ.copy()
    merged_env["PATH"] = "/home/linuxbrew/.linuxbrew/bin:" + merged_env.get("PATH", "")
    merged_env["SOCKET_SECURITY_API_TOKEN"] = token

    for pkg_name in outdated:
        # Skip metadata keys from parse errors
        if pkg_name.startswith("_"):
            continue
        scanned += 1
        try:
            p = subprocess.run(
                [SOCKET_BIN, "package", "score", "npm", pkg_name, "--json"],
                capture_output=True, text=True, timeout=SOCKET_TIMEOUT,
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

    return {
        "ok": True,
        "alerts": alerts,
        "scanned": scanned,
        "has_alerts": len(alerts) > 0,
        "note": "Socket supply-chain scan (malware, typosquats, install scripts). Limited to 20s per package.",
    }


def check_brew_outdated():
    """Homebrew outdated packages (skip if brew not installed)."""
    if not os.path.isfile(BREW_BIN):
        return {"brew_installed": False, "outdated": [], "count": 0}

    rc, stdout, stderr = run([BREW_BIN, "outdated"], timeout=CMD_TIMEOUT)
    packages = [l for l in stdout.split("\n") if l.strip()] if stdout else []
    return {
        "brew_installed": True,
        "brew_path": BREW_BIN,
        "outdated": packages,
        "count": len(packages),
        "error": stderr if rc != 0 else None,
    }


def check_openviking_version():
    """OpenViking version — compares installed version with PyPI latest release."""
    # Get installed version from .venv
    rc_inst, stdout_inst, stderr_inst = run(
        [f"{HERMES_DOTVENV}/bin/python3", "-c",
         "import openviking; print(getattr(openviking, '__version__', 'UNKNOWN'))"],
        timeout=CMD_TIMEOUT,
    )
    installed = stdout_inst.strip() if rc_inst == 0 else None
    # __version__ may not exist; getattr default "UNKNOWN" is not a real version
    if installed and installed.upper() == "UNKNOWN":
        installed = None

    # Get latest version from PyPI
    rc_pypi, stdout_pypi, _ = run(
        ["curl", "-s", "--max-time", "10", OV_PYPI_URL],
        timeout=CMD_TIMEOUT,
    )
    latest = None
    pypi_info = {}
    if rc_pypi == 0 and stdout_pypi:
        try:
            data = json.loads(stdout_pypi)
            latest = data.get("info", {}).get("version")
            pypi_info = {
                "name": data.get("info", {}).get("name"),
                "summary": data.get("info", {}).get("summary", "")[:200],
                "home_page": data.get("info", {}).get("home_page"),
                "release_url": data.get("info", {}).get("release_url"),
            }
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
        "pypi_info": pypi_info,
        "upgrade_command": f"cd {HERMES_REPO} && VIRTUAL_ENV=.venv uv pip install --upgrade openviking" if update_available else None,
        "error": stderr_inst if rc_inst != 0 else (None if rc_pypi == 0 else f"PyPI fetch failed: rc={rc_pypi}"),
    }


def check_searxng_update():
    """SearXNG image update — check Docker Hub for newer image without pulling."""
    # Get LOCAL image digest (docker image inspect, not container inspect)
    rc_loc, stdout_loc, _ = run(
        ["sudo", "docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", SEARXNG_IMAGE],
        timeout=CMD_TIMEOUT,
    )
    local_digest = None
    if rc_loc == 0 and stdout_loc:
        m = re.search(r"sha256:[a-f0-9]+", stdout_loc)
        if m:
            local_digest = m.group(0)

    # Get REMOTE digest from Docker Hub API (read-only, no pull)
    rc_remote, stdout_remote, stderr_remote = run(
        ["curl", "-s", "--max-time", "10",
         "https://hub.docker.com/v2/repositories/searxng/searxng/tags/latest"],
        timeout=CMD_TIMEOUT,
    )
    remote_digest = None
    remote_error = None
    update_available = None
    if rc_remote == 0 and stdout_remote:
        try:
            data = json.loads(stdout_remote)
            images = data.get("images", [])
            # Match local digest against ANY remote platform image (multi-arch support)
            for img in images:
                d = img.get("digest") if isinstance(img, dict) else None
                if d:
                    if remote_digest is None:
                        remote_digest = d
                    if local_digest and d == local_digest:
                        update_available = False
                        break
            if update_available is None and local_digest and remote_digest:
                update_available = True
            elif update_available is None:
                update_available = False
        except json.JSONDecodeError:
            remote_error = "Docker Hub API parse error"
    else:
        remote_error = stderr_remote or f"curl failed: rc={rc_remote}"

    if update_available is None:
        update_available = bool(local_digest and remote_digest and local_digest != remote_digest)
    DIGEST_DISPLAY_LEN = 19

    return {
        "local_digest": local_digest[:DIGEST_DISPLAY_LEN] + "…" if local_digest and len(local_digest) > DIGEST_DISPLAY_LEN + 1 else local_digest,
        "remote_digest": remote_digest[:DIGEST_DISPLAY_LEN] + "…" if remote_digest and len(remote_digest) > DIGEST_DISPLAY_LEN + 1 else remote_digest,
        "update_available": update_available,
        "upgrade_command": "sudo docker pull searxng/searxng:latest && sudo docker restart searxng" if update_available else None,
        "error": remote_error,
    }


def check_gitnexus_version():
    """GitNexus version — compares installed npm version with latest release."""
    # Get installed version
    rc_inst, stdout_inst, _ = run(
        [GITNEXUS_BIN, "--version"],
        timeout=CMD_TIMEOUT,
    )
    installed = stdout_inst.strip() if rc_inst == 0 else None

    # Get latest version from npm registry
    rc_npm, stdout_npm, _ = run(
        ["npm", "view", "gitnexus", "version"],
        timeout=CMD_TIMEOUT,
    )
    latest = stdout_npm.strip() if rc_npm == 0 else None

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
        "error": None if rc_inst == 0 else f"installed check failed: rc={rc_inst}",
    }


def check_hermes_errors():
    """Hermes error log analysis — detect persistent configuration/service errors.

    Reads the last 24h of errors.log, filters out known transient noise, and
    groups remaining warnings/errors by category.
    """
    log_file = "/home/xing/.hermes/logs/errors.log"
    if not os.path.isfile(log_file):
        return {"ok": False, "issues": [], "error": f"log file not found: {log_file}"}

    today = datetime.now().strftime("%Y-%m-%d")

    # Grep today's WARNING/ERROR lines
    rc, stdout, stderr = run(
        f"grep '{today}' {log_file} 2>/dev/null | grep -iE 'WARNING|ERROR' || true",
        shell=True,
        timeout=CMD_TIMEOUT,
    )

    if not stdout.strip():
        return {"ok": True, "issues": [], "note": "no warnings/errors today"}

    lines = [l for l in stdout.strip().split("\n") if l.strip()]

    # ── Noise patterns to suppress ──────────────────────────────
    NOISE = [
        r"QQBot.*WebSocket closed.*Session timed out",   # normal, every 30min
        r"rate limited.*o9cq800v",                        # transient WeChat rate limit
        r"agent\.stream_diag.*Stream drop",              # transient network, auto-retried
        r"agent\.conversation_loop.*Retrying API call",   # transient retry in progress
        r"agent\.conversation_loop.*API call failed.*attempt \d",  # transient retry
        r"agent\.tool_executor:.*returned error",        # user-facing tool errors
        r"execute_code timed out",                        # transient timeout
        r"Tool.*returned error.*exit_code",              # tool errors during normal use
        r"Gateway.*draining",                            # normal restart
        r"Gateway drain timed out",                       # normal restart timeout
        r"background task.*did not exit",                 # normal restart cleanup
        r"origin has thread_id.*delivery target lost",   # known non-actionable delivery quirk
    ]

    # ── Category patterns to classify signals ──────────────────
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
        for p in patterns:
            if re.search(p, line):
                return True
        return False

    # ── Deduplication: normalize variable parts ─────────────────
    def _normalize(line):
        """Strip timestamps, attempt counts, retry delays, thread IDs, tokens counts."""
        s = re.sub(r'\b\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+\b', 'TS', line)
        s = re.sub(r'\battempt \d\b', 'attempt N', s)
        s = re.sub(r'\bretrying in [\d.]+s\b', 'retrying in Xs', s)
        s = re.sub(r'\bThreadPoolExecutor-\d+_\d+:\d+\b', 'THREAD', s)
        s = re.sub(r'\btokens=~[\d,]+\b', 'tokens=~N', s)
        s = re.sub(r'\bmsgs=\d+\b', 'msgs=N', s)
        s = re.sub(r'\[cron_[a-f0-9]+_\d+_\d+\]', '[cron_ID_SESSION]', s)
        s = re.sub(r"Job '[^']+' failed", "Job 'NAME' failed", s)
        s = re.sub(r"thread_id=\d+", "thread_id=N", s)
        return s

    # Filter noise
    signal_lines = [l for l in lines if not _matches_any(l, NOISE)]

    # Classify
    issues = []
    classified = set()
    for cat_name, patterns in CATEGORIES:
        matches = [(i, l) for i, l in enumerate(signal_lines) if _matches_any(l, patterns)]
        if matches:
            msg_counts = defaultdict(int)
            msg_examples = {}
            for idx, m in matches:
                dedup = _normalize(m)
                msg_counts[dedup] += 1
                if dedup not in msg_examples:
                    msg_examples[dedup] = m
                classified.add(idx)
            grouped = [
                {"message": dedup, "count": cnt, "sample_line": msg_examples[dedup][:300]}
                for dedup, cnt in sorted(msg_counts.items(), key=lambda x: -x[1])
            ]
            issues.append({
                "category": cat_name,
                "total_occurrences": sum(g["count"] for g in grouped),
                "unique_messages": len(grouped),
                "details": grouped,
            })

    # Catch any remaining unclassified signals
    unclassified = [l for i, l in enumerate(signal_lines) if i not in classified]
    if unclassified:
        msg_counts = defaultdict(int)
        for m in unclassified:
            msg_counts[_normalize(m)] += 1
        grouped = [
            {"message": dedup, "count": cnt}
            for dedup, cnt in sorted(msg_counts.items(), key=lambda x: -x[1])
        ]
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


def check_searxng_unresponsive_engines():
    """SearXNG unresponsive engines — count engines blocked/errored."""
    # Check container first — if not running, don't misreport zero engines
    rc_ctr, stdout_ctr, _ = run(
        ["sudo", "docker", "inspect", "-f", "{{.State.Running}}", "searxng"],
        timeout=CMD_TIMEOUT,
    )
    if rc_ctr != 0 or stdout_ctr.strip() != "true":
        return {
            "ok": False,
            "unresponsive_count": 0,
            "blocked_count": 0,
            "unresponsive": [],
            "error": "SearXNG container not running",
        }

    rc, stdout, stderr = run(
        ["curl", "-s", "--max-time", "8", f"{SEARXNG_URL}/search?q=test&format=json&limit=1"],
        timeout=15,
    )
    unresponsive = []
    if rc == 0 and stdout:
        try:
            data = json.loads(stdout)
            raw = data.get("unresponsive_engines", [])
            for item in raw:
                if isinstance(item, list) and len(item) >= 2:
                    unresponsive.append({"engine": item[0], "reason": item[1]})
                elif isinstance(item, str):
                    unresponsive.append({"engine": item, "reason": "unknown"})
        except (json.JSONDecodeError, AttributeError):
            pass

    engines_blocked = [e for e in unresponsive if
                       any(kw in e["reason"].lower() for kw in
                           ["captcha", "suspended", "too many", "blocked", "rate limit"])]

    return {
        "ok": len(unresponsive) == 0,
        "unresponsive_count": len(unresponsive),
        "blocked_count": len(engines_blocked),
        "unresponsive": unresponsive,
        "error": stderr if rc != 0 else None,
    }


def check_searxng_health():
    """SearXNG Docker container health + JSON API check."""
    # Check if container is running
    rc, stdout, stderr = run(
        ["sudo", "docker", "inspect", "-f", "{{.State.Running}}", "searxng"],
        timeout=CMD_TIMEOUT,
    )
    container_running = rc == 0 and stdout.strip() == "true"

    # Check JSON API
    api_ok = False
    api_result_count = 0
    if container_running:
        rc_api, stdout_api, _ = run(
            ["curl", "-s", "--max-time", "8", f"{SEARXNG_URL}/search?q=test&format=json&limit=1"],
            timeout=CMD_TIMEOUT,
        )
        try:
            data = json.loads(stdout_api) if stdout_api else {}
            api_ok = "results" in data
            api_result_count = len(data.get("results", []))
        except json.JSONDecodeError:
            api_ok = False

    return {
        "container_running": container_running,
        "api_ok": api_ok,
        "api_result_count": api_result_count,
        "error": stderr if rc != 0 else None,
    }


def check_gitnexus():
    """GitNexus health — gitnexus list exits 0 on success."""
    rc, stdout, stderr = run(
        [GITNEXUS_BIN, "list"],
        timeout=CMD_TIMEOUT,
    )
    ok = rc == 0
    # Extract repo count
    repo_count = 0
    if ok and stdout:
        repos = re.findall(r"^\s{2}(\S+)", stdout, re.MULTILINE)
        repo_count = len(repos)
    return {
        "ok": ok,
        "exit_code": rc,
        "repo_count": repo_count,
        "error": stderr if not ok else None,
    }


def check_bitwarden_sm():
    """Bitwarden Secrets Manager — bws project list exits 0 on success."""
    rc, stdout, stderr = run(
        [BWS_BIN, "project", "list"],
        timeout=CMD_TIMEOUT,
    )
    ok = rc == 0
    # Count projects
    project_count = 0
    if ok and stdout:
        try:
            projects = json.loads(stdout)
            if isinstance(projects, list):
                project_count = len(projects)
        except json.JSONDecodeError:
            pass
    return {
        "ok": ok,
        "exit_code": rc,
        "project_count": project_count,
        "error": stderr if not ok else None,
    }


def main():
    timestamp = datetime.now(timezone.utc).isoformat()

    # Call npm outdated once, share across 3 consumers
    npm_cache = _get_npm_outdated()

    def _safe(name, fn, *args):
        """Run a check function; on exception return error dict instead of crashing."""
        try:
            return fn(*args)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    report = {
        "timestamp": timestamp,
        "checks": {
            "openviking_health": _safe("openviking_health", check_openviking_health),
            "openviking_logs": _safe("openviking_logs", check_openviking_logs),
            "openviking_version": _safe("openviking_version", check_openviking_version),
            "hermes_version": _safe("hermes_version", check_hermes_version),
            "hermes_updates": _safe("hermes_updates", check_hermes_updates),
            "npm_outdated": _safe("npm_outdated", check_npm_outdated, npm_cache),
            "npm_audit": _safe("npm_audit", check_npm_audit),
            "npm_freshness": _safe("npm_freshness", check_npm_freshness, npm_cache),
            "apt_upgradable": _safe("apt_upgradable", check_apt_upgradable),
            "brew_outdated": _safe("brew_outdated", check_brew_outdated),
            "searxng_health": _safe("searxng_health", check_searxng_health),
            "searxng_unresponsive_engines": _safe("searxng_unresponsive_engines", check_searxng_unresponsive_engines),
            "searxng_update": _safe("searxng_update", check_searxng_update),
            "gitnexus": _safe("gitnexus", check_gitnexus),
            "gitnexus_version": _safe("gitnexus_version", check_gitnexus_version),
            "bitwarden_sm": _safe("bitwarden_sm", check_bitwarden_sm),
            "hermes_errors": _safe("hermes_errors", check_hermes_errors),
            "socket_scan": _safe("socket_scan", check_socket_scan, npm_cache),
        },
    }

    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
