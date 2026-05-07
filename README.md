# Hermes Agent — Custom Tools

Custom tool integrations for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

> **Hermes update note:** Tool files in `custom_tools/` are the source of truth. `~/.hermes/hermes-agent/tools/` symlinks to them. The `toolsets.py` changes below must be re-applied after each Hermes update.

---

## GitHub Scouter Tool

**Location:** `custom_tools/github_scouter/`

Fetches top 15 GitHub projects created in the last 20 days (sorted by stars), records them to a Notion database, and returns the list.

### Tools Provided

| Tool | Description |
|------|-------------|
| `github_scouter` | Fetch trending repos, write to Notion, return structured list |

### Environment Variables

| Variable | Required | Source |
|----------|----------|--------|
| `NOTION_TOKEN` or `NOTION_API_KEY` | Yes | Notion integration token |
| `GITHUB_TOKEN` | No | Falls back to `gh auth token` if unset |

### Notion Database IDs (hardcoded)

- `DATABASE_ID = "2f855a34-9949-8020-83b5-cc37c2f54df5"` (knowledge center)
- `DATA_SOURCE_ID = "2f855a34-9949-806b-888c-000bf8c77d79"`

### Installation

#### Step 1 — Create symlink

```bash
ln -s /path/to/hermes-custom-tools/custom_tools/github_scouter/github_scouter.py \
      ~/.hermes/hermes-agent/tools/github_scouter.py
```

#### Step 2 — Patch `toolsets.py`

Edit `~/.hermes/hermes-agent/toolsets.py`:

**A) Add to `_HERMES_CORE_TOOLS`** (around line 60, after `ha_call_service`):
```python
    # GitHub Trending scouter
    "github_scouter",
```

**B) Add `github_scouter` toolset definition** (after `nws_weather` definition):
```python
    "github_scouter": {
        "description": "GitHub Trending scouter — fetch top projects from the last 20 days, record to Notion",
        "tools": ["github_scouter"],
        "includes": []
    },
```

#### Step 3 — Add to `platform_toolsets.cron`

Edit `~/.hermes/config.yaml`, add `github_scouter` to the `platform_toolsets.cron` list:

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
    - nws_weather
    - github_scouter          # ← add this
```

#### Step 4 — Verify

```bash
cd ~/.hermes/hermes-agent && python3 -c "
import sys; sys.path.insert(0, '.')
from model_tools import handle_function_call
import json
result = handle_function_call('github_scouter', {}, None)
parsed = json.loads(result)
print('total:', parsed.get('total'), '| new:', parsed.get('new'), '| updated:', parsed.get('updated'))
"
```

Expected output: `total: 15 | new: N | updated: M`

---

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

#### Step 1 — Create symlink

```bash
ln -s /path/to/hermes-custom-tools/custom_tools/nws_weather_tool/nws_weather_tool.py \
      ~/.hermes/hermes-agent/tools/nws_weather_tool.py
```

#### Step 2 — Patch `toolsets.py`

Edit `~/.hermes/hermes-agent/toolsets.py`:

**A) Add to `_HERMES_CORE_TOOLS`** (around line 60, after `ha_call_service`):
```python
    # NWS weather (Trenton NJ — no API key needed)
    "nws_now", "nws_hourly", "nws_forecast", "nws_alerts",
```

**B) Add `nws_weather` toolset definition** (after `homeassistant` definition):
```python
    "nws_weather": {
        "description": "NWS weather for Trenton NJ — current conditions, hourly, 7-day forecast, and alerts",
        "tools": ["nws_now", "nws_hourly", "nws_forecast", "nws_alerts"],
        "includes": []
    },
```

#### Step 3 — Add to `platform_toolsets.cron`

Edit `~/.hermes/config.yaml`, add `nws_weather` to the `platform_toolsets.cron` list (if not already present):

```yaml
platform_toolsets:
  cron:
    - ...
    - web
    - nws_weather          # ← add this if missing
    - github_scouter
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

### Severe Weather Watchdog Script

**Location:** `custom_tools/nws_weather_tool/severe-weather-watchdog.py`

A standalone Python script for `no_agent` cron mode. Checks NWS alerts and forecast for severe conditions; outputs a warning message only when severe weather is detected. Silent otherwise (watchdog pattern).

**Installation:**

```bash
# Replace the stock ~/.hermes/scripts/ copy with a symlink to the repo
rm -f ~/.hermes/scripts/severe-weather-watchdog.py
ln -s /path/to/hermes-custom-tools/custom_tools/nws_weather_tool/severe-weather-watchdog.py \
      ~/.hermes/scripts/severe-weather-watchdog.py
```

**Thresholds:** precipitation probability > 40%, severe keywords (thunderstorm, tornado, etc.), temp ≤ 25°F or ≥ 100°F.

### Troubleshooting

```bash
# Test all four NWS tools
cd ~/.hermes/hermes-agent && python3 -c "
import sys; sys.path.insert(0, '.')
from model_tools import handle_function_call
import json
for tool in ['nws_now', 'nws_hourly', 'nws_forecast', 'nws_alerts']:
    r = json.loads(handle_function_call(tool, {}, None))
    print(tool + ':', 'OK' if 'error' not in r else 'ERROR: ' + r['error'])
"

# Verify toolsets in platform_toolsets.cron
grep -A20 'platform_toolsets:' ~/.hermes/config.yaml | grep -E 'nws_weather|github_scouter'
```

---

## OpenViking Plugin Patches

The OpenViking memory provider (`plugins/memory/openviking/__init__.py`) is patched with two enhancements:

| Enhancement | Description |
|-------------|-------------|
| `viking_add_resource` | Support local file paths via temp_upload (instead of URL-only) |
| `viking_delete` | Delete files/directories from OpenViking via `DELETE /api/v1/fs` |

Both are bundled in a single combined patch: `hermes-patches/openviking-combined.diff`

### Re-applying

After `hermes update`, run:

```bash
bash ~/hermes-custom-tools/hermes-patches/apply-patches.sh && hermes gateway restart
```

The apply script patches the plugin file and toolsets.py, verifies syntax, then instructs you to restart.

### Verify

```bash
cd ~/.hermes/hermes-agent && python3 -c "
import sys; sys.path.insert(0, '.')
from plugins.memory.openviking import OpenVikingMemoryProvider
print('OK: plugin imports cleanly')
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

When Hermes updates:
- `~/.hermes/hermes-agent/tools/` **persists** (your symlinked tool files survive)
- `toolsets.py` **may be overwritten** → re-apply via `apply-patches.sh`
- `plugins/memory/openviking/__init__.py` **will be overwritten** → re-apply via `apply-patches.sh`
- `~/.hermes/config.yaml` **persists** (platform_toolsets.cron survives)

Full re-apply command: `bash ~/hermes-custom-tools/hermes-patches/apply-patches.sh && hermes gateway restart`

---

## Cron Jobs Using These Tools

| Job | Schedule | Tools Used | enabled_toolsets |
|-----|----------|------------|-----------------|
| Daily Morning Weather | 08:00 daily | `nws_now`, `nws_forecast`, `nws_alerts` | `["nws_weather", "web"]` |
| Severe Weather Alert | 07:00 & 15:00 daily | `nws_alerts`, `nws_forecast` | `["nws_weather", "web"]` |
| GitHub Trending Scouter | 08:00 daily | `github_scouter` | `["github_scouter", "web"]` |

These jobs use `enabled_toolsets` configuration. They do **not** use the Hermes skills system — they call tools directly by name.

**Cron jobs require both `toolsets.py` registration AND `platform_toolsets.cron` inclusion** (Step 3 in each tool's installation above). Without the latter, cron jobs silently skip tool calls in v0.11.0+.
