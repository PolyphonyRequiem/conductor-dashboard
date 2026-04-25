"""Tests for the enricher plugin system."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import WorkflowRun
from enrichers import EnrichmentContext, load_enrichers, reload_enrichers, run_enrichers
from enrichers.ado import _load_hierarchy, _hierarchy_cache, should_enrich as ado_should_enrich
from enrichers.git import _detect_worktree, clear_cache as clear_git_cache


# ===========================================================================
# EnrichmentContext
# ===========================================================================

class TestEnrichmentContext:

    def test_lazy_cwd_not_resolved_until_accessed(self):
        called = []
        def resolver(log, name):
            called.append(True)
            return Path.home()
        ctx = EnrichmentContext(log_file="test.jsonl", wf_name="test", _cwd_resolver=resolver)
        assert len(called) == 0
        _ = ctx.cwd
        assert len(called) == 1

    def test_lazy_cwd_cached_on_second_access(self):
        call_count = []
        def resolver(log, name):
            call_count.append(1)
            return Path("/tmp")
        ctx = EnrichmentContext(log_file="test.jsonl", wf_name="test", _cwd_resolver=resolver)
        _ = ctx.cwd
        _ = ctx.cwd
        assert len(call_count) == 1

    def test_pre_resolved_cwd(self):
        ctx = EnrichmentContext(
            log_file="test.jsonl", wf_name="test",
            _cwd=Path.home(), _cwd_resolved=True,
        )
        assert ctx.cwd == Path.home()


# ===========================================================================
# ADO Enricher
# ===========================================================================

class TestAdoEnricher:

    def test_should_enrich_with_work_item_id(self):
        run = WorkflowRun(work_item_id="123")
        assert ado_should_enrich(run, {}) is True

    def test_should_enrich_with_tracker_metadata(self):
        run = WorkflowRun()
        assert ado_should_enrich(run, {"tracker": "ado"}) is True

    def test_should_not_enrich_without_signals(self):
        run = WorkflowRun()
        assert ado_should_enrich(run, {}) is False

    def test_enrich_generates_url_from_metadata(self):
        from enrichers.ado import enrich
        run = WorkflowRun(work_item_id="1814")
        ctx = EnrichmentContext(_cwd=Path.home(), _cwd_resolved=True)
        result = enrich(run, {"project_url": "https://dev.azure.com/org/Proj"}, ctx)
        assert result["work_item_url"] == "https://dev.azure.com/org/Proj/_workitems/edit/1814"

    def test_enrich_falls_back_to_hardcoded_url(self):
        from enrichers.ado import enrich
        run = WorkflowRun(work_item_id="1814")
        ctx = EnrichmentContext(_cwd=Path.home(), _cwd_resolved=True)
        result = enrich(run, {}, ctx)
        assert "1814" in result.get("work_item_url", "")

    def test_enrich_no_work_item_id_returns_empty(self):
        from enrichers.ado import enrich
        run = WorkflowRun()
        ctx = EnrichmentContext(_cwd=Path.home(), _cwd_resolved=True)
        result = enrich(run, {"tracker": "ado"}, ctx)
        assert result == {}

    def test_hierarchy_from_test_db(self, tmp_path: Path):
        """Create a test SQLite DB and verify hierarchy loading."""
        _hierarchy_cache.clear()
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE work_items (
                id INTEGER PRIMARY KEY, type TEXT, title TEXT,
                state TEXT, parent_id INTEGER, fields_json TEXT
            )
        """)
        conn.execute("INSERT INTO work_items VALUES (100, 'Epic', 'Test Epic', 'Doing', NULL, '{\"System.Tags\": \"twig; PG-2\"}')")
        conn.execute("INSERT INTO work_items VALUES (101, 'Issue', 'Issue 1', 'Done', 100, NULL)")
        conn.execute("INSERT INTO work_items VALUES (102, 'Issue', 'Issue 2', 'To Do', 100, NULL)")
        conn.execute("INSERT INTO work_items VALUES (103, 'Task', 'Task 1', 'Done', 101, NULL)")
        conn.execute("INSERT INTO work_items VALUES (104, 'Task', 'Task 2', 'Doing', 101, NULL)")
        conn.commit()
        conn.close()

        result = _load_hierarchy("100", db_path)
        assert result is not None
        assert result["focus"]["type"] == "Epic"
        assert result["focus"]["state"] == "Doing"
        assert len(result["levels"]) == 2  # Issue + Task

        issue_level = next(l for l in result["levels"] if l["type"] == "Issue")
        assert issue_level["states"]["Done"] == 1
        assert issue_level["states"]["To Do"] == 1
        assert issue_level["total"] == 2

        task_level = next(l for l in result["levels"] if l["type"] == "Task")
        assert task_level["states"]["Done"] == 1
        assert task_level["states"]["Doing"] == 1
        assert task_level["total"] == 2

        # type_defs and type_colors should be present (may be empty if no process_types table)
        assert "type_defs" in result
        assert "type_colors" in result

        # Tags from fields_json
        assert result["tags"] == ["twig", "PG-2"]

    def test_hierarchy_missing_item_returns_none(self, tmp_path: Path):
        _hierarchy_cache.clear()
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE work_items (
                id INTEGER PRIMARY KEY, type TEXT, title TEXT,
                state TEXT, parent_id INTEGER, fields_json TEXT
            )
        """)
        conn.commit()
        conn.close()
        result = _load_hierarchy("999", db_path)
        assert result is None

    def test_hierarchy_missing_db_returns_none(self):
        _hierarchy_cache.clear()
        result = _load_hierarchy("100", Path("/nonexistent/path/twig.db"))
        assert result is None

    def test_hierarchy_invalid_id_returns_none(self):
        _hierarchy_cache.clear()
        result = _load_hierarchy("not-a-number", Path("/any"))
        assert result is None


# ===========================================================================
# Git Enricher
# ===========================================================================

class TestGitEnricher:

    def test_detect_worktree_in_git_repo(self):
        clear_git_cache()
        # Use the dashboard repo itself as a known git repo
        repo_dir = Path(__file__).resolve().parent.parent
        result = _detect_worktree(repo_dir)
        assert "branch" in result
        assert "name" in result

    def test_detect_worktree_outside_git(self, tmp_path: Path):
        clear_git_cache()
        result = _detect_worktree(tmp_path)
        assert result == {}

    def test_cache_cleared(self):
        clear_git_cache()
        # Access once to populate
        repo_dir = Path(__file__).resolve().parent.parent
        result1 = _detect_worktree(repo_dir)
        clear_git_cache()
        # Cache should be empty, next call re-resolves
        result2 = _detect_worktree(repo_dir)
        assert result1 == result2


# ===========================================================================
# Plugin Loader
# ===========================================================================

class TestPluginLoader:

    def test_load_finds_ado_and_git(self):
        enrichers = reload_enrichers()
        names = [name for name, _ in enrichers]
        assert "ado" in names
        assert "git" in names

    def test_run_enrichers_namespaced(self):
        run = WorkflowRun(work_item_id="1814", name="test")
        ctx = EnrichmentContext(_cwd=Path.home(), _cwd_resolved=True)
        results = run_enrichers(run, {}, ctx)
        # ADO should produce output (has work_item_id)
        assert "ado" in results
        assert "work_item_url" in results["ado"]

    def test_run_enrichers_error_isolation(self):
        """A broken enricher should not crash the pipeline."""
        import enrichers as enrichers_mod

        def bad_enrich(run, metadata, ctx):
            raise RuntimeError("boom")

        class FakeModule:
            enrich = staticmethod(bad_enrich)

        original = enrichers_mod._enrichers
        try:
            enrichers_mod._enrichers = [("broken", FakeModule), *load_enrichers()]
            run = WorkflowRun(work_item_id="1", name="test")
            ctx = EnrichmentContext(_cwd=Path.home(), _cwd_resolved=True)
            # Should not raise — broken enricher is skipped
            results = run_enrichers(run, {}, ctx)
            assert "broken" not in results
            assert "ado" in results  # other enrichers still work
        finally:
            enrichers_mod._enrichers = original

    def test_should_enrich_respected(self):
        """Enrichers with should_enrich=False are skipped."""
        run = WorkflowRun(name="test")  # no work_item_id
        ctx = EnrichmentContext(_cwd=Path.home(), _cwd_resolved=True)
        results = run_enrichers(run, {}, ctx)
        # ADO should NOT enrich (no work_item_id, no tracker metadata)
        assert "ado" not in results
