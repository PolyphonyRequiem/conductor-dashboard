"""
Installer / updater CLI for the Conductor Dashboard.

Deploys the dashboard files from a local checkout or a GitHub repository to
``~/.copilot/conductor-dashboard`` and manages the running dashboard + tray
processes during install, update, and uninstall.

Usage::

    python install.py install local   [--source PATH]  [--no-start] [--with-tray]
    python install.py install github  [--repo URL] [--ref BRANCH] [--no-start] [--with-tray]
    python install.py update                           [--no-start] [--with-tray]
    python install.py status
    python install.py uninstall                        [--remove-startup] [--yes]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

INSTALL_DIR = Path.home() / ".copilot" / "conductor-dashboard"
MANIFEST_PATH = INSTALL_DIR / ".install.json"
CACHE_SRC_DIR = INSTALL_DIR / ".cache" / "src"
DEFAULT_REPO = "https://github.com/PolyphonyRequiem/conductor-dashboard.git"
DEFAULT_PORT = 8777

FILES_TO_COPY = [
    "dashboard.py",
    "tray.py",
    "startup.py",
    "__init__.py",
    "__main__.py",
    "README.md",
    "LICENSE",
]
DIRS_TO_COPY = ["tests"]

STARTUP_SHORTCUT = (
    Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs"
    / "Startup"
    / "Conductor Dashboard.lnk"
)

# Windows Popen flags — guarded so import works on non-Windows hosts.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
DETACHED_FLAGS = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def _info(msg: str) -> None:
    print(f"ℹ️  {msg}")


def _ok(msg: str) -> None:
    print(f"✅ {msg}")


def _warn(msg: str) -> None:
    print(f"⚠️  {msg}")


def _err(msg: str) -> None:
    print(f"❌ {msg}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Manifest helpers
# --------------------------------------------------------------------------- #

def read_manifest() -> dict[str, Any] | None:
    """Return the install manifest dict, or ``None`` if not installed."""
    if not MANIFEST_PATH.exists():
        return None
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _warn(f"Could not read manifest at {MANIFEST_PATH}: {exc}")
        return None


def write_manifest(manifest: dict[str, Any]) -> None:
    """Atomically write the manifest JSON file."""
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(tmp, MANIFEST_PATH)


# --------------------------------------------------------------------------- #
# Process detection / stopping
# --------------------------------------------------------------------------- #

def find_pids_on_port(port: int) -> list[int]:
    """Return PIDs that have a LISTENING TCP socket on ``port``."""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except FileNotFoundError:
        return []

    pids: set[int] = set()
    needle = f":{port}"
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        # Format: Proto  Local  Foreign  State  PID
        if len(parts) < 5:
            continue
        local = parts[1]
        if not local.endswith(needle):
            continue
        try:
            pids.add(int(parts[-1]))
        except ValueError:
            continue
    return sorted(pids)


def _ps(command: str) -> str:
    """Run a PowerShell command and return stdout (empty on failure)."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout
    except FileNotFoundError:
        return ""


def find_tray_pids() -> list[int]:
    """Return PIDs of python/pythonw processes running ``tray.py`` from INSTALL_DIR."""
    tray_path = str((INSTALL_DIR / "tray.py").resolve()).replace("\\", "\\\\")
    # Use WQL LIKE escaping: match any command line containing tray.py under INSTALL_DIR.
    cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*tray.py*' -and $_.CommandLine -like '*{INSTALL_DIR}*' }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    out = _ps(cmd)
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    # Fallback: at least match any process referencing the resolved tray.py path.
    if not pids:
        cmd2 = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{tray_path}*' }} | "
            "Select-Object -ExpandProperty ProcessId"
        )
        for line in _ps(cmd2).splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    return sorted(set(pids))


def _kill(pid: int) -> bool:
    """Kill a process tree by PID. Return True on apparent success."""
    result = subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def stop_dashboard(port: int) -> list[int]:
    """Kill any dashboard listening on ``port``. Returns killed PIDs."""
    killed: list[int] = []
    for pid in find_pids_on_port(port):
        if _kill(pid):
            killed.append(pid)
            _ok(f"Stopped dashboard pid={pid} on port {port}")
        else:
            _warn(f"Failed to stop dashboard pid={pid}")
    # Wait for port to free, up to 3s.
    deadline = time.time() + 3.0
    while time.time() < deadline and find_pids_on_port(port):
        time.sleep(0.2)
    return killed


def stop_tray() -> list[int]:
    """Kill tray.py processes running from INSTALL_DIR. Returns killed PIDs."""
    killed: list[int] = []
    for pid in find_tray_pids():
        if _kill(pid):
            killed.append(pid)
            _ok(f"Stopped tray pid={pid}")
        else:
            _warn(f"Failed to stop tray pid={pid}")
    return killed


# --------------------------------------------------------------------------- #
# Starting the dashboard / tray
# --------------------------------------------------------------------------- #

def _pythonw() -> str:
    """Return pythonw.exe path if available, else current interpreter."""
    candidate = Path(sys.executable).parent / "pythonw.exe"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def start_dashboard(port: int) -> subprocess.Popen[bytes] | None:
    """Launch the dashboard in a detached process on ``port``."""
    dashboard_py = INSTALL_DIR / "dashboard.py"
    if not dashboard_py.exists():
        _err(f"Cannot start dashboard: {dashboard_py} not found.")
        return None
    try:
        proc = subprocess.Popen(
            [sys.executable, str(dashboard_py), "--port", str(port)],
            cwd=str(INSTALL_DIR),
            creationflags=DETACHED_FLAGS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        _ok(f"Launched dashboard (pid={proc.pid}, port={port})")
        return proc
    except OSError as exc:
        _err(f"Failed to launch dashboard: {exc}")
        return None


def start_tray() -> subprocess.Popen[bytes] | None:
    """Launch the tray icon in a detached process."""
    tray_py = INSTALL_DIR / "tray.py"
    if not tray_py.exists():
        _warn(f"tray.py not found at {tray_py}; skipping tray launch.")
        return None
    try:
        proc = subprocess.Popen(
            [_pythonw(), str(tray_py)],
            cwd=str(INSTALL_DIR),
            creationflags=DETACHED_FLAGS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        _ok(f"Launched tray (pid={proc.pid})")
        return proc
    except OSError as exc:
        _warn(f"Failed to launch tray: {exc}")
        return None


def wait_for_dashboard(port: int, timeout: float = 10.0) -> bool:
    """Poll ``/api/dashboard`` until it responds 200 or timeout."""
    url = f"http://127.0.0.1:{port}/api/dashboard"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if 200 <= resp.status < 500:
                    return True
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            pass
        time.sleep(0.3)
    return False


# --------------------------------------------------------------------------- #
# File copy
# --------------------------------------------------------------------------- #

def copy_source(src: Path) -> list[str]:
    """Copy declared files/dirs from ``src`` into INSTALL_DIR. Return copied file names."""
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    # Scrub bytecode cache so we don't keep stale .pyc files.
    for pycache in INSTALL_DIR.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)

    copied: list[str] = []
    for name in FILES_TO_COPY:
        s = src / name
        if not s.exists():
            _warn(f"Source missing: {name} — skipping")
            continue
        shutil.copy2(s, INSTALL_DIR / name)
        copied.append(name)

    for name in DIRS_TO_COPY:
        s = src / name
        if not s.exists():
            _warn(f"Source dir missing: {name} — skipping")
            continue
        shutil.copytree(s, INSTALL_DIR / name, dirs_exist_ok=True)
        copied.append(f"{name}/")

    return copied


# --------------------------------------------------------------------------- #
# Git source fetch
# --------------------------------------------------------------------------- #

def _have_git() -> bool:
    return shutil.which("git") is not None


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
    )


def fetch_github_source(repo: str, ref: str) -> tuple[Path, str]:
    """Clone or update the cache checkout of ``repo@ref``. Return (path, sha)."""
    if not _have_git():
        raise RuntimeError(
            "git executable not found on PATH. Install Git for Windows "
            "(https://git-scm.com/download/win) and re-run."
        )

    CACHE_SRC_DIR.parent.mkdir(parents=True, exist_ok=True)
    git_dir = CACHE_SRC_DIR / ".git"
    reuse = False
    if git_dir.exists():
        remote = _git("remote", "get-url", "origin", cwd=CACHE_SRC_DIR, check=False)
        current = (remote.stdout or "").strip()
        if current == repo:
            reuse = True
        else:
            _info(f"Cache remote {current!r} != {repo!r}; re-cloning.")

    if reuse:
        _info(f"Fetching {ref} from {repo}…")
        _git("fetch", "--depth", "1", "origin", ref, cwd=CACHE_SRC_DIR)
        _git("reset", "--hard", f"origin/{ref}", cwd=CACHE_SRC_DIR)
    else:
        if CACHE_SRC_DIR.exists():
            shutil.rmtree(CACHE_SRC_DIR, ignore_errors=True)
        CACHE_SRC_DIR.parent.mkdir(parents=True, exist_ok=True)
        _info(f"Cloning {repo} @ {ref}…")
        _git("clone", "--depth", "1", "--branch", ref, repo, str(CACHE_SRC_DIR))

    sha = _git("rev-parse", "HEAD", cwd=CACHE_SRC_DIR).stdout.strip()
    return CACHE_SRC_DIR, sha


# --------------------------------------------------------------------------- #
# Shared install pipeline
# --------------------------------------------------------------------------- #

def _resolve_port() -> int:
    manifest = read_manifest()
    if manifest and isinstance(manifest.get("port"), int):
        return int(manifest["port"])
    return DEFAULT_PORT


def _deploy(
    src: Path,
    *,
    source_type: str,
    source: str,
    ref: str | None,
    sha: str | None,
    no_start: bool,
    with_tray: bool,
) -> int:
    """Shared deploy flow: stop → copy → write manifest → (re)start."""
    if not (src / "dashboard.py").exists():
        _err(f"Source does not contain dashboard.py: {src}")
        return 1

    port = _resolve_port()
    tray_was_running = bool(find_tray_pids())
    if tray_was_running:
        _info("Tray is currently running; it will be restarted after install.")

    stop_dashboard(port)
    stop_tray()

    _info(f"Copying files from {src} → {INSTALL_DIR}")
    copied = copy_source(src)
    _ok(f"Copied {len(copied)} entries")

    manifest: dict[str, Any] = {
        "source_type": source_type,
        "source": source,
        "installed_at": _now_iso(),
        "files": copied,
        "port": port,
    }
    if source_type == "github":
        manifest["ref"] = ref
        manifest["sha"] = sha
    write_manifest(manifest)
    _ok(f"Manifest written: {MANIFEST_PATH}")

    if no_start:
        _info("--no-start specified; skipping launch.")
        return 0

    proc = start_dashboard(port)
    if proc is None:
        return 1
    if with_tray or tray_was_running:
        start_tray()

    if wait_for_dashboard(port):
        _ok(f"Dashboard is live at http://127.0.0.1:{port}/")
        return 0
    _err(f"Dashboard did not respond on port {port} within 10s.")
    return 1


# --------------------------------------------------------------------------- #
# Subcommand handlers
# --------------------------------------------------------------------------- #

def cmd_install_local(args: argparse.Namespace) -> int:
    """Install the dashboard from a local source tree."""
    src = Path(args.source).resolve() if args.source else Path(__file__).resolve().parent
    if not src.exists():
        _err(f"Source path does not exist: {src}")
        return 1
    return _deploy(
        src,
        source_type="local",
        source=str(src),
        ref=None,
        sha=None,
        no_start=args.no_start,
        with_tray=args.with_tray,
    )


def cmd_install_github(args: argparse.Namespace) -> int:
    """Install the dashboard by cloning a GitHub repository."""
    try:
        src, sha = fetch_github_source(args.repo, args.ref)
    except RuntimeError as exc:
        _err(str(exc))
        return 1
    except subprocess.CalledProcessError as exc:
        _err(f"git failed: {exc.stderr.strip() or exc}")
        return 1
    return _deploy(
        src,
        source_type="github",
        source=args.repo,
        ref=args.ref,
        sha=sha,
        no_start=args.no_start,
        with_tray=args.with_tray,
    )


def cmd_update(args: argparse.Namespace) -> int:
    """Reinstall from the source recorded in the manifest."""
    manifest = read_manifest()
    if manifest is None:
        _err("Not installed — run `install local` or `install github` first.")
        return 1

    source_type = manifest.get("source_type")
    if source_type == "local":
        src = Path(manifest["source"])
        _info(f"Updating from local source: {src}")
        ns = argparse.Namespace(
            source=str(src), no_start=args.no_start, with_tray=args.with_tray
        )
        return cmd_install_local(ns)
    if source_type == "github":
        repo = manifest.get("source", DEFAULT_REPO)
        ref = manifest.get("ref", "main")
        _info(f"Updating from github: {repo} @ {ref}")
        ns = argparse.Namespace(
            repo=repo, ref=ref, no_start=args.no_start, with_tray=args.with_tray
        )
        return cmd_install_github(ns)
    _err(f"Manifest has unknown source_type: {source_type!r}")
    return 1


def cmd_status(_args: argparse.Namespace) -> int:
    """Print install status, manifest, running processes, and startup registration."""
    manifest = read_manifest()
    if manifest is None:
        print("Not installed.")
    else:
        print("Manifest:")
        print(json.dumps(manifest, indent=2))

    if INSTALL_DIR.exists():
        files = sorted(p for p in INSTALL_DIR.iterdir() if p.is_file())
        latest = max((p.stat().st_mtime for p in files), default=0.0)
        when = datetime.fromtimestamp(latest, tz=timezone.utc).isoformat() if latest else "n/a"
        print(f"\nInstall dir: {INSTALL_DIR}")
        print(f"  Files: {len(files)}   Latest mtime: {when}")
    else:
        print(f"\nInstall dir does not exist: {INSTALL_DIR}")

    port = _resolve_port()
    dashboard_pids = find_pids_on_port(port)
    if dashboard_pids:
        print(f"\nDashboard: running on port {port} (pids: {dashboard_pids}) → http://127.0.0.1:{port}/")
    else:
        print(f"\nDashboard: not listening on port {port}")

    tray_pids = find_tray_pids()
    if tray_pids:
        print(f"Tray:      running (pids: {tray_pids})")
    else:
        print("Tray:      not running")

    if STARTUP_SHORTCUT.exists():
        print(f"Startup:   registered → {STARTUP_SHORTCUT}")
    else:
        print("Startup:   not registered")
    return 0


def _remove_startup_shortcut() -> None:
    """Try startup.unregister_startup(); fall back to deleting the shortcut file."""
    startup_py = INSTALL_DIR / "startup.py"
    if startup_py.exists():
        try:
            sys.path.insert(0, str(INSTALL_DIR))
            import importlib
            if "startup" in sys.modules:
                del sys.modules["startup"]
            startup = importlib.import_module("startup")
            startup.unregister_startup()
            return
        except Exception as exc:  # noqa: BLE001 - fallback is intentional
            _warn(f"startup.unregister_startup() failed: {exc}; falling back to direct delete.")
        finally:
            if str(INSTALL_DIR) in sys.path:
                sys.path.remove(str(INSTALL_DIR))

    if STARTUP_SHORTCUT.exists():
        try:
            STARTUP_SHORTCUT.unlink()
            _ok(f"Startup shortcut removed: {STARTUP_SHORTCUT}")
        except OSError as exc:
            _warn(f"Could not remove startup shortcut: {exc}")
    else:
        _info("No startup shortcut found — nothing to remove.")


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Stop processes, optionally remove startup shortcut, and delete INSTALL_DIR."""
    if not args.yes:
        resp = input(f"Uninstall Conductor Dashboard from {INSTALL_DIR}? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            _info("Aborted.")
            return 1

    port = _resolve_port()
    stop_dashboard(port)
    stop_tray()

    if args.remove_startup:
        _remove_startup_shortcut()

    if INSTALL_DIR.exists():
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)
        _ok(f"Removed {INSTALL_DIR}")
    else:
        _info(f"Install dir did not exist: {INSTALL_DIR}")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse parser."""
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install, update, and uninstall the Conductor Dashboard.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # install ---------------------------------------------------------------
    p_install = sub.add_parser("install", help="Install the dashboard")
    install_sub = p_install.add_subparsers(dest="source_type", required=True)

    p_local = install_sub.add_parser("local", help="Install from a local source tree")
    p_local.add_argument("--source", default=None, help="Path to source checkout (default: repo of install.py)")
    p_local.add_argument("--no-start", action="store_true", help="Do not start dashboard after copy")
    p_local.add_argument("--with-tray", action="store_true", help="Also launch the system tray icon")
    p_local.set_defaults(func=cmd_install_local)

    p_gh = install_sub.add_parser("github", help="Install by cloning a GitHub repo")
    p_gh.add_argument("--repo", default=DEFAULT_REPO, help=f"Git URL (default: {DEFAULT_REPO})")
    p_gh.add_argument("--ref", default="main", help="Branch, tag, or ref (default: main)")
    p_gh.add_argument("--no-start", action="store_true", help="Do not start dashboard after copy")
    p_gh.add_argument("--with-tray", action="store_true", help="Also launch the system tray icon")
    p_gh.set_defaults(func=cmd_install_github)

    # update ----------------------------------------------------------------
    p_update = sub.add_parser("update", help="Reinstall from the manifest's recorded source")
    p_update.add_argument("--no-start", action="store_true", help="Do not start dashboard after copy")
    p_update.add_argument("--with-tray", action="store_true", help="Also launch the system tray icon")
    p_update.set_defaults(func=cmd_update)

    # status ----------------------------------------------------------------
    p_status = sub.add_parser("status", help="Show install status")
    p_status.set_defaults(func=cmd_status)

    # uninstall -------------------------------------------------------------
    p_uninstall = sub.add_parser("uninstall", help="Remove the installed dashboard")
    p_uninstall.add_argument("--remove-startup", action="store_true", help="Also remove Windows startup shortcut")
    p_uninstall.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p_uninstall.set_defaults(func=cmd_uninstall)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
