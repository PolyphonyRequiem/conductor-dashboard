"""Enricher plugin loader.

Discovers and loads enricher modules from this directory. Each module
can provide:

    def should_enrich(run: WorkflowRun, metadata: dict) -> bool:
        '''Return True if this enricher applies. Optional — default is True.'''

    def enrich(run: WorkflowRun, metadata: dict, ctx: EnrichmentContext) -> dict:
        '''Return a dict of extra fields. Merged under the module name as namespace.'''

Enricher output is namespaced: ado.py's output lands under result["ado"],
git.py's under result["git"], etc. This prevents field collisions.

Each enricher is wrapped in error isolation — a failing enricher logs a
warning and is skipped, never breaking the dashboard.
"""
from __future__ import annotations

import importlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Module-level cache
_enrichers: list[tuple[str, Any]] | None = None


@dataclass
class EnrichmentContext:
    """Lazy context passed to enrichers."""

    log_file: str = ""
    wf_name: str = ""
    _cwd: Path | None = None
    _cwd_resolved: bool = False
    _cwd_resolver: Any = None  # callable

    @property
    def cwd(self) -> Path:
        """Lazily resolve the workflow working directory."""
        if not self._cwd_resolved:
            if self._cwd_resolver:
                try:
                    self._cwd = self._cwd_resolver(self.log_file, self.wf_name)
                except Exception:
                    self._cwd = Path.home()
            else:
                self._cwd = Path.home()
            self._cwd_resolved = True
        return self._cwd  # type: ignore[return-value]


def load_enrichers() -> list[tuple[str, Any]]:
    """Import all enricher modules from this directory. Returns [(name, module)]."""
    global _enrichers
    if _enrichers is not None:
        return _enrichers

    enrichers_dir = Path(__file__).parent
    _enrichers = []

    for py_file in sorted(enrichers_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = py_file.stem
        try:
            mod = importlib.import_module(f"enrichers.{module_name}")
            if hasattr(mod, "enrich"):
                _enrichers.append((module_name, mod))
            else:
                logger.warning("Enricher %s has no enrich() function, skipping", module_name)
        except Exception:
            logger.exception("Failed to import enricher %s", module_name)

    return _enrichers


def reload_enrichers() -> list[tuple[str, Any]]:
    """Force reload of all enricher modules (for tests/dev)."""
    global _enrichers
    _enrichers = None
    return load_enrichers()


def run_enrichers(run: Any, metadata: dict, ctx: EnrichmentContext) -> dict[str, dict]:
    """Run all applicable enrichers and return namespaced results.

    Returns:
        Dict keyed by enricher name, each value is the enricher's output dict.
        Example: {"ado": {"work_item_url": "...", "hierarchy": {...}}, "git": {"worktree": {...}}}
    """
    results: dict[str, dict] = {}

    for name, mod in load_enrichers():
        try:
            # Check should_enrich if defined
            if hasattr(mod, "should_enrich"):
                if not mod.should_enrich(run, metadata):
                    continue

            extra = mod.enrich(run, metadata, ctx)
            if extra and isinstance(extra, dict):
                results[name] = extra
        except Exception:
            logger.exception("Enricher %s failed for run %s", name, getattr(run, 'name', ''))

    return results
