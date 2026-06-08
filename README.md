# hermes-custom-tools

Custom tools and scripts for the Hermes Agent installation.

## Structure

```
custom_tools/           # Python tools loaded by Hermes at runtime
├── github_scouter.py   # GitHub Trending scouter (7-day window, records to Notion)
└── nws_weather_tool.py # NWS weather: current, hourly, forecast, alerts (Trenton NJ)

scripts/                # Cron / maintenance scripts
├── daily-report.py               # Comprehensive daily report (infra + security + apps)
├── daily-briefing-fallback.py    # Daily news briefing (SearXNG → LLM → Notion)
├── daily-cve-report.py           # CVE vulnerability scanning
├── daily-maintenance-check.py    # App maintenance health checks
├── severe-weather-watchdog.py    # Severe weather alerts
├── gapi.py                       # Google API helper library
├── gws-env.sh                    # Google Workspace environment config
└── sync-searxng-beryl-ip.sh      # Sync Beryl AX IP to SearXNG container

toolsets.patch          # Diff to register tools in hermes-agent/toolsets.py
install.sh              # One-command deploy: tools → hermes-agent, scripts → ~/.hermes/scripts/
```

## Usage

### Deploy everything

```bash
git clone https://github.com/TIMHX/hermes-custom-tools.git
cd hermes-custom-tools
bash install.sh
```

### After hermes update

```bash
cd ~/hermes-custom-tools
git pull
bash install.sh
hermes gateway restart
```

### After editing a tool or script

```bash
cd ~/hermes-custom-tools
# ... edit files ...
bash install.sh         # deploy to runtime
git add -A
git commit -m "feat: <description>"
git push
```

## Environment Variables

Custom tools require these environment variables:

```bash
# NWS Weather
export NWS_HOME_LAT=40.2206
export NWS_HOME_LON=-74.7597

# GitHub Scouter
export NOTION_TOKEN=ntn_...
export GITHUB_TOKEN=ghp_...
```

Set in `~/.hermes/.env` or `~/.hermes/hermes-agent/.env`.
