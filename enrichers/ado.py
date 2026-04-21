"""ADO (Azure DevOps) work item enricher.

Adds work item URL, hierarchy breakdown, and badge data for workflows
that have a work_item_id. Works with the twig SQLite DB for hierarchy
lookups.
"""
from __future__ import annotations

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

TWIG_DB_PATHS: list[Path] = [
    Path.home() / ".twig" / "https___dev.azure.com_dangreen-msft" / "Twig" / "twig.db",
]

_HIERARCHY_LEVELS = ["Epic", "Feature", "Issue", "Task"]

# Cache: work_item_id -> (timestamp, result)
_hierarchy_cache: dict[int, tuple[float, dict | None]] = {}
_HIERARCHY_TTL = 15  # seconds


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

    # Hierarchy from twig DB
    for db_path in TWIG_DB_PATHS:
        hierarchy = _load_hierarchy(wid, db_path)
        if hierarchy:
            result["hierarchy"] = hierarchy
            break

    return result
