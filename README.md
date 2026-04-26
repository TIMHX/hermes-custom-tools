# Hermes Agent — Custom Tools

Custom tool integrations for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

## NWS Weather Tool

**Location:** `custom_tools/nws_weather_tool/`

Weather data for **Trenton, NJ** using the public National Weather Service API — no API key required.

**Home address:** [REDACTED]
**Coordinates:** 0.0, 0.0
**Grid point:** PHI / 62, 92

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

#### Step 3 — Enable for cron jobs (required for v0.11.0+)

**Important:** Since Hermes Agent v0.11.0, cron jobs use a separate toolset configuration via `platform_toolsets.cron` in `config.yaml`. Without this, the scheduler cannot see custom tools even if they are registered in `toolsets.py`.

Add `nws_weather` to the `platform_toolsets.cron` list in `~/.hermes/config.yaml`:

```yaml
platform_toolsets:
  cron:
    - browser
    - clarify
    - code_execution
    - cronjob
    - delegation
    - file
    - image_gen
    - memory
    - messaging
    - session_search
    - skills
    - terminal
    - todo
    - tts
    - vision
    - web
    - nws_weather          # ← add this
```

#### Step 4 — Verify

```bash
cd ~/.hermes/hermes-agent && python3 -c "
import sys; sys.path.insert(0, '.')
from model_tools import handle_function_call
import json
result = handle_function_call('nws_now', {}, None)
parsed = json.loads(result)
print('weather:', parsed.get('weather'))
print('temperature:', parsed.get('temperature'))
"
```

Expected output: `weather: <condition>, temperature: <temp>`

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
# Test tool directly (use model_tools, not direct registry access)
cd ~/.hermes/hermes-agent && python3 -c "
import sys; sys.path.insert(0, '.')
from model_tools import handle_function_call
import json
result = handle_function_call('nws_now', {}, None)
parsed = json.loads(result)
print('weather:', parsed.get('weather'))
print('temperature:', parsed.get('temperature'))
"

# Test all four tools
cd ~/.hermes/hermes-agent && python3 -c "
import sys; sys.path.insert(0, '.')
from model_tools import handle_function_call
import json
for tool in ['nws_now', 'nws_hourly', 'nws_forecast', 'nws_alerts']:
    r = json.loads(handle_function_call(tool, {}, None))
    print(tool + ':', 'OK' if 'error' not in r else 'ERROR: ' + r['error'])
"

# Verify nws_weather is in platform_toolsets.cron
grep -A20 'platform_toolsets:' ~/.hermes/config.yaml | grep nws_weather
```

### Best Practice Notes (v0.11.0)

The tool file follows the official [Adding Tools](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-tools) guide:

- ✅ `registry.register()` with top-level call → auto-discovered
- ✅ Handler returns `json.dumps({...})`, errors as `{"error": "..."}`
- ✅ Schema with `name`, `description`, `parameters`
- ✅ Toolset defined in `TOOLSETS`, tools in `_HERMES_CORE_TOOLS`
- ✅ Handler signature `(args: dict, **kwargs)`
- ✅ `check_fn` omitted — not needed (no external dependencies/API keys)
- ✅ Synchronous handlers (uses `urllib.request`); `is_async` not needed
- ✅ `nws_weather` in `platform_toolsets.cron` → accessible to cron scheduler

**v0.11.0 cron toolset requirement:** The cron scheduler resolves toolsets via `_get_platform_tools(cfg, "cron")` which reads `platform_toolsets.cron` from `config.yaml`. If `nws_weather` is not in that list, cron jobs silently skip weather tool calls. This is independent of the tool file and `toolsets.py` registration.

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
    check_fn=my_check_fn,        # optional: returns bool
    requires_env=[],             # optional env var names
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

| Job | Schedule | Tools Used |
|-----|----------|------------|
| Daily Morning Weather | 08:00 daily | `nws_now`, `nws_forecast`, `nws_alerts` |
| Severe Weather Alert | 07:00 & 15:00 daily | `nws_alerts`, `nws_forecast` |

These jobs use `enabled_toolsets` / `platform_toolsets.cron` configuration. They do **not** use the Hermes skills system — they call NWS tools directly by name.

**Cron jobs require `platform_toolsets.cron` to include `nws_weather`** (Step 3 above). Without it, weather tool calls are silently skipped in v0.11.0+.
