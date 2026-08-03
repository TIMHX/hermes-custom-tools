#!/usr/bin/env python3
"""Shared NWS API utilities for weather scripts.
Extracted from daily-weather.py and severe-weather-watchdog.py.
Import from sibling scripts in ~/.hermes/scripts/.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

NWS_BASE = "https://api.weather.gov"
CONFIG_PATH = os.path.expanduser("~/.hermes/config/nws_profiles.json")


def load_coords(
    lat_env: str = "NWS_HOME_LAT",
    lon_env: str = "NWS_HOME_LON",
    name_env: str = "NWS_LOCATION_NAME",
    profile_env: str = "NWS_PROFILE",
    default_profile: str = "trenton",
) -> tuple[str, float, float]:
    """Return (location_name, lat, lon). 3-level fallback: env vars → profile → default."""
    lat_s = os.getenv(lat_env)
    lon_s = os.getenv(lon_env)
    if lat_s and lon_s:
        name = os.getenv(name_env, "Configured Location")
        return name, float(lat_s), float(lon_s)

    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(
            f"[FATAL] {lat_env}/{lon_env} not set and {CONFIG_PATH} not found.",
            file=sys.stderr,
        )
        sys.exit(1)

    profile_name = os.getenv(profile_env, config.get("default", default_profile))
    profiles = config.get("profiles", {})
    p = profiles.get(profile_name)
    if not p:
        available = ", ".join(profiles.keys())
        print(
            f"[FATAL] Profile '{profile_name}' not found. Available: {available}",
            file=sys.stderr,
        )
        sys.exit(1)

    name = p.get("name", profile_name)
    return name, p["lat"], p["lon"]


def nws_get(url: str, user_agent: str = "hermes-nws/1.0", timeout: int = 15, max_retries: int = 2) -> dict:
    """GET from NWS API with retry and User-Agent. Returns {} on failure."""
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "application/geo+json",
    })
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 503:
                time.sleep(2 ** attempt)
                continue
            print(f"[ERROR] NWS HTTP {e.code} for {url}", file=sys.stderr)
            return {}
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2)
                continue
            print(f"[ERROR] NWS fetch failed: {e}", file=sys.stderr)
            return {}
    return {}


def emoji_for_weather(short_weather: str) -> str:
    """Map NWS shortForecast to emoji."""
    w = short_weather.lower()
    if "thunder" in w or "storm" in w: return "⛈️"
    if "snow" in w or "blizzard" in w: return "🌨️"
    if "rain" in w or "drizzle" in w or "shower" in w: return "🌧️"
    if "cloud" in w and "partly" in w: return "⛅"
    if "cloud" in w or "overcast" in w: return "☁️"
    if "fog" in w or "mist" in w or "haze" in w: return "🌫️"
    if "wind" in w: return "💨"
    if "hot" in w or "heat" in w: return "🔥"
    if "cold" in w: return "🥶"
    if "clear" in w or "sunny" in w: return "☀️"
    return "🌤️"


def get_grid_point(lat: float, lon: float, user_agent: str = "hermes-nws/1.0") -> dict:
    """Return {office, grid_x, grid_y} for a lat/lon. Calls sys.exit(1) on failure."""
    url = f"{NWS_BASE}/points/{lat},{lon}"
    data = nws_get(url, user_agent=user_agent)
    if not data or "properties" not in data:
        print("[ERROR] Failed to get grid point", file=sys.stderr)
        sys.exit(1)
    props = data["properties"]
    return {"office": props["gridId"], "grid_x": props["gridX"], "grid_y": props["gridY"]}
