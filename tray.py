"""
Conductor Dashboard — system tray icon.

Shows a tray icon with live status summary, starts the dashboard if needed,
and provides a right-click menu for common actions.
"""
from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import pystray  # type: ignore[import-untyped]
from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-untyped]

from startup import is_registered, register_startup, unregister_startup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8777
DASHBOARD_URL = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"
STATUS_URL = f"{DASHBOARD_URL}/api/status"
POLL_INTERVAL = 10  # seconds

DASHBOARD_PY = Path(__file__).resolve().parent / "dashboard.py"
PYTHON_EXE = Path(r"C:\Users\dangreen\AppData\Local\Python\pythoncore-3.14-64\python.exe")
if not PYTHON_EXE.exists():
    PYTHON_EXE = Path(sys.executable)

ICON_SIZE = 64

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class TrayState:
    """Mutable state shared between the polling thread and the menu."""

    def __init__(self) -> None:
        self.active: int = 0
        self.completed: int = 0
        self.failed: int = 0
        self.gates_waiting: int = 0
        self.cost_total: float = 0.0
        self.dashboard_reachable: bool = False
        self.dashboard_process: subprocess.Popen[bytes] | None = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    # -- Tooltip -----------------------------------------------------------
    def tooltip(self) -> str:
        if not self.dashboard_reachable:
            return "Conductor Dashboard — offline"
        parts = [f"Conductor: {self.active} active"]
        if self.gates_waiting:
            parts[0] += f", {self.gates_waiting} gate waiting"
        lines = [parts[0]]
        lines.append(f"✅ {self.completed} completed | ❌ {self.failed} failed")
        lines.append(f"Cost: ${self.cost_total:.2f}")
        return "\n".join(lines)

    # -- Icon state --------------------------------------------------------
    @property
    def icon_mode(self) -> str:
        if not self.dashboard_reachable:
            return "error"
        if self.gates_waiting:
            return "gate"
        return "ok"


STATE = TrayState()

# ---------------------------------------------------------------------------
# Icon generation (Pillow)
# ---------------------------------------------------------------------------

# Colours
_GREEN = "#3fb950"
_GRAY = "#8b949e"
_BLUE = "#58a6ff"
_ORANGE = "#d29922"


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a TrueType font or fall back to the built-in default."""
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _draw_base_icon(draw: ImageDraw.ImageDraw, active: bool) -> None:
    """Draw the base C icon — hollow monochrome or filled green."""
    margin = 4
    if active:
        draw.ellipse(
            [margin, margin, ICON_SIZE - margin, ICON_SIZE - margin],
            fill=_GREEN,
        )
        text_color = "white"
    else:
        # Hollow outline only
        draw.ellipse(
            [margin, margin, ICON_SIZE - margin, ICON_SIZE - margin],
            outline=_GRAY,
            width=3,
        )
        text_color = _GRAY

    font = _load_font(36)
    label = "C"
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (ICON_SIZE - tw) / 2 - bbox[0]
    y = (ICON_SIZE - th) / 2 - bbox[1]
    draw.text((x, y), label, fill=text_color, font=font)


def _draw_active_badge(draw: ImageDraw.ImageDraw, count: int) -> None:
    """Draw active session count badge in top-left quadrant."""
    if count <= 0:
        return
    # Badge circle — 24px diameter, positioned at top-left
    cx, cy, r = 12, 12, 12
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_BLUE)

    label = str(count) if count < 10 else "*"
    font = _load_font(16)
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = cx - tw / 2 - bbox[0]
    ty = cy - th / 2 - bbox[1]
    draw.text((tx, ty), label, fill="white", font=font)


def _draw_gate_badge(draw: ImageDraw.ImageDraw) -> None:
    """Draw orange yield-sign triangle badge in top-right quadrant."""
    # Equilateral triangle ~22px, positioned in top-right
    cx = ICON_SIZE - 13  # centre-x of the badge area
    top = 2
    half = 11
    triangle = [
        (cx, top),                          # top vertex
        (cx - half, top + int(half * 1.73)),  # bottom-left
        (cx + half, top + int(half * 1.73)),  # bottom-right
    ]
    draw.polygon(triangle, fill=_ORANGE)

    # "!" inside the triangle
    font = _load_font(13)
    label = "!"
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = cx - tw / 2 - bbox[0]
    ty = top + 4 - bbox[1]
    draw.text((tx, ty), label, fill="white", font=font)


def make_icon(active: int = 0, gates_waiting: int = 0) -> Image.Image:
    """Generate the composite tray icon based on current state."""
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    _draw_base_icon(draw, active > 0)

    if active > 0:
        _draw_active_badge(draw, active)

    if gates_waiting > 0:
        _draw_gate_badge(draw)

    return img


# Icon cache keyed on (active_count, gates_waiting_bool)
_ICONS: dict[tuple[int, bool], Image.Image] = {}


def get_icon(active: int = 0, gates_waiting: int = 0) -> Image.Image:
    """Return a cached composite icon for the given state."""
    key = (active, gates_waiting > 0)
    if key not in _ICONS:
        _ICONS[key] = make_icon(active, gates_waiting)
    return _ICONS[key]


# ---------------------------------------------------------------------------
# Dashboard management
# ---------------------------------------------------------------------------

def _port_open() -> bool:
    """Return True if something is listening on DASHBOARD_PORT."""
    try:
        with socket.create_connection((DASHBOARD_HOST, DASHBOARD_PORT), timeout=2):
            return True
    except OSError:
        return False


def start_dashboard() -> None:
    """Start the dashboard as a subprocess if not already running."""
    if _port_open():
        return
    with STATE.lock:
        if STATE.dashboard_process and STATE.dashboard_process.poll() is None:
            return
        STATE.dashboard_process = subprocess.Popen(
            [str(PYTHON_EXE), str(DASHBOARD_PY), "--port", str(DASHBOARD_PORT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
        )


def stop_dashboard() -> None:
    """Stop the dashboard subprocess if we started it."""
    with STATE.lock:
        proc = STATE.dashboard_process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            STATE.dashboard_process = None


def dashboard_running() -> bool:
    return _port_open()


# ---------------------------------------------------------------------------
# Status polling
# ---------------------------------------------------------------------------

def _fetch_status() -> dict[str, Any] | None:
    """Fetch JSON status from the dashboard API."""
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(STATUS_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _poll_loop(icon: pystray.Icon) -> None:
    """Background thread: poll the dashboard for status."""
    while not STATE.stop_event.is_set():
        data = _fetch_status()
        with STATE.lock:
            if data is not None:
                STATE.dashboard_reachable = True
                STATE.active = data.get("active", 0)
                STATE.completed = data.get("completed", 0)
                STATE.failed = data.get("failed", 0)
                STATE.gates_waiting = data.get("gates_waiting", 0)
                costs = data.get("costs", {})
                STATE.cost_total = costs.get("total", 0.0) if isinstance(costs, dict) else 0.0
            else:
                STATE.dashboard_reachable = False

        # Update icon appearance and tooltip
        try:
            icon.icon = get_icon(STATE.active, STATE.gates_waiting)
            icon.title = STATE.tooltip()
        except Exception:
            pass

        # Update the menu so dynamic items refresh
        try:
            icon.update_menu()
        except Exception:
            pass

        STATE.stop_event.wait(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Menu actions
# ---------------------------------------------------------------------------

def _open_dashboard(_icon: Any = None, _item: Any = None) -> None:
    webbrowser.open(DASHBOARD_URL)


def _start_dashboard(_icon: Any = None, _item: Any = None) -> None:
    start_dashboard()


def _stop_dashboard(_icon: Any = None, _item: Any = None) -> None:
    stop_dashboard()


def _toggle_startup(_icon: Any = None, _item: Any = None) -> None:
    if is_registered():
        unregister_startup()
    else:
        register_startup()


def _quit(icon: pystray.Icon, _item: Any = None) -> None:
    STATE.stop_event.set()
    icon.stop()


# ---------------------------------------------------------------------------
# Menu builder
# ---------------------------------------------------------------------------

def _build_menu() -> pystray.Menu:
    running = dashboard_running()
    status_text = STATE.tooltip().split("\n")[0] if STATE.dashboard_reachable else "Dashboard offline"

    return pystray.Menu(
        pystray.MenuItem("Open Dashboard", _open_dashboard, default=True),
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start Dashboard", _start_dashboard, enabled=not running),
        pystray.MenuItem("Stop Dashboard", _stop_dashboard, enabled=running),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start at Login", _toggle_startup, checked=lambda _: is_registered()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _quit),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Start dashboard if not already running
    start_dashboard()

    icon = pystray.Icon(
        name="conductor-dashboard",
        icon=get_icon(STATE.active, STATE.gates_waiting),
        title=STATE.tooltip(),
        menu=_build_menu(),
    )

    # Start background polling thread
    poller = threading.Thread(target=_poll_loop, args=(icon,), daemon=True)
    poller.start()

    # Handle signals for clean shutdown
    def _signal_handler(sig: int, _frame: Any) -> None:
        STATE.stop_event.set()
        icon.stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    icon.run()


if __name__ == "__main__":
    main()
