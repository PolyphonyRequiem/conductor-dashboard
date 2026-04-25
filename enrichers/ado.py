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

# Ordered hierarchy levels per process template.
# Types not in these lists are appended dynamically.
_HIERARCHY_AGILE = ["Epic", "Feature", "Issue", "Task"]
_HIERARCHY_CMMI = ["Epic", "Scenario", "Deliverable", "Task Group", "Task"]

def _hierarchy_order(found_types: set[str]) -> list[str]:
    """Pick the best hierarchy ordering based on which types are present."""
    if found_types & {"Scenario", "Deliverable"}:
        return _HIERARCHY_CMMI
    return _HIERARCHY_AGILE

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
      1. If metadata declares project_url, use ~/.twig/{org}/{project}/twig.db
      2. Check {cwd}/.twig/ for config and DB
      3. If cwd is a git worktree, check the main worktree's .twig/
      4. Fall back to hardcoded paths
    """
    cache_key = str(cwd) + "|" + metadata.get("project_url", "")
    if cache_key in _db_path_cache:
        return _db_path_cache[cache_key]

    result: Path | None = None

    # Explicit project_url in metadata → find twig DB for that org/project
    if metadata.get("project_url"):
        import re
        m = re.search(r"dev\.azure\.com/([^/]+)/([^/]+)", metadata["project_url"])
        if m:
            org, project = m.group(1), m.group(2)
            # CWD first — the worktree where the workflow runs
            local_db = cwd / ".twig" / org / project / "twig.db"
            if local_db.exists():
                result = local_db
            # Then git_repo (the main repo, for cases where CWD has no .twig)
            if result is None:
                git_repo = metadata.get("git_repo")
                if git_repo:
                    repo_db = Path(git_repo) / ".twig" / org / project / "twig.db"
                    if repo_db.exists():
                        result = repo_db
            # Main worktree via git (fallback when no metadata.git_repo)
            if result is None:
                main_tree = _find_main_worktree(cwd)
                if main_tree and main_tree != cwd:
                    wt_db = main_tree / ".twig" / org / project / "twig.db"
                    if wt_db.exists():
                        result = wt_db
            # Global ~/.twig/{org}/{project}
            if result is None:
                global_db = Path.home() / ".twig" / org / project / "twig.db"
                if global_db.exists():
                    result = global_db

    # No metadata or project_url didn't resolve → try CWD-based discovery
    if result is None:
        result = _find_twig_db_in_dir(cwd)

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

        # Walk UP to build ancestor chain (e.g., Task → Issue → Epic)
        ancestors = []
        parent_id = cur.execute(
            "SELECT parent_id FROM work_items WHERE id = ?", (wid,)
        ).fetchone()
        parent_id = parent_id[0] if parent_id else None
        while parent_id:
            prow = cur.execute(
                "SELECT id, type, title, state, parent_id FROM work_items WHERE id = ?",
                (parent_id,)
            ).fetchone()
            if not prow:
                break
            ancestors.append({"id": prow[0], "type": prow[1], "title": prow[2], "state": prow[3]})
            parent_id = prow[4]

        # Walk DOWN to build child level breakdown
        rows = cur.execute("""
            WITH RECURSIVE descendants AS (
                SELECT id, type, state FROM work_items WHERE parent_id = ?
                UNION ALL
                SELECT w.id, w.type, w.state FROM work_items w
                JOIN descendants d ON w.parent_id = d.id
            )
            SELECT type, state, COUNT(*) FROM descendants GROUP BY type, state
        """, (wid,)).fetchall()

        # Load process type definitions (state names, colors, categories)
        type_defs: dict[str, list[dict]] = {}
        type_colors: dict[str, str] = {}
        type_icons: dict[str, str] = {}
        try:
            pt_rows = cur.execute(
                "SELECT type_name, states_json, color_hex, icon_id FROM process_types"
            ).fetchall()
            for pt_name, states_json, color_hex, icon_id in pt_rows:
                if states_json:
                    type_defs[pt_name] = json.loads(states_json)
                if color_hex:
                    # Strip 'FF' alpha prefix if present (e.g. 'FF339947' -> '339947')
                    c = color_hex.lstrip("#")
                    if len(c) == 8 and c[:2].upper() == "FF":
                        c = c[2:]
                    type_colors[pt_name] = c
                if icon_id:
                    type_icons[pt_name] = icon_id
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            pass  # Older DBs may not have process_types

        conn.close()

        level_map: dict[str, dict[str, int]] = {}
        for typ, state, cnt in rows:
            level_map.setdefault(typ, {})[state] = cnt

        # Pick the right hierarchy ordering based on observed types
        ordered = _hierarchy_order(set(level_map.keys()))
        levels = []
        seen: set[str] = set()
        for lvl in ordered:
            if lvl in level_map:
                counts = level_map[lvl]
                total = sum(counts.values())
                levels.append({
                    "type": lvl,
                    "states": counts,  # Raw state counts: {"To Do": 3, "Doing": 1, ...}
                    "total": total,
                })
                seen.add(lvl)
        for lvl, counts in level_map.items():
            if lvl not in seen:
                total = sum(counts.values())
                levels.append({
                    "type": lvl,
                    "states": counts,
                    "total": total,
                })

        result = {
            "focus": focus,
            "levels": levels,
            "ancestors": ancestors,
            "type_defs": type_defs,     # State definitions per type
            "type_colors": type_colors,  # Hex color per type name
            "type_icons": type_icons,    # icon_id per type name
        }
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

    # If we have a work_item_id but no title yet, try twig CLI
    if not run.work_item_title:
        twig_info = _twig_show(wid, ctx.cwd)
        if twig_info:
            result["twig_title"] = twig_info.get("title", "")
            result["twig_type"] = twig_info.get("type", "")
            result["twig_state"] = twig_info.get("state", "")

    return result


# ---------------------------------------------------------------------------
# Twig CLI fallback
# ---------------------------------------------------------------------------
_twig_cache: dict[str, tuple[float, dict | None]] = {}
_TWIG_CLI_TTL = 60  # seconds


def _twig_show(work_item_id: str, cwd: Path) -> dict | None:
    """Fetch work item info via twig CLI (cached)."""
    import subprocess

    cache_key = work_item_id
    now = time.time()
    cached = _twig_cache.get(cache_key)
    if cached and (now - cached[0]) < _TWIG_CLI_TTL:
        return cached[1]

    try:
        proc = subprocess.run(
            ["twig", "show", work_item_id, "--no-refresh", "--output", "json"],
            capture_output=True, text=True, timeout=5, cwd=str(cwd),
        )
        if proc.returncode == 0:
            import json
            data = json.loads(proc.stdout)
            info = {
                "title": data.get("title", ""),
                "type": data.get("type", ""),
                "state": data.get("state", ""),
            }
            _twig_cache[cache_key] = (now, info)
            return info
    except Exception:
        pass

    _twig_cache[cache_key] = (now, None)
    return None
