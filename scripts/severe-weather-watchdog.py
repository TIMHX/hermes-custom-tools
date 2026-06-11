#!/usr/bin/env python3
"""Severe weather watchdog.
Queries NWS API. Outputs alert on severe weather; silent otherwise.
Location is read from NWS_HOME_LAT/LON env vars or ~/.hermes/config/nws_profiles.json.
Designed for hermes-agent no_agent cron mode."""

import json
import os
import sys
import time
import urllib.request
import urllib.error

# ── Location (3-level fallback: env vars → NWS_PROFILE → default profile) ──
_CONFIG_PATH = os.path.expanduser("~/.hermes/config/nws_profiles.json")
_LOCATION_NAME = "Unknown"


def _load_coords() -> tuple[float, float]:
    """Return (lat, lon), also sets global _LOCATION_NAME."""
    global _LOCATION_NAME

    # Level 1: Direct env var override
    lat_s = os.getenv("NWS_HOME_LAT")
    lon_s = os.getenv("NWS_HOME_LON")
    if lat_s and lon_s:
        _LOCATION_NAME = os.getenv("NWS_LOCATION_NAME", "Configured Location")
        return float(lat_s), float(lon_s)

    # Level 2-3: Profile config
    try:
        with open(_CONFIG_PATH) as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(
            f"[FATAL] NWS_HOME_LAT/LON not set and {_CONFIG_PATH} not found.",
            file=sys.stderr,
        )
        sys.exit(1)

    profile_name = os.getenv("NWS_PROFILE", config.get("default", "trenton"))
    profiles = config.get("profiles", {})
    p = profiles.get(profile_name)
    if not p:
        available = ", ".join(profiles.keys())
        print(f"[FATAL] Profile '{profile_name}' not found. Available: {available}", file=sys.stderr)
        sys.exit(1)

    _LOCATION_NAME = p.get("name", profile_name)
    return p["lat"], p["lon"]


LAT, LON = _load_coords()
USER_AGENT = "hermes-weather-watchdog/1.0"
TIMEOUT = 15
MAX_RETRIES = 2

# ── thresholds ──
PRECIP_THRESHOLD = 40        # % probability
SEVERE_KEYWORDS = [
    "thunderstorm", "winter storm", "blizzard", "freeze",
    "excessive heat", "tornado", "hurricane", "flood",
    "ice storm", "hail", "extreme cold", "extreme heat",
    "wind chill", "heat advisory", "severe",
]
ALERT_SEVERITIES = ["Extreme", "Severe", "Moderate"]

# ── helpers ──

def nws_get(url: str) -> dict:
    """GET from NWS API with retry and User-Agent."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/geo+json",
    })
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 503:
                time.sleep(2 ** attempt)
                continue
            print(f"[ERROR] NWS HTTP {e.code} for {url}", file=sys.stderr)
            return {}
        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(2)
                continue
            print(f"[ERROR] NWS fetch failed: {e}", file=sys.stderr)
            return {}
    return {}


def check_alerts() -> list[str]:
    """Return list of severe alert headlines."""
    url = f"https://api.weather.gov/alerts/active?point={LAT},{LON}"
    data = nws_get(url)
    if not data or "features" not in data:
        return []

    alerts = []
    for feat in data["features"]:
        props = feat.get("properties", {})
        severity = props.get("severity", "")
        headline = props.get("headline") or props.get("event", "Unknown alert")
        desc = props.get("description", "")[:300]
        if severity in ALERT_SEVERITIES:
            alerts.append(f"**{headline}** ({severity})\n{desc}")
    return alerts


def check_forecast() -> str | None:
    """Return formatted forecast alert if bad weather detected, else None."""
    # Get grid endpoint
    points_url = f"https://api.weather.gov/points/{LAT},{LON}"
    points = nws_get(points_url)
    if not points or "properties" not in points:
        print("[ERROR] Failed to get gridpoint", file=sys.stderr)
        return None

    forecast_url = points["properties"].get("forecast")
    if not forecast_url:
        print("[ERROR] No forecast URL in gridpoint response", file=sys.stderr)
        return None

    forecast = nws_get(forecast_url)
    if not forecast or "properties" not in forecast:
        print("[ERROR] Failed to get forecast", file=sys.stderr)
        return None

    periods = forecast["properties"].get("periods", [])
    if not periods:
        return None

    # Check next 4 periods (~48 hours)
    bad_periods = []
    for p in periods[:4]:
        name = p.get("name", "")
        detail = p.get("detailedForecast", "")
        short = p.get("shortForecast", "")
        combined = f"{short} {detail}".lower()
        temp = p.get("temperature", 0)
        precip = p.get("probabilityOfPrecipitation", {}).get("value") or 0
        wind = p.get("windSpeed", "")

        # Check thresholds
        severe = False
        reasons = []

        if isinstance(precip, (int, float)) and precip > PRECIP_THRESHOLD:
            severe = True
            reasons.append(f"降水概率 {int(precip)}%")

        for kw in SEVERE_KEYWORDS:
            if kw in combined:
                severe = True
                reasons.append(kw.title())
                break  # one keyword match is enough

        if temp <= 25:
            severe = True
            reasons.append(f"极低温 {temp}°F")
        elif temp >= 100:
            severe = True
            reasons.append(f"极高温 {temp}°F")

        if severe:
            bad_periods.append({
                "name": name,
                "short": short,
                "temp": temp,
                "precip": int(precip) if precip else 0,
                "reasons": reasons,
            })

    if not bad_periods:
        return None

    # Format output
    lines = [f"⚠️ **恶劣天气提醒 — {_LOCATION_NAME}**\n"]
    for bp in bad_periods:
        reasons_str = " · ".join(bp["reasons"])
        lines.append(
            f"**{bp['name']}:** {bp['short']} — "
            f"{bp['temp']}°F | {reasons_str}"
        )

    # Add action recommendation
    all_text = " ".join(bp["short"].lower() for bp in bad_periods)
    if "rain" in all_text or "shower" in all_text:
        lines.append("\n🌂 记得带伞！")
    if "snow" in all_text or "winter" in all_text:
        lines.append("\n🧣 注意保暖，路面可能结冰。")
    if "thunderstorm" in all_text:
        lines.append("\n⚡ 雷暴天气，减少户外活动。")
    if "heat" in all_text or any(bp["temp"] >= 95 for bp in bad_periods):
        lines.append("\n🥵 高温预警，多喝水避免中暑。")
    if "wind" in all_text:
        lines.append("\n💨 大风天气，注意固定户外物品。")

    return "\n".join(lines)


def main():
    alerts = check_alerts()
    forecast_alert = check_forecast()

    if alerts:
        print("🚨 **NWS 官方警报**\n")
        for a in alerts:
            print(a)
            print()

    if forecast_alert:
        if alerts:
            print("---\n")
        print(forecast_alert)

    # If both empty, script prints nothing → silent delivery


if __name__ == "__main__":
    main()
