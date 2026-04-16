"""
Conductor Status Dashboard — aggregates workflow event logs, checkpoints, and PID files
into a single web UI.

Usage:
    python dashboard.py [--port PORT]
    python -m conductor_dashboard [--port PORT]
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
TEMP_DIR = Path(os.environ.get("TEMP", os.environ.get("TMP", "")))
CONDUCTOR_DIR = TEMP_DIR / "conductor"
CHECKPOINTS_DIR = CONDUCTOR_DIR / "checkpoints"
PID_DIR = Path.home() / ".conductor" / "runs"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class AgentRun:
    name: str
    agent_type: str = ""
    model: str = ""
    elapsed: float = 0.0
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class WorkflowRun:
    log_file: str = ""
    name: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    status: str = "unknown"  # completed, failed, running, timeout
    error_type: str = ""
    error_message: str = ""
    failed_agent: str = ""
    total_cost: float = 0.0
    total_tokens: int = 0
    agents: list[AgentRun] = field(default_factory=list)
    agent_defs: list[dict] = field(default_factory=list)
    routes: list[dict] = field(default_factory=list)
    version: str = ""
    # Live status for running workflows
    current_agent: str = ""
    current_agent_type: str = ""
    gate_waiting: bool = False
    gate_agent: str = ""
    iteration: int = 0
    purpose: str = ""
    # Enriched metadata for twig workflows
    work_item_id: str = ""
    work_item_title: str = ""
    work_item_type: str = ""


@dataclass
class Checkpoint:
    file: str = ""
    workflow_path: str = ""
    created_at: str = ""
    error_type: str = ""
    error_message: str = ""
    failed_agent: str = ""
    iteration: int = 0
    workflow_name: str = ""


@dataclass
class ActiveRun:
    pid: int = 0
    port: int = 0
    workflow: str = ""
    started_at: str = ""
    alive: bool = False
    pid_file: str = ""


# ---------------------------------------------------------------------------
# Active-run detection (Windows-compatible)
# ---------------------------------------------------------------------------
def _is_port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.05) -> bool:
    """Return True if something is actually listening on *port*.

    Timeout is kept very short (50ms) because localhost connections
    either succeed instantly or fail instantly — and we may probe
    hundreds of stale PID files per request.
    """
    if port <= 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError, TimeoutError):
        return False


def _get_listening_ports() -> set[int]:
    """Return the set of TCP ports currently in LISTEN state.

    Uses a single OS-level query instead of probing ports individually.
    """
    listening: set[int] = set()
    try:
        import subprocess
        # Windows netstat is fast and always available
        result = subprocess.run(
            ["netstat", "-an", "-p", "TCP"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[3] == "LISTENING":
                # Local address is like 127.0.0.1:8080 or 0.0.0.0:443
                addr = parts[1]
                try:
                    port = int(addr.rsplit(":", 1)[1])
                    listening.add(port)
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass
    return listening


def _is_conductor_alive(pid: int, port: int, listening_ports: set[int] | None = None) -> bool:
    """Return True only if the conductor dashboard is genuinely running.

    Windows aggressively reuses PIDs, so a bare PID check produces false
    positives.  Instead we check if the dashboard port is in the set of
    currently listening ports (gathered once per request via netstat).
    """
    if port > 0:
        if listening_ports is not None:
            return port in listening_ports
        return _is_port_listening(port)
    # No port recorded — fall back to PID check (rare)
    if pid <= 0:
        return False
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------
def _extract_purpose(prompt: str, max_len: int = 120) -> str:
    """Extract a short purpose/description from the first rendered prompt.

    Looks for the most informative snippet: a description after known
    markers, or falls back to the first non-boilerplate line.
    """
    if not prompt:
        return ""
    # Try to find a purpose after common markers
    for marker in [
        "**New work request:**",
        "**Existing work item:**",
        "**Purpose:**",
        "**Input:**",
        "**Question:**",
        "purpose:",
    ]:
        idx = prompt.find(marker)
        if idx >= 0:
            snippet = prompt[idx + len(marker):].strip()
            # Take up to the next double-newline or max_len
            end = snippet.find("\n\n")
            if end > 0:
                snippet = snippet[:end]
            return snippet[:max_len].strip()
    # Fallback: first non-empty, non-heading, non-boilerplate line
    for raw_line in prompt.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        # Skip generic instruction lines
        if any(skip in line.lower() for skip in ["gather context", "you are", "your task", "phase 1", "phase 2"]):
            continue
        return line[:max_len]
    return ""


def _parse_event_log(path: Path) -> WorkflowRun:
    run = WorkflowRun(log_file=str(path))

    # Extract name & timestamp from filename pattern:
    # conductor-{workflow-name}-{YYYYMMDD-HHMMSS}.events.jsonl
    fname = path.stem  # strip .jsonl
    if fname.endswith(".events"):
        fname = fname[: -len(".events")]
    m = re.match(r"conductor-(.+)-(\d{8}-\d{6})$", fname)
    if m:
        run.name = m.group(1)

    agents_map: dict[str, AgentRun] = {}
    # Track live execution state
    agent_type_map: dict[str, str] = {}  # agent_name -> type
    pending_gates: set[str] = set()
    active_agent: str = ""
    completed_agents: set[str] = set()

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type", "")
                ts = evt.get("timestamp", 0)
                data = evt.get("data", {})

                if etype == "workflow_started":
                    run.name = data.get("name", run.name)
                    run.version = data.get("version", "")
                    run.started_at = ts
                    run.agent_defs = data.get("agents", [])
                    for ad in run.agent_defs:
                        agent_type_map[ad.get("name", "")] = ad.get("type", "agent")

                elif etype == "agent_started":
                    if not run.started_at:
                        run.started_at = ts
                    aname = data.get("agent_name", "")
                    active_agent = aname
                    run.iteration = data.get("iteration", run.iteration)

                elif etype == "agent_prompt_rendered" and not run.purpose:
                    run.purpose = _extract_purpose(data.get("rendered_prompt", ""))
                    # For twig workflows, extract work item ID from intake prompt
                    if data.get("agent_name") == "intake" and not run.work_item_id:
                        wid_match = re.search(r"#(\d+)", data.get("rendered_prompt", ""))
                        if wid_match:
                            run.work_item_id = wid_match.group(1)

                elif etype == "agent_completed":
                    aname = data.get("agent_name", "")
                    # Extract enriched metadata from twig intake agent
                    if aname == "intake":
                        output = data.get("output", {})
                        if isinstance(output, str):
                            try:
                                output = json.loads(output)
                            except (json.JSONDecodeError, ValueError):
                                output = {}
                        if isinstance(output, dict):
                            run.work_item_id = str(output.get("epic_id", run.work_item_id))
                            run.work_item_title = output.get("epic_title", "")
                            run.work_item_type = output.get("item_type", "")
                            if run.work_item_title and not run.purpose:
                                run.purpose = run.work_item_title
                    ar = AgentRun(
                        name=aname,
                        model=data.get("model", ""),
                        elapsed=data.get("elapsed", 0),
                        tokens=data.get("tokens", 0),
                        input_tokens=data.get("input_tokens", 0),
                        output_tokens=data.get("output_tokens", 0),
                        cost_usd=data.get("cost_usd", 0),
                    )
                    agents_map[f"{aname}_{ts}"] = ar
                    run.total_cost += ar.cost_usd
                    run.total_tokens += ar.tokens
                    completed_agents.add(aname)
                    if active_agent == aname:
                        active_agent = ""

                elif etype == "gate_presented":
                    pending_gates.add(data.get("agent_name", ""))

                elif etype == "gate_resolved":
                    pending_gates.discard(data.get("agent_name", ""))

                elif etype == "workflow_completed":
                    run.status = "completed"
                    run.ended_at = ts

                elif etype == "workflow_failed":
                    run.status = "failed"
                    run.ended_at = ts
                    run.error_type = data.get("error_type", "")
                    run.error_message = data.get("message", "")
                    run.failed_agent = data.get("agent_name", "")

                elif etype == "route_taken":
                    run.routes.append(data)

                # Track latest timestamp
                if ts and ts > run.ended_at and run.status not in ("completed", "failed"):
                    run.ended_at = ts

    except Exception:
        run.status = "parse_error"

    run.agents = list(agents_map.values())
    run.current_agent = active_agent
    run.current_agent_type = agent_type_map.get(active_agent, "")
    if pending_gates:
        run.gate_waiting = True
        run.gate_agent = next(iter(pending_gates))

    # If no events parsed at all, this file isn't valid JSONL (may be a
    # conductor --log-file debug log that happens to have .events.jsonl extension)
    if not run.started_at and not run.agents and run.status == "unknown":
        run.status = "invalid"
        return run

    # Determine run status — mtime wins over parsed terminal events because
    # conductor can append to the same JSONL file across resume/restart cycles.
    try:
        mtime = path.stat().st_mtime
        recently_modified = time.time() - mtime < 300  # 5 min window
    except OSError:
        recently_modified = False

    if run.status == "unknown":
        # No terminal event — use mtime to decide
        run.status = "running" if recently_modified else "interrupted"
    elif recently_modified and run.status in ("failed", "completed"):
        # Has a terminal event but file is still being written to.
        # Only override if mtime is significantly after the terminal event
        # (indicates a resume/restart, not just post-failure cleanup writes)
        if run.ended_at and (mtime - run.ended_at > 30):
            run.status = "running"

    return run


def _load_event_logs() -> list[WorkflowRun]:
    runs: list[WorkflowRun] = []
    if not CONDUCTOR_DIR.exists():
        return runs
    for p in sorted(CONDUCTOR_DIR.glob("*.events.jsonl")):
        run = _parse_event_log(p)
        if run.status != "invalid":
            runs.append(run)
    return runs


def _load_checkpoints() -> list[Checkpoint]:
    cps: list[Checkpoint] = []
    if not CHECKPOINTS_DIR.exists():
        return cps
    for p in sorted(CHECKPOINTS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            fail = data.get("failure", {})
            # Derive workflow name from filename
            fname = p.stem
            m = re.match(r"(.+)-(\d{8}-\d{6})$", fname)
            wname = m.group(1) if m else fname
            cps.append(Checkpoint(
                file=str(p),
                workflow_path=data.get("workflow_path", ""),
                created_at=data.get("created_at", ""),
                error_type=fail.get("error_type", ""),
                error_message=fail.get("message", ""),
                failed_agent=fail.get("agent", ""),
                iteration=fail.get("iteration", 0),
                workflow_name=wname,
            ))
        except Exception:
            continue
    return cps


def _get_conductor_ports() -> dict[int, int]:
    """Return {pid: port} for all processes listening on localhost.

    Used to discover conductor web dashboard ports for active runs
    that weren't started with --web-bg (so have no PID files).
    """
    pid_to_port: dict[int, int] = {}
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING":
                addr = parts[1]
                if addr.startswith("127.0.0.1:") or addr.startswith("0.0.0.0:"):
                    try:
                        port = int(addr.rsplit(":", 1)[1])
                        proc_id = int(parts[4])
                        pid_to_port[proc_id] = port
                    except (ValueError, IndexError):
                        pass
    except Exception:
        pass
    return pid_to_port


def _discover_conductor_dashboard_ports(exclude_port: int = 0) -> dict[float, int]:
    """Map workflow start timestamps to their conductor dashboard ports.

    Each conductor dashboard serves a unique run identified by its
    workflow_started timestamp.  Returns {start_timestamp: port} for
    1:1 matching against event log runs.

    Skips ports whose workflow has already reached a terminal state
    (workflow_completed or workflow_failed) — those are stale servers
    that haven't shut down yet.

    *exclude_port* is the dashboard's own port, so it never probes itself.
    """
    import urllib.request
    ts_to_port: dict[float, int] = {}
    listening = _get_listening_ports()
    candidates = [p for p in listening if p > 49000 and p != exclude_port]
    for port in candidates:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/state",
                headers={"User-Agent": "conductor-dashboard-probe"},
            )
            with urllib.request.urlopen(req, timeout=0.2) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                if isinstance(data, list):
                    start_ts = 0
                    last_event_type = ""
                    for event in data:
                        if isinstance(event, dict):
                            etype = event.get("type", "")
                            if etype == "workflow_started":
                                start_ts = event.get("timestamp", 0)
                            if etype:
                                last_event_type = etype
                    # Skip completed/failed workflows — stale servers
                    if last_event_type in ("workflow_completed", "workflow_failed"):
                        continue
                    if start_ts:
                        ts_to_port[start_ts] = port
        except Exception:
            pass
    return ts_to_port


def _load_active_runs() -> list[ActiveRun]:
    runs: list[ActiveRun] = []
    listening = _get_listening_ports()
    if PID_DIR.exists():
        for p in sorted(PID_DIR.glob("*.pid")):
            try:
                data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                ppid = data.get("pid", 0)
                port = data.get("port", 0)
                alive = _is_conductor_alive(ppid, port, listening)
                runs.append(ActiveRun(
                    pid=ppid,
                    port=port,
                    workflow=data.get("workflow", ""),
                    started_at=data.get("started_at", ""),
                    alive=alive,
                    pid_file=str(p),
                ))
            except Exception:
                continue
    return runs


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------
def _aggregate_costs(runs: list[WorkflowRun]) -> dict[str, Any]:
    by_workflow: dict[str, float] = {}
    by_model: dict[str, float] = {}
    total = 0.0
    total_tokens = 0
    for r in runs:
        total += r.total_cost
        total_tokens += r.total_tokens
        by_workflow[r.name] = by_workflow.get(r.name, 0) + r.total_cost
        for a in r.agents:
            if a.model:
                by_model[a.model] = by_model.get(a.model, 0) + a.cost_usd
    return {
        "total": total,
        "total_tokens": total_tokens,
        "by_workflow": dict(sorted(by_workflow.items(), key=lambda x: -x[1])),
        "by_model": dict(sorted(by_model.items(), key=lambda x: -x[1])),
    }


def _aggregate_errors(runs: list[WorkflowRun]) -> dict[str, Any]:
    error_types: dict[str, int] = {}
    agent_failures: dict[str, int] = {}
    for r in runs:
        if r.status == "failed":
            et = r.error_type or "Unknown"
            error_types[et] = error_types.get(et, 0) + 1
            if r.failed_agent:
                agent_failures[r.failed_agent] = agent_failures.get(r.failed_agent, 0) + 1
    return {
        "error_types": dict(sorted(error_types.items(), key=lambda x: -x[1])),
        "agent_failures": dict(sorted(agent_failures.items(), key=lambda x: -x[1])),
    }


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
def _ts_to_str(ts: float) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return "—"


def _duration_str(start: float, end: float) -> str:
    if not start or not end:
        return "—"
    d = end - start
    if d < 0:
        return "—"
    mins, secs = divmod(int(d), 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        return f"{hrs}h {mins}m {secs}s"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"


STATUS_ICONS = {
    "completed": "✅",
    "failed": "❌",
    "running": "🔄",
    "timeout": "⏱",
    "interrupted": "⚠️",
    "parse_error": "⁉️",
    "unknown": "❓",
}

# Work item URL patterns keyed by workflow name prefix.
# Falls back to the first entry if no match.
WORK_ITEM_URLS: dict[str, str] = {
    "twig": "https://dev.azure.com/dangreen-msft/Twig/_workitems/edit/{id}",
}


def _work_item_html(run: WorkflowRun, font_size: str = "0.85rem") -> str:
    """Build HTML snippet for a work item link, or empty string if none."""
    if not run.work_item_id:
        return ""
    # Find URL template by workflow name prefix
    url_template = ""
    for prefix, tpl in WORK_ITEM_URLS.items():
        if run.name.startswith(prefix):
            url_template = tpl
            break
    wi_id_html = f'#{_esc(run.work_item_id)}'
    if url_template:
        url = url_template.replace("{id}", run.work_item_id)
        wi_id_html = f'<a href="{_esc(url)}" target="_blank" style="color:var(--accent);text-decoration:none">#{_esc(run.work_item_id)}</a>'
    type_html = ""
    if run.work_item_type:
        type_color = "var(--green)" if run.work_item_type == "Epic" else "var(--blue)"
        type_html = f'<span style="color:{type_color};font-weight:500">{_esc(run.work_item_type)}</span> '
    title_html = f' {_esc(run.work_item_title)}' if run.work_item_title else ""
    return f'<br><span style="font-size:{font_size}">📋 {type_html}{wi_id_html}{title_html}</span>'


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _build_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Conductor Dashboard</title>
<style>
:root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --text2: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
       background: var(--bg); color: var(--text); padding: 20px; line-height: 1.5; }
h1 { font-size: 1.6rem; margin-bottom: 8px; }
h2 { font-size: 1.15rem; margin: 24px 0 10px; color: var(--accent); border-bottom: 1px solid var(--border); padding-bottom: 6px; display: flex; align-items: center; gap: 10px; }
.meta { color: var(--text2); font-size: 0.85rem; margin-bottom: 20px; }
.stats { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
.stat { background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
         padding: 14px 20px; min-width: 140px; }
.stat .label { font-size: 0.75rem; text-transform: uppercase; color: var(--text2); letter-spacing: 0.5px; }
.stat .value { font-size: 1.5rem; font-weight: 600; margin-top: 4px; }
.stat .value.green { color: var(--green); }
.stat .value.red { color: var(--red); }
.stat .value.blue { color: var(--blue); }
.stat .value.yellow { color: var(--yellow); }
table { width: 100%; border-collapse: collapse; background: var(--surface);
         border: 1px solid var(--border); border-radius: 8px; overflow: hidden; margin-bottom: 20px; font-size: 0.85rem; }
th { background: #1c2128; text-align: left; padding: 10px 12px; color: var(--text2);
      font-weight: 600; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.5px; }
td { padding: 8px 12px; border-top: 1px solid var(--border); }
tr:hover { background: #1c2128; }
.wf-name { font-weight: 600; color: var(--accent); }
.ts { white-space: nowrap; font-size: 0.8rem; }
.err-type { background: #f8514920; color: var(--red); padding: 2px 6px; border-radius: 4px; font-size: 0.8rem; }
.err-agent { color: var(--yellow); font-size: 0.8rem; }
.replay-cmd { font-size: 0.72rem; color: var(--text2); word-break: break-all; }
.empty { text-align: center; color: var(--text2); padding: 20px !important; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
@media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
.status-failed td { border-left: 3px solid var(--red); }
.status-completed td { border-left: 3px solid var(--green); }
.status-running td { border-left: 3px solid var(--yellow); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.refresh-note { color: var(--text2); font-size: 0.75rem; text-align: right; margin-bottom: 8px; }

/* Animations */
@keyframes fadeIn {
    from { opacity: 0; transform: translateY(-4px); }
    to { opacity: 1; transform: translateY(0); }
}
.fade-in { animation: fadeIn 0.3s ease-out; }

@keyframes highlight {
    0% { background: rgba(88, 166, 255, 0.2); }
    100% { background: transparent; }
}
.highlight { animation: highlight 1s ease-out; }

@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
}
.gate-pulse { animation: pulse 2s infinite; }

.expand-content {
    max-height: 0;
    overflow: hidden;
    transition: max-height 0.3s ease-out;
}
.expand-content.open {
    max-height: 500px;
}

/* Action buttons */
.action-btn {
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 4px 10px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.75rem;
    margin-right: 4px;
}
.action-btn:hover { border-color: var(--accent); color: var(--accent); }
.action-btn.review { border-color: var(--green); }
.action-btn.investigate { border-color: var(--yellow); }
.action-btn.restart { border-color: var(--blue); }

/* Toggle button */
.toggle-btn {
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text2);
    padding: 3px 10px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.75rem;
    font-weight: normal;
}
.toggle-btn:hover { border-color: var(--accent); color: var(--accent); }
.toggle-btn.active { border-color: var(--accent); color: var(--accent); background: rgba(88,166,255,0.1); }

/* Active run cards */
.run-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 8px;
    overflow: hidden;
}
.run-card.gate-waiting { border-left: 3px solid var(--yellow); }
.run-card-header {
    padding: 12px 16px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 0.85rem;
}
.run-card-header:hover { background: #1c2128; }
.run-card-body {
    padding: 0 16px;
    max-height: 0;
    overflow: hidden;
    transition: max-height 0.3s ease-out, padding 0.3s ease-out;
    font-size: 0.82rem;
    color: var(--text2);
}
.run-card-body.open {
    max-height: 600px;
    padding: 0 16px 12px;
}
.chevron { color: var(--text2); font-size: 0.75rem; transition: transform 0.2s; display: inline-block; }
.chevron.open { transform: rotate(90deg); }

/* Work item badge */
.work-item { font-size: 0.82rem; }

/* Reviewed row */
.reviewed { opacity: 0.45; }

/* Expandable row detail */
.row-detail { display: none; }
.row-detail.open { display: table-row; }
.row-detail td { background: #1c2128; color: var(--text2); font-size: 0.8rem; padding: 12px 16px; }
</style>
</head>
<body>
<h1>&#9889; Conductor Dashboard</h1>
<p class="meta">Aggregated workflow status &bull; Auto-refreshes every 10s</p>

<div id="stats" class="stats"></div>

<h2>&#128260; Active Runs</h2>
<div id="active-runs"></div>

<h2>&#9989; Completed Runs <button id="toggle-reviewed-completed" class="toggle-btn" title="Show or hide runs you have already reviewed" onclick="toggleShowReviewedCompleted()">Show Reviewed</button></h2>
<div id="completed-runs"></div>

<h2>&#10060; Failed Runs <button id="toggle-reviewed-failed" class="toggle-btn" title="Show or hide failed runs you have already reviewed" onclick="toggleShowReviewedFailed()">Show Reviewed</button></h2>
<div id="failed-runs"></div>

<h2>&#128295; Checkpoint Recovery</h2>
<div id="checkpoints"></div>

<div class="grid">
    <div><h2>&#128176; Cost by Workflow</h2><div id="cost-workflow"></div></div>
    <div><h2>&#128176; Cost by Model</h2><div id="cost-model"></div></div>
</div>
<div class="grid">
    <div><h2>&#9888;&#65039; Error Types</h2><div id="error-types"></div></div>
    <div><h2>&#9888;&#65039; Agent Failures</h2><div id="agent-failures"></div></div>
</div>

<script>
// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let dashboardData = null;
let previousData = null;
const reviewedRuns = new Set(JSON.parse(localStorage.getItem('conductor-reviewed-runs') || '[]'));
let showReviewedCompleted = false;
let showReviewedFailed = false;
const expandedRuns = new Set();

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function esc(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function fmtTokens(n) {
    if (!n) return '\\u2014';
    return Number(n).toLocaleString();
}

function fmtCost(n) {
    if (!n) return '\\u2014';
    return '$' + Number(n).toFixed(4);
}

function fmtCost2(n) {
    if (n == null) return '$0.00';
    return '$' + Number(n).toFixed(2);
}

function workItemHtml(r) {
    if (!r.work_item_id) return '';
    var typeColor = r.work_item_type === 'Epic' ? 'var(--green)' : 'var(--blue)';
    var typeSpan = r.work_item_type ? '<span style="color:'+typeColor+';font-weight:500">'+esc(r.work_item_type)+'</span> ' : '';
    var idHtml = r.work_item_url
        ? '<a href="'+esc(r.work_item_url)+'" target="_blank">#'+esc(r.work_item_id)+'</a>'
        : '#'+esc(r.work_item_id);
    var titleHtml = r.work_item_title ? ' '+esc(r.work_item_title) : '';
    return '<span class="work-item">&#128203; '+typeSpan+idHtml+titleHtml+'</span>';
}

// ---------------------------------------------------------------------------
// Fetch
// ---------------------------------------------------------------------------
async function fetchDashboard() {
    try {
        const resp = await fetch('/api/dashboard');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        previousData = dashboardData;
        dashboardData = await resp.json();
        renderAll();
    } catch (e) {
        console.error('Dashboard fetch error:', e);
    }
}

// ---------------------------------------------------------------------------
// Render: Stats
// ---------------------------------------------------------------------------
function renderStats(stats) {
    var el = document.getElementById('stats');
    el.innerHTML =
        '<div class="stat"><div class="label">Total Runs</div><div class="value blue">'+stats.total+'</div></div>' +
        '<div class="stat"><div class="label">Completed</div><div class="value green">'+stats.completed+'</div></div>' +
        '<div class="stat"><div class="label">Failed</div><div class="value red">'+stats.failed+'</div></div>' +
        '<div class="stat"><div class="label">Active Now</div><div class="value yellow">'+stats.active+'</div></div>' +
        '<div class="stat"><div class="label">Gates Waiting</div><div class="value yellow">'+stats.gates_waiting+'</div></div>' +
        '<div class="stat"><div class="label">Total Cost</div><div class="value">'+fmtCost2(stats.total_cost)+'</div></div>' +
        '<div class="stat"><div class="label">Total Tokens</div><div class="value">'+fmtTokens(stats.total_tokens)+'</div></div>' +
        '<div class="stat"><div class="label">Checkpoints</div><div class="value">'+stats.checkpoints+'</div></div>';
}

// ---------------------------------------------------------------------------
// Render: Active Runs (collapsible cards)
// ---------------------------------------------------------------------------
function renderActiveRuns(runs) {
    var el = document.getElementById('active-runs');
    if (!runs || runs.length === 0) {
        el.innerHTML = '<div class="empty" style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:20px;margin-bottom:20px;">No active workflows</div>';
        return;
    }
    var html = '';
    for (var i = 0; i < runs.length; i++) {
        var r = runs[i];
        var key = r.log_file || ('active-'+i);
        var isExpanded = expandedRuns.has(key);
        var gateClass = r.gate_waiting ? ' gate-waiting' : '';

        // Agent status line
        var agentStatus;
        if (r.gate_waiting) {
            agentStatus = '<span class="gate-pulse">&#128678;</span> <span style="color:var(--yellow)">'+esc(r.gate_agent)+'</span> <span class="err-type" style="background:#d2992220">GATE WAITING</span>';
        } else if (r.current_agent) {
            var atype = r.current_agent_type ? ' <span style="color:var(--text2)">('+esc(r.current_agent_type)+')</span>' : '';
            agentStatus = '&#9881;&#65039; '+esc(r.current_agent)+atype;
        } else {
            agentStatus = '<span style="color:var(--text2)">\\u2014</span>';
        }

        var wiHtml = workItemHtml(r);
        var wiBadge = wiHtml ? ' '+wiHtml : '';

        html += '<div class="run-card fade-in'+gateClass+'">';
        html += '<div class="run-card-header" title="Click to expand details" onclick="toggleExpand(\\''+esc(key).replace(/'/g,"\\\\'")+'\\') ">';
        html += '<span class="chevron'+(isExpanded?' open':'')+'">&#9654;</span>';
        html += '<span class="wf-name">'+esc(r.name)+'</span>'+wiBadge;
        html += '<span style="color:var(--text2);margin-left:auto">'+esc(r.elapsed)+'</span>';
        html += '<span>'+agentStatus+'</span>';
        html += '<span>'+fmtCost(r.total_cost)+'</span>';
        html += '</div>';

        // Expanded body
        html += '<div class="run-card-body'+(isExpanded?' open':'')+'">';
        if (r.agents && r.agents.length > 0) {
            html += '<div style="margin-bottom:6px;font-weight:600;color:var(--text)">Completed Agents:</div>';
            html += '<table style="margin-bottom:8px"><thead><tr><th>Agent</th><th>Model</th><th>Cost</th><th>Tokens</th></tr></thead><tbody>';
            for (var j = 0; j < r.agents.length; j++) {
                var a = r.agents[j];
                html += '<tr><td>'+esc(a.name)+'</td><td>'+esc(a.model)+'</td><td>'+fmtCost(a.cost_usd)+'</td><td>'+fmtTokens(a.tokens)+'</td></tr>';
            }
            html += '</tbody></table>';
        }
        html += '<div>Iteration: '+r.iteration+' &bull; '+r.agent_count+' agents completed</div>';
        if (r.dashboard_url) {
            html += '<div style="margin-top:4px"><a href="'+esc(r.dashboard_url)+'" target="_blank">Dashboard :'+r.dashboard_port+'</a></div>';
        }
        html += '<div style="margin-top:4px"><code class="replay-cmd">'+esc(r.replay_cmd)+'</code></div>';
        html += '</div></div>';
    }
    el.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Render: Completed Runs
// ---------------------------------------------------------------------------
function renderCompletedRuns(runs) {
    var el = document.getElementById('completed-runs');
    if (!runs || runs.length === 0) {
        el.innerHTML = '<table><tbody><tr><td class="empty" colspan="7">No completed runs</td></tr></tbody></table>';
        return;
    }
    var reviewedCount = 0;
    for (var i = 0; i < runs.length; i++) {
        if (reviewedRuns.has(runs[i].log_file)) reviewedCount++;
    }
    // Update toggle button text
    var toggleBtn = document.getElementById('toggle-reviewed-completed');
    if (toggleBtn) {
        toggleBtn.textContent = showReviewedCompleted ? 'Hide Reviewed ('+reviewedCount+')' : 'Show Reviewed ('+reviewedCount+')';
        toggleBtn.className = 'toggle-btn' + (showReviewedCompleted ? ' active' : '');
    }

    var html = '<table><thead><tr><th>Workflow</th><th>Started</th><th>Duration</th><th>Cost</th><th>Tokens</th><th>Agents</th><th>Actions</th></tr></thead><tbody>';
    for (var i = 0; i < runs.length; i++) {
        var r = runs[i];
        var isReviewed = reviewedRuns.has(r.log_file);
        if (isReviewed && !showReviewedCompleted) continue;
        var key = r.log_file;
        var isExpanded = expandedRuns.has('completed-'+key);
        var reviewedClass = isReviewed ? ' reviewed' : '';
        var wiHtml = workItemHtml(r);
        var nameExtra = wiHtml ? '<br>'+wiHtml : (r.purpose ? '<br><span style="color:var(--text2);font-size:0.75rem">'+esc(r.purpose)+'</span>' : '');

        html += '<tr class="status-completed fade-in'+reviewedClass+'" style="cursor:pointer" title="Click to expand details" onclick="toggleExpand(\\'completed-'+esc(key).replace(/'/g,"\\\\'")+'\\') ">';
        html += '<td class="wf-name"><span class="chevron'+(isExpanded?' open':'')+'">&#9654;</span> '+esc(r.name)+nameExtra+'</td>';
        html += '<td class="ts">'+esc(r.started_at_str)+'</td>';
        html += '<td>'+esc(r.elapsed)+'</td>';
        html += '<td>'+fmtCost(r.total_cost)+'</td>';
        html += '<td>'+fmtTokens(r.total_tokens)+'</td>';
        html += '<td>'+r.agent_count+'</td>';
        html += '<td>';
        html += '<button class="action-btn review" title="Open Copilot to review results and file issues" onclick="event.stopPropagation();actionReview(\\''+esc(key).replace(/'/g,"\\\\'")+'\\')">&#128203; Review</button>';
        html += '<button class="action-btn'+(isReviewed?' reviewed-btn':'')+'" title="'+(isReviewed?'Unmark as reviewed':'Mark as reviewed — hides from default view')+'" onclick="event.stopPropagation();toggleReviewed(\\''+esc(key).replace(/'/g,"\\\\'")+'\\')">'+( isReviewed ? '&#9745; Reviewed' : '&#9744; Mark Reviewed')+'</button>';
        html += '</td></tr>';

        // Expandable detail row
        html += '<tr class="row-detail'+(isExpanded?' open':'')+'"><td colspan="7">';
        if (r.agents && r.agents.length > 0) {
            var lastAgent = r.agents[r.agents.length - 1];
            html += '<div style="margin-bottom:6px"><strong>Final Agent:</strong> '+esc(lastAgent.name);
            if (lastAgent.model) html += ' ('+esc(lastAgent.model)+')';
            html += '</div>';
        }
        if (r.purpose) html += '<div><strong>Purpose:</strong> '+esc(r.purpose)+'</div>';
        html += '<div style="margin-top:4px"><code class="replay-cmd">'+esc(r.replay_cmd)+'</code></div>';
        html += '</td></tr>';
    }
    html += '</tbody></table>';
    el.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Render: Failed Runs
// ---------------------------------------------------------------------------
function renderFailedRuns(runs) {
    var el = document.getElementById('failed-runs');
    if (!runs || runs.length === 0) {
        el.innerHTML = '<table><tbody><tr><td class="empty" colspan="8">No failed runs</td></tr></tbody></table>';
        return;
    }
    var reviewedCount = 0;
    for (var i = 0; i < runs.length; i++) {
        if (reviewedRuns.has(runs[i].log_file)) reviewedCount++;
    }
    var toggleBtn = document.getElementById('toggle-reviewed-failed');
    if (toggleBtn) {
        toggleBtn.textContent = showReviewedFailed ? 'Hide Reviewed ('+reviewedCount+')' : 'Show Reviewed ('+reviewedCount+')';
        toggleBtn.className = 'toggle-btn' + (showReviewedFailed ? ' active' : '');
    }

    var html = '<table><thead><tr><th>Workflow</th><th>Started</th><th>Duration</th><th>Error Type</th><th>Failed Agent</th><th>Error Message</th><th>Actions</th></tr></thead><tbody>';
    for (var i = 0; i < runs.length; i++) {
        var r = runs[i];
        var isReviewed = reviewedRuns.has(r.log_file);
        if (isReviewed && !showReviewedFailed) continue;
        var key = r.log_file;
        var isExpanded = expandedRuns.has('failed-'+key);
        var reviewedClass = isReviewed ? ' reviewed' : '';
        var wiHtml = workItemHtml(r);
        var nameExtra = wiHtml ? '<br>'+wiHtml : '';
        var errMsgShort = r.error_message ? (r.error_message.length > 80 ? esc(r.error_message.substring(0,80))+'\\u2026' : esc(r.error_message)) : '\\u2014';

        html += '<tr class="status-failed fade-in'+reviewedClass+'" style="cursor:pointer" title="Click to expand error details" onclick="toggleExpand(\\'failed-'+esc(key).replace(/'/g,"\\\\'")+'\\') ">';
        html += '<td class="wf-name"><span class="chevron'+(isExpanded?' open':'')+'">&#9654;</span> '+esc(r.name)+nameExtra+'</td>';
        html += '<td class="ts">'+esc(r.started_at_str)+'</td>';
        html += '<td>'+esc(r.elapsed)+'</td>';
        html += '<td>'+(r.error_type ? '<span class="err-type">'+esc(r.error_type)+'</span>' : '\\u2014')+'</td>';
        html += '<td>'+(r.failed_agent ? '<span class="err-agent">'+esc(r.failed_agent)+'</span>' : '\\u2014')+'</td>';
        html += '<td>'+errMsgShort+'</td>';
        html += '<td>';
        html += '<button class="action-btn investigate" title="Open Copilot to analyze the failure and advise on fixes" onclick="event.stopPropagation();actionInvestigate(\\''+esc(key).replace(/'/g,"\\\\'")+'\\')">&#128269; Investigate</button>';
        html += '<button class="action-btn restart" title="Re-run this workflow from scratch" onclick="event.stopPropagation();actionRestart(\\''+esc(key).replace(/'/g,"\\\\'")+'\\')">&#128260; Restart</button>';
        html += '<button class="action-btn'+(isReviewed?' reviewed-btn':'')+'" title="'+(isReviewed?'Unmark as reviewed':'Mark as reviewed — hides from default view')+'" onclick="event.stopPropagation();toggleReviewed(\\''+esc(key).replace(/'/g,"\\\\'")+'\\')">'+( isReviewed ? '&#9745; Reviewed' : '&#9744; Mark Reviewed')+'</button>';
        html += '</td></tr>';

        // Expandable detail row — full error message
        html += '<tr class="row-detail'+(isExpanded?' open':'')+'"><td colspan="7">';
        html += '<div><strong>Full Error:</strong></div>';
        html += '<pre style="white-space:pre-wrap;color:var(--red);margin-top:4px;font-size:0.8rem">'+esc(r.error_message || 'No error message')+'</pre>';
        if (r.purpose) html += '<div style="margin-top:6px"><strong>Purpose:</strong> '+esc(r.purpose)+'</div>';
        html += '<div style="margin-top:4px"><code class="replay-cmd">'+esc(r.replay_cmd)+'</code></div>';
        html += '</td></tr>';
    }
    html += '</tbody></table>';
    el.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Render: Checkpoints
// ---------------------------------------------------------------------------
function renderCheckpoints(checkpoints) {
    var el = document.getElementById('checkpoints');
    if (!checkpoints || checkpoints.length === 0) {
        el.innerHTML = '<table><tbody><tr><td class="empty" colspan="6">No checkpoints</td></tr></tbody></table>';
        return;
    }
    var html = '<table><thead><tr><th>Workflow</th><th>Created</th><th>Error Type</th><th>Failed Agent</th><th>Message</th><th>Resume Command</th></tr></thead><tbody>';
    for (var i = 0; i < checkpoints.length; i++) {
        var c = checkpoints[i];
        var createdStr = c.created_at ? esc(c.created_at.substring(0,19)) : '\\u2014';
        var msgShort = c.error_message ? (c.error_message.length > 80 ? esc(c.error_message.substring(0,80))+'\\u2026' : esc(c.error_message)) : '\\u2014';
        html += '<tr class="fade-in">';
        html += '<td>'+esc(c.workflow_name)+'</td>';
        html += '<td>'+createdStr+'</td>';
        html += '<td><span class="err-type">'+esc(c.error_type)+'</span></td>';
        html += '<td>'+esc(c.failed_agent)+' (iter '+c.iteration+')</td>';
        html += '<td title="'+esc(c.error_message)+'">'+msgShort+'</td>';
        html += '<td><code class="replay-cmd">'+esc(c.resume_cmd)+'</code></td>';
        html += '</tr>';
    }
    html += '</tbody></table>';
    el.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Render: Costs
// ---------------------------------------------------------------------------
function renderCosts(costs) {
    // Cost by Workflow
    var wfEl = document.getElementById('cost-workflow');
    var byWf = costs.by_workflow || {};
    var wfKeys = Object.keys(byWf);
    if (wfKeys.length === 0) {
        wfEl.innerHTML = '<table><tbody><tr><td class="empty" colspan="2">No cost data</td></tr></tbody></table>';
    } else {
        var html = '<table><thead><tr><th>Workflow</th><th>Cost</th></tr></thead><tbody>';
        for (var i = 0; i < wfKeys.length; i++) {
            html += '<tr><td>'+esc(wfKeys[i])+'</td><td>$'+Number(byWf[wfKeys[i]]).toFixed(4)+'</td></tr>';
        }
        html += '</tbody></table>';
        wfEl.innerHTML = html;
    }

    // Cost by Model
    var mdEl = document.getElementById('cost-model');
    var byModel = costs.by_model || {};
    var mdKeys = Object.keys(byModel);
    if (mdKeys.length === 0) {
        mdEl.innerHTML = '<table><tbody><tr><td class="empty" colspan="2">No cost data</td></tr></tbody></table>';
    } else {
        var html = '<table><thead><tr><th>Model</th><th>Cost</th></tr></thead><tbody>';
        for (var i = 0; i < mdKeys.length; i++) {
            html += '<tr><td>'+esc(mdKeys[i])+'</td><td>$'+Number(byModel[mdKeys[i]]).toFixed(4)+'</td></tr>';
        }
        html += '</tbody></table>';
        mdEl.innerHTML = html;
    }
}

// ---------------------------------------------------------------------------
// Render: Errors
// ---------------------------------------------------------------------------
function renderErrors(errors) {
    // Error Types
    var etEl = document.getElementById('error-types');
    var errTypes = errors.error_types || {};
    var etKeys = Object.keys(errTypes);
    if (etKeys.length === 0) {
        etEl.innerHTML = '<table><tbody><tr><td class="empty" colspan="2">No errors</td></tr></tbody></table>';
    } else {
        var html = '<table><thead><tr><th>Error Type</th><th>Count</th></tr></thead><tbody>';
        for (var i = 0; i < etKeys.length; i++) {
            html += '<tr><td>'+esc(etKeys[i])+'</td><td>'+errTypes[etKeys[i]]+'</td></tr>';
        }
        html += '</tbody></table>';
        etEl.innerHTML = html;
    }

    // Agent Failures
    var afEl = document.getElementById('agent-failures');
    var agentFails = errors.agent_failures || {};
    var afKeys = Object.keys(agentFails);
    if (afKeys.length === 0) {
        afEl.innerHTML = '<table><tbody><tr><td class="empty" colspan="2">No failures</td></tr></tbody></table>';
    } else {
        var html = '<table><thead><tr><th>Agent</th><th>Failures</th></tr></thead><tbody>';
        for (var i = 0; i < afKeys.length; i++) {
            html += '<tr><td>'+esc(afKeys[i])+'</td><td>'+agentFails[afKeys[i]]+'</td></tr>';
        }
        html += '</tbody></table>';
        afEl.innerHTML = html;
    }
}

// ---------------------------------------------------------------------------
// Render All
// ---------------------------------------------------------------------------
function renderAll() {
    if (!dashboardData) return;
    renderStats(dashboardData.stats);
    renderActiveRuns(dashboardData.active_runs);
    renderCompletedRuns(dashboardData.completed_runs);
    renderFailedRuns(dashboardData.failed_runs);
    renderCheckpoints(dashboardData.checkpoints);
    renderCosts(dashboardData.costs);
    renderErrors(dashboardData.errors);
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------
async function actionReview(logFile) {
    try {
        await fetch('/api/action/review', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({log_file: logFile})
        });
    } catch (e) { console.error('Review action failed:', e); }
}

async function actionInvestigate(logFile) {
    try {
        await fetch('/api/action/investigate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({log_file: logFile})
        });
    } catch (e) { console.error('Investigate action failed:', e); }
}

async function actionRestart(logFile) {
    try {
        await fetch('/api/action/restart', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({log_file: logFile})
        });
    } catch (e) { console.error('Restart action failed:', e); }
}

// ---------------------------------------------------------------------------
// Toggle reviewed
// ---------------------------------------------------------------------------
function toggleReviewed(logFile) {
    if (reviewedRuns.has(logFile)) reviewedRuns.delete(logFile);
    else reviewedRuns.add(logFile);
    localStorage.setItem('conductor-reviewed-runs', JSON.stringify([...reviewedRuns]));
    renderAll();
}

function toggleShowReviewedCompleted() {
    showReviewedCompleted = !showReviewedCompleted;
    renderAll();
}

function toggleShowReviewedFailed() {
    showReviewedFailed = !showReviewedFailed;
    renderAll();
}

// ---------------------------------------------------------------------------
// Toggle expand
// ---------------------------------------------------------------------------
function toggleExpand(key) {
    if (expandedRuns.has(key)) expandedRuns.delete(key);
    else expandedRuns.add(key);
    renderAll();
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
fetchDashboard();
setInterval(fetchDashboard, 10000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Conductor Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# The dashboard's own port — set at startup so discovery can exclude it.
_dashboard_port: int = 0


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return _build_html()


@app.get("/api/status")
async def api_status():
    """JSON endpoint for programmatic access (tray icon uses this)."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _compute_status)


def _compute_status():
    runs = _load_event_logs()
    checkpoints = _load_checkpoints()
    active = _load_active_runs()
    costs = _aggregate_costs(runs)
    errors = _aggregate_errors(runs)
    gates = sum(1 for r in runs if r.gate_waiting and r.status == "running")
    return {
        "runs": len(runs),
        "completed": sum(1 for r in runs if r.status == "completed"),
        "failed": sum(1 for r in runs if r.status == "failed"),
        "active": sum(1 for r in runs if r.status == "running"),
        "gates_waiting": gates,
        "costs": costs,
        "errors": errors,
    }


@app.get("/api/dashboard")
async def api_dashboard():
    """Full dashboard data for AJAX frontend."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _compute_dashboard)


def _serialize_run(r: WorkflowRun, ts_to_port: dict[float, int]) -> dict:
    """Convert a WorkflowRun to a JSON-serializable dict."""
    # Find matching dashboard port
    dashboard_port = ts_to_port.get(r.started_at)
    if not dashboard_port:
        for ts, port in ts_to_port.items():
            if abs(ts - r.started_at) < 2.0:
                dashboard_port = port
                break

    # Build work item URL
    work_item_url = ""
    if r.work_item_id:
        for prefix, tpl in WORK_ITEM_URLS.items():
            if r.name.startswith(prefix):
                work_item_url = tpl.replace("{id}", r.work_item_id)
                break

    return {
        "log_file": r.log_file,
        "name": r.name,
        "started_at": r.started_at,
        "started_at_str": _ts_to_str(r.started_at),
        "ended_at": r.ended_at,
        "ended_at_str": _ts_to_str(r.ended_at),
        "elapsed": _duration_str(r.started_at, r.ended_at if r.status != "running" else time.time()),
        "status": r.status,
        "status_icon": STATUS_ICONS.get(r.status, "❓"),
        "error_type": r.error_type,
        "error_message": r.error_message,
        "failed_agent": r.failed_agent,
        "total_cost": r.total_cost,
        "cost_str": f"${r.total_cost:.4f}" if r.total_cost else "—",
        "total_tokens": r.total_tokens,
        "tokens_str": f"{r.total_tokens:,}" if r.total_tokens else "—",
        "agents": [
            {"name": a.name, "model": a.model, "elapsed": a.elapsed,
             "tokens": a.tokens, "cost_usd": a.cost_usd}
            for a in r.agents
        ],
        "agent_count": len(r.agents),
        "current_agent": r.current_agent,
        "current_agent_type": r.current_agent_type,
        "gate_waiting": r.gate_waiting,
        "gate_agent": r.gate_agent,
        "iteration": r.iteration,
        "purpose": r.purpose,
        "work_item_id": r.work_item_id,
        "work_item_title": r.work_item_title,
        "work_item_type": r.work_item_type,
        "work_item_url": work_item_url,
        "dashboard_port": dashboard_port,
        "dashboard_url": f"http://localhost:{dashboard_port}" if dashboard_port else "",
        "replay_cmd": f'conductor replay "{r.log_file}"',
    }


def _compute_dashboard() -> dict:
    runs = _load_event_logs()
    checkpoints = _load_checkpoints()
    costs = _aggregate_costs(runs)
    errors = _aggregate_errors(runs)
    ts_to_port = _discover_conductor_dashboard_ports(exclude_port=_dashboard_port)

    sorted_runs = sorted(runs, key=lambda r: r.started_at or 0, reverse=True)

    active_runs = [_serialize_run(r, ts_to_port) for r in sorted_runs if r.status == "running"]
    completed_runs = [_serialize_run(r, ts_to_port) for r in sorted_runs if r.status == "completed"]
    failed_runs = [_serialize_run(r, ts_to_port) for r in sorted_runs if r.status == "failed"]
    other_runs = [_serialize_run(r, ts_to_port) for r in sorted_runs
                  if r.status not in ("running", "completed", "failed")]

    gates_waiting = sum(1 for r in runs if r.gate_waiting and r.status == "running")

    return {
        "active_runs": active_runs,
        "completed_runs": completed_runs,
        "failed_runs": failed_runs,
        "other_runs": other_runs,
        "stats": {
            "total": len(runs),
            "completed": len(completed_runs),
            "failed": len(failed_runs),
            "active": len(active_runs),
            "gates_waiting": gates_waiting,
            "total_cost": costs["total"],
            "total_tokens": costs["total_tokens"],
            "checkpoints": len(checkpoints),
        },
        "costs": costs,
        "errors": errors,
        "checkpoints": [
            {
                "file": c.file,
                "workflow_name": c.workflow_name,
                "workflow_path": c.workflow_path,
                "created_at": c.created_at,
                "error_type": c.error_type,
                "error_message": c.error_message,
                "failed_agent": c.failed_agent,
                "iteration": c.iteration,
                "resume_cmd": f'conductor resume "{c.file}"',
            }
            for c in sorted(checkpoints, key=lambda c: c.created_at or "", reverse=True)
        ],
    }


@app.post("/api/action/review")
async def action_review(request: Request):
    """Open a terminal session to review completed workflow results."""
    body = await request.json()
    log_file = body.get("log_file", "")
    if not log_file:
        return {"error": "log_file required"}, 400
    prompt = f"Review the conductor workflow results in {log_file}. Identify any issues worth filing and file them as GitHub issues."
    return _spawn_terminal_with_copilot(prompt)


@app.post("/api/action/investigate")
async def action_investigate(request: Request):
    """Open a terminal session to investigate a workflow failure."""
    body = await request.json()
    log_file = body.get("log_file", "")
    if not log_file:
        return {"error": "log_file required"}, 400
    prompt = f"Investigate the failure in conductor workflow log {log_file}. Analyze the error, identify root cause, and advise on fixes."
    return _spawn_terminal_with_copilot(prompt)


@app.post("/api/action/restart")
async def action_restart(request: Request):
    """Restart a conductor workflow."""
    body = await request.json()
    log_file = body.get("log_file", "")
    if not log_file:
        return {"error": "log_file required"}, 400
    # Extract workflow path from event log
    workflow_path = _extract_workflow_path(log_file)
    if not workflow_path:
        return {"error": "Could not determine workflow path from log file"}
    try:
        subprocess.Popen(
            ["conductor", "run", workflow_path, "--web-bg"],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        )
        return {"status": "started", "workflow": workflow_path}
    except Exception as e:
        return {"error": str(e)}


def _spawn_terminal_with_copilot(prompt: str) -> dict:
    """Spawn a Windows Terminal tab (or cmd fallback) with copilot-cli."""
    escaped = prompt.replace('"', '\\"')
    cmd = f'copilot-cli -p "{escaped}"'
    try:
        # Try Windows Terminal first
        subprocess.Popen(["wt.exe", "new-tab", "cmd", "/k", cmd],
                        creationflags=subprocess.CREATE_NO_WINDOW)
        return {"status": "launched", "method": "wt"}
    except FileNotFoundError:
        try:
            subprocess.Popen(f'start cmd /k {cmd}', shell=True)
            return {"status": "launched", "method": "cmd"}
        except Exception as e:
            return {"error": str(e)}


def _extract_workflow_path(log_file: str) -> str:
    """Extract the workflow YAML path from a conductor event log."""
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                    if evt.get("type") == "workflow_started":
                        # The workflow path might be in data
                        data = evt.get("data", {})
                        wp = data.get("workflow_path", "")
                        if wp:
                            return wp
                        # Fallback: reconstruct from name
                        name = data.get("name", "")
                        if name:
                            return name
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    global _dashboard_port
    parser = argparse.ArgumentParser(description="Conductor Status Dashboard")
    parser.add_argument("--port", type=int, default=8777, help="Port to serve on (default: 8777)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    args = parser.parse_args()

    _dashboard_port = args.port

    import uvicorn  # type: ignore

    print(f"🚀 Conductor Dashboard starting on http://{args.host}:{args.port}")
    print(f"   Event logs:   {CONDUCTOR_DIR}")
    print(f"   Checkpoints:  {CHECKPOINTS_DIR}")
    print(f"   PID files:    {PID_DIR}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
