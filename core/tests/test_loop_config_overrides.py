"""Unit tests for the per-spawn LoopConfig override helper.

The helper is the choke point that turns queen-supplied overrides into a
real ``LoopConfig`` for the worker's ``AgentLoop``. We test it directly
because mistakes here silently change every spawned worker's budget,
which is hard to catch in integration tests.
"""

from __future__ import annotations

import pytest

from framework.agent_loop.agent_loop import LoopConfig
from framework.host.colony_runtime import (
    _ALLOWED_WORKER_LOOP_OVERRIDES,
    _build_worker_loop_config,
)


def test_no_overrides_returns_defaults() -> None:
    cfg = _build_worker_loop_config({})
    default = LoopConfig()
    assert cfg.max_iterations == default.max_iterations
    assert cfg.max_tool_calls_per_turn == default.max_tool_calls_per_turn
    assert cfg.max_context_tokens == default.max_context_tokens


def test_max_iterations_override() -> None:
    cfg = _build_worker_loop_config({"max_iterations": 12})
    assert cfg.max_iterations == 12
    # Other fields untouched.
    assert cfg.max_context_tokens == LoopConfig().max_context_tokens


def test_max_tool_calls_per_turn_zero_means_unlimited() -> None:
    """Boundary: 0 is the documented "unlimited" sentinel and must be allowed."""
    cfg = _build_worker_loop_config({"max_tool_calls_per_turn": 0})
    assert cfg.max_tool_calls_per_turn == 0


def test_max_context_tokens_override() -> None:
    cfg = _build_worker_loop_config({"max_context_tokens": 64_000})
    assert cfg.max_context_tokens == 64_000


def test_combined_overrides() -> None:
    cfg = _build_worker_loop_config(
        {
            "max_iterations": 8,
            "max_tool_calls_per_turn": 5,
            "max_context_tokens": 12_000,
        }
    )
    assert cfg.max_iterations == 8
    assert cfg.max_tool_calls_per_turn == 5
    assert cfg.max_context_tokens == 12_000


def test_unknown_keys_silently_dropped() -> None:
    """Typos shouldn't crash the spawn — drop, log, continue."""
    cfg = _build_worker_loop_config(
        {"max_iterations": 7, "judge_every_n_turns": 99, "garbage_field": "x"}
    )
    assert cfg.max_iterations == 7
    # Framework-controlled field stays at default — override IGNORED.
    assert cfg.judge_every_n_turns == LoopConfig().judge_every_n_turns


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_iterations", 0),
        ("max_iterations", 1001),
        ("max_iterations", -5),
        ("max_tool_calls_per_turn", -1),
        ("max_tool_calls_per_turn", 201),
        ("max_context_tokens", 999),
        ("max_context_tokens", 1_000_001),
    ],
)
def test_out_of_range_raises(field: str, value: int) -> None:
    with pytest.raises(ValueError, match="out of range"):
        _build_worker_loop_config({field: value})


def test_non_int_raises() -> None:
    with pytest.raises(ValueError, match="must be int"):
        _build_worker_loop_config({"max_iterations": "100"})


def test_allowed_set_is_the_three_documented_fields() -> None:
    """Lock the queen-tunable surface — adding a 4th override is a deliberate change."""
    assert _ALLOWED_WORKER_LOOP_OVERRIDES == frozenset(
        {"max_iterations", "max_tool_calls_per_turn", "max_context_tokens"}
    )
