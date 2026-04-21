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
import sqlite3
import subprocess
import socket
import sys
import tempfile
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
    # Inline subworkflow tracking
    subworkflows: list[dict] = field(default_factory=list)
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
                    # Extract work item ID from any agent prompt (not just intake)
                    if not run.work_item_id:
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


def _pid_matches_run(active: "ActiveRun", run: WorkflowRun) -> bool:
    """Return True if the alive PID-registered ActiveRun matches this event log.

    Matching uses (1) workflow yaml stem == run.name and (2) started_at within
    a 5-second tolerance (ISO UTC → epoch conversion).
    """
    if not active.workflow or not run.name:
        return False
    try:
        yaml_stem = Path(active.workflow).stem
    except Exception:
        return False
    if yaml_stem != run.name:
        return False
    if not active.started_at or not run.started_at:
        return False
    try:
        pid_epoch = datetime.fromisoformat(active.started_at).timestamp()
    except (ValueError, TypeError):
        return False
    return abs(pid_epoch - run.started_at) < 5.0


def _load_event_logs() -> list[WorkflowRun]:
    runs: list[WorkflowRun] = []
    if not CONDUCTOR_DIR.exists():
        return runs
    # Load alive PID-registered runs once so we can cross-check liveness below.
    active_runs = [a for a in _load_active_runs() if a.alive]
    now = time.time()
    for p in sorted(CONDUCTOR_DIR.glob("*.events.jsonl")):
        run = _parse_event_log(p)
        if run.status == "invalid":
            continue

        # Fix #1: if an alive PID file matches this run, force status=running
        # regardless of mtime (backgrounded conductor runs may be silent for
        # extended periods during long tool calls).
        if run.status != "running" and run.status not in ("completed", "failed"):
            if any(_pid_matches_run(a, run) for a in active_runs):
                run.status = "running"

        # Fix #2: gate-waiting runs are legitimately idle — no events are
        # emitted between gate_presented and gate_resolved (the human may
        # take hours/days to respond). Keep them marked running regardless
        # of mtime so they stay visible in the Active Runs section.
        if (
            run.status not in ("running", "completed", "failed")
            and run.gate_waiting
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
def _detect_worktree(cwd: Path, cache: dict[str, dict]) -> dict:
    """Return info about the git worktree covering *cwd*.

    Uses a short-timeout git invocation. Results are cached in *cache*
    (keyed by str(cwd)) so repeated calls within one dashboard refresh
    don't re-shell-out.
    """
    key = str(cwd)
    if key in cache:
        return cache[key]
    info: dict = {}
    try:
        top = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=1.5,
        )
        if top.returncode != 0:
            cache[key] = info
            return info
        toplevel = top.stdout.strip()
        if not toplevel:
            cache[key] = info
            return info
        br = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=1.5,
        )
        branch = br.stdout.strip() if br.returncode == 0 else ""
        wl = subprocess.run(
            ["git", "-C", str(cwd), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=1.5,
        )
        main_wt = ""
        if wl.returncode == 0:
            for line in wl.stdout.splitlines():
                if line.startswith("worktree "):
                    main_wt = line[len("worktree "):].strip()
                    break
        is_worktree = False
        try:
            if main_wt:
                is_worktree = Path(main_wt).resolve() != Path(toplevel).resolve()
        except Exception:
            is_worktree = False
        info = {
            "path": toplevel,
            "name": Path(toplevel).name,
            "is_worktree": is_worktree,
            "branch": branch,
        }
    except Exception:
        info = {}
    cache[key] = info
    return info


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
        w["avg_duration_sec"] = (sum(durs) / len(durs)) if durs else 0.0
        w["success_rate"] = (w["completed"] / w["runs"]) if w["runs"] else 0.0
    for ag in by_agent.values():
        es = ag.pop("_elapsed_sum")
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
        "total_cost": r.total_cost,
        "total_tokens": r.total_tokens,
        "agents": [
            {"name": a.name, "model": a.model, "tokens": a.tokens,
             "cost_usd": a.cost_usd, "elapsed": a.elapsed}
            for a in r.agents
        ],
        "failed_agent": r.failed_agent,
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

# Work item URL templates. Used for any run with a work_item_id.
WORK_ITEM_URLS: list[str] = [
    "https://dev.azure.com/dangreen-msft/Twig/_workitems/edit/{id}",
]

# Known project directories for skill/worktree lookups.
WORKFLOW_DIRS: list[Path] = [
    Path.home() / "projects" / "twig2",
    Path.home() / "projects" / "cloudvault-service-api",
]

# Twig SQLite DB paths for work item hierarchy lookups.
TWIG_DB_PATHS: list[Path] = [
    Path.home() / ".twig" / "https___dev.azure.com_dangreen-msft" / "Twig" / "twig.db",
]

# Ordered hierarchy levels for deterministic display.
_HIERARCHY_LEVELS = ["Epic", "Feature", "Issue", "Task"]

# Cache: work_item_id -> (timestamp, result)
_hierarchy_cache: dict[int, tuple[float, dict | None]] = {}
_HIERARCHY_TTL = 15  # seconds


def _load_twig_hierarchy(work_item_id: str, db_path: Path) -> dict | None:
    """Load work item hierarchy status from the twig SQLite DB.

    Returns a dict like:
        {
            "focus": {"id": 1782, "type": "Issue", "state": "Done", "title": "..."},
            "levels": [
                {"type": "Task", "To Do": 1, "Doing": 2, "Done": 5, "total": 8}
            ]
        }
    Returns None if the DB is unavailable or the item doesn't exist.
    """
    try:
        wid = int(work_item_id)
    except (ValueError, TypeError):
        return None

    now = time.time()
    cached = _hierarchy_cache.get(wid)
    if cached and (now - cached[0]) < _HIERARCHY_TTL:
        return cached[1]

    if not db_path.exists():
        _hierarchy_cache[wid] = (now, None)
        return None

    result = None
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.5)
        conn.execute("PRAGMA journal_mode")  # ensure connection is live
        cur = conn.cursor()

        # Focus item
        row = cur.execute(
            "SELECT id, type, title, state FROM work_items WHERE id = ?", (wid,)
        ).fetchone()
        if not row:
            conn.close()
            _hierarchy_cache[wid] = (now, None)
            return None

        focus = {"id": row[0], "type": row[1], "title": row[2], "state": row[3]}

        # Descendant breakdown by type and state using recursive CTE
        rows = cur.execute("""
            WITH RECURSIVE descendants AS (
                SELECT id, type, state FROM work_items WHERE parent_id = ?
                UNION ALL
                SELECT w.id, w.type, w.state FROM work_items w
                JOIN descendants d ON w.parent_id = d.id
            )
            SELECT type, state, COUNT(*) FROM descendants GROUP BY type, state
        """, (wid,)).fetchall()
        conn.close()

        # Build levels dict: {type: {state: count}}
        level_map: dict[str, dict[str, int]] = {}
        for typ, state, cnt in rows:
            if typ not in level_map:
                level_map[typ] = {}
            level_map[typ][state] = cnt

        # Convert to ordered list
        levels = []
        for lvl in _HIERARCHY_LEVELS:
            if lvl in level_map:
                counts = level_map[lvl]
                total = sum(counts.values())
                levels.append({
                    "type": lvl,
                    "To Do": counts.get("To Do", 0),
                    "Doing": counts.get("Doing", 0),
                    "Done": counts.get("Done", 0),
                    "total": total,
                })
        # Include any types not in the standard list
        for lvl, counts in level_map.items():
            if lvl not in _HIERARCHY_LEVELS:
                total = sum(counts.values())
                levels.append({
                    "type": lvl,
                    "To Do": counts.get("To Do", 0),
                    "Doing": counts.get("Doing", 0),
                    "Done": counts.get("Done", 0),
                    "total": total,
                })

        result = {"focus": focus, "levels": levels}
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError):
        result = None

    _hierarchy_cache[wid] = (now, result)
    return result


def _work_item_html(run: WorkflowRun, font_size: str = "0.85rem") -> str:
    """Build HTML snippet for a work item link, or empty string if none."""
    if not run.work_item_id:
        return ""
    url_template = WORK_ITEM_URLS[0] if WORK_ITEM_URLS else ""
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
.comp-tree .inline-sub { opacity: 0.7; }
.tree-cost-badge { background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 1px 5px; font-size: 0.72rem; color: var(--text2); }

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
        ? '<a href="'+esc(r.work_item_url)+'" target="_blank">#'+esc(r.work_item_id)+'</a>'
        : '#'+esc(r.work_item_id);
    var titleHtml = r.work_item_title ? ' '+esc(r.work_item_title) : '';
    return '<span class="work-item">&#128203; '+typeSpan+idHtml+titleHtml+'</span>';
}

function hierarchyHtml(r) {
    var h = r.hierarchy;
    if (!h || !h.levels || h.levels.length === 0) return '';
    var html = '<span class="hierarchy">';
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
    var children = r.children;
    var subs = r.subworkflows;
    if ((!children || children.length === 0) && (!subs || subs.length === 0)) return '';
    var html = '<div class="comp-tree">';
    html += '<div class="tree-section-label">Composition Tree</div>';
    // Render child workflow runs (separate log files grouped by work_item_id)
    if (children && children.length > 0) {
        for (var i = 0; i < children.length; i++) {
            var c = children[i];
            var prefix = (i === children.length - 1 && (!subs || subs.length === 0)) ? '\\u2514\\u2500 ' : '\\u251c\\u2500 ';
            var icon = _STATUS_ICON[c.status] || '\\u2753';
            html += '<div class="tree-node">';
            html += '<span class="tree-prefix">' + prefix + '</span>';
            html += '<span class="tree-status">' + icon + '</span>';
            html += '<span class="tree-name">' + esc(c.name) + '</span>';
            html += '<span class="tree-meta">' + esc(c.elapsed) + '</span>';
            if (c.total_cost) html += '<span class="tree-cost-badge">' + fmtCost(c.total_cost) + '</span>';
            html += '</div>';
            // Show inline subworkflows of this child
            if (c.subworkflows && c.subworkflows.length > 0) {
                var subsByWf = {};
                for (var s = 0; s < c.subworkflows.length; s++) {
                    var sw = c.subworkflows[s];
                    var wfName = sw.workflow.replace('./', '').replace('.yaml', '');
                    if (!subsByWf[wfName]) subsByWf[wfName] = {done: 0, total: 0, elapsed: 0};
                    subsByWf[wfName].total++;
                    if (sw.status === 'completed') subsByWf[wfName].done++;
                    subsByWf[wfName].elapsed += (sw.elapsed || 0);
                }
                var cPrefix = (i === children.length - 1) ? '   ' : '\\u2502  ';
                for (var wfName in subsByWf) {
                    var info = subsByWf[wfName];
                    var subIcon = (info.done === info.total) ? '\\u2705' : '\\ud83d\\udd04';
                    html += '<div class="tree-node inline-sub">';
                    html += '<span class="tree-prefix">' + cPrefix + '\\u21b3 </span>';
                    html += '<span class="tree-status">' + subIcon + '</span>';
                    html += '<span class="tree-name">' + esc(wfName) + '</span>';
                    html += '<span class="tree-meta">\\u00d7' + info.total;
                    if (info.done < info.total) html += ' (' + info.done + '/' + info.total + ' done)';
                    html += '</span>';
                    html += '</div>';
                }
            }
        }
    }
    // Show inline subworkflows of the root itself
    if (subs && subs.length > 0) {
        var subsByWf = {};
        for (var s = 0; s < subs.length; s++) {
            var sw = subs[s];
            var wfName = sw.workflow.replace('./', '').replace('.yaml', '');
            if (!subsByWf[wfName]) subsByWf[wfName] = {done: 0, total: 0, elapsed: 0};
            subsByWf[wfName].total++;
            if (sw.status === 'completed') subsByWf[wfName].done++;
            subsByWf[wfName].elapsed += (sw.elapsed || 0);
        }
        var keys = Object.keys(subsByWf);
        for (var k = 0; k < keys.length; k++) {
            var wfName = keys[k];
            var info = subsByWf[wfName];
            var subIcon = (info.done === info.total) ? '\\u2705' : '\\ud83d\\udd04';
            var prefix = (k === keys.length - 1) ? '\\u2514\\u2500 ' : '\\u251c\\u2500 ';
            html += '<div class="tree-node inline-sub">';
            html += '<span class="tree-prefix">' + prefix + '\\u21b3 </span>';
            html += '<span class="tree-status">' + subIcon + '</span>';
            html += '<span class="tree-name">' + esc(wfName) + '</span>';
            html += '<span class="tree-meta">\\u00d7' + info.total;
            if (info.done < info.total) html += ' (' + info.done + '/' + info.total + ' done)';
            html += '</span>';
            html += '</div>';
        }
    }
    html += '</div>';
    return html;
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
        '<div class="stat"><div class="label">Abandoned</div><div class="value red">'+(stats.abandoned||0)+'</div></div>' +
        '<div class="stat"><div class="label">Total Cost</div><div class="value">'+fmtCost2(stats.total_cost)+'</div></div>' +
        '<div class="stat"><div class="label">Total Tokens</div><div class="value">'+fmtTokens(stats.total_tokens)+'</div></div>';
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
        html += renderRunCard(runs[i], i, 'active');
    }
    el.innerHTML = html;
}

function renderRunCard(r, i, keyPrefix) {
        var key = r.log_file || (keyPrefix+'-'+i);
        var isExpanded = expandedRuns.has(key);
        var isAbandoned = !r.process_alive;
        var gateClass = r.gate_waiting ? ' gate-waiting' : '';
        if (isAbandoned) gateClass += ' abandoned';

        // Agent status line
        var agentStatus;
        if (r.gate_waiting) {
            if (isAbandoned) {
                agentStatus = '&#128123; <span style="color:var(--red)">'+esc(r.gate_agent)+'</span> <span class="abandoned-badge">GATE ABANDONED</span>';
            } else {
                agentStatus = '<span class="gate-pulse">&#128678;</span> <span style="color:var(--yellow)">'+esc(r.gate_agent)+'</span> <span class="err-type" style="background:#d2992220">GATE WAITING</span>';
            }
        } else if (isAbandoned) {
            var atype = r.current_agent_type ? ' <span style="color:var(--text2)">('+esc(r.current_agent_type)+')</span>' : '';
            agentStatus = '&#128123; '+esc(r.current_agent || '\\u2014')+atype+' <span class="abandoned-badge">ABANDONED</span>';
        } else if (r.current_agent) {
            var atype = r.current_agent_type ? ' <span style="color:var(--text2)">('+esc(r.current_agent_type)+')</span>' : '';
            agentStatus = '&#9881;&#65039; '+esc(r.current_agent)+atype;
        } else {
            agentStatus = '<span style="color:var(--text2)">\\u2014</span>';
        }

        var wiHtml = workItemHtml(r);
        var wtHtml = worktreeBadge(r);
        var hiHtml = hierarchyHtml(r);
        var wiBadge = (wiHtml ? ' '+wiHtml : '') + wtHtml + hiHtml;

        var html = '<div class="run-card fade-in'+gateClass+'">';
        html += '<div class="run-card-header" title="Click to expand details" onclick="toggleExpand(\\''+jsEsc(key)+'\\') ">';
        html += '<span class="chevron'+(isExpanded?' open':'')+'">&#9654;</span>';
        html += '<span class="wf-name">'+esc(r.name)+'</span>'+wiBadge;
        html += '<span style="color:var(--text2);margin-left:auto">'+esc(r.elapsed)+'</span>';
        html += '<span>'+agentStatus+'</span>';
        var costDisplay = (r.children && r.children.length > 0) ? fmtCost(r.tree_total_cost) : fmtCost(r.total_cost);
        html += '<span>'+costDisplay+'</span>';
        if (r.dashboard_url) {
            html += '<a class="action-btn" href="'+esc(r.dashboard_url)+'" target="_blank" title="Open per-run conductor dashboard" onclick="event.stopPropagation()" style="margin-left:8px;text-decoration:none">&#128279; Dashboard</a>';
        }
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
        html += compositionTreeHtml(r);
        if (r.dashboard_url) {
            html += '<div style="margin-top:4px"><a class="action-btn" href="'+esc(r.dashboard_url)+'" target="_blank" title="Open per-run conductor dashboard" style="text-decoration:none;display:inline-block">&#128279; Dashboard :'+r.dashboard_port+'</a></div>';
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
        var wtHtml = worktreeBadge(r);
        var hiHtml = hierarchyHtml(r);
        var nameExtra = wiHtml ? '<br>'+wiHtml+wtHtml+hiHtml : (r.purpose ? '<br><span style="color:var(--text2);font-size:0.75rem">'+esc(r.purpose)+'</span>'+wtHtml+hiHtml : wtHtml+hiHtml);

        html += '<tr class="status-completed fade-in'+reviewedClass+'" style="cursor:pointer" title="Click to expand details" onclick="toggleExpand(\\'completed-'+jsEsc(key)+'\\') ">';
        html += '<td class="wf-name"><span class="chevron'+(isExpanded?' open':'')+'">&#9654;</span> '+esc(r.name)+nameExtra+'</td>';
        html += '<td class="ts">'+esc(r.started_at_str)+'</td>';
        html += '<td>'+esc(r.elapsed)+'</td>';
        var cCost = (r.children && r.children.length > 0) ? fmtCost(r.tree_total_cost) : fmtCost(r.total_cost);
        var cTokens = (r.children && r.children.length > 0) ? fmtTokens(r.tree_total_tokens) : fmtTokens(r.total_tokens);
        html += '<td>'+cCost+'</td>';
        html += '<td>'+cTokens+'</td>';
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
    el.innerHTML = html;
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
        w.avg_duration_sec = w._durs.length ? (w._durs.reduce(function(a,b){return a+b;},0)/w._durs.length) : 0;
        w.success_rate = w.runs ? (w.completed / w.runs) : 0;
        delete w._durs;
    });
    Object.keys(byAgent).forEach(function(k){
        var ag = byAgent[k];
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
            fmtDuration(w.avg_duration_sec),
            '$'+Number(w.total_cost).toFixed(4),
            fmtTokens(w.total_tokens),
        ];
    });
    document.getElementById('metrics-by-workflow').innerHTML = tableFromRows(
        ['Workflow','Runs','OK','Fail','Success','Avg Dur','Cost','Tokens'], wfRows, 'No runs in range');

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
                fmtTokens(x.total_tokens), fmtDuration(x.avg_elapsed)];
    });
    document.getElementById('metrics-by-agent').innerHTML = tableFromRows(
        ['Agent','Invocations','Cost','Tokens','Avg Elapsed'], agRows, 'No agent data');

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
// Render All
// ---------------------------------------------------------------------------
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
    container.innerHTML = html;
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


def _serialize_run(r: WorkflowRun, ts_to_port: dict[float, int],
                   worktree_cache: dict[str, dict] | None = None,
                   alive_pid_runs: list["ActiveRun"] | None = None) -> dict:
    """Convert a WorkflowRun to a JSON-serializable dict."""
    if worktree_cache is None:
        worktree_cache = {}
    if alive_pid_runs is None:
        alive_pid_runs = []
    # Find matching dashboard port
    dashboard_port = ts_to_port.get(r.started_at)
    if not dashboard_port:
        for ts, port in ts_to_port.items():
            if abs(ts - r.started_at) < 2.0:
                dashboard_port = port
                break

    # Determine whether the backing conductor process is actually alive.
    # A run is "alive" if either:
    #   (a) its per-run dashboard port is currently listening (ts_to_port hit), OR
    #   (b) a registered PID file matches (backgrounded --web-bg runs), OR
    #   (c) the dashboard parser force-marked it running via the tool_in_flight
    #       grace window (foreground run still producing events).
    process_alive = bool(dashboard_port) or any(
        _pid_matches_run(a, r) for a in alive_pid_runs
    )
    if not process_alive and r.tool_in_flight and r.last_event_ts:
        if (time.time() - r.last_event_ts) < 600:
            process_alive = True

    # Build work item URL
    work_item_url = ""
    if r.work_item_id and WORK_ITEM_URLS:
        work_item_url = WORK_ITEM_URLS[0].replace("{id}", r.work_item_id)

    # Check if closeout-filing skill is available for review
    wf_name = r.name or ""
    cwd = _resolve_workflow_dir(r.log_file, wf_name)
    skill_path = cwd / ".github" / "skills" / "closeout-filing" / "SKILL.md"
    review_available = skill_path.exists()

    # Fallback: if the resolved dir (e.g. a deleted worktree) doesn't have the
    # skill, check known project directories.
    if not review_available:
        for directory in WORKFLOW_DIRS:
            fallback_skill = directory / ".github" / "skills" / "closeout-filing" / "SKILL.md"
            if fallback_skill.exists():
                skill_path = fallback_skill
                review_available = True
                break

    worktree = _detect_worktree(cwd, worktree_cache)

    # Load work item hierarchy from any available twig DB.
    hierarchy = None
    if r.work_item_id:
        for db_path in TWIG_DB_PATHS:
            hierarchy = _load_twig_hierarchy(r.work_item_id, db_path)
            if hierarchy:
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
        "replay_cmd": f'conductor replay "{r.log_file}" --web-bg',
        "review_available": review_available,
        "review_skill_path": str(skill_path),
        "cwd": str(cwd),
        "worktree": worktree,
        "process_alive": process_alive,
        "hierarchy": hierarchy,
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
        "children": [],       # populated by _compute_dashboard grouping
        "is_child": False,     # set True when nested under a root
        "tree_total_cost": r.total_cost,
        "tree_total_tokens": r.total_tokens,
    }


def _compute_dashboard() -> dict:
    runs = _load_event_logs()
    checkpoints = _load_checkpoints()
    costs = _aggregate_costs(runs)
    errors = _aggregate_errors(runs)
    metrics = _aggregate_metrics(runs)
    ts_to_port = _discover_conductor_dashboard_ports(exclude_port=_dashboard_port)
    worktree_cache: dict[str, dict] = {}
    alive_pid_runs = [a for a in _load_active_runs() if a.alive]

    sorted_runs = sorted(runs, key=lambda r: r.started_at or 0, reverse=True)

    all_serialized = [_serialize_run(r, ts_to_port, worktree_cache, alive_pid_runs) for r in sorted_runs]

    # --- Group runs by work_item_id into composition trees ---
    child_log_files: set[str] = set()
    wid_groups: dict[str, list[dict]] = {}
    for sr in all_serialized:
        wid = sr.get("work_item_id")
        if wid:
            wid_groups.setdefault(wid, []).append(sr)

    _STATUS_PRIORITY = {"running": 0, "failed": 1, "completed": 2, "unknown": 3}

    for wid, group in wid_groups.items():
        if len(group) < 2:
            continue
        # Pick root: prefer running over terminal, then newest.
        # This prevents a completed run from being root while a sibling
        # is still running (which would hide live work).
        def _root_sort_key(sr: dict) -> tuple:
            status_pri = _STATUS_PRIORITY.get(sr.get("status", "unknown"), 3)
            started = -(sr.get("started_at") or 0)  # newest first
            return (status_pri, started)

        group.sort(key=_root_sort_key)
        root = group[0]

        children = group[1:]
        # Sort children chronologically (oldest first)
        children.sort(key=lambda sr: sr.get("started_at") or 0)
        for child in children:
            child["is_child"] = True
            child_log_files.add(child["log_file"])
        root["children"] = children
        # Aggregate tree metrics
        root["tree_total_cost"] = root["total_cost"] + sum(c["total_cost"] for c in children)
        root["tree_total_tokens"] = root["total_tokens"] + sum(c["total_tokens"] for c in children)
        # Tree status: worst-of across all runs (running > failed > completed)
        worst = min(_STATUS_PRIORITY.get(sr.get("status", "unknown"), 3) for sr in group)
        root["tree_status"] = {0: "running", 1: "failed", 2: "completed"}.get(worst, "unknown")

    # Filter out children from top-level lists
    def _not_child(sr: dict) -> bool:
        return sr["log_file"] not in child_log_files

    all_running = [sr for sr in all_serialized if sr["status"] == "running"]
    active_runs = [sr for sr in all_running if sr["process_alive"] and _not_child(sr)]
    abandoned_runs = [sr for sr in all_running if not sr["process_alive"] and _not_child(sr)]
    completed_runs = [sr for sr in all_serialized if sr["status"] == "completed" and _not_child(sr)]
    failed_runs = [sr for sr in all_serialized if sr["status"] == "failed" and _not_child(sr)]
    other_runs = [sr for sr in all_serialized
                  if sr["status"] not in ("running", "completed", "failed") and _not_child(sr)]

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
        # Fallback: check known project directories
        for directory in WORKFLOW_DIRS:
            fallback = directory / ".github" / "skills" / "closeout-filing" / "SKILL.md"
            if fallback.exists():
                skill_path = fallback
                cwd = directory
                break
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


def _resolve_workflow_dir(log_file: str, wf_name: str) -> Path:
    """Return the project directory a workflow was operating on.

    Strategy:
      1. Scan early tool-call events for file paths and derive the project root.
      2. Fall back to the static WORKFLOW_DIRS mapping.
      3. Fall back to HOME.
    """
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
                if checked > 400:  # scan enough lines to get past setup events
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
        return best_candidate

    # 2. Static mapping fallback — return the first known directory that exists
    for directory in WORKFLOW_DIRS:
        if directory.exists():
            return directory
    return Path.home()


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

    _dashboard_port = args.port

    import uvicorn  # type: ignore

    print(f"🚀 Conductor Dashboard starting on http://{args.host}:{args.port}")
    print(f"   Event logs:   {CONDUCTOR_DIR}")
    print(f"   Checkpoints:  {CHECKPOINTS_DIR}")
    print(f"   PID files:    {PID_DIR}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
