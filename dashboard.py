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
import shutil
import subprocess
import socket
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncio
import hashlib

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

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
    failed_subworkflow_path: str = ""  # e.g. "foreach>builder" — chain of subworkflows leading to failure
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
    # Inline subworkflow tracking
    subworkflows: list[dict] = field(default_factory=list)
    # Workflow metadata from YAML/CLI (passed through workflow_started event)
    metadata: dict = field(default_factory=dict)
    # Run identity (from conductor's event log subscriber)
    run_id: str = ""
    # System metadata from conductor's workflow_started event ($system)
    system_meta: dict = field(default_factory=dict)
    # Liveness signals
    tool_in_flight: bool = False  # last tool event was agent_tool_start with no matching complete
    last_event_ts: float = 0.0    # timestamp of the most recent event parsed


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
def _safe_int(val: Any, default: int = 0) -> int:
    """Coerce a value to int, returning *default* on failure."""
    if isinstance(val, int):
        return val
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _is_pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID exists (Windows)."""
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


def _is_pid_from_run(pid: int, run_started_at: float) -> bool:
    """Return True only if *pid* belongs to the process that started the run.

    Windows aggressively reuses PIDs, so a bare OpenProcess check produces
    false positives.  This function cross-checks the process creation time
    against the run's start timestamp — if the process was created well after
    the run started, it's a different process that reused the PID.
    """
    if pid <= 0 or not run_started_at:
        return False
    try:
        import ctypes.wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            creation = ctypes.wintypes.FILETIME()
            exit_ft = ctypes.wintypes.FILETIME()
            kern = ctypes.wintypes.FILETIME()
            user = ctypes.wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation), ctypes.byref(exit_ft),
                ctypes.byref(kern), ctypes.byref(user),
            ):
                return False
            # Convert FILETIME (100ns ticks since 1601-01-01) to Unix epoch
            EPOCH_DIFF = 116444736000000000
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            create_time = (ticks - EPOCH_DIFF) / 10_000_000
            # The conductor process is created slightly before it writes the
            # workflow_started event, so create_time should be ≤ run_started_at
            # (with a small margin for clock granularity).  A process created
            # significantly after the run started is PID reuse.
            return (create_time - run_started_at) < 30 and \
                   (run_started_at - create_time) < 120
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


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
        if any(skip in line.lower() for skip in ["gather context", "you are", "your task", "phase 1", "phase 2", "check if"]):
            continue
        return line[:max_len]
    return ""


def _parse_event_log(path: Path) -> WorkflowRun:
    run = WorkflowRun(log_file=str(path))

    # Extract name & run_id from filename pattern:
    # conductor-{workflow-name}-{YYYYMMDD-HHMMSS}-{run_id}.events.jsonl
    fname = path.stem  # strip .jsonl
    if fname.endswith(".events"):
        fname = fname[: -len(".events")]
    m = re.match(r"conductor-(.+)-(\d{8}-\d{6})-([a-f0-9]+)$", fname)
    if m:
        run.name = m.group(1)
        run.run_id = m.group(3)
    else:
        # Fallback: old format without run_id suffix
        m2 = re.match(r"conductor-(.+)-(\d{8}-\d{6})$", fname)
        if m2:
            run.name = m2.group(1)

    agents_map: dict[str, AgentRun] = {}
    # Track live execution state
    agent_type_map: dict[str, str] = {}  # agent_name -> type
    pending_gates: set[str] = set()
    active_agent: str = ""
    completed_agents: set[str] = set()
    wf_depth: int = 0  # track nested workflow_started depth
    saw_json_error: bool = False  # evidence of partially-written log lines

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    saw_json_error = True
                    continue
                etype = evt.get("type", "")
                ts = evt.get("timestamp", 0)
                data = evt.get("data", {})

                if etype == "workflow_started":
                    # Only use the FIRST workflow_started — subsequent ones
                    # are inline subworkflows that shouldn't override the
                    # outer workflow's identity.
                    if wf_depth == 0:
                        run.name = data.get("name", run.name)
                        run.version = data.get("version", "")
                        run.started_at = ts
                        run.agent_defs = data.get("agents", [])
                        run.metadata = data.get("metadata", {})
                        # run_id: prefer top-level, fall back to system
                        raw_system = data.get("system", {})
                        run.system_meta = raw_system if isinstance(raw_system, dict) else {}
                        run.run_id = data.get("run_id", "") or run.system_meta.get("run_id", "")
                        # Coerce numeric system fields to int for safe comparison
                        for _k in ("pid", "dashboard_port", "parent_pid"):
                            if _k in run.system_meta:
                                run.system_meta[_k] = _safe_int(run.system_meta[_k])
                        for ad in run.agent_defs:
                            agent_type_map[ad.get("name", "")] = ad.get("type", "agent")
                        # Early work_item_id from metadata (injected at invocation time)
                        for field in ("workitem_id", "work_item_id", "input_work_item_id"):
                            mid = run.metadata.get(field)
                            if mid and str(mid) != "{work_item_id}" and not str(mid).startswith("{"):
                                run.work_item_id = str(mid)
                                break
                    wf_depth += 1

                elif etype == "agent_started":
                    if not run.started_at:
                        run.started_at = ts
                    aname = data.get("agent_name", "")
                    active_agent = aname
                    run.iteration = data.get("iteration", run.iteration)

                elif etype == "agent_prompt_rendered" and not run.purpose:
                    run.purpose = _extract_purpose(data.get("rendered_prompt", ""))

                elif etype == "agent_completed":
                    aname = data.get("agent_name", "")
                    # Extract work item ID from agent output.
                    # Uses metadata.work_item_id_agent / work_item_id_field if declared,
                    # otherwise falls back to intake agent with epic_id (backward compat).
                    wid_agent = run.metadata.get("work_item_id_agent", "intake")
                    wid_field = run.metadata.get("work_item_id_field", "epic_id")
                    if aname == wid_agent:
                        output = data.get("output", {})
                        if isinstance(output, str):
                            try:
                                output = json.loads(output)
                            except (json.JSONDecodeError, ValueError):
                                output = {}
                        if isinstance(output, dict):
                            extracted_id = output.get(wid_field)
                            if extracted_id:
                                run.work_item_id = str(extracted_id)
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
                    wf_depth = max(0, wf_depth - 1)
                    # Guard against depth-tracking errors from partially-
                    # written log files: if depth hits 0 but subworkflows
                    # are still running, a child workflow_started was
                    # likely truncated during a concurrent read — treat
                    # as child completion, not root.
                    has_running_subs = any(
                        sw["status"] == "running" for sw in run.subworkflows
                    )
                    if wf_depth == 0 and not has_running_subs:
                        run.status = "completed"
                        run.ended_at = ts
                    else:
                        # Child workflow completed — mark matching subworkflow
                        # done. Handles for-each loops that emit workflow_completed
                        # but NOT subworkflow_completed.
                        for sw in reversed(run.subworkflows):
                            if sw["status"] == "running":
                                sw["status"] = "completed"
                                break

                elif etype == "agent_failed":
                    aname = data.get("agent_name", "")
                    if aname:
                        run.failed_agent = aname

                elif etype in ("for_each_item_failed",):
                    # Deepest failure point — capture the group/agent name
                    # before workflow_failed events bubble it up.
                    if not run.failed_agent:
                        run.failed_agent = data.get("group_name", "") or data.get("agent_name", "")

                elif etype == "workflow_failed":
                    wf_depth = max(0, wf_depth - 1)
                    if wf_depth == 0:
                        run.status = "failed"
                        run.ended_at = ts
                        run.error_type = data.get("error_type", "")
                        run.error_message = data.get("message", "")
                        # Only set failed_agent from the top-level workflow_failed
                        # if no deeper event already captured the real agent.
                        if not run.failed_agent:
                            run.failed_agent = data.get("agent_name", "")
                    else:
                        # Inner workflow_failed — capture the deepest agent_name
                        # as the real failure point (first inner failure wins).
                        if not run.failed_agent:
                            run.failed_agent = data.get("agent_name", "")
                        for sw in reversed(run.subworkflows):
                            if sw["status"] == "running":
                                sw["status"] = "failed"
                                break

                elif etype == "route_taken":
                    run.routes.append(data)

                elif etype == "agent_tool_start":
                    run.tool_in_flight = True

                elif etype == "agent_tool_complete":
                    run.tool_in_flight = False

                elif etype == "subworkflow_started":
                    run.subworkflows.append({
                        "agent": data.get("agent_name", ""),
                        "workflow": data.get("workflow", ""),
                        "item_key": data.get("item_key", ""),
                        "iteration": data.get("iteration", ""),
                        "started_at": ts,
                        "status": "running",
                        "elapsed": 0,
                    })

                elif etype == "subworkflow_completed":
                    # Match to the most recent unfinished subworkflow from same agent
                    aname = data.get("agent_name", "")
                    for sw in reversed(run.subworkflows):
                        if sw["agent"] == aname and sw["status"] == "running":
                            sw["status"] = "completed"
                            sw["elapsed"] = data.get("elapsed", 0)
                            break

                elif etype == "subworkflow_failed":
                    aname = data.get("agent_name", "")
                    for sw in reversed(run.subworkflows):
                        if sw["agent"] == aname and sw["status"] == "running":
                            sw["status"] = "failed"
                            sw["elapsed"] = data.get("elapsed", 0)
                            break

                # Track latest timestamp
                if ts:
                    if ts > run.last_event_ts:
                        run.last_event_ts = ts
                    if ts > run.ended_at and run.status not in ("completed", "failed"):
                        run.ended_at = ts

    except Exception:
        run.status = "parse_error"

    run.agents = list(agents_map.values())
    run.current_agent = active_agent
    run.current_agent_type = agent_type_map.get(active_agent, "")
    if pending_gates:
        run.gate_waiting = True
        run.gate_agent = next(iter(pending_gates))

    # Build failed_subworkflow_path from subworkflows that ended in "failed".
    # Use agent name (the subworkflow node) for readability, with workflow
    # file as fallback.
    failed_sws = [sw for sw in run.subworkflows if sw["status"] == "failed"]
    if failed_sws:
        path_parts = [sw["agent"] or sw["workflow"] for sw in failed_sws]
        run.failed_subworkflow_path = " > ".join(path_parts)

    # Post-parse invariant: "completed" is impossible while subworkflows are
    # still running.  If we see this state it means depth tracking was thrown
    # off by a partially-written log line (race between conductor writing and
    # dashboard reading).  Reset to "unknown" and let the mtime classifier
    # below decide running vs interrupted.
    if run.status == "completed" and any(
        sw["status"] == "running" for sw in run.subworkflows
    ):
        run.status = "unknown"

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


# Cache parsed event logs: path -> (mtime, size, WorkflowRun)
_parsed_log_cache: dict[str, tuple[float, int, WorkflowRun]] = {}


def _load_event_logs(
    name_to_port: dict[str, int] | None = None,
    _listening_snapshot: set[int] | None = None,
) -> list[WorkflowRun]:
    runs: list[WorkflowRun] = []
    if not CONDUCTOR_DIR.exists():
        return runs
    if name_to_port is None:
        name_to_port = {}
    if _listening_snapshot is None:
        _listening_snapshot = set()
    now = time.time()
    for p in sorted(CONDUCTOR_DIR.glob("*.events.jsonl")):
        # Use cached parse result if file hasn't changed
        key = str(p)
        try:
            st = p.stat()
            cached = _parsed_log_cache.get(key)
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                run = cached[2]
                # Re-evaluate mtime-based status for cached runs.
                # Previous polls may have promoted status to "running" via
                # liveness checks (Fix #1–#4).  Reset non-terminal runs so
                # that liveness is re-evaluated from scratch each poll —
                # otherwise a dead process stays "running" in cache forever.
                recently_modified = (now - st.st_mtime) < 300
                if run.status in ("unknown", "running") and not run.ended_at:
                    run.status = "running" if recently_modified else "interrupted"
                elif recently_modified and run.status in ("failed", "completed"):
                    if run.ended_at and (st.st_mtime - run.ended_at > 30):
                        run.status = "running"
            else:
                run = _parse_event_log(p)
                # Cache terminal runs (won't change) and running ones (will be re-checked via mtime)
                _parsed_log_cache[key] = (st.st_mtime, st.st_size, run)
        except OSError:
            run = _parse_event_log(p)

        if run.status == "invalid":
            continue

        # Skip runs that declare themselves hidden via metadata
        if run.metadata.get("dashboard_hidden"):
            continue

        # Fix #1: if a conductor dashboard port matches this run, force
        # status=running regardless of mtime (backgrounded conductor runs may
        # be silent for extended periods during long tool calls or gate waits).
        def _has_live_port(run: WorkflowRun) -> bool:
            if run.run_id and run.run_id in name_to_port:
                return True
            if run.name and run.name in name_to_port:
                return True
            return False

        if run.status != "running" and run.status not in ("completed", "failed"):
            if _has_live_port(run):
                run.status = "running"

        # Fix #2: gate-waiting runs with a live dashboard port should stay running.
        if (
            run.status not in ("running", "completed", "failed")
            and run.gate_waiting
            and _has_live_port(run)
        ):
            run.status = "running"

        # Fix #3: detect in-flight foreground runs. If the last parsed event
        # is an agent_tool_start with no matching complete AND the process
        # hasn't been declared terminal, treat it as running — but only for
        # a short grace window. Backgrounded long-running runs are already
        # kept alive via the PID-match check above, so this branch only
        # affects foreground runs where the process may have died silently.
        if (
            run.status not in ("running", "completed", "failed")
            and run.tool_in_flight
            and run.last_event_ts
            # 10 min: a real in-flight tool call rarely exceeds this.
            # Beyond that, the harness almost certainly died without
            # emitting a terminal event — mark as interrupted so it
            # moves out of "Active Runs".
            and (now - run.last_event_ts) < 600
        ):
            run.status = "running"

        # Fix #4: system metadata PID-based liveness (supplementary, not authoritative).
        # If system.pid is present and alive, promote non-terminal runs to "running".
        # Uses _is_pid_from_run to cross-check process creation time against
        # the run's start time, preventing false positives from Windows PID reuse.
        system_pid = _safe_int(run.system_meta.get("pid"))
        if (
            system_pid
            and run.status not in ("running", "completed", "failed")
            and run.started_at
            and (now - run.started_at) < 86400  # 24h window for PID trust
            and _is_pid_from_run(system_pid, run.started_at)
        ):
            run.status = "running"

        # Fix #5: system metadata dashboard_port shortcut.
        # If the event log declares a dashboard_port (from $system metadata)
        # and that port is still listening, add it to name_to_port so
        # _serialize_run can find it without the expensive port scan.
        sys_port = _safe_int(run.system_meta.get("dashboard_port"))
        if sys_port and run.run_id and run.run_id not in name_to_port:
            if sys_port in _listening_snapshot:
                name_to_port[run.run_id] = sys_port
                if run.status not in ("completed", "failed"):
                    run.status = "running"

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


_port_cache: dict[str, int] | None = None
_port_cache_time: float = 0
_PORT_CACHE_TTL = 10  # seconds


def _probe_conductor_port(port: int) -> tuple[str, str] | None:
    """Probe a single port for a conductor API. Returns (run_id, wf_name) or None."""
    import urllib.request
    # Try /api/info first (fast, lightweight)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/info",
            headers={"User-Agent": "conductor-dashboard-probe"},
        )
        with urllib.request.urlopen(req, timeout=0.3) as resp:
            info = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(info, dict):
                return (info.get("run_id", ""), info.get("workflow_name", ""))
    except Exception:
        pass
    # Fallback: /api/state (heavier, parses event log)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/state",
            headers={"User-Agent": "conductor-dashboard-probe"},
        )
        with urllib.request.urlopen(req, timeout=0.3) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(data, list):
                wf_name = ""
                depth = 0
                terminal_at_root = False
                for event in data:
                    if isinstance(event, dict):
                        etype = event.get("type", "")
                        if etype == "workflow_started":
                            if depth == 0:
                                wf_name = event.get("data", {}).get("name", "")
                            depth += 1
                        elif etype in ("workflow_completed", "workflow_failed"):
                            depth = max(0, depth - 1)
                            if depth == 0:
                                terminal_at_root = True
                if terminal_at_root:
                    return None
                if wf_name:
                    return ("", wf_name)
    except Exception:
        pass
    return None


def _discover_conductor_dashboard_ports(exclude_port: int = 0) -> dict[str, int]:
    """Map run_ids to their conductor dashboard ports.

    Strategy:
      1. Read PID files (fast, no HTTP) for run_id + port.
      2. Only probe HTTP for remaining ports without PID matches.

    Results are cached for 10 seconds to avoid repeated HTTP probing.

    Returns {run_id_or_name: port}. Prefers run_id when available.
    """
    global _port_cache, _port_cache_time
    now = time.time()
    if _port_cache is not None and (now - _port_cache_time) < _PORT_CACHE_TTL:
        return _port_cache
    import urllib.request
    result: dict[str, int] = {}
    listening = _get_listening_ports()
    known_ports: set[int] = set()

    # Fast path: use PID files which have run_id and port
    if PID_DIR.exists():
        for p in sorted(PID_DIR.glob("*.pid")):
            try:
                data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                port = data.get("port", 0)
                if not port or port == exclude_port or port not in listening:
                    continue
                run_id = data.get("run_id", "")
                wf_name = data.get("workflow", "")
                if wf_name:
                    wf_name = Path(wf_name).stem
                if run_id:
                    result[run_id] = port
                elif wf_name:
                    result[wf_name] = port
                known_ports.add(port)
            except Exception:
                continue

    # Slow path: probe remaining high ports not covered by PID files
    # Use thread pool to probe in parallel (each probe has 0.3s timeout)
    candidates = [p for p in listening if p > 49000 and p != exclude_port and p not in known_ports]
    if candidates:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=min(len(candidates), 16)) as pool:
            futures = {pool.submit(_probe_conductor_port, port): port for port in candidates}
            for fut in as_completed(futures):
                port = futures[fut]
                try:
                    probe_result = fut.result()
                    if probe_result:
                        run_id, wf_name = probe_result
                        if run_id:
                            result[run_id] = port
                        elif wf_name:
                            if wf_name not in result or port > result[wf_name]:
                                result[wf_name] = port
                except Exception:
                    pass
    _port_cache = result
    _port_cache_time = time.time()
    return result


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
def _aggregate_metrics(runs: list[WorkflowRun]) -> dict[str, Any]:
    """Aggregate rich metrics across runs (server-side, all-time)."""
    by_workflow: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}
    error_types: dict[str, int] = {}
    agent_failures: dict[str, int] = {}
    total_cost = 0.0
    total_tokens = 0
    total_runs = 0
    total_completed = 0
    total_failed = 0
    for r in runs:
        total_runs += 1
        total_cost += r.total_cost
        total_tokens += r.total_tokens
        if r.status == "completed":
            total_completed += 1
        elif r.status == "failed":
            total_failed += 1
            et = r.error_type or "Unknown"
            error_types[et] = error_types.get(et, 0) + 1
            if r.failed_agent:
                agent_failures[r.failed_agent] = agent_failures.get(r.failed_agent, 0) + 1
        w = by_workflow.setdefault(r.name or "(unknown)", {
            "runs": 0, "completed": 0, "failed": 0,
            "total_cost": 0.0, "total_tokens": 0, "_durations": [],
        })
        w["runs"] += 1
        w["total_cost"] += r.total_cost
        w["total_tokens"] += r.total_tokens
        if r.status == "completed":
            w["completed"] += 1
        elif r.status == "failed":
            w["failed"] += 1
        if r.started_at and r.ended_at and r.ended_at > r.started_at:
            w["_durations"].append(r.ended_at - r.started_at)
        for a in r.agents:
            a_cost = a.cost_usd or 0.0
            a_tokens = a.tokens or 0
            a_elapsed = a.elapsed or 0.0
            if a.model:
                m = by_model.setdefault(a.model, {"cost": 0.0, "tokens": 0, "invocations": 0})
                m["cost"] += a_cost
                m["tokens"] += a_tokens
                m["invocations"] += 1
            if a.name:
                ag = by_agent.setdefault(a.name, {
                    "invocations": 0, "total_cost": 0.0,
                    "total_tokens": 0, "_elapsed_sum": 0.0,
                })
                ag["invocations"] += 1
                ag["total_cost"] += a_cost
                ag["total_tokens"] += a_tokens
                ag["_elapsed_sum"] += a_elapsed
    for w in by_workflow.values():
        durs = w.pop("_durations")
        w["total_runtime_sec"] = sum(durs) if durs else 0.0
        w["avg_duration_sec"] = (w["total_runtime_sec"] / len(durs)) if durs else 0.0
        w["success_rate"] = (w["completed"] / w["runs"]) if w["runs"] else 0.0
    for ag in by_agent.values():
        es = ag.pop("_elapsed_sum")
        ag["total_elapsed"] = es
        ag["avg_elapsed"] = (es / ag["invocations"]) if ag["invocations"] else 0.0
    top_agents_by_cost = sorted(
        [{"name": n, **v} for n, v in by_agent.items()],
        key=lambda x: -x["total_cost"],
    )[:10]
    return {
        "by_workflow": dict(sorted(by_workflow.items(), key=lambda x: -x[1]["total_cost"])),
        "by_model": dict(sorted(by_model.items(), key=lambda x: -x[1]["cost"])),
        "by_agent": dict(sorted(by_agent.items(), key=lambda x: -x[1]["total_cost"])),
        "top_agents_by_cost": top_agents_by_cost,
        "error_types": dict(sorted(error_types.items(), key=lambda x: -x[1])),
        "agent_failures": dict(sorted(agent_failures.items(), key=lambda x: -x[1])),
        "totals": {
            "cost": total_cost, "tokens": total_tokens,
            "runs": total_runs, "completed": total_completed, "failed": total_failed,
        },
    }


def _run_to_raw(r: WorkflowRun) -> dict:
    """Minimal run representation for client-side metrics recomputation."""
    duration_sec = 0.0
    if r.started_at and r.ended_at and r.ended_at > r.started_at:
        duration_sec = r.ended_at - r.started_at
    return {
        "log_file": r.log_file,
        "name": r.name,
        "status": r.status,
        "started_at": r.started_at,
        "ended_at": r.ended_at,
        "run_id": r.run_id,
        "total_cost": r.total_cost,
        "total_tokens": r.total_tokens,
        "agents": [
            {"name": a.name, "model": a.model, "tokens": a.tokens,
             "cost_usd": a.cost_usd, "elapsed": a.elapsed}
            for a in r.agents
        ],
        "failed_agent": r.failed_agent,
        "failed_subworkflow_path": r.failed_subworkflow_path,
        "error_type": r.error_type,
        "duration_sec": duration_sec,
    }


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
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
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
.run-card.abandoned { border-left: 3px solid var(--red); background: #1a1416; }
.run-card.abandoned .wf-name { opacity: 0.75; }
.run-card.abandoned .run-card-header { opacity: 0.85; }
.abandoned-badge {
    background: #f8514925;
    color: var(--red);
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.5px;
    border: 1px solid #f8514950;
}
.run-card-header {
    padding: 12px 16px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.85rem;
}
.run-card-header:hover { background: #1c2128; }

/* Powerline breadcrumb chain — clip-path approach for seamless chevrons */
.powerline { display: flex; align-items: stretch; flex-shrink: 0; }
.pl-seg {
    display: flex;
    align-items: center;
    gap: 5px;
    padding: 4px 14px 4px 12px;
    background: #2a3545;
    white-space: nowrap;
    font-size: 0.82rem;
    font-weight: 500;
    color: #8ba4c0;
    /* Clip-path: flat left, arrow right. 10px arrow depth. */
    clip-path: polygon(0 0, calc(100% - 10px) 0, 100% 50%, calc(100% - 10px) 100%, 0 100%);
    margin-right: -10px;
    position: relative;
    z-index: 1;
}
/* Segments after the first get an inset left to show previous seg's arrow color */
.pl-seg + .pl-seg {
    padding-left: 22px;
    clip-path: polygon(0 0, calc(100% - 10px) 0, 100% 50%, calc(100% - 10px) 100%, 0 100%, 10px 50%);
}
/* First segment: rounded left cap */
.pl-seg:first-child { border-radius: 4px 0 0 4px; padding-left: 10px; }
/* Last segment: rounded right cap, no arrow */
.pl-seg:last-child {
    clip-path: polygon(0 0, calc(100% - 4px) 0, 100% 4px, 100% calc(100% - 4px), calc(100% - 4px) 100%, 0 100%, 10px 50%);
    padding-right: 12px;
    margin-right: 0;
}
.pl-seg:first-child:last-child {
    clip-path: polygon(0 4px, 4px 0, calc(100% - 4px) 0, 100% 4px, 100% calc(100% - 4px), calc(100% - 4px) 100%, 4px 100%, 0 calc(100% - 4px));
    padding-left: 10px;
    border-radius: 4px;
}
/* Stacking: later segments render on top */
.pl-seg:nth-child(1) { z-index: 5; }
.pl-seg:nth-child(2) { z-index: 4; }
.pl-seg:nth-child(3) { z-index: 3; }
.pl-seg:nth-child(4) { z-index: 2; }
.pl-seg:nth-child(5) { z-index: 1; }
/* Active segment: oscillating blue — background animates and clip-path follows */
.pl-seg.active {
    background: #1a3a5c;
    color: #7ab8e8;
    animation: pl-active-pulse 2.5s ease-in-out infinite;
}
@keyframes pl-active-pulse {
    0%, 100% { background: #1a3a5c; color: #7ab8e8; }
    50% { background: #1e4470; color: #58a6ff; }
}
/* Agent segment: oscillating green */
.pl-seg.agent-seg {
    background: #1a2e1a;
    color: #4eda8a;
    animation: pl-agent-pulse 2.5s ease-in-out infinite;
}
@keyframes pl-agent-pulse {
    0%, 100% { background: #1a2e1a; color: #4eda8a; }
    50% { background: #1e3a20; color: #3fb950; }
}
.pl-seg svg { flex-shrink: 0; }
.pl-seg-name { }

.status-label {
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.5px;
}
.status-label.running { background: #238636aa; color: var(--green); border: 1px solid #23863680; }
.status-label.idle { background: #388bfd20; color: var(--blue); border: 1px solid #388bfd40; }
.status-label.gate { background: #d2992220; color: var(--yellow); border: 1px solid #d2992240; }
.run-card-body {
    padding: 0 16px;
    max-height: 0;
    overflow: hidden;
    transition: max-height 0.3s ease-out, padding 0.3s ease-out;
    font-size: 0.82rem;
    color: var(--text2);
}
.run-card-body.open {
    max-height: 1000px;
    padding: 0 16px 12px;
}
.chevron { color: var(--text2); font-size: 0.75rem; transition: transform 0.2s; display: inline-block; }
.chevron.open { transform: rotate(90deg); }

/* Work item badge */
.work-item { font-size: 0.82rem; }

/* Hierarchy status breakdown */
.hierarchy { display: inline-flex; align-items: center; gap: 10px; font-size: 0.78rem; margin-left: 8px; }
.hierarchy-level { display: inline-flex; align-items: center; gap: 3px; }
.hierarchy-label { color: var(--text2); font-weight: 500; }
.hierarchy-bar { display: inline-flex; height: 10px; border-radius: 3px; overflow: hidden; min-width: 40px; }
.hierarchy-bar .seg { height: 100%; min-width: 2px; }
.seg-done { background: var(--green); }
.seg-doing { background: var(--yellow); }
.seg-todo { background: #30363d; }
.hierarchy-counts { color: var(--text2); white-space: nowrap; }
.hierarchy-counts .done-ct { color: var(--green); }
.hierarchy-counts .doing-ct { color: var(--yellow); }
.hierarchy-counts .todo-ct { color: var(--text2); }
.hierarchy-focus { color: var(--text2); font-size: 0.78rem; }
.hierarchy-focus .state-done { color: var(--green); }
.hierarchy-focus .state-doing { color: var(--yellow); }
.hierarchy-focus .state-todo { color: var(--text2); }

/* Composition tree */
.comp-tree { margin-top: 8px; font-size: 0.8rem; }
.comp-tree .tree-node { display: flex; align-items: center; gap: 6px; padding: 3px 0; color: var(--text2); }
.comp-tree .tree-prefix { color: var(--border); font-family: monospace; white-space: pre; user-select: none; }
.comp-tree .tree-name { font-weight: 500; color: var(--text); }
.comp-tree .tree-status { min-width: 18px; text-align: center; }
.comp-tree .tree-meta { color: var(--text2); font-size: 0.75rem; }
.comp-tree .tree-section-label { color: var(--text2); font-size: 0.72rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 6px; margin-bottom: 2px; }

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
<p class="meta">Aggregated workflow status &bull; Auto-refreshes every 5s</p>

<div id="stats" class="stats"></div>

<h2>&#128260; Active Runs</h2>
<div id="active-runs"></div>

<h2 id="abandoned-heading" style="display:none">&#128123; Abandoned Runs <span id="abandoned-count" style="color:var(--text2);font-size:0.8rem;font-weight:normal"></span> <button id="toggle-abandoned" class="toggle-btn" title="Show or hide abandoned runs" onclick="toggleShowAbandoned()">Show</button></h2>
<div id="abandoned-runs" style="display:none"></div>

<h2>&#9989; Completed Runs <button id="toggle-reviewed-completed" class="toggle-btn" title="Show or hide runs you have already reviewed" onclick="toggleShowReviewedCompleted()">Show Reviewed</button></h2>
<div id="completed-runs"></div>

<h2>&#10060; Failed Runs <button id="toggle-reviewed-failed" class="toggle-btn" title="Show or hide failed runs you have already reviewed" onclick="toggleShowReviewedFailed()">Show Reviewed</button></h2>
<div id="failed-runs"></div>

<h2>&#128200; Metrics
    <span style="margin-left:auto;display:inline-flex;gap:4px;align-items:center;font-size:0.8rem;font-weight:normal">
        <span style="color:var(--text2)">Range:</span>
        <button class="toggle-btn metrics-range-btn" data-range="24h" onclick="setMetricsRange('24h')">24h</button>
        <button class="toggle-btn metrics-range-btn" data-range="7d" onclick="setMetricsRange('7d')">7d</button>
        <button class="toggle-btn metrics-range-btn" data-range="30d" onclick="setMetricsRange('30d')">30d</button>
        <button class="toggle-btn metrics-range-btn" data-range="all" onclick="setMetricsRange('all')">All</button>
    </span>
</h2>
<div id="metrics-totals" style="margin-bottom:12px"></div>
<div class="grid">
    <div><h3 style="font-size:0.9rem;color:var(--text2);margin-bottom:6px">By Workflow</h3><div id="metrics-by-workflow"></div></div>
    <div><h3 style="font-size:0.9rem;color:var(--text2);margin-bottom:6px">By Model</h3><div id="metrics-by-model"></div></div>
</div>
<div class="grid">
    <div><h3 style="font-size:0.9rem;color:var(--text2);margin-bottom:6px">By Agent</h3><div id="metrics-by-agent"></div></div>
    <div><h3 style="font-size:0.9rem;color:var(--text2);margin-bottom:6px">Top Agents by Cost</h3><div id="metrics-top-agents"></div></div>
</div>
<div class="grid">
    <div><h3 style="font-size:0.9rem;color:var(--text2);margin-bottom:6px">Error Types</h3><div id="metrics-error-types"></div></div>
    <div><h3 style="font-size:0.9rem;color:var(--text2);margin-bottom:6px">Agent Failures</h3><div id="metrics-agent-failures"></div></div>
</div>

<script>
// morphdom — DOM diffing library (2.7.4, inlined)
(function(global,factory){typeof exports==="object"&&typeof module!=="undefined"?module.exports=factory():typeof define==="function"&&define.amd?define(factory):(global=global||self,global.morphdom=factory())})(this,function(){"use strict";var DOCUMENT_FRAGMENT_NODE=11;function morphAttrs(fromNode,toNode){var toNodeAttrs=toNode.attributes;var attr;var attrName;var attrNamespaceURI;var attrValue;var fromValue;if(toNode.nodeType===DOCUMENT_FRAGMENT_NODE||fromNode.nodeType===DOCUMENT_FRAGMENT_NODE){return}for(var i=toNodeAttrs.length-1;i>=0;i--){attr=toNodeAttrs[i];attrName=attr.name;attrNamespaceURI=attr.namespaceURI;attrValue=attr.value;if(attrNamespaceURI){attrName=attr.localName||attrName;fromValue=fromNode.getAttributeNS(attrNamespaceURI,attrName);if(fromValue!==attrValue){if(attr.prefix==="xmlns"){attrName=attr.name}fromNode.setAttributeNS(attrNamespaceURI,attrName,attrValue)}}else{fromValue=fromNode.getAttribute(attrName);if(fromValue!==attrValue){fromNode.setAttribute(attrName,attrValue)}}}var fromNodeAttrs=fromNode.attributes;for(var d=fromNodeAttrs.length-1;d>=0;d--){attr=fromNodeAttrs[d];attrName=attr.name;attrNamespaceURI=attr.namespaceURI;if(attrNamespaceURI){attrName=attr.localName||attrName;if(!toNode.hasAttributeNS(attrNamespaceURI,attrName)){fromNode.removeAttributeNS(attrNamespaceURI,attrName)}}else{if(!toNode.hasAttribute(attrName)){fromNode.removeAttribute(attrName)}}}}var range;var NS_XHTML="http://www.w3.org/1999/xhtml";var doc=typeof document==="undefined"?undefined:document;var HAS_TEMPLATE_SUPPORT=!!doc&&"content"in doc.createElement("template");var HAS_RANGE_SUPPORT=!!doc&&doc.createRange&&"createContextualFragment"in doc.createRange();function createFragmentFromTemplate(str){var template=doc.createElement("template");template.innerHTML=str;return template.content.childNodes[0]}function createFragmentFromRange(str){if(!range){range=doc.createRange();range.selectNode(doc.body)}var fragment=range.createContextualFragment(str);return fragment.childNodes[0]}function createFragmentFromWrap(str){var fragment=doc.createElement("body");fragment.innerHTML=str;return fragment.childNodes[0]}function toElement(str){str=str.trim();if(HAS_TEMPLATE_SUPPORT){return createFragmentFromTemplate(str)}else if(HAS_RANGE_SUPPORT){return createFragmentFromRange(str)}return createFragmentFromWrap(str)}function compareNodeNames(fromEl,toEl){var fromNodeName=fromEl.nodeName;var toNodeName=toEl.nodeName;var fromCodeStart,toCodeStart;if(fromNodeName===toNodeName){return true}fromCodeStart=fromNodeName.charCodeAt(0);toCodeStart=toNodeName.charCodeAt(0);if(fromCodeStart<=90&&toCodeStart>=97){return fromNodeName===toNodeName.toUpperCase()}else if(toCodeStart<=90&&fromCodeStart>=97){return toNodeName===fromNodeName.toUpperCase()}else{return false}}function createElementNS(name,namespaceURI){return!namespaceURI||namespaceURI===NS_XHTML?doc.createElement(name):doc.createElementNS(namespaceURI,name)}function moveChildren(fromEl,toEl){var curChild=fromEl.firstChild;while(curChild){var nextChild=curChild.nextSibling;toEl.appendChild(curChild);curChild=nextChild}return toEl}function syncBooleanAttrProp(fromEl,toEl,name){if(fromEl[name]!==toEl[name]){fromEl[name]=toEl[name];if(fromEl[name]){fromEl.setAttribute(name,"")}else{fromEl.removeAttribute(name)}}}var specialElHandlers={OPTION:function(fromEl,toEl){var parentNode=fromEl.parentNode;if(parentNode){var parentName=parentNode.nodeName.toUpperCase();if(parentName==="OPTGROUP"){parentNode=parentNode.parentNode;parentName=parentNode&&parentNode.nodeName.toUpperCase()}if(parentName==="SELECT"&&!parentNode.hasAttribute("multiple")){if(fromEl.hasAttribute("selected")&&!toEl.selected){fromEl.setAttribute("selected","selected");fromEl.removeAttribute("selected")}parentNode.selectedIndex=-1}}syncBooleanAttrProp(fromEl,toEl,"selected")},INPUT:function(fromEl,toEl){syncBooleanAttrProp(fromEl,toEl,"checked");syncBooleanAttrProp(fromEl,toEl,"disabled");if(fromEl.value!==toEl.value){fromEl.value=toEl.value}if(!toEl.hasAttribute("value")){fromEl.removeAttribute("value")}},TEXTAREA:function(fromEl,toEl){var newValue=toEl.value;if(fromEl.value!==newValue){fromEl.value=newValue}var firstChild=fromEl.firstChild;if(firstChild){var oldValue=firstChild.nodeValue;if(oldValue==newValue||!newValue&&oldValue==fromEl.placeholder){return}firstChild.nodeValue=newValue}},SELECT:function(fromEl,toEl){if(!toEl.hasAttribute("multiple")){var selectedIndex=-1;var i=0;var curChild=fromEl.firstChild;var optgroup;var nodeName;while(curChild){nodeName=curChild.nodeName&&curChild.nodeName.toUpperCase();if(nodeName==="OPTGROUP"){optgroup=curChild;curChild=optgroup.firstChild}else{if(nodeName==="OPTION"){if(curChild.hasAttribute("selected")){selectedIndex=i;break}i++}curChild=curChild.nextSibling;if(!curChild&&optgroup){curChild=optgroup.nextSibling;optgroup=null}}}fromEl.selectedIndex=selectedIndex}}};var ELEMENT_NODE=1;var DOCUMENT_FRAGMENT_NODE$1=11;var TEXT_NODE=3;var COMMENT_NODE=8;function noop(){}function defaultGetNodeKey(node){if(node){return node.getAttribute&&node.getAttribute("id")||node.id}}function morphdomFactory(morphAttrs){return function morphdom(fromNode,toNode,options){if(!options){options={}}if(typeof toNode==="string"){if(fromNode.nodeName==="#document"||fromNode.nodeName==="HTML"||fromNode.nodeName==="BODY"){var toNodeHtml=toNode;toNode=doc.createElement("html");toNode.innerHTML=toNodeHtml}else{toNode=toElement(toNode)}}else if(toNode.nodeType===DOCUMENT_FRAGMENT_NODE$1){toNode=toNode.firstElementChild}var getNodeKey=options.getNodeKey||defaultGetNodeKey;var onBeforeNodeAdded=options.onBeforeNodeAdded||noop;var onNodeAdded=options.onNodeAdded||noop;var onBeforeElUpdated=options.onBeforeElUpdated||noop;var onElUpdated=options.onElUpdated||noop;var onBeforeNodeDiscarded=options.onBeforeNodeDiscarded||noop;var onNodeDiscarded=options.onNodeDiscarded||noop;var onBeforeElChildrenUpdated=options.onBeforeElChildrenUpdated||noop;var skipFromChildren=options.skipFromChildren||noop;var addChild=options.addChild||function(parent,child){return parent.appendChild(child)};var childrenOnly=options.childrenOnly===true;var fromNodesLookup=Object.create(null);var keyedRemovalList=[];function addKeyedRemoval(key){keyedRemovalList.push(key)}function walkDiscardedChildNodes(node,skipKeyedNodes){if(node.nodeType===ELEMENT_NODE){var curChild=node.firstChild;while(curChild){var key=undefined;if(skipKeyedNodes&&(key=getNodeKey(curChild))){addKeyedRemoval(key)}else{onNodeDiscarded(curChild);if(curChild.firstChild){walkDiscardedChildNodes(curChild,skipKeyedNodes)}}curChild=curChild.nextSibling}}}function removeNode(node,parentNode,skipKeyedNodes){if(onBeforeNodeDiscarded(node)===false){return}if(parentNode){parentNode.removeChild(node)}onNodeDiscarded(node);walkDiscardedChildNodes(node,skipKeyedNodes)}function indexTree(node){if(node.nodeType===ELEMENT_NODE||node.nodeType===DOCUMENT_FRAGMENT_NODE$1){var curChild=node.firstChild;while(curChild){var key=getNodeKey(curChild);if(key){fromNodesLookup[key]=curChild}indexTree(curChild);curChild=curChild.nextSibling}}}indexTree(fromNode);function handleNodeAdded(el){onNodeAdded(el);var curChild=el.firstChild;while(curChild){var nextSibling=curChild.nextSibling;var key=getNodeKey(curChild);if(key){var unmatchedFromEl=fromNodesLookup[key];if(unmatchedFromEl&&compareNodeNames(curChild,unmatchedFromEl)){curChild.parentNode.replaceChild(unmatchedFromEl,curChild);morphEl(unmatchedFromEl,curChild)}else{handleNodeAdded(curChild)}}else{handleNodeAdded(curChild)}curChild=nextSibling}}function cleanupFromEl(fromEl,curFromNodeChild,curFromNodeKey){while(curFromNodeChild){var fromNextSibling=curFromNodeChild.nextSibling;if(curFromNodeKey=getNodeKey(curFromNodeChild)){addKeyedRemoval(curFromNodeKey)}else{removeNode(curFromNodeChild,fromEl,true)}curFromNodeChild=fromNextSibling}}function morphEl(fromEl,toEl,childrenOnly){var toElKey=getNodeKey(toEl);if(toElKey){delete fromNodesLookup[toElKey]}if(!childrenOnly){var beforeUpdateResult=onBeforeElUpdated(fromEl,toEl);if(beforeUpdateResult===false){return}else if(beforeUpdateResult instanceof HTMLElement){fromEl=beforeUpdateResult;indexTree(fromEl)}morphAttrs(fromEl,toEl);onElUpdated(fromEl);if(onBeforeElChildrenUpdated(fromEl,toEl)===false){return}}if(fromEl.nodeName!=="TEXTAREA"){morphChildren(fromEl,toEl)}else{specialElHandlers.TEXTAREA(fromEl,toEl)}}function morphChildren(fromEl,toEl){var skipFrom=skipFromChildren(fromEl,toEl);var curToNodeChild=toEl.firstChild;var curFromNodeChild=fromEl.firstChild;var curToNodeKey;var curFromNodeKey;var fromNextSibling;var toNextSibling;var matchingFromEl;outer:while(curToNodeChild){toNextSibling=curToNodeChild.nextSibling;curToNodeKey=getNodeKey(curToNodeChild);while(!skipFrom&&curFromNodeChild){fromNextSibling=curFromNodeChild.nextSibling;if(curToNodeChild.isSameNode&&curToNodeChild.isSameNode(curFromNodeChild)){curToNodeChild=toNextSibling;curFromNodeChild=fromNextSibling;continue outer}curFromNodeKey=getNodeKey(curFromNodeChild);var curFromNodeType=curFromNodeChild.nodeType;var isCompatible=undefined;if(curFromNodeType===curToNodeChild.nodeType){if(curFromNodeType===ELEMENT_NODE){if(curToNodeKey){if(curToNodeKey!==curFromNodeKey){if(matchingFromEl=fromNodesLookup[curToNodeKey]){if(fromNextSibling===matchingFromEl){isCompatible=false}else{fromEl.insertBefore(matchingFromEl,curFromNodeChild);if(curFromNodeKey){addKeyedRemoval(curFromNodeKey)}else{removeNode(curFromNodeChild,fromEl,true)}curFromNodeChild=matchingFromEl;curFromNodeKey=getNodeKey(curFromNodeChild)}}else{isCompatible=false}}}else if(curFromNodeKey){isCompatible=false}isCompatible=isCompatible!==false&&compareNodeNames(curFromNodeChild,curToNodeChild);if(isCompatible){morphEl(curFromNodeChild,curToNodeChild)}}else if(curFromNodeType===TEXT_NODE||curFromNodeType==COMMENT_NODE){isCompatible=true;if(curFromNodeChild.nodeValue!==curToNodeChild.nodeValue){curFromNodeChild.nodeValue=curToNodeChild.nodeValue}}}if(isCompatible){curToNodeChild=toNextSibling;curFromNodeChild=fromNextSibling;continue outer}if(curFromNodeKey){addKeyedRemoval(curFromNodeKey)}else{removeNode(curFromNodeChild,fromEl,true)}curFromNodeChild=fromNextSibling}if(curToNodeKey&&(matchingFromEl=fromNodesLookup[curToNodeKey])&&compareNodeNames(matchingFromEl,curToNodeChild)){if(!skipFrom){addChild(fromEl,matchingFromEl)}morphEl(matchingFromEl,curToNodeChild)}else{var onBeforeNodeAddedResult=onBeforeNodeAdded(curToNodeChild);if(onBeforeNodeAddedResult!==false){if(onBeforeNodeAddedResult){curToNodeChild=onBeforeNodeAddedResult}if(curToNodeChild.actualize){curToNodeChild=curToNodeChild.actualize(fromEl.ownerDocument||doc)}addChild(fromEl,curToNodeChild);handleNodeAdded(curToNodeChild)}}curToNodeChild=toNextSibling;curFromNodeChild=fromNextSibling}cleanupFromEl(fromEl,curFromNodeChild,curFromNodeKey);var specialElHandler=specialElHandlers[fromEl.nodeName];if(specialElHandler){specialElHandler(fromEl,toEl)}}var morphedNode=fromNode;var morphedNodeType=morphedNode.nodeType;var toNodeType=toNode.nodeType;if(!childrenOnly){if(morphedNodeType===ELEMENT_NODE){if(toNodeType===ELEMENT_NODE){if(!compareNodeNames(fromNode,toNode)){onNodeDiscarded(fromNode);morphedNode=moveChildren(fromNode,createElementNS(toNode.nodeName,toNode.namespaceURI))}}else{morphedNode=toNode}}else if(morphedNodeType===TEXT_NODE||morphedNodeType===COMMENT_NODE){if(toNodeType===morphedNodeType){if(morphedNode.nodeValue!==toNode.nodeValue){morphedNode.nodeValue=toNode.nodeValue}return morphedNode}else{morphedNode=toNode}}}if(morphedNode===toNode){onNodeDiscarded(fromNode)}else{if(toNode.isSameNode&&toNode.isSameNode(morphedNode)){return}morphEl(morphedNode,toNode,childrenOnly);if(keyedRemovalList){for(var i=0,len=keyedRemovalList.length;i<len;i++){var elToRemove=fromNodesLookup[keyedRemovalList[i]];if(elToRemove){removeNode(elToRemove,elToRemove.parentNode,false)}}}}if(!childrenOnly&&morphedNode!==fromNode&&fromNode.parentNode){if(morphedNode.actualize){morphedNode=morphedNode.actualize(fromNode.ownerDocument||doc)}fromNode.parentNode.replaceChild(morphedNode,fromNode)}return morphedNode}}var morphdom=morphdomFactory(morphAttrs);return morphdom});
</script>
<script>
// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let dashboardData = null;
let previousData = null;
const reviewedRuns = new Set(JSON.parse(localStorage.getItem('conductor-reviewed-runs') || '[]'));
let showReviewedCompleted = false;
let showReviewedFailed = false;
let showAbandoned = localStorage.getItem('conductor-show-abandoned') === '1';
const expandedRuns = new Set();

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function esc(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function jsEsc(s) {
    if (!s) return '';
    return String(s).replace(/\\\\/g, '\\\\\\\\').replace(/'/g, "\\\\'");
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
        ? '<a href="'+esc(r.work_item_url)+'" target="_blank">#'+esc(r.work_item_id)+(r.work_item_title ? ' '+esc(r.work_item_title) : '')+'</a>'
        : '#'+esc(r.work_item_id);
    var titleHtml = (r.work_item_title && !r.work_item_url) ? ' '+esc(r.work_item_title) : '';
    return '<span class="work-item">&#128203; '+typeSpan+idHtml+titleHtml+'</span>';
}

function hierarchyHtml(r) {
    var h = r.hierarchy;
    if (!h || !h.focus) return '';
    var html = '<span class="hierarchy">';

    // Always show focus item type and state
    var focusClass = h.focus.state === 'Done' ? 'done-ct' : (h.focus.state === 'Doing' ? 'doing-ct' : 'todo-ct');
    // Show ancestor chain before focus if it has parents
    if (h.ancestors && h.ancestors.length > 0) {
        for (var a = h.ancestors.length - 1; a >= 0; a--) {
            var anc = h.ancestors[a];
            var ancClass = anc.state === 'Done' ? 'done-ct' : (anc.state === 'Doing' ? 'doing-ct' : 'todo-ct');
            html += '<span class="hierarchy-focus"><span class="'+ancClass+'">' + esc(anc.type) + '</span> \\u203A </span>';
        }
    }
    html += '<span class="hierarchy-focus"><span class="'+focusClass+'">' + esc(h.focus.type) + ' (' + esc(h.focus.state) + ')</span></span>';

    // Show child level progress bars or "no children" message
    if (h.levels && h.levels.length > 0) {
        html += ' \\u203A ';
        for (var i = 0; i < h.levels.length; i++) {
            var lv = h.levels[i];
            var total = lv.total || 1;
            var donePct = Math.round((lv.Done / total) * 100);
            var doingPct = Math.round((lv.Doing / total) * 100);
            var todoPct = 100 - donePct - doingPct;
            html += '<span class="hierarchy-level">';
            html += '<span class="hierarchy-label">'+esc(lv.type)+':</span>';
            html += '<span class="hierarchy-bar" title="'+lv.Done+' Done, '+lv.Doing+' Doing, '+(lv['To Do'])+' To Do">';
            if (lv.Done > 0) html += '<span class="seg seg-done" style="width:'+donePct+'%"></span>';
            if (lv.Doing > 0) html += '<span class="seg seg-doing" style="width:'+doingPct+'%"></span>';
            if (lv['To Do'] > 0) html += '<span class="seg seg-todo" style="width:'+todoPct+'%"></span>';
            html += '</span>';
            html += '<span class="hierarchy-counts">';
            var parts = [];
            if (lv.Done > 0) parts.push('<span class="done-ct">'+lv.Done+'\\u2714</span>');
            if (lv.Doing > 0) parts.push('<span class="doing-ct">'+lv.Doing+'\\u2699</span>');
            if (lv['To Do'] > 0) parts.push('<span class="todo-ct">'+lv['To Do']+'\\u25cb</span>');
            html += parts.join(' ');
            html += '</span></span>';
        }
    } else {
        html += '<span style="color:var(--text2);opacity:0.7;font-size:0.78rem"> \\u203A no child work items planned yet</span>';
    }
    html += '</span>';
    return html;
}

function worktreeBadge(r) {
    var wt = r.worktree;
    if (!wt || (!wt.branch && !wt.name)) return '';
    // 📁 <worktree-dir-name> · 🌿 <branch>
    // Linked worktrees get a distinct icon so they stand out from the primary checkout.
    var dirIcon = wt.is_worktree ? '&#128230;' : '&#128193;'; // 📦 vs 📁
    var parts = [];
    if (wt.name) parts.push(dirIcon + ' ' + esc(wt.name));
    if (wt.branch) parts.push('&#127807; ' + esc(wt.branch));
    var sep = ' <span style="color:var(--border)">\u00b7</span> ';
    return '<span class="worktree-badge" style="font-size:0.78rem;color:var(--text2);margin-left:6px">' + parts.join(sep) + '</span>';
}

var _STATUS_ICON = {completed: '\\u2705', failed: '\\u274c', running: '\\ud83d\\udd04', unknown: '\\u2753'};

function compositionTreeHtml(r) {
    var subs = r.subworkflows;
    if (!subs || subs.length === 0) return '';
    // Group inline subworkflows by workflow name
    var subsByWf = {};
    for (var s = 0; s < subs.length; s++) {
        var sw = subs[s];
        var wfName = sw.workflow.replace('./', '').replace('.yaml', '');
        if (!subsByWf[wfName]) subsByWf[wfName] = {done: 0, running: 0, total: 0};
        subsByWf[wfName].total++;
        if (sw.status === 'completed') subsByWf[wfName].done++;
        else subsByWf[wfName].running++;
    }
    var keys = Object.keys(subsByWf);
    var html = '<div class="comp-tree">';
    html += '<div class="tree-section-label">Subworkflows</div>';
    for (var k = 0; k < keys.length; k++) {
        var wfName = keys[k];
        var info = subsByWf[wfName];
        var subIcon = (info.done === info.total) ? '\\u2705' : '\\ud83d\\udd04';
        var prefix = (k === keys.length - 1) ? '\\u2514\\u2500 ' : '\\u251c\\u2500 ';
        html += '<div class="tree-node">';
        html += '<span class="tree-prefix">' + prefix + '</span>';
        html += '<span class="tree-status">' + subIcon + '</span>';
        html += '<span class="tree-name">' + esc(wfName) + '</span>';
        html += '<span class="tree-meta">\\u00d7' + info.total;
        if (info.done > 0 && info.done < info.total) html += ' (' + info.done + '/' + info.total + ' done)';
        html += '</span>';
        html += '</div>';
    }
    html += '</div>';
    return html;
}

// ---------------------------------------------------------------------------
// Fetch
// ---------------------------------------------------------------------------
async function fetchDashboard() {
    try {
        var reviewedParam = reviewedRuns.size > 0 ? '?reviewed=' + encodeURIComponent([...reviewedRuns].join(',')) : '';
        const resp = await fetch('/api/dashboard' + reviewedParam);
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
    var html =
        '<div class="stat"><div class="label">Active Now</div><div class="value yellow">'+stats.active+'</div></div>' +
        '<div class="stat"><div class="label">Gates Waiting</div><div class="value yellow">'+stats.gates_waiting+'</div></div>' +
        '<div class="stat"><div class="label">Abandoned</div><div class="value red">'+(stats.abandoned||0)+'</div></div>';
    patchEl('stats', html);
}

// ---------------------------------------------------------------------------
// Render: Active Runs (collapsible cards)
// ---------------------------------------------------------------------------
function renderActiveRuns(runs) {
    if (!runs || runs.length === 0) {
        patchEl('active-runs', '<div class="empty" style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:20px;margin-bottom:20px;">No active workflows</div>');
        return;
    }
    var html = '';
    for (var i = 0; i < runs.length; i++) {
        html += renderRunCard(runs[i], i, 'active');
    }
    patchEl('active-runs', html);
}

function renderRunCard(r, i, keyPrefix) {
        var key = r.log_file || (keyPrefix+'-'+i);
        var isExpanded = expandedRuns.has(key);
        var isAbandoned = !r.process_alive;
        var gateClass = r.gate_waiting ? ' gate-waiting' : '';
        if (isAbandoned) gateClass += ' abandoned';

        // Compact status label
        var statusLabel;
        if (r.gate_waiting) {
            if (isAbandoned) {
                statusLabel = '<span class="abandoned-badge">GATE ABANDONED</span>';
            } else {
                statusLabel = '<span class="gate-pulse">&#128678;</span> <span class="status-label gate">Human Gate \\u2014 '+esc(r.gate_agent)+'</span>';
            }
        } else if (isAbandoned) {
            statusLabel = '<span class="abandoned-badge">ABANDONED</span>';
        } else if (r.current_agent) {
            statusLabel = '<span class="status-label running">Running</span>';
        } else {
            statusLabel = '<span class="status-label idle">Idle</span>';
        }

        // Lucide SVG icons matching conductor web app (14x14)
        var svgBot = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/></svg>';
        var svgShield = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/></svg>';
        var svgTerminal = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/></svg>';
        var svgLayers = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/></svg>';
        function agentTypeIcon(atype) {
            switch ((atype || '').toLowerCase()) {
                case 'human_gate': return svgShield;
                case 'script': return svgTerminal;
                case 'workflow': return svgLayers;
                default: return svgBot;
            }
        }

        // Build powerline segments: workflows + active agent as final segment
        var plSegments = [];
        var runningSubs = (r.subworkflows || []).filter(function(s) { return s.status === 'running'; });
        plSegments.push({ name: r.name, type: 'workflow', isActive: runningSubs.length === 0 });
        for (var si = 0; si < runningSubs.length; si++) {
            var sw = runningSubs[si];
            var swName = (sw.workflow || '').replace('./', '').replace('.yaml', '');
            plSegments.push({ name: swName, type: 'workflow', isActive: si === runningSubs.length - 1 });
        }
        // Active agent as final segment
        var activeAgent = r.current_agent || '';
        var activeAgentType = r.current_agent_type || 'agent';

        var wtHtml = worktreeBadge(r);

        var html = '<div class="run-card fade-in'+gateClass+'">';

        // --- Row 1: Workflow identity + runtime info ---
        html += '<div class="run-card-header" title="Click to expand details" onclick="toggleExpand(\\''+jsEsc(key)+'\\') ">';
        html += '<span class="chevron'+(isExpanded?' open':'')+'">&#9654;</span>';
        html += '<div class="powerline">';
        for (var pi = 0; pi < plSegments.length; pi++) {
            var seg = plSegments[pi];
            var segClass = seg.isActive ? ' active' : '';
            html += '<div class="pl-seg'+segClass+'">';
            html += svgLayers;
            html += '<span class="pl-seg-name">'+esc(seg.name)+'</span>';
            html += '</div>';
        }
        // Agent segment (final, green oscillating)
        if (activeAgent) {
            html += '<div class="pl-seg agent-seg">';
            html += agentTypeIcon(activeAgentType);
            html += '<span class="pl-seg-name">'+esc(activeAgent)+'</span>';
            html += '</div>';
        }
        html += '</div>';
        html += wtHtml;
        // Elapsed: client-side ticking for running, static for terminal
        if (r.status === 'running' && r.started_at) {
            html += '<span class="live-elapsed" data-started="'+r.started_at+'" style="color:var(--text2);margin-left:auto"></span>';
        } else {
            html += '<span style="color:var(--text2);margin-left:auto">'+esc(r.elapsed)+'</span>';
        }
        html += '<span>'+statusLabel+'</span>';
        html += '<span>'+fmtCost(r.total_cost)+'</span>';
        if (r.dashboard_url) {
            html += '<a class="action-btn" href="'+esc(r.dashboard_url)+'" target="_blank" title="Open per-run conductor dashboard" onclick="event.stopPropagation()" style="margin-left:8px;text-decoration:none">&#128279; Dashboard</a>';
        }
        html += '</div>';

        // --- Row 2: Work item info + hierarchy (always visible, not in expandable body) ---
        var wiHtml = workItemHtml(r);
        var hiHtml = hierarchyHtml(r);
        if (wiHtml || hiHtml) {
            html += '<div style="display:flex;align-items:center;gap:8px;padding:2px 12px 6px 32px;font-size:0.82rem;flex-wrap:wrap">';
            if (wiHtml) html += wiHtml;
            if (hiHtml) html += hiHtml;
            html += '</div>';
        }

        // Expanded body — focused on context, not agent minutiae
        html += '<div class="run-card-body'+(isExpanded?' open':'')+'">';

        // Worktree info (prominent)
        var wt = r.worktree;
        if (wt && (wt.branch || wt.name)) {
            var dirIcon = wt.is_worktree ? '&#128230;' : '&#128193;';
            html += '<div style="margin-bottom:8px;font-size:0.85rem">';
            if (wt.name) html += dirIcon + ' <strong>' + esc(wt.name) + '</strong>';
            if (wt.branch) html += ' &nbsp;&#127807; <span style="color:var(--accent)">' + esc(wt.branch) + '</span>';
            html += '</div>';
        }

        // Work item hierarchy
        if (r.hierarchy) {
            var h = r.hierarchy;
            html += '<div style="margin-bottom:8px">';
            var focusStateClass = h.focus.state === 'Done' ? 'done-ct' : (h.focus.state === 'Doing' ? 'doing-ct' : 'todo-ct');
            html += '<div style="font-size:0.85rem;margin-bottom:4px"><strong>' + esc(h.focus.type) + ' #' + esc(String(h.focus.id)) + '</strong> ';
            html += '<span class="' + focusStateClass + '">' + esc(h.focus.state) + '</span>';
            html += ' &mdash; ' + esc(h.focus.title) + '</div>';
            for (var lv = 0; lv < h.levels.length; lv++) {
                var level = h.levels[lv];
                var total = level.total || 1;
                var donePct = Math.round((level.Done / total) * 100);
                var doingPct = Math.round((level.Doing / total) * 100);
                var todoPct = 100 - donePct - doingPct;
                html += '<div style="display:flex;align-items:center;gap:6px;font-size:0.8rem;margin-bottom:2px">';
                html += '<span style="min-width:40px;color:var(--text2)">' + esc(level.type) + '</span>';
                html += '<span class="hierarchy-bar" style="flex:1;max-width:200px" title="' + level.Done + ' Done, ' + level.Doing + ' Doing, ' + level['To Do'] + ' To Do">';
                if (level.Done > 0) html += '<span class="seg seg-done" style="width:' + donePct + '%"></span>';
                if (level.Doing > 0) html += '<span class="seg seg-doing" style="width:' + doingPct + '%"></span>';
                if (level['To Do'] > 0) html += '<span class="seg seg-todo" style="width:' + todoPct + '%"></span>';
                html += '</span>';
                html += '<span style="color:var(--text2)">' + level.Done + '/' + total + ' done</span>';
                html += '</div>';
            }
            html += '</div>';
        }

        // Composition tree (active child workflows)
        html += compositionTreeHtml(r);

        // Dashboard link + replay (compact footer)
        if (r.dashboard_url) {
            html += '<div style="margin-top:6px"><a class="action-btn" href="'+esc(r.dashboard_url)+'" target="_blank" title="Open per-run conductor dashboard" style="text-decoration:none;display:inline-block">&#128279; Dashboard :'+r.dashboard_port+'</a></div>';
        }
        html += '<div style="margin-top:4px"><code class="replay-cmd">'+esc(r.replay_cmd)+'</code></div>';
        html += '</div></div>';
        return html;
}

// ---------------------------------------------------------------------------
// Render: Completed Runs
// ---------------------------------------------------------------------------
function renderCompletedRuns(runs) {
    var el = document.getElementById('completed-runs');
    if (!runs || runs.length === 0) {
        patchEl('completed-runs', '<table><tbody><tr><td class="empty" colspan="7">No completed runs</td></tr></tbody></table>');
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
        var wtHtml = worktreeBadge(r);
        var hiHtml = hierarchyHtml(r);
        var nameExtra = wiHtml ? '<br>'+wiHtml+wtHtml+hiHtml : (r.purpose ? '<br><span style="color:var(--text2);font-size:0.75rem">'+esc(r.purpose)+'</span>'+wtHtml+hiHtml : wtHtml+hiHtml);

        html += '<tr class="status-completed fade-in'+reviewedClass+'" style="cursor:pointer" title="Click to expand details" onclick="toggleExpand(\\'completed-'+jsEsc(key)+'\\') ">';
        html += '<td class="wf-name"><span class="chevron'+(isExpanded?' open':'')+'">&#9654;</span> '+esc(r.name)+nameExtra+'</td>';
        html += '<td class="ts">'+esc(r.started_at_str)+'</td>';
        html += '<td>'+esc(r.elapsed)+'</td>';
        html += '<td>'+fmtCost(r.total_cost)+'</td>';
        html += '<td>'+fmtTokens(r.total_tokens)+'</td>';
        html += '<td>'+r.agent_count+'</td>';
        html += '<td>';
        if (r.review_available) {
            html += '<button class="action-btn review" title="File closeout findings using the closeout-filing skill" onclick="event.stopPropagation();actionReview(\\''+jsEsc(key)+'\\')">&#128203; Review</button>';
        } else {
            html += '<button class="action-btn" disabled title="Skill not found: '+esc(r.review_skill_path)+'" style="opacity:0.4;cursor:not-allowed">&#128203; Review</button>';
        }
        html += '<button class="action-btn'+(isReviewed?' reviewed-btn':'')+'" title="'+(isReviewed?'Unmark as reviewed':'Mark as reviewed — hides from default view')+'" onclick="event.stopPropagation();toggleReviewed(\\''+jsEsc(key)+'\\')">'+( isReviewed ? '&#9745; Reviewed' : '&#9744; Mark Reviewed')+'</button>';
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
        html += compositionTreeHtml(r);
        html += '<div style="margin-top:4px"><code class="replay-cmd">'+esc(r.replay_cmd)+'</code></div>';
        html += '</td></tr>';
    }
    html += '</tbody></table>';
    patchEl('completed-runs', html);
}

// ---------------------------------------------------------------------------
// Render: Failed Runs
// ---------------------------------------------------------------------------
function renderFailedRuns(runs) {
    var el = document.getElementById('failed-runs');
    if (!runs || runs.length === 0) {
        patchEl('failed-runs', '<table><tbody><tr><td class="empty" colspan="8">No failed runs</td></tr></tbody></table>');
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
        var wtHtml = worktreeBadge(r);
        var hiHtml = hierarchyHtml(r);
        var nameExtra = (wiHtml ? '<br>'+wiHtml : '') + wtHtml + hiHtml;
        var errMsgShort = r.error_message ? (r.error_message.length > 80 ? esc(r.error_message.substring(0,80))+'\\u2026' : esc(r.error_message)) : '\\u2014';

        html += '<tr class="status-failed fade-in'+reviewedClass+'" style="cursor:pointer" title="Click to expand error details" onclick="toggleExpand(\\'failed-'+jsEsc(key)+'\\') ">';
        html += '<td class="wf-name"><span class="chevron'+(isExpanded?' open':'')+'">&#9654;</span> '+esc(r.name)+nameExtra+'</td>';
        html += '<td class="ts">'+esc(r.started_at_str)+'</td>';
        html += '<td>'+esc(r.elapsed)+'</td>';
        html += '<td>'+(r.error_type ? '<span class="err-type">'+esc(r.error_type)+'</span>' : '\\u2014')+'</td>';
        html += '<td>'+(r.failed_agent ? '<span class="err-agent">'+esc(r.failed_agent)+'</span>' : '\\u2014')+'</td>';
        html += '<td>'+errMsgShort+'</td>';
        html += '<td>';
        html += '<button class="action-btn investigate" title="Open Copilot to analyze the failure and advise on fixes" onclick="event.stopPropagation();actionInvestigate(\\''+jsEsc(key)+'\\')">&#128269; Investigate</button>';
        html += '<button class="action-btn restart" title="Re-run this workflow from scratch" onclick="event.stopPropagation();actionRestart(\\''+jsEsc(key)+'\\')">&#128260; Restart</button>';
        html += '<button class="action-btn'+(isReviewed?' reviewed-btn':'')+'" title="'+(isReviewed?'Unmark as reviewed':'Mark as reviewed — hides from default view')+'" onclick="event.stopPropagation();toggleReviewed(\\''+jsEsc(key)+'\\')">'+( isReviewed ? '&#9745; Reviewed' : '&#9744; Mark Reviewed')+'</button>';
        html += '</td></tr>';

        // Expandable detail row — full error message
        html += '<tr class="row-detail'+(isExpanded?' open':'')+'"><td colspan="7">';
        html += '<div><strong>Full Error:</strong></div>';
        html += '<pre style="white-space:pre-wrap;color:var(--red);margin-top:4px;font-size:0.8rem">'+esc(r.error_message || 'No error message')+'</pre>';
        if (r.purpose) html += '<div style="margin-top:6px"><strong>Purpose:</strong> '+esc(r.purpose)+'</div>';
        html += compositionTreeHtml(r);
        html += '<div style="margin-top:4px"><code class="replay-cmd">'+esc(r.replay_cmd)+'</code></div>';
        html += '</td></tr>';
    }
    html += '</tbody></table>';
    patchEl('failed-runs', html);
}

// ---------------------------------------------------------------------------
// Render: Metrics (with client-side time-range filtering)
// ---------------------------------------------------------------------------
let metricsRange = localStorage.getItem('conductor-metrics-range') || 'all';

function setMetricsRange(range) {
    metricsRange = range;
    localStorage.setItem('conductor-metrics-range', range);
    renderMetrics();
}

function rangeCutoffSec(range) {
    var now = Date.now() / 1000;
    switch (range) {
        case '24h': return now - 24*3600;
        case '7d':  return now - 7*24*3600;
        case '30d': return now - 30*24*3600;
        default:    return 0;
    }
}

function computeMetricsFromRuns(runs) {
    var byWorkflow = {};
    var byModel = {};
    var byAgent = {};
    var errorTypes = {};
    var agentFailures = {};
    var totalCost = 0, totalTokens = 0, totalRuns = 0, totalCompleted = 0, totalFailed = 0;
    for (var i = 0; i < runs.length; i++) {
        var r = runs[i];
        totalRuns++;
        totalCost += r.total_cost || 0;
        totalTokens += r.total_tokens || 0;
        if (r.status === 'completed') totalCompleted++;
        else if (r.status === 'failed') {
            totalFailed++;
            var et = r.error_type || 'Unknown';
            errorTypes[et] = (errorTypes[et] || 0) + 1;
            if (r.failed_agent) agentFailures[r.failed_agent] = (agentFailures[r.failed_agent] || 0) + 1;
        }
        var wname = r.name || '(unknown)';
        var w = byWorkflow[wname] || (byWorkflow[wname] = {
            runs:0, completed:0, failed:0, total_cost:0, total_tokens:0, _durs:[]
        });
        w.runs++;
        w.total_cost += r.total_cost || 0;
        w.total_tokens += r.total_tokens || 0;
        if (r.status === 'completed') w.completed++;
        else if (r.status === 'failed') w.failed++;
        if (r.duration_sec && r.duration_sec > 0) w._durs.push(r.duration_sec);
        var agents = r.agents || [];
        for (var j = 0; j < agents.length; j++) {
            var a = agents[j];
            if (a.model) {
                var m = byModel[a.model] || (byModel[a.model] = {cost:0, tokens:0, invocations:0});
                m.cost += a.cost_usd || 0;
                m.tokens += a.tokens || 0;
                m.invocations++;
            }
            if (a.name) {
                var ag = byAgent[a.name] || (byAgent[a.name] = {
                    invocations:0, total_cost:0, total_tokens:0, _elapsed:0
                });
                ag.invocations++;
                ag.total_cost += a.cost_usd || 0;
                ag.total_tokens += a.tokens || 0;
                ag._elapsed += a.elapsed || 0;
            }
        }
    }
    Object.keys(byWorkflow).forEach(function(k){
        var w = byWorkflow[k];
        w.total_runtime_sec = w._durs.length ? w._durs.reduce(function(a,b){return a+b;},0) : 0;
        w.avg_duration_sec = w._durs.length ? (w.total_runtime_sec/w._durs.length) : 0;
        w.success_rate = w.runs ? (w.completed / w.runs) : 0;
        delete w._durs;
    });
    Object.keys(byAgent).forEach(function(k){
        var ag = byAgent[k];
        ag.total_elapsed = ag._elapsed;
        ag.avg_elapsed = ag.invocations ? (ag._elapsed / ag.invocations) : 0;
        delete ag._elapsed;
    });
    var topAgents = Object.keys(byAgent).map(function(n){
        return Object.assign({name:n}, byAgent[n]);
    }).sort(function(a,b){return b.total_cost - a.total_cost;}).slice(0, 10);
    return {
        by_workflow: byWorkflow, by_model: byModel, by_agent: byAgent,
        top_agents_by_cost: topAgents,
        error_types: errorTypes, agent_failures: agentFailures,
        totals: {cost: totalCost, tokens: totalTokens, runs: totalRuns,
                 completed: totalCompleted, failed: totalFailed}
    };
}

function fmtDuration(sec) {
    if (!sec) return '\\u2014';
    sec = Math.round(sec);
    if (sec < 60) return sec + 's';
    var m = Math.floor(sec/60), s = sec%60;
    if (m < 60) return m + 'm ' + s + 's';
    var h = Math.floor(m/60); m = m%60;
    return h + 'h ' + m + 'm';
}

function tableFromRows(headers, rows, emptyMsg) {
    if (!rows || rows.length === 0) {
        return '<table><tbody><tr><td class="empty" colspan="'+headers.length+'">'+esc(emptyMsg||'No data')+'</td></tr></tbody></table>';
    }
    var h = '<table><thead><tr>';
    for (var i = 0; i < headers.length; i++) h += '<th>'+esc(headers[i])+'</th>';
    h += '</tr></thead><tbody>';
    for (var r = 0; r < rows.length; r++) {
        h += '<tr>';
        for (var c = 0; c < rows[r].length; c++) h += '<td>'+rows[r][c]+'</td>';
        h += '</tr>';
    }
    h += '</tbody></table>';
    return h;
}

function renderMetrics() {
    // Update active state on range buttons
    var btns = document.querySelectorAll('.metrics-range-btn');
    for (var i = 0; i < btns.length; i++) {
        var isActive = btns[i].getAttribute('data-range') === metricsRange;
        btns[i].className = 'toggle-btn metrics-range-btn' + (isActive ? ' active' : '');
    }
    if (!dashboardData) return;
    var raw = dashboardData.runs_raw || [];
    var cutoff = rangeCutoffSec(metricsRange);
    var filtered = cutoff > 0 ? raw.filter(function(r){ return (r.started_at || 0) >= cutoff; }) : raw;
    var m = computeMetricsFromRuns(filtered);

    // Totals
    var t = m.totals;
    var totalsHtml =
        '<div class="stats" style="margin-bottom:0">' +
        '<div class="stat"><div class="label">Runs</div><div class="value blue">'+t.runs+'</div></div>' +
        '<div class="stat"><div class="label">Completed</div><div class="value green">'+t.completed+'</div></div>' +
        '<div class="stat"><div class="label">Failed</div><div class="value red">'+t.failed+'</div></div>' +
        '<div class="stat"><div class="label">Cost</div><div class="value">'+fmtCost2(t.cost)+'</div></div>' +
        '<div class="stat"><div class="label">Tokens</div><div class="value">'+fmtTokens(t.tokens)+'</div></div>' +
        '</div>';
    document.getElementById('metrics-totals').innerHTML = totalsHtml;

    // By Workflow
    var wfRows = Object.keys(m.by_workflow).map(function(name){
        var w = m.by_workflow[name];
        return [
            esc(name), w.runs, w.completed, w.failed,
            (w.success_rate*100).toFixed(0)+'%',
            fmtDuration(w.total_runtime_sec),
            fmtDuration(w.avg_duration_sec),
            '$'+Number(w.total_cost).toFixed(4),
            fmtTokens(w.total_tokens),
        ];
    });
    document.getElementById('metrics-by-workflow').innerHTML = tableFromRows(
        ['Workflow','Runs','OK','Fail','Success','Total Runtime','Avg Dur','Cost','Tokens'], wfRows, 'No runs in range');

    // By Model
    var mdRows = Object.keys(m.by_model).map(function(name){
        var x = m.by_model[name];
        return [esc(name), '$'+Number(x.cost).toFixed(4), fmtTokens(x.tokens), x.invocations];
    });
    document.getElementById('metrics-by-model').innerHTML = tableFromRows(
        ['Model','Cost','Tokens','Invocations'], mdRows, 'No model data');

    // By Agent
    var agRows = Object.keys(m.by_agent).map(function(name){
        var x = m.by_agent[name];
        return [esc(name), x.invocations, '$'+Number(x.total_cost).toFixed(4),
                fmtTokens(x.total_tokens), fmtDuration(x.total_elapsed), fmtDuration(x.avg_elapsed)];
    });
    document.getElementById('metrics-by-agent').innerHTML = tableFromRows(
        ['Agent','Invocations','Cost','Tokens','Total Elapsed','Avg Elapsed'], agRows, 'No agent data');

    // Top agents by cost
    var topRows = m.top_agents_by_cost.map(function(x){
        return [esc(x.name), '$'+Number(x.total_cost).toFixed(4), x.invocations, fmtTokens(x.total_tokens)];
    });
    document.getElementById('metrics-top-agents').innerHTML = tableFromRows(
        ['Agent','Cost','Invocations','Tokens'], topRows, 'No agent data');

    // Error types
    var etRows = Object.keys(m.error_types).map(function(name){
        return ['<span class="err-type">'+esc(name)+'</span>', m.error_types[name]];
    });
    document.getElementById('metrics-error-types').innerHTML = tableFromRows(
        ['Error Type','Count'], etRows, 'No errors in range');

    // Agent failures
    var afRows = Object.keys(m.agent_failures).map(function(name){
        return [esc(name), m.agent_failures[name]];
    });
    document.getElementById('metrics-agent-failures').innerHTML = tableFromRows(
        ['Agent','Failures'], afRows, 'No failures in range');
}

// ---------------------------------------------------------------------------
// Render All — morphdom-based patching (no flicker)
// ---------------------------------------------------------------------------
function patchEl(id, html) {
    var el = document.getElementById(id);
    if (!el) return;
    // Wrap in a temporary container so morphdom can diff children
    var tmp = document.createElement(el.tagName);
    tmp.id = id;
    // Copy className to preserve styling
    tmp.className = el.className;
    tmp.innerHTML = html;
    morphdom(el, tmp);
}

function renderAll() {
    if (!dashboardData) return;
    renderStats(dashboardData.stats);
    renderActiveRuns(dashboardData.active_runs);
    renderAbandonedRuns(dashboardData.abandoned_runs || []);
    renderCompletedRuns(dashboardData.completed_runs);
    renderFailedRuns(dashboardData.failed_runs);
    renderMetrics();
}

function renderAbandonedRuns(runs) {
    const heading = document.getElementById('abandoned-heading');
    const container = document.getElementById('abandoned-runs');
    const countSpan = document.getElementById('abandoned-count');
    const toggleBtn = document.getElementById('toggle-abandoned');
    if (!runs.length) {
        heading.style.display = 'none';
        container.style.display = 'none';
        return;
    }
    heading.style.display = '';
    countSpan.textContent = '(' + runs.length + ')';
    toggleBtn.textContent = showAbandoned ? 'Hide' : 'Show';
    if (!showAbandoned) {
        container.style.display = 'none';
        return;
    }
    container.style.display = '';
    var html = '';
    for (var i = 0; i < runs.length; i++) {
        html += renderRunCard(runs[i], i, 'abandoned');
    }
    patchEl('abandoned-runs', html);
}

function toggleShowAbandoned() {
    showAbandoned = !showAbandoned;
    localStorage.setItem('conductor-show-abandoned', showAbandoned ? '1' : '0');
    renderAbandonedRuns(dashboardData.abandoned_runs || []);
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------
async function actionReview(logFile) {
    try {
        var res = await fetch('/api/action/review', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({log_file: logFile})
        });
        var data = await res.json();
        if (data.error) alert('Review failed: ' + data.error);
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
        const res = await fetch('/api/action/restart', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({log_file: logFile})
        });
        const data = await res.json().catch(() => ({}));
        if (data && data.status === 'started') {
            toast('\\u{1F504} Restart launched (detached)\\n' + (data.workflow || '') + '\\ncwd: ' + (data.cwd || '') + (data.stderr_log ? '\\nlog: ' + data.stderr_log : ''), 'ok');
        } else {
            toast('\\u26A0\\uFE0F Restart failed: ' + (data.error || 'unknown error'), 'err');
        }
    } catch (e) {
        console.error('Restart action failed:', e);
        toast('\\u26A0\\uFE0F Restart request failed: ' + e, 'err');
    }
}

function toast(msg, kind) {
    let el = document.getElementById('toast');
    if (!el) {
        el = document.createElement('div');
        el.id = 'toast';
        el.style.cssText = 'position:fixed;bottom:20px;right:20px;padding:12px 18px;border-radius:6px;font-size:0.95rem;white-space:pre-line;z-index:9999;max-width:480px;box-shadow:0 4px 12px rgba(0,0,0,0.4);transition:opacity 0.3s';
        document.body.appendChild(el);
    }
    el.style.background = (kind === 'err') ? '#5a1f1f' : '#1f3d1f';
    el.style.border = (kind === 'err') ? '1px solid var(--red)' : '1px solid var(--green)';
    el.style.color = 'var(--text)';
    el.textContent = msg;
    el.style.opacity = '1';
    clearTimeout(window.__toastTimer);
    window.__toastTimer = setTimeout(() => { el.style.opacity = '0'; }, 6000);
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
setInterval(fetchDashboard, 5000);

// Client-side elapsed timer — ticks every second, updates .live-elapsed spans
function fmtElapsed(sec) {
    if (sec < 0) return '0s';
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = Math.floor(sec % 60);
    if (h > 0) return h + 'h ' + m + 'm ' + s + 's';
    if (m > 0) return m + 'm ' + s + 's';
    return s + 's';
}
setInterval(function() {
    var now = Date.now() / 1000;
    document.querySelectorAll('.live-elapsed').forEach(function(el) {
        var started = parseFloat(el.getAttribute('data-started'));
        if (started) el.textContent = fmtElapsed(now - started);
    });
}, 1000);
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

# Path to the React frontend build output
_FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"


def _dashboard_hash(data: dict) -> str:
    """Compute a fast hash of the dashboard JSON for change detection."""
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.md5(raw.encode()).hexdigest()


# Track last known hash for SSE diffing
_last_sse_hash: str = ""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    # Serve React frontend if built, otherwise fall back to legacy inline HTML
    index_html = _FRONTEND_DIST / "index.html"
    if index_html.exists():
        return HTMLResponse(index_html.read_text(encoding="utf-8"))
    return _build_html()


@app.get("/api/status")
async def api_status():
    """JSON endpoint for programmatic access (tray icon uses this)."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _compute_status)


@app.get("/api/open-folder", include_in_schema=False)
async def api_open_folder(path: str):
    """Open a folder in Windows Explorer."""
    import subprocess
    folder = Path(path)
    if folder.exists() and folder.is_dir():
        subprocess.Popen(["explorer", str(folder)])
        return {"ok": True}
    return {"ok": False, "error": "Folder not found"}


@app.get("/api/run/{port:int}/state", include_in_schema=False)
async def api_run_state(port: int):
    """Proxy a conductor instance's /api/state to avoid CORS issues."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://localhost:{port}/api/state")
            return resp.json()
    except Exception:
        return []


def _compute_status():
    name_to_port = _discover_conductor_dashboard_ports(exclude_port=_dashboard_port)
    runs = _load_event_logs(name_to_port)
    checkpoints = _load_checkpoints()
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
async def api_dashboard(reviewed: str = ""):
    """Full dashboard data for AJAX frontend."""
    reviewed_set = set(reviewed.split(",")) if reviewed else set()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _compute_dashboard(reviewed_set))


@app.get("/api/events")
async def api_events(request: Request, reviewed: str = ""):
    """Server-Sent Events endpoint for real-time dashboard updates.

    On connect: sends a full snapshot. Then polls every 2s and sends updates
    when the dashboard state changes. Sends heartbeat pings every 15s.
    """
    reviewed_set = set(reviewed.split(",")) if reviewed else set()

    async def event_stream():
        last_hash = ""
        heartbeat_counter = 0
        try:
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break

                loop = asyncio.get_event_loop()
                data = await loop.run_in_executor(
                    None, lambda: _compute_dashboard(reviewed_set)
                )
                current_hash = _dashboard_hash(data)

                if current_hash != last_hash:
                    event_type = "snapshot" if not last_hash else "update"
                    payload = json.dumps(data, default=str)
                    yield f"event: {event_type}\ndata: {payload}\n\n"
                    last_hash = current_hash
                    heartbeat_counter = 0

                heartbeat_counter += 1
                # Heartbeat every ~15s (15 / 2s poll interval)
                if heartbeat_counter >= 8:
                    yield f"event: ping\ndata: {{}}\n\n"
                    heartbeat_counter = 0

                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.websocket("/api/run/{log_file:path}/ws")
async def ws_proxy(websocket: WebSocket, log_file: str):
    """Proxy WebSocket to a running conductor instance's /ws endpoint.

    Looks up the dashboard_port for the given log_file, then bridges
    messages between the browser and the conductor instance.
    """
    import websockets  # type: ignore[import-untyped]
    import urllib.parse

    await websocket.accept()

    # Find the dashboard port for this run
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, lambda: _compute_dashboard(set()))

    target_port = None
    for section in ("active_runs", "completed_runs", "failed_runs"):
        for run in data.get(section, []):
            if run.get("log_file") == urllib.parse.unquote(log_file):
                target_port = run.get("dashboard_port")
                break
        if target_port:
            break

    if not target_port:
        await websocket.close(code=4004, reason="No conductor instance found for this run")
        return

    target_url = f"ws://localhost:{target_port}/ws"

    try:
        async with websockets.connect(target_url) as upstream:
            async def forward_to_browser():
                try:
                    async for msg in upstream:
                        await websocket.send_text(msg if isinstance(msg, str) else msg.decode())
                except Exception:
                    pass

            async def forward_to_conductor():
                try:
                    while True:
                        msg = await websocket.receive_text()
                        await upstream.send(msg)
                except (WebSocketDisconnect, Exception):
                    pass

            # Run both directions concurrently
            done, pending = await asyncio.wait(
                [asyncio.create_task(forward_to_browser()),
                 asyncio.create_task(forward_to_conductor())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except Exception:
        await websocket.close(code=4002, reason="Cannot connect to conductor instance")


def _compute_title_provider(
    work_item_id: str, work_item_title: str, tags: list[str] | None = None,
) -> tuple[str, str, list[str]]:
    """Derive the title provider, display title, and tags for a workflow run.

    Returns (title_provider, display_title, tags).
    Currently the only provider is "ado-work-item" — keyed off work_item_id presence.
    """
    if work_item_id:
        display = work_item_title or f"#{work_item_id}"
        return ("ado-work-item", display, tags or [])
    return ("", "", [])


def _serialize_run(r: WorkflowRun, name_to_port: dict[str, int],
                   skip_enrichment: bool = False) -> dict:
    """Convert a WorkflowRun to a JSON-serializable dict."""
    # Find matching dashboard port — prefer exact run_id match, fall back to name
    dashboard_port = None
    port_match_exact = False
    if r.run_id:
        dashboard_port = name_to_port.get(r.run_id)
        if dashboard_port:
            port_match_exact = True
    if not dashboard_port:
        dashboard_port = name_to_port.get(r.name)

    # Liveness detection priority:
    # 1. Dashboard port (authoritative — verified via netstat + HTTP probe)
    # 2. System PID (supplementary — cross-checked via process creation time)
    # 3. Log mtime heuristic (legacy fallback)
    #
    # If system metadata provides a dashboard_port that discovery missed
    # (e.g., port was probed before event log was parsed), try it directly
    # — but only if the port is actually listening.
    if not dashboard_port:
        sys_port = _safe_int(r.system_meta.get("dashboard_port"))
        if sys_port and sys_port != _dashboard_port and _is_port_listening(sys_port):
            dashboard_port = sys_port

    now = time.time()
    if dashboard_port:
        if port_match_exact:
            # run_id matched — port is authoritative for this specific run
            process_alive = _is_port_listening(dashboard_port)
        else:
            # Name-matched port could belong to a different run with the same
            # workflow name. Cross-check with the system PID to avoid false positives.
            system_pid = _safe_int(r.system_meta.get("pid"))
            process_alive = bool(
                system_pid and r.started_at
                and _is_pid_from_run(system_pid, r.started_at)
            )
            if not process_alive:
                dashboard_port = None  # Don't show a stale dashboard link
    else:
        # PID check: supplement when no dashboard port, gated on time window
        # and cross-checked against process creation time to prevent PID reuse.
        system_pid = _safe_int(r.system_meta.get("pid"))
        if (
            system_pid
            and r.started_at
            and (now - r.started_at) < 86400
            and _is_pid_from_run(system_pid, r.started_at)
        ):
            process_alive = True
        elif r.log_file:
            try:
                mtime = Path(r.log_file).stat().st_mtime
                process_alive = (now - mtime) < 300  # 5 min
            except OSError:
                process_alive = False
        else:
            process_alive = False

    # Check if closeout-filing skill is available for review
    wf_name = r.name or ""

    # Fast path: skip expensive CWD resolution and enrichers for runs
    # the user has already reviewed or that are abandoned/interrupted.
    if skip_enrichment:
        tp, dt, tags = _compute_title_provider(r.work_item_id, r.work_item_title)
        return {
            "log_file": r.log_file,
            "name": r.name,
            "started_at": r.started_at,
            "started_at_str": _ts_to_str(r.started_at),
            "ended_at": r.ended_at,
            "ended_at_str": _ts_to_str(r.ended_at),
        "elapsed": _duration_str(r.started_at, r.ended_at) if r.status != "running" else "",
            "status": r.status,
            "status_icon": STATUS_ICONS.get(r.status, "❓"),
            "error_type": r.error_type,
            "error_message": r.error_message,
            "failed_agent": r.failed_agent,
            "failed_subworkflow_path": r.failed_subworkflow_path,
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
            "work_item_url": "",
            "title_provider": tp,
            "display_title": dt,
            "display_tags": tags,
            "run_id": r.run_id,
            "metadata": r.metadata,
            "system_meta": r.system_meta,
            "dashboard_port": dashboard_port,
            "dashboard_url": f"http://localhost:{dashboard_port}" if dashboard_port else "",
            "replay_cmd": f'conductor replay "{r.log_file}" --web-bg',
            "review_available": False,
            "review_skill_path": "",
            "cwd": "",
            "worktree": {},
            "process_alive": process_alive,
            "hierarchy": None,
            "subworkflows": [
                {
                    "workflow": sw["workflow"],
                    "agent": sw["agent"],
                    "item_key": sw["item_key"],
                    "iteration": sw["iteration"],
                    "status": sw["status"],
                    "elapsed": sw["elapsed"],
                }
                for sw in r.subworkflows
            ],
        }

    cwd: Path = Path.home()

    # Best: system_meta.cwd from $system metadata (authoritative, set by conductor)
    sys_cwd = r.system_meta.get("cwd")
    if sys_cwd and isinstance(sys_cwd, str):
        p = Path(sys_cwd.replace("/", os.sep))
        if p.exists():
            cwd = p

    # Second: metadata.cwd injected at invocation time (user-defined)
    if cwd == Path.home():
        meta_cwd = r.metadata.get("cwd")
        if meta_cwd and "{" not in str(meta_cwd):
            p = Path(str(meta_cwd).replace("/", os.sep))
            if p.exists():
                cwd = p

    # Fallback: worktree_name pattern from metadata
    if cwd == Path.home():
        wt_pattern = r.metadata.get("worktree_name")
        if wt_pattern and r.work_item_id:
            wt_name = wt_pattern.replace("{work_item_id}", r.work_item_id)
            wt_name = wt_name.replace("{workflow_name}", wf_name)
            wt_candidate = Path.home() / "projects" / wt_name
            if wt_candidate.exists():
                cwd = wt_candidate

    # Last resort: scan log file for file paths
    if cwd == Path.home():
        cwd = _resolve_workflow_dir(r.log_file, wf_name)

    skill_path = cwd / ".github" / "skills" / "closeout-filing" / "SKILL.md"
    review_available = skill_path.exists()

    # Run enricher plugins (namespaced output)
    from enrichers import EnrichmentContext, run_enrichers
    ctx = EnrichmentContext(
        log_file=r.log_file,
        wf_name=wf_name,
        _cwd=cwd,
        _cwd_resolved=True,
    )
    enrichments = run_enrichers(r, r.metadata, ctx)

    # Extract enricher data for backward-compatible field placement
    ado_data = enrichments.get("ado", {})
    git_data = enrichments.get("git", {})

    effective_wi_title = r.work_item_title or ado_data.get("twig_title", "")
    hierarchy = ado_data.get("hierarchy")
    wi_tags = hierarchy.get("tags", []) if hierarchy else []
    tp, dt, tags = _compute_title_provider(r.work_item_id, effective_wi_title, wi_tags)

    return {
        "log_file": r.log_file,
        "name": r.name,
        "started_at": r.started_at,
        "started_at_str": _ts_to_str(r.started_at),
        "ended_at": r.ended_at,
        "ended_at_str": _ts_to_str(r.ended_at),
        "elapsed": _duration_str(r.started_at, r.ended_at) if r.status != "running" else "",
        "status": r.status,
        "status_icon": STATUS_ICONS.get(r.status, "❓"),
        "error_type": r.error_type,
        "error_message": r.error_message,
        "failed_agent": r.failed_agent,
        "failed_subworkflow_path": r.failed_subworkflow_path,
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
        "work_item_title": effective_wi_title,
        "work_item_type": r.work_item_type or ado_data.get("twig_type", ""),
        "work_item_url": ado_data.get("work_item_url", ""),
        "title_provider": tp,
        "display_title": dt,
        "display_tags": tags,
        "run_id": r.run_id,
        "metadata": r.metadata,
        "system_meta": r.system_meta,
        "dashboard_port": dashboard_port,
        "dashboard_url": f"http://localhost:{dashboard_port}" if dashboard_port else "",
        "replay_cmd": f'conductor replay "{r.log_file}" --web-bg',
        "review_available": review_available,
        "review_skill_path": str(skill_path),
        "cwd": str(cwd),
        "worktree": git_data.get("worktree", {}),
        "process_alive": process_alive,
        "hierarchy": ado_data.get("hierarchy"),
        "subworkflows": [
            {
                "workflow": sw["workflow"],
                "agent": sw["agent"],
                "item_key": sw["item_key"],
                "iteration": sw["iteration"],
                "status": sw["status"],
                "elapsed": sw["elapsed"],
            }
            for sw in r.subworkflows
        ],
    }


def _compute_dashboard(reviewed_set: set[str] | None = None) -> dict:
    # Single netstat snapshot shared across discovery and event-log loading
    listening = _get_listening_ports()
    name_to_port = _discover_conductor_dashboard_ports(exclude_port=_dashboard_port)
    runs = _load_event_logs(name_to_port, _listening_snapshot=listening)
    checkpoints = _load_checkpoints()
    costs = _aggregate_costs(runs)
    errors = _aggregate_errors(runs)
    metrics = _aggregate_metrics(runs)

    # Enricher caches use TTLs — no need to clear between refreshes.
    # Git worktree cache: 60s TTL. ADO DB path cache: persistent (path→DB is stable).
    # CWD cache: persistent (log file→CWD is stable).

    sorted_runs = sorted(runs, key=lambda r: r.started_at or 0, reverse=True)

    # Skip enrichment for reviewed runs and non-running interrupted/abandoned runs
    if reviewed_set is None:
        reviewed_set = set()

    all_serialized = []
    for r in sorted_runs:
        skip = False
        if r.log_file in reviewed_set:
            skip = True
        elif r.status not in ("running", "completed", "failed"):
            # interrupted/invalid — skip enrichment
            skip = True
        all_serialized.append(_serialize_run(r, name_to_port, skip_enrichment=skip))

    all_running = [sr for sr in all_serialized if sr["status"] == "running"]
    active_runs = [sr for sr in all_running if sr["process_alive"]]
    abandoned_runs = [sr for sr in all_running if not sr["process_alive"]]
    completed_runs = [sr for sr in all_serialized if sr["status"] == "completed"]
    failed_runs = [sr for sr in all_serialized if sr["status"] == "failed"]
    other_runs = [sr for sr in all_serialized
                  if sr["status"] not in ("running", "completed", "failed")]

    gates_waiting = sum(1 for r in active_runs if r["gate_waiting"])
    gates_abandoned = sum(1 for r in abandoned_runs if r["gate_waiting"])

    return {
        "active_runs": active_runs,
        "abandoned_runs": abandoned_runs,
        "completed_runs": completed_runs,
        "failed_runs": failed_runs,
        "other_runs": other_runs,
        "stats": {
            "total": len(runs),
            "completed": len(completed_runs),
            "failed": len(failed_runs),
            "active": len(active_runs),
            "gates_waiting": gates_waiting,
            "gates_abandoned": gates_abandoned,
            "abandoned": len(abandoned_runs),
            "total_cost": costs["total"],
            "total_tokens": costs["total_tokens"],
            "checkpoints": len(checkpoints),
        },
        "costs": costs,
        "errors": errors,
        "metrics": metrics,
        "runs_raw": [_run_to_raw(r) for r in sorted_runs],
    }


@app.post("/api/action/review")
async def action_review(request: Request):
    """Open a terminal session to file closeout findings using the closeout-filing skill."""
    body = await request.json()
    log_file = body.get("log_file", "")
    if not log_file:
        return {"error": "log_file required"}, 400
    wf_name = _extract_workflow_name(log_file)
    cwd = _resolve_workflow_dir(log_file, wf_name)
    skill_path = cwd / ".github" / "skills" / "closeout-filing" / "SKILL.md"
    if not skill_path.exists():
        return {"error": f"Closeout filing skill not found at {skill_path}"}
    prompt = (
        f"Load the skill at .github/skills/closeout-filing/SKILL.md and use it to "
        f"review and file closeout findings from the conductor workflow log at {log_file}."
    )
    return _spawn_terminal_with_copilot(prompt, cwd)


@app.post("/api/action/investigate")
async def action_investigate(request: Request):
    """Open a terminal session to investigate a workflow failure."""
    body = await request.json()
    log_file = body.get("log_file", "")
    if not log_file:
        return {"error": "log_file required"}, 400
    wf_name = _extract_workflow_name(log_file)
    cwd = _resolve_workflow_dir(log_file, wf_name)
    prompt = f"Investigate the failure in conductor workflow log {log_file}. Analyze the error, identify root cause, and advise on fixes."
    return _spawn_terminal_with_copilot(prompt, cwd)


@app.post("/api/action/restart")
async def action_restart(request: Request):
    """Restart a conductor workflow as a fully detached process.

    Windows' ``--web-bg`` flag does not actually daemonize — it only arms an
    auto-shutdown grace timer on client disconnect. The conductor child
    therefore stays in the spawning process's tree, and any attempt to show
    a visible terminal (wt.exe tab, cmd /k, etc.) ends up killing conductor
    mid-turn when the user closes the window. To survive, we must use
    ``DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP``
    with stdout/stderr redirected to a log file. Progress visibility comes
    from the dashboard itself polling the conductor event log.
    """
    body = await request.json()
    log_file = body.get("log_file", "")
    if not log_file:
        return {"error": "log_file required"}, 400
    wf_name = _extract_workflow_name(log_file)
    cwd = _resolve_workflow_dir(log_file, wf_name)
    workflow_path = _extract_workflow_path(log_file)
    # The workflow_started event stores the inline YAML source but not the
    # path on disk. If we only got the name back, search the resolved cwd and
    # common workflow folders for a matching YAML so we can pass a real file.
    resolved_path = _find_workflow_yaml(workflow_path or wf_name, cwd)
    if not resolved_path:
        return {"error": f"Could not locate YAML for workflow '{wf_name}' under {cwd}. Re-run manually with the explicit path."}
    workflow_path = str(resolved_path)

    # Invoke conductor via ``pythonw.exe -m conductor``. The pip-generated
    # ``conductor.exe`` wrapper re-spawns python.exe internally without
    # propagating our DETACHED_PROCESS flag, which pops a console window.
    # Going through pythonw (the GUI variant, which never allocates a
    # console) avoids that entirely.
    py_exe = Path(sys.executable)
    pythonw = py_exe.with_name("pythonw.exe")
    if pythonw.exists():
        cmd = [str(pythonw), "-m", "conductor", "run", str(workflow_path), "--web-bg"]
    else:
        cmd = [str(py_exe), "-m", "conductor", "run", str(workflow_path), "--web-bg"]

    # Capture stdout/stderr to a sidecar file so crash output isn't lost when
    # we detach. The dashboard shows progress via the event log; this file is
    # only needed when something goes wrong during startup.
    log_dir = Path(tempfile.gettempdir()) / "conductor"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stderr_path = log_dir / f"restart-{wf_name}-{stamp}.log"

    # Launch fully detached from the dashboard's process tree. Without these
    # flags the conductor child is a descendant of cmd.exe / our Python
    # process, and closing the spawning terminal (or killing the dashboard)
    # propagates SIGTERM to conductor — killing it mid-turn. Note that
    # ``--web-bg`` on Windows does NOT daemonize; it only arms an auto-
    # shutdown grace timer for client disconnects.
    DETACHED_PROCESS = 0x00000008
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | DETACHED_PROCESS
        | CREATE_BREAKAWAY_FROM_JOB
    )

    try:
        log_fh = open(stderr_path, "w", encoding="utf-8")
        subprocess.Popen(
            cmd,
            cwd=str(cwd),
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
        # We deliberately do not close log_fh here — the child inherits the
        # handle and will close it on exit. Python GC will drop our reference
        # shortly; the OS keeps the file open via the inherited descriptor.
        return {
            "status": "started",
            "workflow": str(workflow_path),
            "cwd": str(cwd),
            "stderr_log": str(stderr_path),
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/action/stop")
async def action_stop(request: Request):
    """Terminate a running conductor workflow process."""
    body = await request.json()
    log_file = body.get("log_file", "")
    if not log_file:
        return {"error": "log_file required"}

    # Parse the event log to get system PID
    run = _parse_event_log(Path(log_file))
    system_pid = _safe_int(run.system_meta.get("pid"))

    if not system_pid or system_pid <= 0:
        return {"error": "No PID found in workflow metadata"}

    if not _is_pid_alive(system_pid):
        # Process already dead — clean up PID file if present
        _cleanup_pid_file(log_file, run.run_id)
        return {"status": "already_stopped", "pid": system_pid}

    # Guard against Windows PID reuse: verify the process creation time
    # matches the run start time before terminating.
    if run.started_at and not _is_pid_from_run(system_pid, run.started_at):
        _cleanup_pid_file(log_file, run.run_id)
        return {"status": "already_stopped", "pid": system_pid,
                "detail": "PID was reused by a different process"}

    # Terminate the process (hard kill)
    try:
        PROCESS_TERMINATE = 0x0001
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, system_pid)
        if not handle:
            return {"error": f"Cannot open process {system_pid} (access denied or not found)"}
        success = kernel32.TerminateProcess(handle, 1)
        kernel32.CloseHandle(handle)
        if not success:
            return {"error": f"TerminateProcess failed for PID {system_pid}"}
    except Exception as e:
        return {"error": f"Failed to terminate PID {system_pid}: {e}"}

    # Clean up PID file
    _cleanup_pid_file(log_file, run.run_id)

    return {"status": "stopped", "pid": system_pid}


def _cleanup_pid_file(log_file: str, run_id: str) -> None:
    """Remove the .pid file matching this run, if any."""
    if not PID_DIR.exists():
        return
    for p in PID_DIR.glob("*.pid"):
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            # Match by run_id or by log file path
            if (run_id and data.get("run_id") == run_id) or \
               (log_file and data.get("log_file") == log_file):
                p.unlink(missing_ok=True)
                return
        except Exception:
            continue


_cwd_cache: dict[str, Path] = {}


def _resolve_workflow_dir(log_file: str, wf_name: str) -> Path:
    """Return the project directory a workflow was operating on.

    Strategy:
      1. Scan early tool-call events for file paths and derive the project root.
      2. Fall back to the static WORKFLOW_DIRS mapping.
      3. Fall back to HOME.
    """
    if log_file in _cwd_cache:
        return _cwd_cache[log_file]
    # 1. Dynamic: inspect the first tool calls for file path arguments.
    # Conductor stores `arguments` as either a dict OR a Python-repr string
    # (e.g. "{'command': 'cd C:\\\\Users\\\\...\\\\projects\\\\twig2-1643'}"),
    # so we scan both forms by stringifying when needed.
    path_re = re.compile(r"([A-Za-z]:[\\/]+(?:[^\"'\s\\/]+[\\/]+)*projects[\\/]+[^\"'\s\\/]+)")
    best_candidate: Path | None = None
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            checked = 0
            for line in f:
                if checked > 80:  # first tool calls appear early; deeper scanning has diminishing returns
                    break
                checked += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("type") not in ("agent_tool_start", "agent_tool_complete"):
                    continue
                args = evt.get("data", {}).get("arguments", {})
                # Normalize to a single string blob to search.
                if isinstance(args, dict):
                    blob = " ".join(str(v) for v in args.values() if isinstance(v, (str, int, float)))
                elif isinstance(args, str):
                    blob = args
                else:
                    continue
                for m in path_re.finditer(blob):
                    raw = m.group(1).rstrip(".,;:)]}>\"'")
                    # Normalize backslashes (possibly doubled by repr) and forward slashes.
                    normalized = raw.replace("\\\\", "\\").replace("/", os.sep)
                    candidate = Path(normalized)
                    # Windows strips trailing dots from paths at the filesystem layer,
                    # so re-verify by comparing the stripped name to the resolved name.
                    if not candidate.exists() or not candidate.is_dir():
                        continue
                    try:
                        if candidate.resolve().name != candidate.name:
                            continue
                    except OSError:
                        continue
                    # Prefer the deepest / most specific match seen so far.
                    if best_candidate is None or len(str(candidate)) > len(str(best_candidate)):
                        best_candidate = candidate
    except Exception:
        pass
    if best_candidate is not None:
        _cwd_cache[log_file] = best_candidate
        return best_candidate

    # 2. Fallback: HOME directory (no assumptions about which project)
    result = Path.home()
    _cwd_cache[log_file] = result
    return result


def _extract_workflow_name(log_file: str) -> str:
    """Extract workflow name from a log file path or its content."""
    fname = Path(log_file).stem
    if fname.endswith(".events"):
        fname = fname[: -len(".events")]
    m = re.match(r"conductor-(.+)-(\d{8}-\d{6})", fname)
    if m:
        return m.group(1)
    # Fallback: read the first workflow_started event
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                    if evt.get("type") == "workflow_started":
                        return evt.get("data", {}).get("name", "")
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return ""


def _spawn_terminal_with_copilot(prompt: str, cwd: Path | None = None) -> dict:
    """Add a new tab to Windows Terminal running agency copilot.

    Writes a temporary .ps1 script to avoid wt.exe argument-quoting issues,
    then launches it in a new tab of the most recent Windows Terminal window.
    """
    import tempfile

    escaped = prompt.replace("'", "''")  # PowerShell single-quote escape
    cwd_str = str(cwd) if cwd else str(Path.home())

    script = tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", prefix="conductor-action-",
        dir=str(TEMP_DIR), delete=False,
    )
    script.write(f"Set-Location -LiteralPath '{cwd_str}'\n")
    script.write(f"agency copilot -p '{escaped}'\n")
    script.close()

    try:
        subprocess.Popen(
            ["wt.exe", "-w", "0", "new-tab", "--title", "Conductor Agent",
             "pwsh.exe", "-NoExit", "-File", script.name],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return {"status": "launched", "method": "wt", "cwd": cwd_str}
    except FileNotFoundError:
        try:
            subprocess.Popen(
                ["pwsh.exe", "-NoExit", "-File", script.name],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            return {"status": "launched", "method": "pwsh", "cwd": cwd_str}
        except Exception as e:
            return {"error": str(e)}


def _find_workflow_yaml(name_or_path: str, cwd: Path) -> Path | None:
    """Locate a workflow YAML file by name under cwd or common locations.

    Conductor event logs store only the workflow name (not its source path),
    so to re-run we have to find a matching .yaml/.yml on disk.

    Search order:
      1. If ``name_or_path`` already points to an existing file, use it.
      2. Look under ``cwd`` for ``.conductor/<name>.yaml`` (and .yml).
      3. Look for ``<name>.yaml`` anywhere under ``cwd`` (capped at ~6 levels).
      4. Scan a few well-known global locations.
    """
    # 1. Already a path?
    candidate = Path(name_or_path)
    if candidate.exists() and candidate.is_file():
        return candidate.resolve()

    name = Path(name_or_path).stem  # strip any extension
    exts = (".yaml", ".yml")
    filenames = [name + ext for ext in exts]

    # 2. .conductor folder in cwd (common convention).
    for fn in filenames:
        p = cwd / ".conductor" / fn
        if p.is_file():
            return p.resolve()
        p = cwd / "conductor" / fn
        if p.is_file():
            return p.resolve()

    # 3. Shallow recursive search under cwd (avoid exploding on big repos).
    try:
        for fn in filenames:
            for hit in cwd.rglob(fn):
                try:
                    rel = hit.relative_to(cwd)
                    if len(rel.parts) <= 6:
                        return hit.resolve()
                except ValueError:
                    continue
    except Exception:
        pass

    # 4. Well-known global locations.
    home = Path.home()
    for base in [home / ".conductor" / "workflows", home / ".copilot" / "conductor" / "workflows"]:
        for fn in filenames:
            p = base / fn
            if p.is_file():
                return p.resolve()

    return None


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

    # Mount React frontend static assets if the build exists
    if _FRONTEND_DIST.exists() and (_FRONTEND_DIST / "index.html").exists():
        # Mount assets directory for JS/CSS bundles
        assets_dir = _FRONTEND_DIST / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
        # Serve favicon from dist root
        favicon_path = _FRONTEND_DIST / "favicon.svg"
        if favicon_path.exists():
            from starlette.responses import FileResponse as _FileResponse
            @app.get("/favicon.svg", include_in_schema=False)
            async def _favicon():
                return _FileResponse(str(favicon_path), media_type="image/svg+xml")
        print(f"   Frontend:     {_FRONTEND_DIST} (React)")
    else:
        print(f"   Frontend:     inline (legacy)")

    _dashboard_port = args.port

    import uvicorn  # type: ignore

    print(f"🚀 Conductor Dashboard starting on http://{args.host}:{args.port}")
    print(f"   Event logs:   {CONDUCTOR_DIR}")
    print(f"   Checkpoints:  {CHECKPOINTS_DIR}")
    print(f"   PID files:    {PID_DIR}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
