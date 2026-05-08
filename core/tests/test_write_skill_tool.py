"""Tests for the queen's mid-run write_skill tool.

Companion to ``create_colony``'s inline-skill writing. Covers:
  - Happy path: writes ``~/.hive/colonies/{name}/skills/{skill_name}/`` and
    returns the path so the queen can immediately reference it.
  - Replace-existing: re-writing the same name replaces in place.
  - No colony bound: fails cleanly (the tool is meaningless outside a colony).
  - Invalid input: surfaces validator errors.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from framework.host.event_bus import EventBus
from framework.llm.provider import ToolUse
from framework.loader.tool_registry import ToolRegistry
from framework.tools.queen_lifecycle_tools import register_queen_lifecycle_tools


def _session_for_colony(colony_name: str | None) -> SimpleNamespace:
    """Build a minimal session-like object the tool reads from."""
    bus = EventBus()
    return SimpleNamespace(
        id=f"session_for_{colony_name or 'none'}",
        colony_name=colony_name,
        colony=None,
        colony_runtime=None,
        event_bus=bus,
        worker_path=None,
        available_triggers={},
        active_trigger_ids=set(),
    )


def _registry_for(session: SimpleNamespace) -> ToolRegistry:
    reg = ToolRegistry()
    register_queen_lifecycle_tools(reg, session=session, session_id=session.id)
    return reg


async def _call_write_skill(reg: ToolRegistry, payload: dict) -> dict:
    executor = reg.get_executor()
    result = executor(ToolUse(id="tu_write_skill", name="write_skill", input=payload))
    if asyncio.iscoroutine(result):
        result = await result
    return json.loads(result.content)


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_skill_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("framework.config.COLONIES_DIR", tmp_path)
    session = _session_for_colony("research_competitors")
    reg = _registry_for(session)

    payload = {
        "skill_name": "competitor-research-protocol",
        "skill_description": "Per-row competitor research procedure for the Series A memo.",
        "skill_body": (
            "# Competitor Research Protocol\n\n"
            "For each row, fill: website, year_founded, total_funding_usd, "
            "primary_segment, pricing_model. Use tracker_upsert with "
            "company_name as the key.\n"
        ),
    }
    body = await _call_write_skill(reg, payload)

    assert body.get("success") is True, body
    assert body["skill_name"] == "competitor-research-protocol"
    assert body["replaced"] is False

    skill_dir = (
        tmp_path
        / "research_competitors"
        / "skills"
        / "competitor-research-protocol"
    )
    assert skill_dir.is_dir()
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file()
    text = skill_md.read_text(encoding="utf-8")
    assert "Competitor Research Protocol" in text
    assert "tracker_upsert" in text


@pytest.mark.asyncio
async def test_write_skill_replaces_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("framework.config.COLONIES_DIR", tmp_path)
    session = _session_for_colony("c1")
    reg = _registry_for(session)

    p1 = await _call_write_skill(
        reg,
        {
            "skill_name": "protocol-v1",
            "skill_description": "first draft",
            "skill_body": "# v1\nfirst content",
        },
    )
    assert p1["success"] is True
    assert p1["replaced"] is False

    p2 = await _call_write_skill(
        reg,
        {
            "skill_name": "protocol-v1",
            "skill_description": "revised",
            "skill_body": "# v1\nNEW content",
        },
    )
    assert p2["success"] is True
    assert p2["replaced"] is True

    skill_md = tmp_path / "c1" / "skills" / "protocol-v1" / "SKILL.md"
    assert "NEW content" in skill_md.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_write_skill_no_colony_returns_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the session is NOT bound to a colony (queen DM only), refuse cleanly."""
    monkeypatch.setattr("framework.config.COLONIES_DIR", tmp_path)
    session = _session_for_colony(colony_name=None)
    reg = _registry_for(session)

    body = await _call_write_skill(
        reg,
        {
            "skill_name": "x",
            "skill_description": "y",
            "skill_body": "z",
        },
    )
    assert "error" in body
    assert "no colony" in body["error"].lower()


@pytest.mark.asyncio
async def test_write_skill_validation_error_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty body / bad name → validator error returned to the queen."""
    monkeypatch.setattr("framework.config.COLONIES_DIR", tmp_path)
    session = _session_for_colony("c1")
    reg = _registry_for(session)

    body = await _call_write_skill(
        reg,
        {
            "skill_name": "BadName",  # uppercase rejected
            "skill_description": "ok",
            "skill_body": "ok",
        },
    )
    assert "error" in body
    # Helpful hint included.
    assert "hint" in body
