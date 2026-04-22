"""ADO (Azure DevOps) work item enricher.

Adds work item URL, hierarchy breakdown, and badge data for workflows
that have a work_item_id. Works with the twig SQLite DB for hierarchy
lookups.

DB resolution order:
  1. {ctx.cwd}/.twig/{org}/{project}/twig.db  (per-repo, via .twig/config)
  2. {ctx.cwd}/.twig/**/twig.db               (per-repo, any org/project)
  3. Hardcoded fallback paths                  (backward compat, will be removed)
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WORK_ITEM_URLS: list[str] = [
    "https://dev.azure.com/dangreen-msft/Twig/_workitems/edit/{id}",
]

# Legacy hardcoded fallbacks — kept for backward compat until all workflows
# emit metadata with project_url.
TWIG_DB_FALLBACK_PATHS: list[Path] = [
    Path.home() / ".twig" / "dangreen-msft" / "Twig" / "twig.db",
    Path.home() / ".twig" / "twig.db",
]

_HIERARCHY_LEVELS = ["Epic", "Feature", "Issue", "Task"]

# Cache: work_item_id -> (timestamp, result)
_hierarchy_cache: dict[int, tuple[float, dict | None]] = {}
_HIERARCHY_TTL = 15  # seconds

# Cache: cwd -> resolved DB path (or None)
_db_path_cache: dict[str, Path | None] = {}


# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------
def _resolve_twig_db(cwd: Path, metadata: dict) -> Path | None:
    """Find the twig.db file for the given working directory.

    Strategy:
      1. Check {cwd}/.twig/ for config and DB
      2. If cwd is a git worktree, check the main worktree's .twig/
      3. Fall back to hardcoded paths
    """
    cache_key = str(cwd)
    if cache_key in _db_path_cache:
        return _db_path_cache[cache_key]

    result = _find_twig_db_in_dir(cwd)

    # If not found and cwd might be a git worktree, check the main tree
    if result is None:
        main_tree = _find_main_worktree(cwd)
        if main_tree and main_tree != cwd:
            result = _find_twig_db_in_dir(main_tree)

    # Hardcoded fallbacks
    if result is None:
        for fallback in TWIG_DB_FALLBACK_PATHS:
            if fallback.exists():
                result = fallback
                break

    _db_path_cache[cache_key] = result
    return result


def _find_twig_db_in_dir(directory: Path) -> Path | None:
    """Look for twig.db inside {directory}/.twig/."""
    twig_config = directory / ".twig" / "config"
    if twig_config.exists():
        try:
            with open(twig_config, "r", encoding="utf-8") as f:
                cfg = json.loads(f.read())
            org = cfg.get("organization", "")
            project = cfg.get("project", "")
            if org and project:
                candidate = directory / ".twig" / org / project / "twig.db"
                if candidate.exists():
                    return candidate
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    # Glob for any twig.db under .twig/
    twig_dir = directory / ".twig"
    if twig_dir.is_dir():
        for db in twig_dir.glob("**/twig.db"):
            if db.parent == twig_dir:
                continue  # skip root .twig/twig.db (often empty)
            return db
        root_db = twig_dir / "twig.db"
        if root_db.exists():
            return root_db

    return None


def _find_main_worktree(cwd: Path) -> Path | None:
    """If cwd is a git worktree, return the main worktree path."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("worktree "):
                    return Path(line[len("worktree "):].strip())
    except Exception:
        pass
    return None


def clear_db_cache() -> None:
    """Clear the DB path cache (called between dashboard refreshes)."""
    _db_path_cache.clear()


# ---------------------------------------------------------------------------
# Hierarchy loader
# ---------------------------------------------------------------------------
def _load_hierarchy(work_item_id: str, db_path: Path) -> dict | None:
    """Load work item hierarchy from the twig SQLite DB.

    Returns:
        {"focus": {"id", "type", "title", "state"}, "levels": [...]}
        or None if unavailable.
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
        conn.execute("PRAGMA journal_mode")
        cur = conn.cursor()

        row = cur.execute(
            "SELECT id, type, title, state FROM work_items WHERE id = ?", (wid,)
        ).fetchone()
        if not row:
            conn.close()
            _hierarchy_cache[wid] = (now, None)
            return None

        focus = {"id": row[0], "type": row[1], "title": row[2], "state": row[3]}

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

        level_map: dict[str, dict[str, int]] = {}
        for typ, state, cnt in rows:
            level_map.setdefault(typ, {})[state] = cnt

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


# ---------------------------------------------------------------------------
# Enricher interface
# ---------------------------------------------------------------------------
def should_enrich(run: Any, metadata: dict) -> bool:
    """Enrich if work_item_id is set or tracker is ado."""
    return bool(run.work_item_id) or metadata.get("tracker") == "ado"


def enrich(run: Any, metadata: dict, ctx: Any) -> dict:
    """Return work item URL and hierarchy data."""
    result: dict[str, Any] = {}

    wid = run.work_item_id
    if not wid:
        return result

    # Work item URL
    url_template = metadata.get("project_url")
    if url_template:
        result["work_item_url"] = f"{url_template}/_workitems/edit/{wid}"
    elif WORK_ITEM_URLS:
        result["work_item_url"] = WORK_ITEM_URLS[0].replace("{id}", wid)

    # Hierarchy from twig DB — resolve path from CWD
    db_path = _resolve_twig_db(ctx.cwd, metadata)
    if db_path:
        hierarchy = _load_hierarchy(wid, db_path)
        if hierarchy:
            result["hierarchy"] = hierarchy

    return result
