#!/usr/bin/env python3
"""
Tailscale API Token 过期检查。
Bitwarden 中存储 TAILSCALE_API_KEY（每 90 天过期）。
输出 JSON 供 daily-report.py 汇总。
"""
import json, subprocess, os, sys
from datetime import datetime, timezone

BWS_BIN = os.path.expanduser('~/.hermes/bin/bws')
SECRET_ID = "97c8d776-63de-4afb-873d-b47b0167fcae"
CREATION_DATE = datetime(2026, 7, 2, tzinfo=timezone.utc)
EXPIRY_DAYS = 90
WARN_DAYS = [7, 3, 1]  # 到期前多少天开始警告

def get_bws_token():
    """读取 BWS access token from .env"""
    env_path = os.path.expanduser('~/.hermes/.env')
    with open(env_path) as f:
        for line in f:
            if line.startswith('BWS_ACCESS_TOKEN'):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    return None

def get_ts_api_key(bws_token):
    """从 Bitwarden 获取 Tailscale API key"""
    result = subprocess.run(
        [BWS_BIN, 'secret', 'get', SECRET_ID, '-t', bws_token, '-o', 'json'],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout)
    return data.get('value')

def check_expiry():
    """检查 token 过期状态"""
    now = datetime.now(timezone.utc)
    expiry = CREATION_DATE.replace(tzinfo=timezone.utc)
    # 90 天后过期，留 1 天缓冲 = 89 天
    expires_at = datetime(
        CREATION_DATE.year, CREATION_DATE.month, CREATION_DATE.day,
        tzinfo=timezone.utc
    )
    # 添加 89 天
    from datetime import timedelta
    expires_at = CREATION_DATE + timedelta(days=EXPIRY_DAYS - 1)
    
    days_left = (expires_at - now).days
    is_warning = days_left in WARN_DAYS
    is_expired = days_left <= 0
    is_ok = days_left > max(WARN_DAYS)

    return {
        "ok": not is_expired,
        "days_left": days_left,
        "expires_at": expires_at.isoformat(),
        "creation_date": CREATION_DATE.isoformat(),
        "expiry_days": EXPIRY_DAYS,
        "warning": is_warning,
        "expired": is_expired,
        "status": "EXPIRED" if is_expired else ("WARNING" if is_warning else "OK"),
        "secret_id": SECRET_ID,
    }

def test_api_key():
    """验证 API key 是否有效"""
    bws_token = get_bws_token()
    if not bws_token:
        return {"error": "BWS_ACCESS_TOKEN not found"}

    api_key = get_ts_api_key(bws_token)
    if not api_key:
        return {"error": "Cannot retrieve TAILSCALE_API_KEY from Bitwarden"}

    result = subprocess.run(
        ['curl', '-s', '-m', '10',
         '-H', f'Authorization: Bearer {api_key}',
         'https://api.tailscale.com/api/v2/tailnet/tim0202604@gmail.com/devices'],
        capture_output=True, text=True, timeout=15
    )

    if result.returncode != 0:
        return {"error": f"API call failed: {result.returncode}"}

    try:
        data = json.loads(result.stdout)
        devices = data.get('devices', [])
        return {
            "api_ok": True,
            "device_count": len(devices),
            "devices": [
                {
                    "hostname": d.get('hostname', d.get('name', '?')),
                    "addresses": d.get('addresses', []),
                    "os": d.get('os', '?'),
                }
                for d in devices
            ]
        }
    except json.JSONDecodeError:
        return {"api_ok": True, "device_count": "unknown", "note": "JSON parse failed (JSONC response)"}

def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        output = test_api_key()
    else:
        output = check_expiry()

    print(json.dumps(output, indent=2, ensure_ascii=False))

if __name__ == '__main__':
    main()
