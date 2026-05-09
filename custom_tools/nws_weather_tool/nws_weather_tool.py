"""NWS (National Weather Service) weather tools.

Uses the public NWS API at https://api.weather.gov/ — no API key required.

Configuration via environment variables:
  NWS_HOME_LAT  — home latitude  (e.g. 40.7128)
  NWS_HOME_LON  — home longitude (e.g. -74.0060)

If not set, the tools raise a clear error so the user knows what to configure.

Registers four LLM-callable tools:
- ``nws_now``       -- current conditions (temp, humidity, wind, etc.)
- ``nws_hourly``    -- hourly forecast (next 12 hours)
- ``nws_forecast``  -- 7-day forecast
- ``nws_alerts``    -- active weather alerts (blizzard, flood, etc.)
"""

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

_USER_AGENT = "Hermes-NWS-Tool/1.0"
_NWS_BASE = "https://api.weather.gov"

# Cached grid point — refreshed once per session
_CACHED_GRID: Optional[Dict[str, str]] = None


def _get_coords() -> tuple[float, float]:
    """Return (lat, lon) from NWS_HOME_LAT / NWS_HOME_LON env vars."""
    lat_s = os.getenv("NWS_HOME_LAT")
    lon_s = os.getenv("NWS_HOME_LON")
    if not lat_s or not lon_s:
        raise RuntimeError(
            "NWS_HOME_LAT and NWS_HOME_LON environment variables must be set. "
            "Example: export NWS_HOME_LAT=40.7128 NWS_HOME_LON=-74.0060"
        )
    return float(lat_s), float(lon_s)


def _get_grid() -> Dict[str, str]:
    """Return (office, grid_x, grid_y) for the configured location. Caches after first call."""
    global _CACHED_GRID
    if _CACHED_GRID is not None:
        return _CACHED_GRID

    import urllib.request

    lat, lon = _get_coords()
    url = f"{_NWS_BASE}/points/{lat},{lon}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    props = data["properties"]
    _CACHED_GRID = {
        "office": props["gridId"],
        "grid_x": str(props["gridX"]),
        "grid_y": str(props["gridY"]),
    }
    logger.info("NWS grid cached: %s", _CACHED_GRID)
    return _CACHED_GRID


def _nws_fetch(url: str) -> Dict:
    """GET a NWS API endpoint and return parsed JSON."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _emoji(shortWeather: str) -> str:
    """Return emoji for NWS short weather description."""
    w = shortWeather.lower()
    if "thunder" in w or "storm" in w:
        return "⛈️"
    if "snow" in w or "blizzard" in w:
        return "🌨️"
    if "rain" in w or "drizzle" in w or "shower" in w:
        return "🌧️"
    if "cloud" in w and "partly" in w:
        return "⛅"
    if "cloud" in w or "overcast" in w:
        return "☁️"
    if "fog" in w or "mist" in w or "haze" in w:
        return "🌫️"
    if "wind" in w:
        return "💨"
    if "hot" in w or "heat" in w:
        return "🔥"
    if "cold" in w:
        return "🥶"
    if "clear" in w or "sunny" in w:
        return "☀️"
    return "🌤️"


def _uv_label(uv: float) -> str:
    if uv is None:
        return "N/A"
    if uv <= 2:
        return "Low"
    if uv <= 5:
        return "Moderate"
    if uv <= 7:
        return "High"
    if uv <= 10:
        return "Very High"
    return "Extreme"


# ------------------------------------------------------------------
# Tool implementations (handlers receive args dict, return JSON string)
# ------------------------------------------------------------------

def _handle_nws_now(args: Dict[str, Any], **kwargs) -> str:
    """Handler for nws_now."""
    try:
        grid = _get_grid()
        # Use grid data directly for current conditions (more reliable than station)
        grid_url = f"{_NWS_BASE}/gridpoints/{grid['office']}/{grid['grid_x']},{grid['grid_y']}"
        grid_data = _nws_fetch(grid_url)

        # Get current temperature from grid data
        temp_data = grid_data.get("properties", {}).get("temperature", {})
        temp_values = temp_data.get("values", []) if isinstance(temp_data, dict) else []
        current_temp_c = None
        if temp_values:
            # Most recent valid temperature value
            for v in reversed(temp_values):
                if v.get("value") is not None:
                    current_temp_c = v["value"]
                    break

        if current_temp_c is not None:
            temp_f = round(current_temp_c * 9 / 5 + 32)
            temp_c = round(current_temp_c, 1)
            temp_str = f"{temp_f}°F ({temp_c}°C)"
        else:
            temp_str = "N/A"

        # Get weather description from the first forecast period
        forecast_url = f"{_NWS_BASE}/gridpoints/{grid['office']}/{grid['grid_x']},{grid['grid_y']}/forecast"
        forecast_data = _nws_fetch(forecast_url)
        periods = forecast_data.get("properties", {}).get("periods", [])
        first_period = periods[0] if periods else {}
        weather = first_period.get("shortForecast", "Unknown")
        emoji = _emoji(weather)

        return json.dumps({
            "weather": weather,
            "emoji": emoji,
            "temperature": temp_str,
            "source": "NWS Grid Data",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_nws_hourly(args: Dict[str, Any], **kwargs) -> str:
    """Handler for nws_hourly."""
    try:
        grid = _get_grid()
        url = f"{_NWS_BASE}/gridpoints/{grid['office']}/{grid['grid_x']},{grid['grid_y']}/forecast/hourly"
        data = _nws_fetch(url)
        periods = data.get("properties", {}).get("periods", [])[:12]

        if not periods:
            return json.dumps({"error": "No hourly forecast data available."})

        result = []
        for p in periods:
            result.append({
                "time": p.get("startTime", "")[11:16],
                "temperature": f"{p.get('temperature', 'N/A')}°{p.get('temperatureUnit', 'F')}",
                "wind": f"{p.get('windDirection', '')} {p.get('windSpeed', 'N/A')}",
                "forecast": p.get("shortForecast", "N/A"),
                "emoji": _emoji(p.get("shortForecast", "")),
            })
        return json.dumps({"hourly": result})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_nws_forecast(args: Dict[str, Any], **kwargs) -> str:
    """Handler for nws_forecast."""
    try:
        grid = _get_grid()
        url = f"{_NWS_BASE}/gridpoints/{grid['office']}/{grid['grid_x']},{grid['grid_y']}/forecast"
        data = _nws_fetch(url)
        periods = data.get("properties", {}).get("periods", [])

        if not periods:
            return json.dumps({"error": "No forecast data available."})

        result = []
        for p in periods[:7]:
            result.append({
                "name": p.get("name", ""),
                "temperature": f"{p.get('temperature', 'N/A')}°{p.get('temperatureUnit', 'F')}",
                "wind": f"{p.get('windDirection', '')} {p.get('windSpeed', 'N/A')}",
                "short_forecast": p.get("shortForecast", "N/A"),
                "detailed_forecast": p.get("detailedForecast", "N/A"),
                "emoji": _emoji(p.get("shortForecast", "")),
            })
        return json.dumps({"forecast": result})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_nws_alerts(args: Dict[str, Any], **kwargs) -> str:
    """Handler for nws_alerts."""
    try:
        lat, lon = _get_coords()
        url = f"{_NWS_BASE}/alerts/active?point={lat},{lon}"
        data = _nws_fetch(url)
        features = data.get("features", [])

        if not features:
            return json.dumps({"alerts": [], "message": "No active weather alerts for your area."})

        result = []
        for f in features[:5]:
            props = f.get("properties", {})
            severity = props.get("severity", "")
            emoji = "🔴" if severity.lower() == "extreme" else "🟠" if severity.lower() == "severe" else "🟡"
            result.append({
                "emoji": emoji,
                "event": props.get("event", "Unknown Event"),
                "severity": severity,
                "certainty": props.get("certainty", ""),
                "onset": props.get("onset", "")[:16],
                "expires": props.get("expires", "")[:16],
                "headline": props.get("headline", ""),
                "description": props.get("description", "")[:300],
            })
        return json.dumps({"alerts": result})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------

NWS_NOW_SCHEMA = {
    "name": "nws_now",
    "description": "Get current weather conditions for your configured location (temperature, humidity, wind, UV index).",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    }
}

NWS_HOURLY_SCHEMA = {
    "name": "nws_hourly",
    "description": "Get hourly weather forecast for the next 12 hours at your configured location.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    }
}

NWS_FORECAST_SCHEMA = {
    "name": "nws_forecast",
    "description": "Get 7-day weather forecast for your configured location.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    }
}

NWS_ALERTS_SCHEMA = {
    "name": "nws_alerts",
    "description": "Get active weather alerts (blizzard, flood, severe thunderstorm, etc.) for your configured location.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    }
}


# ------------------------------------------------------------------
# Registration (top-level auto-discovery)
# ------------------------------------------------------------------

from tools.registry import registry

registry.register(
    name="nws_now",
    toolset="nws_weather",
    schema=NWS_NOW_SCHEMA,
    handler=_handle_nws_now,
)
registry.register(
    name="nws_hourly",
    toolset="nws_weather",
    schema=NWS_HOURLY_SCHEMA,
    handler=_handle_nws_hourly,
)
registry.register(
    name="nws_forecast",
    toolset="nws_weather",
    schema=NWS_FORECAST_SCHEMA,
    handler=_handle_nws_forecast,
)
registry.register(
    name="nws_alerts",
    toolset="nws_weather",
    schema=NWS_ALERTS_SCHEMA,
    handler=_handle_nws_alerts,
)
