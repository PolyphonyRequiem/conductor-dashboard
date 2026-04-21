"""Unit tests for the Conductor Dashboard data-loading and aggregation functions."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

# Ensure the conductor-dashboard package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import (
    AgentRun,
    WorkflowRun,
    _aggregate_costs,
    _aggregate_errors,
    _duration_str,
    _extract_purpose,
    _parse_event_log,
    _serialize_run,
    _ts_to_str,
    app,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_events(
    tmp_path: Path, events: list[dict], name: str = "test-wf", *, old: bool = True
) -> Path:
    """Write events to a JSONL file following conductor naming convention.

    When *old* is True (default), the file mtime is back-dated so
    _parse_event_log won't override terminal status based on recency.
    """
    fname = f"conductor-{name}-20260416-120000.events.jsonl"
    p = tmp_path / fname
    with open(p, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")
    if old:
        old_time = time.time() - 600  # 10 minutes ago
        os.utime(p, (old_time, old_time))
    return p


def _make_basic_events(
    ts_start: float = 1000.0,
    workflow_name: str = "demo",
    agent_name: str = "planner",
    model: str = "claude-sonnet-4",
    cost: float = 0.05,
    tokens: int = 1500,
    include_completed: bool = True,
) -> list[dict]:
    """Return a minimal set of workflow events."""
    events = [
        {"type": "workflow_started", "timestamp": ts_start,
         "data": {"name": workflow_name, "version": "1.0",
                  "agents": [{"name": agent_name, "type": "agent"}]}},
        {"type": "agent_started", "timestamp": ts_start + 1,
         "data": {"agent_name": agent_name, "iteration": 1}},
        {"type": "agent_completed", "timestamp": ts_start + 10,
         "data": {"agent_name": agent_name, "model": model,
                  "elapsed": 9.0, "tokens": tokens,
                  "input_tokens": 1000, "output_tokens": 500,
                  "cost_usd": cost}},
    ]
    if include_completed:
        events.append(
            {"type": "workflow_completed", "timestamp": ts_start + 12, "data": {}}
        )
    return events


# ===========================================================================
# _parse_event_log
# ===========================================================================

class TestParseEventLog:
    """Tests for _parse_event_log."""

    def test_basic_completed_workflow(self, tmp_path: Path):
        events = _make_basic_events()
        p = _write_events(tmp_path, events)
        run = _parse_event_log(p)

        assert run.name == "demo"
        assert run.status == "completed"
        assert run.version == "1.0"
        assert run.started_at == 1000.0
        assert run.ended_at == 1012.0
        assert run.total_cost == pytest.approx(0.05)
        assert run.total_tokens == 1500
        assert len(run.agents) == 1
        assert run.agents[0].name == "planner"
        assert run.agents[0].model == "claude-sonnet-4"

    def test_failed_workflow(self, tmp_path: Path):
        events = [
            {"type": "workflow_started", "timestamp": 2000.0,
             "data": {"name": "failing-wf", "version": "2.0", "agents": []}},
            {"type": "agent_started", "timestamp": 2001.0,
             "data": {"agent_name": "builder", "iteration": 1}},
            {"type": "workflow_failed", "timestamp": 2005.0,
             "data": {"error_type": "AgentError",
                      "message": "Build failed",
                      "agent_name": "builder"}},
        ]
        p = _write_events(tmp_path, events, name="failing-wf")
        run = _parse_event_log(p)

        assert run.status == "failed"
        assert run.error_type == "AgentError"
        assert run.error_message == "Build failed"
        assert run.failed_agent == "builder"

    def test_gate_waiting(self, tmp_path: Path):
        events = [
            {"type": "workflow_started", "timestamp": 3000.0,
             "data": {"name": "gated", "version": "1.0", "agents": []}},
            {"type": "gate_presented", "timestamp": 3010.0,
             "data": {"agent_name": "reviewer"}},
        ]
        p = _write_events(tmp_path, events, name="gated")
        run = _parse_event_log(p)

        assert run.gate_waiting is True
        assert run.gate_agent == "reviewer"

    def test_gate_resolved_clears_waiting(self, tmp_path: Path):
        events = [
            {"type": "workflow_started", "timestamp": 3000.0,
             "data": {"name": "gated2", "version": "1.0", "agents": []}},
            {"type": "gate_presented", "timestamp": 3010.0,
             "data": {"agent_name": "reviewer"}},
            {"type": "gate_resolved", "timestamp": 3020.0,
             "data": {"agent_name": "reviewer"}},
            {"type": "workflow_completed", "timestamp": 3025.0, "data": {}},
        ]
        p = _write_events(tmp_path, events, name="gated2")
        run = _parse_event_log(p)

        assert run.gate_waiting is False
        assert run.gate_agent == ""

    def test_empty_file_returns_invalid(self, tmp_path: Path):
        p = _write_events(tmp_path, [], name="empty")
        run = _parse_event_log(p)
        assert run.status == "invalid"

    def test_malformed_json_returns_invalid(self, tmp_path: Path):
        p = tmp_path / "conductor-bad-20260416-120000.events.jsonl"
        p.write_text("not valid json\nalso not json\n", encoding="utf-8")
        run = _parse_event_log(p)
        assert run.status == "invalid"

    def test_work_item_extraction_from_intake(self, tmp_path: Path):
        events = [
            {"type": "workflow_started", "timestamp": 5000.0,
             "data": {"name": "twig-intake", "version": "1.0",
                      "agents": [{"name": "intake", "type": "agent"}]}},
            {"type": "agent_started", "timestamp": 5001.0,
             "data": {"agent_name": "intake", "iteration": 1}},
            {"type": "agent_prompt_rendered", "timestamp": 5002.0,
             "data": {"agent_name": "intake",
                      "rendered_prompt": "Process work item #42 for the team"}},
            {"type": "agent_completed", "timestamp": 5010.0,
             "data": {"agent_name": "intake", "model": "gpt-4",
                      "elapsed": 8.0, "tokens": 500,
                      "input_tokens": 300, "output_tokens": 200,
                      "cost_usd": 0.02,
                      "output": json.dumps({
                          "epic_id": 42,
                          "epic_title": "Build feature X",
                          "item_type": "Epic",
                      })}},
            {"type": "workflow_completed", "timestamp": 5012.0, "data": {}},
        ]
        p = _write_events(tmp_path, events, name="twig-intake")
        run = _parse_event_log(p)

        assert run.work_item_id == "42"
        assert run.work_item_title == "Build feature X"
        assert run.work_item_type == "Epic"

    def test_name_extracted_from_filename(self, tmp_path: Path):
        events = [
            {"type": "workflow_started", "timestamp": 100.0,
             "data": {"version": "1.0", "agents": []}},
            {"type": "workflow_completed", "timestamp": 110.0, "data": {}},
        ]
        p = _write_events(tmp_path, events, name="my-cool-workflow")
        run = _parse_event_log(p)
        # workflow_started data.name is missing, so filename is used first,
        # then overridden by data.name="" — but since data has no name key
        # at all, .get("name", run.name) preserves the filename-based name.
        assert run.name == "my-cool-workflow"

    def test_route_taken_events(self, tmp_path: Path):
        events = _make_basic_events()
        events.insert(2, {"type": "route_taken", "timestamp": 1005.0,
                          "data": {"from": "planner", "to": "builder"}})
        p = _write_events(tmp_path, events)
        run = _parse_event_log(p)
        assert len(run.routes) == 1
        assert run.routes[0]["from"] == "planner"

    def test_purpose_from_prompt(self, tmp_path: Path):
        events = [
            {"type": "workflow_started", "timestamp": 6000.0,
             "data": {"name": "purpose-wf", "version": "1.0", "agents": []}},
            {"type": "agent_prompt_rendered", "timestamp": 6001.0,
             "data": {"agent_name": "planner",
                      "rendered_prompt": "**Purpose:** Fix the login bug"}},
            {"type": "workflow_completed", "timestamp": 6010.0, "data": {}},
        ]
        p = _write_events(tmp_path, events, name="purpose-wf")
        run = _parse_event_log(p)
        assert run.purpose == "Fix the login bug"


# ===========================================================================
# _aggregate_costs
# ===========================================================================

class TestAggregateCosts:
    """Tests for _aggregate_costs."""

    def test_empty_list(self):
        result = _aggregate_costs([])
        assert result["total"] == 0.0
        assert result["total_tokens"] == 0
        assert result["by_workflow"] == {}
        assert result["by_model"] == {}

    def test_single_run(self):
        run = WorkflowRun(
            name="wf-a", total_cost=0.10, total_tokens=2000,
            agents=[AgentRun(name="agent1", model="claude-sonnet-4", cost_usd=0.10)],
        )
        result = _aggregate_costs([run])
        assert result["total"] == pytest.approx(0.10)
        assert result["total_tokens"] == 2000
        assert result["by_workflow"] == {"wf-a": pytest.approx(0.10)}
        assert result["by_model"] == {"claude-sonnet-4": pytest.approx(0.10)}

    def test_multiple_runs(self):
        runs = [
            WorkflowRun(
                name="wf-a", total_cost=0.10, total_tokens=1000,
                agents=[
                    AgentRun(name="a1", model="claude-sonnet-4", cost_usd=0.06),
                    AgentRun(name="a2", model="gpt-4", cost_usd=0.04),
                ],
            ),
            WorkflowRun(
                name="wf-b", total_cost=0.20, total_tokens=3000,
                agents=[
                    AgentRun(name="a1", model="claude-sonnet-4", cost_usd=0.20),
                ],
            ),
        ]
        result = _aggregate_costs(runs)
        assert result["total"] == pytest.approx(0.30)
        assert result["total_tokens"] == 4000
        assert result["by_workflow"]["wf-a"] == pytest.approx(0.10)
        assert result["by_workflow"]["wf-b"] == pytest.approx(0.20)
        assert result["by_model"]["claude-sonnet-4"] == pytest.approx(0.26)
        assert result["by_model"]["gpt-4"] == pytest.approx(0.04)

    def test_by_workflow_sorted_descending(self):
        runs = [
            WorkflowRun(name="cheap", total_cost=0.01, total_tokens=100, agents=[]),
            WorkflowRun(name="expensive", total_cost=1.00, total_tokens=50000, agents=[]),
        ]
        result = _aggregate_costs(runs)
        keys = list(result["by_workflow"].keys())
        assert keys[0] == "expensive"


# ===========================================================================
# _aggregate_errors
# ===========================================================================

class TestAggregateErrors:
    """Tests for _aggregate_errors."""

    def test_no_failures(self):
        runs = [WorkflowRun(status="completed"), WorkflowRun(status="running")]
        result = _aggregate_errors(runs)
        assert result["error_types"] == {}
        assert result["agent_failures"] == {}

    def test_single_failure(self):
        runs = [
            WorkflowRun(status="failed", error_type="AgentError", failed_agent="builder"),
        ]
        result = _aggregate_errors(runs)
        assert result["error_types"] == {"AgentError": 1}
        assert result["agent_failures"] == {"builder": 1}

    def test_multiple_failures(self):
        runs = [
            WorkflowRun(status="failed", error_type="AgentError", failed_agent="builder"),
            WorkflowRun(status="failed", error_type="AgentError", failed_agent="tester"),
            WorkflowRun(status="failed", error_type="TimeoutError", failed_agent="builder"),
            WorkflowRun(status="completed"),
        ]
        result = _aggregate_errors(runs)
        assert result["error_types"]["AgentError"] == 2
        assert result["error_types"]["TimeoutError"] == 1
        assert result["agent_failures"]["builder"] == 2
        assert result["agent_failures"]["tester"] == 1

    def test_unknown_error_type(self):
        runs = [WorkflowRun(status="failed", error_type="", failed_agent="x")]
        result = _aggregate_errors(runs)
        assert result["error_types"] == {"Unknown": 1}

    def test_empty_list(self):
        result = _aggregate_errors([])
        assert result["error_types"] == {}
        assert result["agent_failures"] == {}


# ===========================================================================
# _extract_purpose
# ===========================================================================

class TestExtractPurpose:
    """Tests for _extract_purpose."""

    def test_purpose_marker(self):
        assert _extract_purpose("**Purpose:** Fix the login bug") == "Fix the login bug"

    def test_new_work_request_marker(self):
        assert _extract_purpose("**New work request:** Add caching layer") == "Add caching layer"

    def test_existing_work_item_marker(self):
        result = _extract_purpose("**Existing work item:** Upgrade to Python 3.12")
        assert result == "Upgrade to Python 3.12"

    def test_input_marker(self):
        assert _extract_purpose("**Input:** some input data") == "some input data"

    def test_question_marker(self):
        assert _extract_purpose("**Question:** How does auth work?") == "How does auth work?"

    def test_lowercase_purpose_marker(self):
        assert _extract_purpose("purpose: make it faster") == "make it faster"

    def test_truncates_at_double_newline(self):
        prompt = "**Purpose:** First paragraph\n\nSecond paragraph"
        assert _extract_purpose(prompt) == "First paragraph"

    def test_no_markers_falls_back_to_first_line(self):
        prompt = "Fix the authentication module\nThis is extra detail."
        assert _extract_purpose(prompt) == "Fix the authentication module"

    def test_skips_headings_and_separators(self):
        prompt = "# Heading\n---\nActual content here"
        assert _extract_purpose(prompt) == "Actual content here"

    def test_skips_boilerplate_lines(self):
        prompt = "You are a helpful assistant\nPhase 1: Gather context\nDo something useful"
        assert _extract_purpose(prompt) == "Do something useful"

    def test_empty_string(self):
        assert _extract_purpose("") == ""

    def test_max_len_truncation(self):
        long_text = "**Purpose:** " + "a" * 200
        result = _extract_purpose(long_text)
        assert len(result) <= 120


# ===========================================================================
# _ts_to_str
# ===========================================================================

class TestTsToStr:
    """Tests for _ts_to_str."""

    def test_valid_timestamp(self):
        # 2025-01-01 00:00:00 UTC
        result = _ts_to_str(1735689600.0)
        assert "2025-01-01" in result
        assert "UTC" in result

    def test_zero_returns_dash(self):
        assert _ts_to_str(0) == "—"

    def test_none_like_falsy(self):
        assert _ts_to_str(0.0) == "—"


# ===========================================================================
# _duration_str
# ===========================================================================

class TestDurationStr:
    """Tests for _duration_str."""

    def test_seconds_only(self):
        assert _duration_str(100.0, 145.0) == "45s"

    def test_minutes_and_seconds(self):
        assert _duration_str(100.0, 225.0) == "2m 5s"

    def test_hours_minutes_seconds(self):
        assert _duration_str(1.0, 3662.0) == "1h 1m 1s"

    def test_zero_start_returns_dash(self):
        assert _duration_str(0, 100.0) == "—"

    def test_zero_end_returns_dash(self):
        assert _duration_str(100.0, 0) == "—"

    def test_both_zero_returns_dash(self):
        assert _duration_str(0, 0) == "—"

    def test_negative_duration_returns_dash(self):
        assert _duration_str(200.0, 100.0) == "—"


# ===========================================================================
# _serialize_run
# ===========================================================================

class TestSerializeRun:
    """Tests for _serialize_run."""

    def test_basic_serialization(self):
        run = WorkflowRun(
            log_file="/logs/test.events.jsonl",
            name="demo",
            started_at=1000.0,
            ended_at=1060.0,
            status="completed",
            total_cost=0.05,
            total_tokens=1500,
            agents=[AgentRun(name="planner", model="claude-sonnet-4",
                             elapsed=9.0, tokens=1500, cost_usd=0.05)],
        )
        result = _serialize_run(run, {})

        assert result["name"] == "demo"
        assert result["status"] == "completed"
        assert result["status_icon"] == "✅"
        assert result["total_cost"] == pytest.approx(0.05)
        assert result["cost_str"] == "$0.0500"
        assert result["total_tokens"] == 1500
        assert result["tokens_str"] == "1,500"
        assert result["agent_count"] == 1
        assert result["agents"][0]["name"] == "planner"
        assert result["dashboard_port"] is None
        assert result["dashboard_url"] == ""
        assert "replay" in result["replay_cmd"]

    def test_work_item_data(self):
        run = WorkflowRun(
            name="twig-my-wf",
            status="completed",
            started_at=2000.0,
            ended_at=2100.0,
            work_item_id="123",
            work_item_title="Epic title",
            work_item_type="Epic",
        )
        result = _serialize_run(run, {})
        assert result["work_item_id"] == "123"
        assert result["work_item_title"] == "Epic title"
        assert result["work_item_type"] == "Epic"
        assert "123" in result["work_item_url"]

    def test_dashboard_port_exact_match(self):
        run = WorkflowRun(name="demo", started_at=5000.0, ended_at=5100.0, status="completed")
        ts_to_port = {5000.0: 49999}
        result = _serialize_run(run, ts_to_port)
        assert result["dashboard_port"] == 49999
        assert "49999" in result["dashboard_url"]

    def test_dashboard_port_fuzzy_match(self):
        run = WorkflowRun(name="demo", started_at=5000.0, ended_at=5100.0, status="completed")
        ts_to_port = {5001.5: 50001}  # within 2s
        result = _serialize_run(run, ts_to_port)
        assert result["dashboard_port"] == 50001

    def test_no_dashboard_port(self):
        run = WorkflowRun(name="demo", started_at=5000.0, ended_at=5100.0, status="completed")
        ts_to_port = {9999.0: 50001}  # not within 2s
        result = _serialize_run(run, ts_to_port)
        assert result["dashboard_port"] is None
        assert result["dashboard_url"] == ""

    def test_zero_cost_shows_dash(self):
        run = WorkflowRun(name="x", status="completed", started_at=1.0, ended_at=2.0)
        result = _serialize_run(run, {})
        assert result["cost_str"] == "—"
        assert result["tokens_str"] == "—"

    def test_gate_waiting_fields(self):
        run = WorkflowRun(
            name="gated", status="running", started_at=1.0, ended_at=2.0,
            gate_waiting=True, gate_agent="reviewer",
        )
        result = _serialize_run(run, {})
        assert result["gate_waiting"] is True
        assert result["gate_agent"] == "reviewer"


# ===========================================================================
# API endpoint tests
# ===========================================================================

class TestAPIEndpoints:
    """Tests for FastAPI endpoints using TestClient."""

    @pytest.fixture(autouse=True)
    def _client(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(app)

    def test_root_returns_html(self):
        resp = self.client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Conductor Dashboard" in resp.text

    def test_api_status_returns_json(self):
        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "runs" in data
        assert "completed" in data
        assert "failed" in data
        assert "active" in data
        assert "costs" in data
        assert "errors" in data

    def test_api_status_has_gates_waiting(self):
        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "gates_waiting" in data

    def test_api_dashboard_structure(self):
        resp = self.client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert "active_runs" in data
        assert "completed_runs" in data
        assert "failed_runs" in data
        assert "stats" in data
        assert "costs" in data
        assert "metrics" in data
        assert "runs_raw" in data


# ===========================================================================
# Workflow Composition (subworkflow depth tracking)
# ===========================================================================

class TestWorkflowComposition:
    """Tests for nested subworkflow parsing and depth tracking."""

    def _subworkflow_events(self, ts: float = 1000.0) -> list[dict]:
        """Outer workflow with one inline subworkflow that completes."""
        return [
            {"type": "workflow_started", "timestamp": ts,
             "data": {"name": "outer-orchestrator", "version": "1.0",
                      "agents": [{"name": "dispatcher", "type": "agent"}]}},
            {"type": "agent_started", "timestamp": ts + 1,
             "data": {"agent_name": "dispatcher", "iteration": 1}},
            {"type": "subworkflow_started", "timestamp": ts + 2,
             "data": {"agent_name": "dispatcher", "workflow": "./child.yaml",
                      "item_key": "0"}},
            # Child workflow events (inline, same log file)
            {"type": "workflow_started", "timestamp": ts + 2,
             "data": {"name": "child", "version": "1.0", "agents": []}},
            {"type": "agent_started", "timestamp": ts + 3,
             "data": {"agent_name": "worker", "iteration": 1}},
            {"type": "agent_completed", "timestamp": ts + 8,
             "data": {"agent_name": "worker", "model": "claude-sonnet-4",
                      "elapsed": 5.0, "tokens": 500, "cost_usd": 0.01}},
            {"type": "workflow_completed", "timestamp": ts + 9, "data": {}},
            # Back to parent
            {"type": "subworkflow_completed", "timestamp": ts + 9,
             "data": {"agent_name": "dispatcher", "elapsed": 7.0}},
            {"type": "agent_completed", "timestamp": ts + 10,
             "data": {"agent_name": "dispatcher", "model": "claude-sonnet-4",
                      "elapsed": 9.0, "tokens": 200, "cost_usd": 0.02}},
            {"type": "workflow_completed", "timestamp": ts + 12, "data": {}},
        ]

    def test_outer_name_preserved(self, tmp_path: Path):
        """Subworkflow workflow_started must not override outer name."""
        p = _write_events(tmp_path, self._subworkflow_events(), name="outer-orchestrator")
        run = _parse_event_log(p)
        assert run.name == "outer-orchestrator"

    def test_outer_status_not_set_by_child_completion(self, tmp_path: Path):
        """Child workflow_completed at depth>0 must not set outer status."""
        # Remove the outer workflow_completed to test isolation
        events = self._subworkflow_events()
        events = [e for e in events if not (
            e["type"] == "workflow_completed" and e["timestamp"] == 1012.0
        )]
        p = _write_events(tmp_path, events, name="outer-orchestrator")
        run = _parse_event_log(p)
        # Without the outer completed event, status should NOT be "completed"
        assert run.status != "completed"

    def test_outer_completed_at_depth_zero(self, tmp_path: Path):
        """Only depth-0 workflow_completed sets terminal status."""
        p = _write_events(tmp_path, self._subworkflow_events(), name="outer-orchestrator")
        run = _parse_event_log(p)
        assert run.status == "completed"
        assert run.ended_at == 1012.0

    def test_subworkflows_tracked(self, tmp_path: Path):
        """subworkflow_started/completed events populate subworkflows list."""
        p = _write_events(tmp_path, self._subworkflow_events(), name="outer-orchestrator")
        run = _parse_event_log(p)
        assert len(run.subworkflows) == 1
        sw = run.subworkflows[0]
        assert sw["workflow"] == "./child.yaml"
        assert sw["status"] == "completed"
        assert sw["agent"] == "dispatcher"

    def test_for_each_subworkflow_completion(self, tmp_path: Path):
        """For-each loops: child workflow_completed marks subworkflow done
        even without subworkflow_completed event."""
        events = [
            {"type": "workflow_started", "timestamp": 1000.0,
             "data": {"name": "dispatcher", "version": "1.0", "agents": []}},
            # 3 for-each subworkflows with NO subworkflow_completed
            {"type": "subworkflow_started", "timestamp": 1001.0,
             "data": {"agent_name": "planner", "workflow": "./plan.yaml",
                      "item_key": "0"}},
            {"type": "workflow_started", "timestamp": 1001.0,
             "data": {"name": "plan", "agents": []}},
            {"type": "workflow_completed", "timestamp": 1005.0, "data": {}},
            {"type": "subworkflow_started", "timestamp": 1006.0,
             "data": {"agent_name": "planner", "workflow": "./plan.yaml",
                      "item_key": "1"}},
            {"type": "workflow_started", "timestamp": 1006.0,
             "data": {"name": "plan", "agents": []}},
            {"type": "workflow_completed", "timestamp": 1010.0, "data": {}},
            {"type": "subworkflow_started", "timestamp": 1011.0,
             "data": {"agent_name": "planner", "workflow": "./plan.yaml",
                      "item_key": "2"}},
            {"type": "workflow_started", "timestamp": 1011.0,
             "data": {"name": "plan", "agents": []}},
            {"type": "workflow_completed", "timestamp": 1015.0, "data": {}},
            # Outer completes
            {"type": "workflow_completed", "timestamp": 1020.0, "data": {}},
        ]
        p = _write_events(tmp_path, events, name="dispatcher")
        run = _parse_event_log(p)
        assert run.name == "dispatcher"
        assert run.status == "completed"
        assert len(run.subworkflows) == 3
        for sw in run.subworkflows:
            assert sw["status"] == "completed", f"Subworkflow {sw['item_key']} not completed"

    def test_many_nested_subworkflows_depth_correct(self, tmp_path: Path):
        """Verify depth tracking stays correct with many sequential subworkflows."""
        events = [
            {"type": "workflow_started", "timestamp": 1000.0,
             "data": {"name": "implementer", "version": "1.0", "agents": []}},
        ]
        # 5 sequential subworkflows
        ts = 1001.0
        for i in range(5):
            events.extend([
                {"type": "subworkflow_started", "timestamp": ts,
                 "data": {"agent_name": "builder", "workflow": "./build.yaml",
                          "iteration": str(i + 1)}},
                {"type": "workflow_started", "timestamp": ts,
                 "data": {"name": "build", "agents": []}},
                {"type": "workflow_completed", "timestamp": ts + 5, "data": {}},
                {"type": "subworkflow_completed", "timestamp": ts + 5,
                 "data": {"agent_name": "builder", "elapsed": 5.0}},
            ])
            ts += 10
        events.append(
            {"type": "workflow_completed", "timestamp": ts, "data": {}}
        )
        p = _write_events(tmp_path, events, name="implementer")
        run = _parse_event_log(p)
        assert run.name == "implementer"
        assert run.status == "completed"
        assert len(run.subworkflows) == 5
        assert all(sw["status"] == "completed" for sw in run.subworkflows)

    def test_running_workflow_with_active_subworkflow(self, tmp_path: Path):
        """A workflow with an in-progress subworkflow stays running."""
        events = [
            {"type": "workflow_started", "timestamp": 1000.0,
             "data": {"name": "implementer", "version": "1.0", "agents": []}},
            {"type": "subworkflow_started", "timestamp": 1001.0,
             "data": {"agent_name": "builder", "workflow": "./build.yaml"}},
            {"type": "workflow_started", "timestamp": 1001.0,
             "data": {"name": "build", "agents": []}},
            {"type": "agent_started", "timestamp": 1002.0,
             "data": {"agent_name": "coder", "iteration": 1}},
            {"type": "agent_tool_start", "timestamp": 1003.0,
             "data": {"agent_name": "coder", "tool": "edit"}},
            # No workflow_completed — still running
        ]
        p = _write_events(tmp_path, events, name="implementer", old=False)
        run = _parse_event_log(p)
        assert run.name == "implementer"
        assert run.status == "running"
        assert len(run.subworkflows) == 1
        assert run.subworkflows[0]["status"] == "running"

    def test_work_item_id_from_non_intake_agent(self, tmp_path: Path):
        """work_item_id is extracted from any agent prompt, not just intake."""
        events = [
            {"type": "workflow_started", "timestamp": 1000.0,
             "data": {"name": "implementer", "version": "1.0", "agents": []}},
            {"type": "agent_prompt_rendered", "timestamp": 1001.0,
             "data": {"agent_name": "plan_reader",
                      "rendered_prompt": "Read work item #1814 and gather context."}},
            {"type": "workflow_completed", "timestamp": 1010.0, "data": {}},
        ]
        p = _write_events(tmp_path, events, name="implementer")
        run = _parse_event_log(p)
        assert run.work_item_id == "1814"
