#!/usr/bin/env python3
"""Daily morning weather report.
Queries NWS API directly. Outputs a formatted weather report to stdout.
Location from NWS_HOME_LAT/LON env vars or ~/.hermes/config/nws_profiles.json.
Designed for hermes-agent no_agent cron mode.
"""

import os
import sys
import time

from nws_utils import load_coords, nws_get, emoji_for_weather, get_grid_point, NWS_BASE

_USER_AGENT = "hermes-daily-weather/1.0"
_TIMEOUT = 15
_MAX_RETRIES = 2

_LOCATION_NAME, LAT, LON = load_coords()


def _get_current(grid: dict) -> str:
    """Return formatted current conditions line."""
    url = f"{NWS_BASE}/gridpoints/{grid['office']}/{grid['grid_x']},{grid['grid_y']}"
    data = nws_get(url)
    if not data:
        return ""

    props = data.get("properties", {})

    # Temperature from grid values
    temp_values = props.get("temperature", {}).get("values", [])
    temp_f = None
    for v in reversed(temp_values):
        if v.get("value") is not None:
            temp_f = round(v["value"] * 9 / 5 + 32)
            break

    # Weather from first forecast period
    forecast_url = f"{NWS_BASE}/gridpoints/{grid['office']}/{grid['grid_x']},{grid['grid_y']}/forecast"
    fc_data = nws_get(forecast_url)
    periods = fc_data.get("properties", {}).get("periods", [])
    first = periods[0] if periods else {}
    weather = first.get("shortForecast", "Unknown")
    em = emoji_for_weather(weather)

    # Humidity
    humidity = props.get("relativeHumidity", {}).get("values", [])
    rh = None
    for v in reversed(humidity):
        if v.get("value") is not None:
            rh = round(v["value"])
            break

    # Wind
    wind_speed = first.get("windSpeed", "")
    wind_dir = first.get("windDirection", "")

    parts = []
    if temp_f is not None:
        parts.append(f"🌡️ {temp_f}°F")
    if weather != "Unknown":
        parts.append(f"{em} {weather}")
    if rh is not None:
        parts.append(f"💧 湿度 {rh}%")
    if wind_speed:
        parts.append(f"💨 {wind_dir} {wind_speed}" if wind_dir else f"💨 {wind_speed}")

    return "  ".join(parts)


def _get_forecast_periods(grid: dict) -> list:
    """Return formatted forecast periods (today's two + next 4 days)."""
    url = f"{NWS_BASE}/gridpoints/{grid['office']}/{grid['grid_x']},{grid['grid_y']}/forecast"
    data = nws_get(url)
    if not data:
        return []
    return data.get("properties", {}).get("periods", [])


def _get_alerts() -> list:
    """Return active alert headlines."""
    url = f"{NWS_BASE}/alerts/active?point={LAT},{LON}"
    data = nws_get(url)
    if not data or "features" not in data:
        return []

    alerts = []
    for feat in data["features"]:
        props = feat.get("properties", {})
        headline = props.get("headline") or props.get("event", "Unknown alert")
        severity = props.get("severity", "")
        sev_emoji = "🔴" if severity.lower() == "extreme" else "🟠" if severity.lower() == "severe" else "🟡"
        alerts.append(f"{sev_emoji} **{headline}** ({severity})")
    return alerts


def _get_recommendations(periods: list) -> list:
    """Generate recommendations based on forecast."""
    recs = []
    all_text = " ".join(
        (p.get("shortForecast", "") + " " + p.get("detailedForecast", "")).lower()
        for p in periods[:4]
    )
    temps = [p.get("temperature", 0) for p in periods[:4] if p.get("temperature") is not None]

    if "rain" in all_text or "shower" in all_text:
        recs.append("🌂 今天可能下雨，记得带伞")
    if "snow" in all_text or "winter" in all_text:
        recs.append("🧣 注意保暖，路面可能结冰")
    if "thunderstorm" in all_text:
        recs.append("⚡ 雷暴预警，减少户外活动")
    if temps and max(temps) >= 95:
        recs.append("🥵 高温预警，多喝水避免中暑")
    if temps and min(temps) <= 25:
        recs.append("🥶 极寒天气，注意保暖")
    if "wind" in all_text:
        recs.append("💨 大风天气，注意固定户外物品")

    return recs


def main():
    grid = get_grid_point(LAT, LON)

    # ── Header ──
    from datetime import datetime
    now = datetime.now()
    print(f"☀️ **{_LOCATION_NAME} 天气 | {now.strftime('%Y年%m月%d日 %A')}**")
    print()

    # ── Current Conditions ──
    current = _get_current(grid)
    if current:
        print("**📍 当前天气**")
        print(current)
        print()

    # ── Forecast ──
    periods = _get_forecast_periods(grid)
    if periods:
        print("**📅 天气预报**")
        shown_dates = set()
        count = 0
        for p in periods:
            if count >= 7:
                break
            name = p.get("name", "")
            temp = p.get("temperature", "N/A")
            unit = p.get("temperatureUnit", "F")
            short = p.get("shortForecast", "")
            em = emoji_for_weather(short)

            # Deduplicate same date periods (day + night share date)
            date_key = name.split(" ")[0] if " " in name else name
            if date_key in shown_dates and "Night" in name:
                print(f"  {em} **{name}:** {short} — {temp}°{unit}")
                count += 1
            elif date_key not in shown_dates:
                shown_dates.add(date_key)
                print(f"  {em} **{name}:** {short} — {temp}°{unit}")
                count += 1
        print()

    # ── Alerts ──
    alerts = _get_alerts()
    if alerts:
        print("**🚨 天气警报**")
        for a in alerts:
            print(f"  {a}")
        print()

    # ── Recommendations ──
    recs = _get_recommendations(periods)
    if recs:
        print("**💡 出行建议**")
        for r in recs:
            print(r)
    else:
        print("**💡 出行建议**")
        print("  天气不错，适合出门 😎")


if __name__ == "__main__":
    main()
