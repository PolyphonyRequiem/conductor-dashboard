# Conductor Dashboard

Standalone aggregated status dashboard for [microsoft/conductor](https://github.com/microsoft/conductor) multi-agent workflows.

## Features

- **Real-time AJAX dashboard** — live updates with no page reloads
- **Active run cards** — expandable detail cards showing live workflow progress
- **Split completed/failed views** — separate tabs with full output and error details
- **Action buttons** — review & file issues, investigate failures, restart workflows
- **Hide-reviewed** — mark runs as reviewed with localStorage persistence
- **Cost breakdown** — per-workflow and per-model cost analysis
- **Error pattern analysis** — identifies recurring failure patterns across runs
- **Worktree badges** — surface the git worktree each active run is operating in
- **Time-range-filtered metrics** — scope cost, duration, and error stats to a chosen window
- **System tray icon** — overlay badges showing active count and gate-waiting indicator
- **Windows startup registration** — auto-launch on login
- **CSS animations** — smooth transitions on data changes

## Screenshots

> _Screenshots coming soon._

## Install / Update

The dashboard deploys to `~/.copilot/conductor-dashboard/`, separate from this git checkout. Use `install.py` to manage that deployment:

```bash
# Install from this local checkout (default source = repo containing install.py)
python install.py install local [--source PATH] [--no-start] [--with-tray]

# Install by cloning from GitHub
python install.py install github [--repo URL] [--ref BRANCH] [--no-start] [--with-tray]

# Re-run the last install (local or github) to pick up new changes
python install.py update [--no-start] [--with-tray]

# Show manifest, install dir mtimes, running dashboard/tray PIDs, startup shortcut
python install.py status

# Stop dashboard/tray and remove the install dir (optionally the startup shortcut)
python install.py uninstall [--remove-startup] [--yes]
```

Install/update writes `~/.copilot/conductor-dashboard/.install.json` recording the source, ref, and SHA, stops any running dashboard on the configured port, copies files, and restarts the dashboard (and tray, if it was running or `--with-tray` was passed). It polls `/api/dashboard` for up to 10 s and prints ✅ / ❌.

### Alternative: run directly from the checkout

```bash
pip install fastapi uvicorn pystray pillow

python dashboard.py          # start the dashboard
pythonw tray.py              # start the tray (also starts the dashboard)
python startup.py register   # register tray for Windows startup
```

The dashboard will be available at [http://localhost:8777](http://localhost:8777).

## Architecture

| File | Description |
|------|-------------|
| `dashboard.py` | FastAPI web server + API endpoints + embedded JS frontend |
| `tray.py` | System tray icon with live status overlays |
| `startup.py` | Windows startup shortcut registration |
| `install.py` | Installer/updater CLI (local or GitHub source → `~/.copilot/conductor-dashboard/`) |

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
