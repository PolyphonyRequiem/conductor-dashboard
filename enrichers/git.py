"""Git worktree enricher.

Detects the git worktree, branch, and directory name for any workflow
that operates in a git repository.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

# Module-level cache: cwd -> (timestamp, result).
# Kept across refreshes with a TTL since worktree data is mostly stable.
_worktree_cache: dict[str, tuple[float, dict]] = {}
_WORKTREE_TTL = 300  # 5 minutes — worktree data is very stable


def _detect_worktree(cwd: Path) -> dict:
    """Return info about the git worktree covering *cwd*."""
    import time as _time
    key = str(cwd)
    cached = _worktree_cache.get(key)
    if cached and (_time.time() - cached[0]) < _WORKTREE_TTL:
        return cached[1]

    info: dict = {}
    try:
        top = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=1.5,
        )
        if top.returncode != 0:
            _worktree_cache[key] = (_time.time(), info)
            return info
        toplevel = top.stdout.strip()
        if not toplevel:
            _worktree_cache[key] = (_time.time(), info)
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
    _worktree_cache[key] = (_time.time(), info)
    return info


def clear_cache() -> None:
    """Clear the worktree cache."""
    _worktree_cache.clear()


# ---------------------------------------------------------------------------
# Enricher interface
# ---------------------------------------------------------------------------
def enrich(run: Any, metadata: dict, ctx: Any) -> dict:
    """Return worktree info for the run's working directory.

    If metadata declares worktree_name (e.g. "twig2-{work_item_id}"),
    resolve the CWD from that pattern first — more reliable than
    parsing file paths from log events.
    """
    cwd = ctx.cwd

    # Use metadata worktree_name pattern if available
    wt_pattern = metadata.get("worktree_name")
    if wt_pattern and hasattr(run, "work_item_id") and run.work_item_id:
        wt_name = wt_pattern.replace("{work_item_id}", run.work_item_id)
        wt_name = wt_name.replace("{workflow_name}", getattr(run, "name", ""))
        candidate = Path.home() / "projects" / wt_name
        if candidate.exists():
            cwd = candidate

    worktree = _detect_worktree(cwd)
    if worktree:
        return {"worktree": worktree}
    return {}
