"""
Windows startup registration helper for the Conductor Dashboard tray icon.

Provides functions to create/remove a Windows Start Menu startup shortcut
so the tray icon launches automatically at login.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


SHORTCUT_NAME = "Conductor Dashboard.lnk"
STARTUP_FOLDER = Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup"
SHORTCUT_PATH = STARTUP_FOLDER / SHORTCUT_NAME

TRAY_PY = Path(__file__).resolve().parent / "tray.py"

# Prefer the specific Python install; fall back to PATH discovery
_PYTHON_DIR = Path(r"C:\Users\dangreen\AppData\Local\Python\pythoncore-3.14-64")
PYTHONW_EXE = _PYTHON_DIR / "pythonw.exe" if (_PYTHON_DIR / "pythonw.exe").exists() else Path(
    sys.executable
).parent / "pythonw.exe"


def register_startup() -> Path:
    """Create a startup shortcut that launches the tray icon at login."""
    import win32com.client  # type: ignore[import-untyped]

    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(str(SHORTCUT_PATH))
    shortcut.Targetpath = str(PYTHONW_EXE)
    shortcut.Arguments = f'"{TRAY_PY}"'
    shortcut.WorkingDirectory = str(TRAY_PY.parent)
    shortcut.Description = "Conductor Dashboard system tray icon"
    shortcut.save()
    print(f"✅ Startup shortcut created: {SHORTCUT_PATH}")
    return SHORTCUT_PATH


def unregister_startup() -> None:
    """Remove the startup shortcut if it exists."""
    if SHORTCUT_PATH.exists():
        SHORTCUT_PATH.unlink()
        print(f"✅ Startup shortcut removed: {SHORTCUT_PATH}")
    else:
        print("ℹ️  No startup shortcut found — nothing to remove.")


def is_registered() -> bool:
    """Return True if the startup shortcut exists."""
    return SHORTCUT_PATH.exists()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manage Conductor Dashboard startup registration")
    parser.add_argument("action", nargs="?", default="register", choices=["register", "unregister", "status"])
    args = parser.parse_args()

    if args.action == "register":
        register_startup()
    elif args.action == "unregister":
        unregister_startup()
    else:
        print(f"Registered: {is_registered()}")
        if is_registered():
            print(f"  Shortcut: {SHORTCUT_PATH}")
