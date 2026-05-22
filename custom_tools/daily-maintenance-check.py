#!/usr/bin/env python3
"""Daily maintenance data-collection script.

Runs 9 checks (+ 1 optional Socket scan), outputs JSON to stdout.
Designed for cron: `no_agent=true, script=...` watchdog pattern.
Silent on empty output paths (no updates, no warnings) — the downstream
LLM agent processes the JSON.

Checks:
  1. OpenViking health            — GET http://127.0.0.1:1933/health
  2. OpenViking logs              — tail ~/.openviking/data/log/openviking.log, grep WARNING|ERROR
  3. Hermes version               — git describe --tags
  4. Hermes pending updates       — git fetch myfork; git log HEAD..myfork/main --oneline (max 20)
  5. npm global outdated          — npm outdated -g --json
  6. npm audit (global)           — npm audit --json (CVE + dependency vulnerabilities)
  7. npm publish freshness        — check if latest versions were published recently (<7d)
  8. APT security updates         — apt list --upgradable | grep -i security
  9. Homebrew outdated            — brew outdated (graceful skip if brew absent)
 10. Socket.dev scan              — supply-chain scan on outdated packages (optional)
 11. SearXNG health               — Docker container running + JSON API responding
 12. Hermes error log analysis    — detect persistent config/service errors from errors.log
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERMES_REPO = "/home/xing/.hermes/hermes-agent"
OV_HEALTH_URL = "http://127.0.0.1:1933/health"
SEARXNG_URL = "http://127.0.0.1:8888"
CMD_TIMEOUT = 30  # seconds for most commands
GIT_FETCH_TIMEOUT = 60  # git fetch can be slower
BREW_BIN = "/home/linuxbrew/.linuxbrew/bin/brew"
SOCKET_BIN = "/home/xing/.nvm/versions/node/v24.15.0/bin/socket"
SOCKET_TOKEN_FILE = "/home/xing/.hermes/.socket-token"


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
    """1. OpenViking health check."""
    rc, stdout, stderr = run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10", OV_HEALTH_URL])
    ok = rc == 0 and stdout == "200"
    return {
        "ok": ok,
        "http_code": stdout if rc == 0 else None,
        "error": stderr if not ok else None,
    }


def check_openviking_logs():
    """2. OpenViking logs from ~/.openviking/data/log/openviking.log. Grep WARNING/ERROR."""
    log_file = "/home/xing/.openviking/data/log/openviking.log"
    rc, stdout, stderr = run(
        f"tail -200 {log_file} 2>/dev/null | grep -iE 'WARNING|ERROR' || true",
        shell=True,
    )
    lines = [l for l in stdout.split("\n") if l.strip()] if stdout else []
    return {
        "source": f"{log_file} (last 200 lines)",
        "warnings_errors": lines,
        "count": len(lines),
    }


def check_hermes_version():
    """3. Hermes version via git describe --tags."""
    rc, stdout, stderr = run(
        ["git", "describe", "--tags", "--always", "--dirty"],
        cwd=HERMES_REPO,
    )
    return {
        "version": stdout if rc == 0 else None,
        "error": stderr if rc != 0 else None,
    }


def check_hermes_updates():
    """4. Fetch myfork and list pending commits."""
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
    commits = [l for l in stdout_log.split("\n") if l.strip()] if stdout_log else []
    return {
        "fetch_ok": True,
        "pending_commits": commits,
        "count": len(commits),
    }


def check_npm_outdated():
    """5. npm global outdated packages."""
    rc, stdout, stderr = run(
        ["npm", "outdated", "-g", "--json"],
        timeout=CMD_TIMEOUT,
    )
    try:
        data = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        data = {"_parse_error": stdout[:500] if stdout else "empty"}
    return {
        "ok": rc == 0 or rc == 1,  # npm outdated exits 1 when packages ARE outdated
        "outdated": data,
        "error": stderr if rc > 1 else None,
    }


def check_npm_audit():
    """6. npm audit — known CVEs in global dependency tree."""
    rc, stdout, stderr = run(
        ["npm", "audit", "--json"],
        timeout=CMD_TIMEOUT * 2,  # audit can be slower
    )
    try:
        data = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        data = {"_parse_error": stdout[:500] if stdout else "empty"}
    # Extract summary
    vulns = data.get("vulnerabilities", {}) if isinstance(data, dict) else {}
    summary = {
        "info": 0,
        "low": 0,
        "moderate": 0,
        "high": 0,
        "critical": 0,
    }
    for pkg, info in vulns.items():
        if isinstance(info, dict):
            for via in info.get("via", []):
                if isinstance(via, dict):
                    sev = via.get("severity", "")
                    if sev in summary:
                        summary[sev] += 1
    return {
        "total_vulnerabilities": sum(summary.values()),
        "summary": summary,
        "full_report": data if isinstance(data, dict) else {"raw": str(data)[:2000]},
        "error": stderr if rc > 1 else None,
    }


def check_npm_freshness():
    """7. Check if any global package's 'latest' version was published recently (<7d)."""
    rc, stdout, stderr = run(
        ["npm", "outdated", "-g", "--json"],
        timeout=CMD_TIMEOUT,
    )
    try:
        outdated = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        return {"ok": False, "fresh_packages": [], "error": "npm outdated parse error"}

    fresh_packages = []
    for pkg_name, info in outdated.items():
        if not isinstance(info, dict):
            continue
        latest_ver = info.get("latest", "")
        if not latest_ver:
            continue

        rc_reg, reg_stdout, reg_stderr = run(
            f"npm view {pkg_name}@latest time --json 2>/dev/null",
            shell=True,
            timeout=CMD_TIMEOUT,
        )
        try:
            time_data = json.loads(reg_stdout) if reg_stdout else {}
        except json.JSONDecodeError:
            continue

        publish_time = None
        if isinstance(time_data, dict):
            publish_time = time_data.get(latest_ver) or time_data.get("modified")

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


def check_apt_security():
    """8. APT security-update packages."""
    rc, stdout, stderr = run(
        "apt list --upgradable 2>/dev/null | grep -i security || true",
        shell=True,
        timeout=CMD_TIMEOUT,
    )
    lines = [l for l in stdout.split("\n") if l.strip()] if stdout else []
    return {
        "source": "apt list --upgradable | grep -i security",
        "packages": lines,
        "count": len(lines),
    }


def check_socket_scan():
    """10. Socket.dev supply-chain scan on outdated global npm packages."""
    try:
        with open(SOCKET_TOKEN_FILE) as f:
            token = f.read().strip()
    except (FileNotFoundError, PermissionError):
        return {"ok": False, "skipped": True, "reason": "no token file"}

    if not os.path.isfile(SOCKET_BIN):
        return {"ok": False, "skipped": True, "reason": "socket CLI not found"}

    # Get outdated packages
    rc, stdout, _ = run(["npm", "outdated", "-g", "--json"], timeout=CMD_TIMEOUT)
    try:
        outdated = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        return {"ok": False, "skipped": True, "reason": "npm outdated parse error"}

    if not outdated:
        return {"ok": True, "alerts": [], "scanned": 0}

    alerts = []
    scanned = 0
    SOCKET_TIMEOUT = 20
    merged_env = os.environ.copy()
    merged_env["PATH"] = "/home/linuxbrew/.linuxbrew/bin:" + merged_env.get("PATH", "")
    merged_env["SOCKET_SECURITY_API_TOKEN"] = token

    for pkg_name in outdated:
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
                pkg_summary.setdefault("top_alert", items[0].get("type", "unknown"))

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
    """9. Homebrew outdated packages (skip if brew not installed)."""
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


def check_hermes_errors():
    """12. Hermes error log analysis — detect persistent configuration/service errors.

    Reads the last 24h of errors.log, filters out known transient noise, and
    groups remaining warnings/errors by category.
    """
    import re
    from collections import defaultdict

    log_file = "/home/xing/.hermes/logs/errors.log"
    today = datetime.now().strftime("%Y-%m-%d")

    # Grep today's WARNING/ERROR lines
    rc, stdout, stderr = run(
        f"grep '{today}' {log_file} 2>/dev/null | grep -iE 'WARNING|ERROR' || true",
        shell=True,
        timeout=CMD_TIMEOUT,
    )

    if not stdout.strip():
        return {"ok": True, "issues": [], "note": "no warnings/errors today"}

    lines = stdout.strip().split("\n")

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
        matches = [l for l in signal_lines if _matches_any(l, patterns)]
        if matches:
            msg_counts = defaultdict(int)
            msg_examples = {}
            for m in matches:
                dedup = _normalize(m)
                msg_counts[dedup] += 1
                if dedup not in msg_examples:
                    msg_examples[dedup] = m
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
            for m in matches:
                classified.add(id(m))

    # Catch any remaining unclassified signals
    unclassified = [l for l in signal_lines if id(l) not in classified]
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


def check_searxng_health():
    """11. SearXNG Docker container health + JSON API check."""
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
            f"curl -s --max-time 8 '{SEARXNG_URL}/search?q=test&format=json&limit=1'",
            shell=True,
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


def main():
    timestamp = datetime.now(timezone.utc).isoformat()

    report = {
        "timestamp": timestamp,
        "checks": {
            "openviking_health": check_openviking_health(),
            "openviking_logs": check_openviking_logs(),
            "hermes_version": check_hermes_version(),
            "hermes_updates": check_hermes_updates(),
            "npm_outdated": check_npm_outdated(),
            "npm_audit": check_npm_audit(),
            "npm_freshness": check_npm_freshness(),
            "apt_security": check_apt_security(),
            "brew_outdated": check_brew_outdated(),
            "searxng_health": check_searxng_health(),
            "hermes_errors": check_hermes_errors(),
            "socket_scan": check_socket_scan(),
        },
    }

    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
