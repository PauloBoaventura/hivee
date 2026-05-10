"""Tests for ``framework.agents.queen.worker_definition``.

Locks in the worker identity contract: a focused task executor, no
escalation, no persona leakage, report_to_parent terminal channel,
tracker-aware, and the worker.json serialization format that
``fork_session_into_colony`` relies on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from framework.agents.queen.worker_definition import (
    DEFAULT_LOOP_CONFIG,
    WORKER_SYSTEM_PROMPT,
    build_input_data,
    build_loop_config_dict,
    build_meta,
    build_system_prompt,
    worker_goal,
)


# ---------------------------------------------------------------------------
# Runtime identity
# ---------------------------------------------------------------------------


def test_worker_goal_is_queen_worker() -> None:
    assert worker_goal.id == "queen-worker"
    assert "ephemeral" in worker_goal.description.lower()
    assert "no escalation" in worker_goal.description.lower()


class TestSystemPrompt:
    def test_teaches_report_to_parent_not_set_output(self) -> None:
        """Workers terminate via report_to_parent — set_output is the
        queen's channel and must not appear."""
        prompt = build_system_prompt("do something")
        assert "report_to_parent" in prompt
        assert "status='success'" in prompt
        assert "set_output" not in prompt

    def test_teaches_fail_fast(self) -> None:
        prompt = build_system_prompt("do something")
        assert "FAIL FAST" in prompt
        assert "NO escalation" in prompt
        assert "status='failed'" in prompt

    def test_mentions_attached_skills(self) -> None:
        prompt = build_system_prompt("do something")
        assert "skill" in prompt.lower()
        assert "protocol" in prompt.lower()

    def test_mentions_tracker_tools(self) -> None:
        prompt = build_system_prompt("do something")
        assert "tracker_upsert" in prompt
        assert "tracker_query" in prompt

    def test_includes_task_footer(self) -> None:
        prompt = build_system_prompt("research 25 competitors")
        assert "research 25 competitors" in prompt
        assert "Task:" in prompt

    def test_default_task_when_missing(self) -> None:
        for task in (None, ""):
            prompt = build_system_prompt(task)
            assert "Continue the work from the queen's current session" in prompt

    def test_is_identity_free(self) -> None:
        prompt = build_system_prompt("do something")
        for name in ("Charlotte", "Alexandra"):
            assert name not in prompt
        assert "you remember" not in prompt.lower()

    def test_module_level_constants_exist(self) -> None:
        assert isinstance(WORKER_SYSTEM_PROMPT, str) and len(WORKER_SYSTEM_PROMPT) > 200
        assert isinstance(DEFAULT_LOOP_CONFIG, dict)


class TestDefaultLoopConfig:
    def test_reasonable_defaults(self) -> None:
        """Worker defaults should be tighter than the queen's 999_999."""
        assert DEFAULT_LOOP_CONFIG["max_iterations"] == 50
        assert DEFAULT_LOOP_CONFIG["max_tool_calls_per_turn"] == 30
        assert DEFAULT_LOOP_CONFIG["max_context_tokens"] == 64_000

    def test_all_keys_are_ints(self) -> None:
        for v in DEFAULT_LOOP_CONFIG.values():
            assert isinstance(v, int)


# ---------------------------------------------------------------------------
# Input data
# ---------------------------------------------------------------------------


class TestBuildInputData:
    def test_carries_tracker_db_path_and_colony_id(self) -> None:
        data = build_input_data(
            tracker_db_path="/c/data/tracker.db",
            colony_id="colony_x",
        )
        assert data["tracker_db_path"] == "/c/data/tracker.db"
        assert data["colony_id"] == "colony_x"
        assert "task_id" not in data

    def test_carries_task_id_when_provided(self) -> None:
        data = build_input_data(
            tracker_db_path="/x/t.db",
            colony_id="c",
            task_id="task_42",
        )
        assert data["task_id"] == "task_42"


# ---------------------------------------------------------------------------
# Loop config dict
# ---------------------------------------------------------------------------


@dataclass
class _FakeLoopConfig:
    max_iterations: int = 50
    max_tool_calls_per_turn: int = 30
    max_context_tokens: int = 64_000
    max_tool_result_chars: int = 30_000


class TestBuildLoopConfigDict:
    def test_uses_defaults_when_queen_config_missing(self) -> None:
        cfg = build_loop_config_dict(None)
        assert cfg["max_iterations"] == DEFAULT_LOOP_CONFIG["max_iterations"]
        assert cfg["max_tool_calls_per_turn"] == DEFAULT_LOOP_CONFIG["max_tool_calls_per_turn"]
        assert cfg["max_context_tokens"] == DEFAULT_LOOP_CONFIG["max_context_tokens"]

    def test_propagates_queen_values(self) -> None:
        cfg = build_loop_config_dict(_FakeLoopConfig(max_iterations=42))
        assert cfg["max_iterations"] == 42
        # max_tool_result_chars propagated when present.
        assert cfg["max_tool_result_chars"] == 30_000


# ---------------------------------------------------------------------------
# Worker meta (worker.json dict)
# ---------------------------------------------------------------------------


def _meta(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "worker_name": "worker",
        "source_session_id": "session_abc",
        "task": "do the thing",
        "tool_names": ["read_file", "tracker_upsert"],
        "skills_catalog_prompt": "<skills/>",
        "protocols_prompt": "<protocols/>",
        "skill_dirs": ["/path/to/skills"],
        "queen_loop_config": None,
        "queen_phase": "colony",
        "queen_id": "queen_finance",
        "input_data": {
            "tracker_db_path": "/x/t.db",
            "colony_id": "c",
        },
    }
    base.update(overrides)
    return build_meta(**base)


class TestBuildMeta:
    def test_identity_fields_are_empty(self) -> None:
        meta = _meta()
        assert meta["identity_prompt"] == ""
        assert meta["memory_prompt"] == ""

    def test_inherits_skills_and_protocols(self) -> None:
        meta = _meta(
            skills_catalog_prompt="<s>x</s>",
            protocols_prompt="<p>y</p>",
            skill_dirs=["/a", "/b"],
        )
        assert meta["skills_catalog_prompt"] == "<s>x</s>"
        assert meta["protocols_prompt"] == "<p>y</p>"
        assert meta["skill_dirs"] == ["/a", "/b"]

    def test_threads_input_data(self) -> None:
        meta = _meta(
            input_data={
                "tracker_db_path": "/y/t.db",
                "colony_id": "yc",
                "task_id": "task_99",
            }
        )
        assert meta["input_data"]["tracker_db_path"] == "/y/t.db"
        assert meta["input_data"]["task_id"] == "task_99"

    def test_concurrency_hint_optional(self) -> None:
        assert "concurrency_hint" not in _meta()
        assert _meta(concurrency_hint=8)["concurrency_hint"] == 8
        assert "concurrency_hint" not in _meta(concurrency_hint=0)

    def test_goal_description_from_task(self) -> None:
        meta = _meta(task="research 25 competitors")
        assert meta["goal"]["description"] == "research 25 competitors"
        assert meta["goal"]["success_criteria"] == []
        assert meta["goal"]["constraints"] == []

    def test_goal_default_when_task_missing(self) -> None:
        assert "Continue the work" in _meta(task=None)["goal"]["description"]

    def test_loop_config_propagates_queen(self) -> None:
        cfg = _FakeLoopConfig(max_iterations=7)
        assert _meta(queen_loop_config=cfg)["loop_config"]["max_iterations"] == 7

    def test_tools_is_independent_copy(self) -> None:
        src = ["read_file"]
        meta = _meta(tool_names=src)
        meta["tools"].append("hacked")
        assert "hacked" not in src

    def test_spawned_from_records_source_session(self) -> None:
        meta = _meta(source_session_id="session_xyz")
        assert meta["spawned_from"] == "session_xyz"
        assert "session_xyz" in meta["description"]

    @pytest.mark.parametrize(
        "field",
        [
            "name", "version", "description", "input_data", "goal",
            "system_prompt", "tools", "skills_catalog_prompt",
            "protocols_prompt", "skill_dirs", "identity_prompt",
            "memory_prompt", "queen_phase", "queen_id",
            "loop_config", "spawned_from", "spawned_at",
        ],
    )
    def test_includes_required_field(self, field: str) -> None:
        assert field in _meta()
