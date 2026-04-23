# Hermes Agent — Custom Tools

Custom tool integrations for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

## NWS Weather Tool

**Location:** `custom_tools/nws_weather_tool/`

Weather data for **Trenton, NJ** using the public National Weather Service API — no API key required.

**Home address:** [REDACTED]

### Tools Provided

| Tool | Description |
|------|-------------|
| `nws_now` | Current conditions (temperature, weather description) |
| `nws_hourly` | Hourly forecast — next 12 hours |
| `nws_forecast` | 7-day forecast |
| `nws_alerts` | Active weather alerts for location |

### Installation

#### Step 1 — Copy the tool file

```bash
cp custom_tools/nws_weather_tool/nws_weather_tool.py \
   ~/.hermes/hermes-agent/tools/nws_weather_tool.py
```

#### Step 2 — Patch `toolsets.py`

Edit `~/.hermes/hermes-agent/toolsets.py`:

**A) Add to `_HERMES_CORE_TOOLS`** (around line 60, after `ha_call_service`):
```python
    # NWS weather (Trenton NJ — no API key needed)
    "nws_now", "nws_hourly", "nws_forecast", "nws_alerts",
```

**B) Add `nws_weather` toolset definition** (after `homeassistant` definition, around line 200):
```python
    "nws_weather": {
        "description": "NWS weather for Trenton NJ — current conditions, hourly, 7-day forecast, and alerts",
        "tools": ["nws_now", "nws_hourly", "nws_forecast", "nws_alerts"],
        "includes": []
    },
```

#### Step 3 — Verify

```bash
cd ~/.hermes/hermes-agent && python3 -c "
import sys; sys.path.insert(0, '.')
from tools.registry import discover_builtin_tools, registry
discover_builtin_tools()
nws = [n for n in registry.get_all_tool_names() if 'nws' in n]
print('NWS tools:', nws)
"
```

Expected output: `['nws_alerts', 'nws_forecast', 'nws_hourly', 'nws_now']`

### NWS API Endpoints Used

| Purpose | Endpoint |
|---------|----------|
| Grid point lookup | `GET /points/{lat},{lon}` |
| Grid data (temp, etc.) | `GET /gridpoints/{office}/{gridX},{gridY}` |
| 7-day forecast | `GET /gridpoints/{office}/{gridX},{gridY}/forecast` |
| Hourly forecast | `GET /gridpoints/{office}/{gridX},{gridY}/forecast/hourly` |
| Active alerts | `GET /alerts/active?point={lat},{lon}` |

Base URL: `https://api.weather.gov/` — no API key needed.

### Troubleshooting

```bash
# Test tool directly
cd ~/.hermes/hermes-agent && python3 -c "
import sys; sys.path.insert(0, '.')
import tools.nws_weather_tool
from tools.registry import registry
entry = registry.get_entry('nws_now')
print(entry.handler({}, task_id='test'))
"
```

---

## How Tools Work in Hermes Agent

### Auto-discovery

Any `tools/*.py` in `~/.hermes/hermes-agent/tools/` with a **top-level** `registry.register()` call is auto-discovered at startup via `discover_builtin_tools()` in `tools/registry.py`. No manual import needed.

### Registration signature

```python
from tools.registry import registry

registry.register(
    name="my_tool",
    toolset="my_toolset",
    schema=MY_TOOL_SCHEMA,
    handler=my_handler,          # receives (args: dict, **kwargs)
    check_fn=my_check_fn,         # optional: returns bool
    requires_env=[],              # optional env var names
    is_async=False,
)
```

**Important:** The handler function must return a **JSON string** (`json.dumps(...)`). Errors should be returned as `{"error": "message"}`.

### Schema format

```python
MY_TOOL_SCHEMA = {
    "name": "my_tool",
    "description": "What this tool does.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": []
    }
}
```

### Handler pattern

```python
def _handle_my_tool(args: dict, **kwargs) -> str:
    """Handler receives args dict, returns JSON string."""
    try:
        result = do_something(args.get("param"))
        return json.dumps({"result": result})
    except Exception as e:
        return json.dumps({"error": str(e)})
```

### Toolset must exist

If `registry.register(toolset="my_toolset", ...)` is used but `"my_toolset"` is not defined in `toolsets.py`, the tool still registers but may not be accessible via normal toolset resolution. Always define the toolset in `toolsets.py`.

### Hermes update gotcha

When Hermes updates, `~/.hermes/hermes-agent/tools/` **persists** (your custom tool files survive), but `toolsets.py` may be overwritten. Re-apply the `toolsets.py` changes after each Hermes update.

---

## Cron Jobs Using These Tools

| Job | Schedule | Tool |
|-----|----------|------|
| Daily Morning Weather | 08:00 daily | `nws_now` |
| Severe Weather Alert | 07:00 & 18:00 daily | `nws_alerts` |
| Daily Briefing | 08:00 daily | `nws_now` (in briefing) |

These jobs reference `nws_weather_tool` as a skill (skill name = toolset name).
