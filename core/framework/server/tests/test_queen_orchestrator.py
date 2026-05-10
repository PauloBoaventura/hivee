from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import MagicMock

import pytest

from framework.agents.queen import queen_memory_v2
from framework.host.event_bus import EventBus
from framework.llm.mock import MockLLMProvider
from framework.llm.provider import Tool
from framework.loader.tool_registry import ToolRegistry
from framework.server.queen_orchestrator import create_queen
from framework.server.session_manager import Session


@pytest.mark.asyncio
async def test_create_queen_injects_identity_into_initial_prompt(monkeypatch, tmp_path) -> None:
    """The first queen prompt should already include the selected profile."""
    monkeypatch.setattr(queen_memory_v2, "MEMORIES_DIR", tmp_path / "memories")

    session = Session(
        id="session_test",
        event_bus=EventBus(),
        llm=MockLLMProvider(),
        loaded_at=0.0,
        queen_name="queen_technology",
    )
    manager = MagicMock()
    manager._subscribe_worker_handoffs = MagicMock()
    queen_profile = {
        "name": "Alexandra",
        "title": "Head of Technology",
        "core_traits": "A pragmatic technical leader.",
    }

    task = await create_queen(
        session=session,
        session_manager=manager,
        worker_identity=None,
        queen_dir=tmp_path / "queen",
        queen_profile=queen_profile,
        initial_prompt="who are you",
        initial_phase="independent",
        tool_registry=ToolRegistry(),
    )

    try:
        assert session.phase_state is not None
        assert "<core_identity>" in session.phase_state.queen_identity_prompt
        assert "Alexandra" in session.phase_state.queen_identity_prompt
        assert "Head of Technology" in session.phase_state.queen_identity_prompt

        prompt = session.phase_state.get_current_prompt()
        assert prompt.startswith(session.phase_state.queen_identity_prompt)
        assert "<core_identity>" in prompt
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_create_queen_keeps_configurable_mcp_tools_in_colony_phase(monkeypatch, tmp_path) -> None:
    """Colony phase gates system tools, not user-configured MCP tools."""
    monkeypatch.setattr(queen_memory_v2, "MEMORIES_DIR", tmp_path / "memories")

    session = Session(
        id="session_colony_mcp_tools",
        event_bus=EventBus(),
        llm=MockLLMProvider(),
        loaded_at=0.0,
        queen_name="queen_custom_unknown",
    )
    manager = MagicMock()
    manager._subscribe_worker_handoffs = MagicMock()
    queen_profile = {
        "name": "Custom Queen",
        "title": "Custom Role",
        "core_traits": "Keeps configured tools across phases.",
    }

    registry = ToolRegistry()
    browser_tool = Tool(name="browser_open", description="Open browser", parameters={"type": "object"})
    write_tool = Tool(name="write_file", description="Write file", parameters={"type": "object"})
    registry.register("browser_open", browser_tool, lambda _inputs: {"ok": True})
    registry.register("write_file", write_tool, lambda _inputs: {"ok": True})
    registry._mcp_server_tools["gcu-tools"] = {"browser_open"}  # type: ignore[attr-defined]
    registry._mcp_server_tools["files-tools"] = {"write_file"}  # type: ignore[attr-defined]

    task = await create_queen(
        session=session,
        session_manager=manager,
        worker_identity={"name": "worker"},
        queen_dir=tmp_path / "queen",
        queen_profile=queen_profile,
        initial_prompt="colony ready",
        initial_phase="colony",
        tool_registry=registry,
    )

    try:
        assert session.phase_state is not None
        assert session._queen_tool_registry is registry  # type: ignore[attr-defined]
        assert "browser_open" in {t.name for t in session.phase_state.independent_tools}
        assert "browser_open" in {t.name for t in session.phase_state.incubating_tools}
        assert "browser_open" in {t.name for t in session.phase_state.colony_tools}
        assert "write_file" in {t.name for t in session.phase_state.incubating_tools}
        assert "browser_open" in {t.name for t in session.phase_state.get_current_tools()}
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
