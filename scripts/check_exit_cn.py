#!/usr/bin/env python3
"""阿里云深圳 exit node (aliyun-sz-exit) 健康检查模块。

Extracted from daily-report.py, mirroring check_beryl.py's pattern.
Single entry point: check_exit_node_cn() → dict[str, Any].

严格只读 —— 本模块及其调用的一切子命令都不得执行
`tailscale set --exit-node=...`、`tailscale up` 或任何会改变 VPS/
节点网络状态的操作。2026-09-03 事故正是在 VPS 自身上执行了
`tailscale set --exit-node=` 才把 hermes 宿主机断网的，
所以这里额外校验 VPS 自身的 ExitNodeID 必须恒为空、死人开关必须在位，
而不是反过来去"修"它——修复动作只属于 ts-exitnode-deadman.sh。
完整事故复盘：engineering-wiki/troubleshooting/exit-node-cn-handoff-20260903.md
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any

# ═══════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════

ALIYUN_SZ_EXIT_HOST = "aliyun-sz-exit"
ALIYUN_SZ_EXIT_EXPIRY = "2027-09-04"  # 阿里云轻量应用服务器到期日，硬编码，到期后手动更新
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

def _get_ts_status_json() -> dict[str, Any]:
    """Get Tailscale status as JSON (local copy — daily-report.py has its own,
    duplicated the same way check_beryl.py duplicates run_cmd() rather than
    importing back from daily-report.py, to avoid a circular import)."""
    rc, ts_json, _ = run_cmd("tailscale status --json 2>/dev/null || echo '{}'", shell=True, timeout=8)
    try:
        return json.loads(ts_json)
    except json.JSONDecodeError:
        return {}


def _ssh_aliyun_sz_exit(command: str, timeout: int = 15) -> tuple[int, str, str]:
    """SSH into aliyun-sz-exit via Tailscale SSH, timeout-wrapped.

    Read-only checks ONLY — this helper must never be handed a command that
    mutates state on the node (and definitely never `tailscale set`/`up`).
    `command` is always a fixed literal built in this file, never user input
    or data returned from a prior remote call, so there is no injection
    surface to worry about — but keep it that way.
    """
    ssh_args = ["timeout", str(timeout), "ssh"]
    for opt in SSH_TIMEOUT_OPTS.split():
        ssh_args.append(opt)
    ssh_args += ["-o", "StrictHostKeyChecking=accept-new", f"root@{ALIYUN_SZ_EXIT_HOST}", command]
    return run_cmd(ssh_args, timeout=timeout + 5, shell=False)


# ═══════════════════════════════════════════
# Main check
# ═══════════════════════════════════════════

def check_exit_node_cn() -> dict[str, Any]:
    """阿里云深圳 exit node (aliyun-sz-exit) 健康检查。Single entry point.

    任何一步失败都要降级为字段值 "n/a" / False 而不是抛异常，
    因为这是每日无人值守 cron 的一部分：一个远端节点抽风不该打断整份日报。
    """
    result: dict[str, Any] = {
        "online": False,
        "tags_ok": None,
        "advertises_exit": None,
        "ts_active": "n/a",
        "ip_forward_ok": "n/a",
        "masquerade_ok": "n/a",
        "dns_ok": "n/a",
        "egress_ok": "n/a",
        "egress_city": "n/a",
        "vps_exitnode_id_empty": None,
        "deadman_cron_ok": None,
        "deadman_last_log": None,
        "renewal_days_left": None,
        "renewal_warning": "none",
    }

    # ---- 1. 节点在线 + 身份 (本机 tailscale status --json，无需 SSH) ----
    ts_json = _get_ts_status_json()
    peer = None
    for _peer_id, p in ts_json.get("Peer", {}).items():
        if p.get("HostName") == ALIYUN_SZ_EXIT_HOST:
            peer = p
            break

    if peer is None:
        result["error"] = "peer not found in tailscale status --json"
    else:
        result["online"] = bool(peer.get("Online"))
        tags = peer.get("Tags") or []
        result["tags_ok"] = "tag:exit-node-cn" in tags
        result["advertises_exit"] = bool(peer.get("ExitNodeOption"))

    # ---- 2+3. 节点侧服务健康 + 出口能力 (单次只读 SSH，全部固定字面量命令) ----
    if peer is not None and result["online"]:
        remote_cmd = (
            "echo 'TS_ACTIVE='$(systemctl is-active tailscaled 2>/dev/null); "
            "echo 'IP_FORWARD='$(sysctl -n net.ipv4.ip_forward 2>/dev/null); "
            "echo 'MASQ_COUNT='$(nft list ruleset 2>/dev/null | grep -c masquerade); "
            "echo 'DNS_OK='$(getent hosts www.bilibili.com >/dev/null 2>&1 && echo yes || echo no); "
            "echo 'EGRESS='$(curl -s -m 10 https://api.bilibili.com/x/web-interface/zone 2>/dev/null)"
        )
        # timeout=20: leaves headroom over the embedded `curl -m 10` so the
        # outer `timeout` wrapper doesn't race the inner one and kill a
        # command that was about to finish cleanly.
        rc, out, _err = _ssh_aliyun_sz_exit(remote_cmd, timeout=20)
        if rc == 0 and out:
            fields: dict[str, str] = {}
            for line in out.split("\n"):
                if "=" in line:
                    k, _, v = line.partition("=")
                    fields[k] = v
            result["ts_active"] = fields.get("TS_ACTIVE") == "active"
            result["ip_forward_ok"] = fields.get("IP_FORWARD") == "1"
            try:
                result["masquerade_ok"] = int(fields.get("MASQ_COUNT", "0")) >= 1
            except ValueError:
                result["masquerade_ok"] = False
            result["dns_ok"] = fields.get("DNS_OK") == "yes"

            egress_raw = fields.get("EGRESS", "")
            if egress_raw:
                try:
                    egress_json = json.loads(egress_raw)
                    city = (egress_json.get("data") or {}).get("city", "")
                    result["egress_city"] = city or "unknown"
                    result["egress_ok"] = "深圳" in city
                except (json.JSONDecodeError, AttributeError, TypeError):
                    result["egress_ok"] = False
                    result["egress_city"] = "parse_error"
            else:
                result["egress_ok"] = False
                result["egress_city"] = "n/a"
        else:
            result["ssh_error"] = f"SSH failed (rc={rc}) or empty output"
    elif peer is not None:
        result["ssh_error"] = "node offline, skipped SSH checks"

    # ---- 4. VPS 自身死人开关状态 (本机，只读文件/命令) ----
    # ExitNodeID 必须恒为空 —— VPS 是 hermes 宿主，绝不允许"使用"任何 exit node。
    self_data = ts_json.get("Self", {})
    vps_exit_node_id = self_data.get("ExitNodeID", "")
    result["vps_exitnode_id_empty"] = vps_exit_node_id == ""
    if vps_exit_node_id:
        # 这本身就是 2026-09-03 事故的征兆，值得在日报里显著标红。
        result["vps_exitnode_id_warning"] = (
            f"VPS ExitNodeID 非空 ({vps_exit_node_id!r})！VPS 不应使用任何 exit node，"
            "死人开关应已自动回滚，若未回滚需人工介入 tailscale set --exit-node= 清除。"
        )

    result["deadman_cron_ok"] = os.path.exists("/etc/cron.d/ts-exitnode-deadman")

    deadman_log = "/var/log/ts-exitnode-deadman.log"
    if os.path.exists(deadman_log):
        rc, tail_out, _ = run_cmd(["tail", "-2", deadman_log], timeout=5)
        result["deadman_last_log"] = tail_out or None
    else:
        result["deadman_last_log"] = None

    # ---- 5. 阿里云实例到期提醒 (静态硬编码日期，非 API 查询) ----
    try:
        expiry_date = datetime.strptime(ALIYUN_SZ_EXIT_EXPIRY, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_left = (expiry_date - datetime.now(timezone.utc)).days
        result["renewal_days_left"] = days_left
        if days_left <= 7:
            result["renewal_warning"] = "critical"
        elif days_left <= 30:
            result["renewal_warning"] = "soon"
        else:
            result["renewal_warning"] = "none"
    except ValueError:
        result["renewal_days_left"] = None
        result["renewal_warning"] = "none"
    # TODO: 阿里云 API 用量查询需要 AK/SK，第一版不做——不要在这里硬编码密钥，
    # 将来接入时必须走 Bitwarden Secrets Manager (见 daily-report.py 的
    # check_bitwarden_sm)，绝不允许把 AK/SK 明文写进这个脚本或日报 JSON 输出。

    return result
