"""Git worktree enricher.

Detects the git worktree, branch, and directory name for any workflow
that operates in a git repository.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

# Module-level cache shared across enricher calls within one dashboard refresh.
_worktree_cache: dict[str, dict] = {}


def _detect_worktree(cwd: Path) -> dict:
    """Return info about the git worktree covering *cwd*."""
    key = str(cwd)
    if key in _worktree_cache:
        return _worktree_cache[key]

    info: dict = {}
    try:
        top = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=1.5,
        )
        if top.returncode != 0:
            _worktree_cache[key] = info
            return info
        toplevel = top.stdout.strip()
        if not toplevel:
            _worktree_cache[key] = info
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
    _worktree_cache[key] = info
    return info


def clear_cache() -> None:
    """Clear the worktree cache (called between dashboard refreshes)."""
    _worktree_cache.clear()


# ---------------------------------------------------------------------------
# Enricher interface
# ---------------------------------------------------------------------------
def enrich(run: Any, metadata: dict, ctx: Any) -> dict:
    """Return worktree info for the run's working directory."""
    worktree = _detect_worktree(ctx.cwd)
    if worktree:
        return {"worktree": worktree}
    return {}
