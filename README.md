# Conductor Dashboard

Standalone aggregated status dashboard for [microsoft/conductor](https://github.com/microsoft/conductor) multi-agent workflows.

## Features

- **Real-time AJAX dashboard** — live updates with no page reloads
- **Active run cards** — expandable detail cards showing live workflow progress
- **Split completed/failed views** — separate tabs with full output and error details
- **Action buttons** — review & file issues, investigate failures, restart workflows
- **Hide-reviewed** — mark runs as reviewed with localStorage persistence
- **Checkpoint recovery panel** — resume workflows from last checkpoint
- **Cost breakdown** — per-workflow and per-model cost analysis
- **Error pattern analysis** — identifies recurring failure patterns across runs
- **System tray icon** — overlay badges showing active count and gate-waiting indicator
- **Windows startup registration** — auto-launch on login
- **CSS animations** — smooth transitions on data changes

## Screenshots

> _Screenshots coming soon._

## Quick Start

```bash
# Install dependencies
pip install fastapi uvicorn pystray pillow

# Start the dashboard
python dashboard.py

# Or start the tray icon (also starts dashboard automatically)
pythonw tray.py

# Register for Windows startup
python startup.py register
```

The dashboard will be available at [http://localhost:9120](http://localhost:9120).

## Architecture

| File | Description |
|------|-------------|
| `dashboard.py` | FastAPI web server + API endpoints + embedded JS frontend |
| `tray.py` | System tray icon with live status overlays |
| `startup.py` | Windows startup shortcut registration |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard web UI |
| `GET` | `/api/status` | Summary status (used by tray icon) |
| `GET` | `/api/dashboard` | Full dashboard data (used by frontend JS) |
| `POST` | `/api/action/review` | Launch copilot review session |
| `POST` | `/api/action/investigate` | Launch failure investigation |
| `POST` | `/api/action/restart` | Restart a workflow |

## Data Sources

The dashboard reads workflow run data from conductor's output directories. It scans for active, completed, and failed runs and aggregates status, cost, and error information into the dashboard views.

| Source | Location |
|--------|----------|
| Event Logs | `%TEMP%\conductor\*.events.jsonl` |
| Checkpoints | `%TEMP%\conductor\checkpoints\*.json` |
| PID Files | `~\.conductor\runs\*.pid` |

## Requirements

- Python 3.12+
- Windows (for system tray icon and startup registration)

## License

[MIT](LICENSE)
