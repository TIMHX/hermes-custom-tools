#!/usr/bin/env python3
"""MiniMax endpoint health check — test both /v1 and /anthropic daily.

Tests MiniMax-M2.7 on both endpoints with the same API key.
Reports status as JSON.  Silent when everything is healthy (empty stdout
→ no message delivered).  Non-zero exit on any failure.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

API_KEY = os.environ.get("MINIMAX_CN_API_KEY", "").strip()
if not API_KEY:
    # Try reading from .env as fallback
    env_file = os.path.expanduser("~/.hermes/.env")
    if os.path.isfile(env_file):
        for line in open(env_file):
            if line.startswith("MINIMAX_CN_API_KEY="):
                API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

MODEL = "MiniMax-M2.7"
TIMEOUT = 30

def test_openai_endpoint():
    """Test /v1/chat/completions (native OpenAI-compatible)."""
    start = time.time()
    try:
        p = subprocess.run(
            [
                "curl", "-s", "--max-time", str(TIMEOUT),
                "https://api.minimaxi.com/v1/chat/completions",
                "-H", "Content-Type: application/json",
                "-H", f"Authorization: Bearer {API_KEY}",
                "-d", json.dumps({
                    "model": MODEL,
                    "messages": [{"role": "user", "content": "Say OK"}],
                    "max_tokens": 10,
                    "stream": False,
                }),
            ],
            capture_output=True, text=True, timeout=TIMEOUT + 5,
        )
        elapsed = time.time() - start
        if p.returncode != 0:
            return {"ok": False, "elapsed_s": round(elapsed, 1),
                    "error": f"curl exit {p.returncode}", "http_status": None}
        try:
            data = json.loads(p.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "elapsed_s": round(elapsed, 1),
                    "error": "invalid JSON", "raw": p.stdout[:300]}
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {
            "ok": bool(content.strip()),
            "elapsed_s": round(elapsed, 1),
            "model": data.get("model"),
            "response": content.strip()[:100],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "elapsed_s": TIMEOUT,
                "error": "timeout", "http_status": None}
    except Exception as e:
        return {"ok": False, "elapsed_s": time.time() - start,
                "error": str(e), "http_status": None}


def test_anthropic_endpoint():
    """Test /anthropic/messages (broken Anthropic-compatible)."""
    start = time.time()
    try:
        p = subprocess.run(
            [
                "curl", "-s", "--max-time", str(TIMEOUT),
                "https://api.minimaxi.com/anthropic/v1/messages",
                "-H", "Content-Type: application/json",
                "-H", "x-api-key: " + API_KEY,
                "-H", "anthropic-version: 2023-06-01",
                "-d", json.dumps({
                    "model": MODEL,
                    "max_tokens": 10,
                    "stream": True,
                    "messages": [{"role": "user", "content": "Say OK"}],
                }),
            ],
            capture_output=True, text=True, timeout=TIMEOUT + 5,
        )
        elapsed = time.time() - start
        if p.returncode != 0:
            return {"ok": False, "elapsed_s": round(elapsed, 1),
                    "error": f"curl exit {p.returncode}", "http_status": None}

        # Anthropic streaming returns SSE — check for error patterns
        stdout = p.stdout
        if "model_dump" in stdout.lower():
            return {"ok": False, "elapsed_s": round(elapsed, 1),
                    "error": "model_dump error (dict instead of model)",
                    "snippet": stdout[:200]}
        if '"type":"error"' in stdout or '"type": "error"' in stdout:
            return {"ok": False, "elapsed_s": round(elapsed, 1),
                    "error": "SSE error event", "snippet": stdout[:200]}
        if "stop_reason" in stdout or '"type":"message_stop"' in stdout:
            return {"ok": True, "elapsed_s": round(elapsed, 1)}
        if '"type":"message_delta"' in stdout:
            return {"ok": True, "elapsed_s": round(elapsed, 1)}
        if "HTTP 404" in stdout or "Not Found" in stdout:
            return {"ok": False, "elapsed_s": round(elapsed, 1),
                    "error": "HTTP 404 — endpoint not found"}
        return {"ok": False, "elapsed_s": round(elapsed, 1),
                "error": "unrecognized response", "snippet": stdout[:200]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "elapsed_s": TIMEOUT,
                "error": "timeout", "http_status": None}
    except Exception as e:
        return {"ok": False, "elapsed_s": time.time() - start,
                "error": str(e), "http_status": None}


def main():
    now = datetime.now(timezone.utc).isoformat()
    openai_result = test_openai_endpoint()
    anthropic_result = test_anthropic_endpoint()

    report = {
        "timestamp": now,
        "model": MODEL,
        "openai_v1": openai_result,
        "anthropic": anthropic_result,
    }

    # Build human-readable status line
    ov = "✅" if openai_result["ok"] else "❌"
    av = "✅" if anthropic_result["ok"] else "❌"
    oe = f" — {openai_result.get('error', '')}" if not openai_result["ok"] else ""
    ae = f" — {anthropic_result.get('error', '')}" if not anthropic_result["ok"] else ""
    status_line = f"MiniMax {MODEL}: /v1 {ov}{oe} | /anthropic {av}{ae}"

    # Print to stdout for delivery
    print(f"## 🤖 MiniMax 端点监测 — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print()
    print(f"| 端点 | 状态 | 延迟 | 详情 |")
    print(f"|------|------|------|------|")
    print(f"| /v1/chat/completions | {ov} | {openai_result.get('elapsed_s','?')}s | {openai_result.get('response', openai_result.get('error',''))} |")
    print(f"| /anthropic | {av} | {anthropic_result.get('elapsed_s','?')}s | {anthropic_result.get('error', anthropic_result.get('response',''))} |")
    print()
    print(f"> {status_line}")

    # Exit code signals failure to cron scheduler
    all_ok = openai_result["ok"] and anthropic_result["ok"]
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
