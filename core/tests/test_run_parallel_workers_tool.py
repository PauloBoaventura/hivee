"""Coverage of the run_parallel_workers tool (fire-and-forget contract).

The tool spawns workers and returns immediately with worker_ids. Each
worker's completion arrives on the event bus as SUBAGENT_REPORT, which
the queen orchestrator's _on_worker_report bridge turns into a
[WORKER_REPORT] user inject. These tests verify:

1. The tool returns immediately with status="started" and the list of
   worker_ids, not with aggregated reports.
2. SUBAGENT_REPORT events are emitted for every spawned worker with
   the expected payload (status, summary, data).
3. Soft-timeout inject reaches still-active workers that haven't
   filed an explicit report; workers that finished early are not
   disturbed.
4. Hard cutoff force-stops workers that ignored the warning, but
   preserves any explicit report filed right before the stop.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from framework.agent_loop.types import AgentSpec
from framework.host.colony_runtime import ColonyRuntime
from framework.host.event_bus import AgentEvent, EventBus, EventType
from framework.llm.provider import LLMProvider, LLMResponse, Tool, ToolResult, ToolUse
from framework.llm.stream_events import FinishEvent, TextDeltaEvent, ToolCallEvent
from framework.loader.tool_registry import ToolRegistry
from framework.schemas.goal import Goal
from framework.tools.queen_lifecycle_tools import register_queen_lifecycle_tools

# ---------------------------------------------------------------------------
# Mock LLM that routes scenarios by task text in the first user message
# ---------------------------------------------------------------------------


class _ByTaskMockLLM(LLMProvider):
    model: str = "mock"

    def __init__(self, by_task: dict[str, list]):
        self.by_task = by_task
        self._used_tasks: set[str] = set()

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[Tool] | None = None,
        max_tokens: int = 4096,
        **kwargs,
    ) -> AsyncIterator:
        first_user = ""
        for m in messages:
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            content = block.get("text", "")
                            break
                first_user = str(content)
                break
        for key, events in self.by_task.items():
            if key in first_user:
                if key in self._used_tasks:
                    yield TextDeltaEvent(content="Done.", snapshot="Done.")
                    yield FinishEvent(stop_reason="stop", input_tokens=1, output_tokens=1, model="mock")
                    return
                self._used_tasks.add(key)
                for ev in events:
                    yield ev
                return

    def complete(self, messages, system="", **kwargs) -> LLMResponse:
        return LLMResponse(content="", model="mock", stop_reason="stop")


def _report(status: str, summary: str, data: dict | None = None) -> list:
    return [
        ToolCallEvent(
            tool_use_id="report_1",
            tool_name="report_to_parent",
            tool_input={"status": status, "summary": summary, "data": data or {}},
        ),
        FinishEvent(stop_reason="tool_calls", input_tokens=10, output_tokens=5, model="mock"),
    ]


def _stub_executor(tool_use: ToolUse) -> ToolResult:
    return ToolResult(tool_use_id=tool_use.tool_use_id, content="ok", is_error=False)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal session-like object exposing ``colony`` for the tool."""

    def __init__(self, colony: ColonyRuntime, session_id: str):
        self.colony = colony
        self.id = session_id
        # Fields the tool registration may touch even if our test path
        # doesn't exercise them.
        self.colony_runtime = None
        self.event_bus = colony.event_bus
        self.worker_path = None
        self.available_triggers = {}
        self.active_trigger_ids = set()


@pytest.mark.asyncio
async def test_run_parallel_workers_tool_returns_immediately_and_emits_reports(
    tmp_path: Path,
) -> None:
    """Contract: tool returns status='started' right away; SUBAGENT_REPORT
    events for every spawned worker arrive asynchronously on the bus."""
    bus = EventBus()
    llm = _ByTaskMockLLM(
        by_task={
            "fetch-A": _report("success", "A done", {"rows": 10}),
            "fetch-B": _report("success", "B done", {"rows": 20}),
            "fetch-C": _report("failed", "C broke", {"error_code": 503}),
        }
    )

    colony = ColonyRuntime(
        agent_spec=AgentSpec(
            id="test_colony",
            name="Test Colony",
            description="async-spawn test colony.",
            system_prompt="You are a test agent.",
            agent_type="event_loop",
            output_keys=[],
            tool_access_policy="all",
        ),
        goal=Goal(id="g", name="g", description="g"),
        storage_path=tmp_path / "colony",
        llm=llm,
        tools=[],
        tool_executor=_stub_executor,
        event_bus=bus,
        colony_id="async_test",
        pipeline_stages=[],
    )
    await colony.start()

    # Collect SUBAGENT_REPORT events as they arrive.
    collected_reports: list[dict] = []

    async def _on_report(event: AgentEvent) -> None:
        collected_reports.append(event.data or {})

    bus.subscribe(event_types=[EventType.SUBAGENT_REPORT], handler=_on_report)

    session = _FakeSession(colony, "async_test")
    registry = ToolRegistry()
    register_queen_lifecycle_tools(registry, session=session, session_id=session.id)

    try:
        tools = registry.get_tools()
        assert "run_parallel_workers" in tools

        executor = registry.get_executor()
        tool_use = ToolUse(
            id="tu_run_parallel",
            name="run_parallel_workers",
            input={
                "tasks": [
                    {"task": "fetch-A"},
                    {"task": "fetch-B"},
                    {"task": "fetch-C"},
                ],
                "timeout": 30.0,
            },
        )

        # The tool must return quickly — well before workers finish.
        async def _invoke() -> Any:
            r = executor(tool_use)
            if asyncio.iscoroutine(r):
                r = await r
            return r

        result = await asyncio.wait_for(_invoke(), timeout=5.0)

        assert not result.is_error, f"Tool errored: {result.content}"
        payload = json.loads(result.content)
        assert payload["status"] == "started"
        assert payload["worker_count"] == 3
        assert len(payload["worker_ids"]) == 3
        assert payload["soft_timeout_seconds"] == 30.0
        assert payload["hard_timeout_seconds"] >= 30.0 + 60.0  # at least 60s grace
        assert "[WORKER_REPORT]" in payload["message"]
        assert "reports" not in payload  # fire-and-forget — no aggregated reports

        # Now wait for workers to finish and SUBAGENT_REPORT to fire.
        for _ in range(40):
            if len(collected_reports) >= 3:
                break
            await asyncio.sleep(0.1)

        assert len(collected_reports) == 3, f"Expected 3 SUBAGENT_REPORT events, got {len(collected_reports)}"
        statuses = sorted(r["status"] for r in collected_reports)
        summaries = sorted(r["summary"] for r in collected_reports)
        assert statuses == ["failed", "success", "success"]
        assert summaries == ["A done", "B done", "C broke"]

        # Each worker landed under {storage}/workers/{worker_id}/
        worker_root = tmp_path / "colony" / "workers"
        assert worker_root.exists()
        worker_dirs = list(worker_root.iterdir())
        assert len(worker_dirs) == 3
    finally:
        await colony.stop()


@pytest.mark.asyncio
async def test_run_parallel_workers_returns_error_when_no_colony() -> None:
    """If session.colony is None the tool returns a structured error, not a crash."""

    class _SessionWithoutColony:
        colony = None
        id = "no_colony"
        colony_runtime = None
        event_bus = EventBus()
        worker_path = None
        available_triggers: dict = {}
        active_trigger_ids: set = set()

    registry = ToolRegistry()
    register_queen_lifecycle_tools(
        registry,
        session=_SessionWithoutColony(),
        session_id="no_colony",
    )

    executor = registry.get_executor()
    tool_use = ToolUse(
        id="tu_no_colony",
        name="run_parallel_workers",
        input={"tasks": [{"task": "anything"}]},
    )
    result = executor(tool_use)
    if asyncio.iscoroutine(result):
        result = await result

    payload = json.loads(result.content)
    assert "error" in payload
    assert "ColonyRuntime" in payload["error"]


@pytest.mark.asyncio
async def test_run_parallel_workers_workers_inherit_only_queens_tools(
    tmp_path: Path,
) -> None:
    """Spawned workers must NOT receive tools the parent queen herself
    doesn't currently have (her phase tool list).

    Setup: colony has a broader tool set [A, B, C, D]. The queen's
    available_tools (her current phase) is the subset [A, B]. When she
    fans out via run_parallel_workers, every spawned worker must get
    only A and B.

    Why: the queen is the authority. If a worker can call a tool the
    queen herself can't, the queen is delegating capabilities she
    doesn't own — surprising for the user, breaks the phase-gating
    contract.
    """
    from types import SimpleNamespace

    bus = EventBus()
    # Colony tools: a broader set than the queen sees in her phase.
    tool_a = Tool(name="tool_a", description="a")
    tool_b = Tool(name="tool_b", description="b")
    tool_c = Tool(name="tool_c", description="c — out of queen scope")
    tool_d = Tool(name="tool_d", description="d — out of queen scope")
    colony_tools = [tool_a, tool_b, tool_c, tool_d]

    colony = ColonyRuntime(
        agent_spec=AgentSpec(
            id="t",
            name="t",
            description="t",
            system_prompt="t",
            agent_type="event_loop",
        ),
        goal=Goal(id="g", name="g", description="g"),
        storage_path=tmp_path / "colony",
        llm=_ByTaskMockLLM({}),
        tools=colony_tools,
        tool_executor=_stub_executor,
        event_bus=bus,
        colony_id="scope_test",
        pipeline_stages=[],
    )
    await colony.start()
    try:
        # Capture spawn_batch's tools_override. We don't actually spawn
        # workers — replace with a stub that records the arg and returns
        # synthetic ids. This lets us assert the filter without booting
        # AgentLoops.
        captured: dict = {}

        async def _capturing_spawn_batch(tasks, *, tools_override=None, profile_name=None, skills=None, loop_config_overrides=None, batch_id=None):
            captured["tools_override"] = tools_override
            captured["task_count"] = len(tasks)
            return [f"w_{i}" for i in range(len(tasks))]

        colony.spawn_batch = _capturing_spawn_batch  # type: ignore[assignment]

        # Build a fake queen executor whose available_tools is a SUBSET.
        queen_loop = SimpleNamespace(_last_ctx=SimpleNamespace(available_tools=[tool_a, tool_b]))
        queen_executor = SimpleNamespace(node_registry={"queen": queen_loop})

        session = _FakeSession(colony, "scope_test")
        session.queen_executor = queen_executor  # type: ignore[attr-defined]

        registry = ToolRegistry()
        register_queen_lifecycle_tools(registry, session=session, session_id=session.id)
        executor = registry.get_executor()

        result = executor(
            ToolUse(
                id="tu",
                name="run_parallel_workers",
                input={"tasks": [{"task": "x"}, {"task": "y"}]},
            )
        )
        if asyncio.iscoroutine(result):
            result = await result

        assert not result.is_error, f"Tool errored: {result.content}"
        # Debug: surface what actually happened.
        assert "task_count" in captured, (
            f"spawn_batch was never called. tool result: {result.content}"
        )
        # The override must be a strict subset of the queen's scope.
        override = captured.get("tools_override")
        assert override is not None, (
            f"tools_override must be passed to spawn_batch. result={result.content}"
        )
        names = {getattr(t, "name", None) for t in override}
        assert names == {"tool_a", "tool_b"}, (
            f"workers must only see queen's tools [a,b], got {names}"
        )
        # Out-of-scope tools must NOT leak through.
        assert "tool_c" not in names
        assert "tool_d" not in names
    finally:
        await colony.stop()


@pytest.mark.asyncio
async def test_run_parallel_workers_threads_skills_to_spawn_batch(
    tmp_path: Path,
) -> None:
    """Skills passed to run_parallel_workers must reach spawn_batch.

    Both batch-level (every worker gets the same skills) and per-task
    (one worker gets a different skill set) paths flow through
    intact — that's how the queen factors shared protocol out of the
    duplicated task strings.
    """
    from types import SimpleNamespace

    bus = EventBus()
    colony = ColonyRuntime(
        agent_spec=AgentSpec(
            id="t",
            name="t",
            description="t",
            system_prompt="t",
            agent_type="event_loop",
        ),
        goal=Goal(id="g", name="g", description="g"),
        storage_path=tmp_path / "colony",
        llm=_ByTaskMockLLM({}),
        tools=[],
        tool_executor=_stub_executor,
        event_bus=bus,
        colony_id="skills_test",
        pipeline_stages=[],
    )
    await colony.start()
    try:
        captured: dict = {}

        async def _capturing_spawn_batch(tasks, *, tools_override=None, profile_name=None, skills=None, loop_config_overrides=None, batch_id=None):
            captured["batch_skills"] = skills
            captured["task_skills"] = [t.get("skills") for t in tasks]
            captured["task_count"] = len(tasks)
            return [f"w_{i}" for i in range(len(tasks))]

        colony.spawn_batch = _capturing_spawn_batch  # type: ignore[assignment]

        session = _FakeSession(colony, "skills_test")
        # Stub queen_executor so the strict-bound filter doesn't kick in.
        session.queen_executor = SimpleNamespace(  # type: ignore[attr-defined]
            node_registry={"queen": SimpleNamespace(_last_ctx=None)}
        )

        registry = ToolRegistry()
        register_queen_lifecycle_tools(registry, session=session, session_id=session.id)
        executor = registry.get_executor()

        # Batch-level skills + one task with per-task override.
        result = executor(
            ToolUse(
                id="tu",
                name="run_parallel_workers",
                input={
                    "tasks": [
                        {"task": "fill row 1"},
                        {"task": "fill row 2"},
                        {"task": "validate", "skills": ["validator-protocol"]},
                    ],
                    "skills": ["competitor-research-protocol"],
                },
            )
        )
        if asyncio.iscoroutine(result):
            result = await result

        assert not result.is_error, f"Tool errored: {result.content}"
        assert captured["task_count"] == 3
        # Batch-level reaches spawn_batch.
        assert captured["batch_skills"] == ["competitor-research-protocol"]
        # Tasks 0+1 don't override (None means "fall back to batch").
        assert captured["task_skills"][0] is None
        assert captured["task_skills"][1] is None
        # Task 2 overrides with its own list.
        assert captured["task_skills"][2] == ["validator-protocol"]
    finally:
        await colony.stop()


@pytest.mark.asyncio
async def test_spawn_batch_per_task_skills_overrides_batch_default(
    tmp_path: Path,
) -> None:
    """ColonyRuntime.spawn_batch routes per-task skills to spawn() correctly.

    Direct test of the dispatcher: per-task skills replace batch skills
    for that one worker, missing per-task skills fall back to batch.
    """
    bus = EventBus()
    colony = ColonyRuntime(
        agent_spec=AgentSpec(id="t", name="t", description="t", system_prompt="t", agent_type="event_loop"),
        goal=Goal(id="g", name="g", description="g"),
        storage_path=tmp_path / "colony",
        llm=_ByTaskMockLLM({}),
        tools=[],
        tool_executor=_stub_executor,
        event_bus=bus,
        colony_id="dispatch_test",
        pipeline_stages=[],
    )
    await colony.start()
    try:
        captured: list[dict] = []

        async def _capturing_spawn(*args, **kwargs):
            captured.append(
                {
                    "extra_skills": kwargs.get("extra_skills"),
                    "task": kwargs.get("task"),
                }
            )
            return ["w"]

        colony.spawn = _capturing_spawn  # type: ignore[assignment]

        await colony.spawn_batch(
            [
                {"task": "default-skills"},  # falls back to batch
                {"task": "override-empty", "skills": []},  # explicit empty = none
                {"task": "override-set", "skills": ["x", "y"]},
            ],
            skills=["batch-default"],
        )

        assert len(captured) == 3
        assert captured[0]["extra_skills"] == ["batch-default"]
        # Empty list explicitly passed: spawn gets None (no skills).
        assert captured[1]["extra_skills"] is None
        assert captured[2]["extra_skills"] == ["x", "y"]
    finally:
        await colony.stop()


@pytest.mark.asyncio
async def test_run_parallel_workers_threads_budget_overrides_to_spawn_batch(
    tmp_path: Path,
) -> None:
    """Per-task and batch-level budget overrides must reach spawn_batch
    in the right shape, and per-task overrides must win for that task."""
    from types import SimpleNamespace

    bus = EventBus()
    colony = ColonyRuntime(
        agent_spec=AgentSpec(
            id="t",
            name="t",
            description="t",
            system_prompt="t",
            agent_type="event_loop",
        ),
        goal=Goal(id="g", name="g", description="g"),
        storage_path=tmp_path / "colony",
        llm=_ByTaskMockLLM({}),
        tools=[],
        tool_executor=_stub_executor,
        event_bus=bus,
        colony_id="budget_test",
        pipeline_stages=[],
    )
    await colony.start()
    try:
        captured: dict = {}

        async def _capturing_spawn_batch(
            tasks,
            *,
            tools_override=None,
            profile_name=None,
            skills=None,
            loop_config_overrides=None,
            batch_id=None,
        ):
            captured["batch_loop"] = loop_config_overrides
            captured["task_loops"] = [t.get("loop_config_overrides") for t in tasks]
            captured["batch_id"] = batch_id
            return [f"w_{i}" for i in range(len(tasks))]

        colony.spawn_batch = _capturing_spawn_batch  # type: ignore[assignment]

        session = _FakeSession(colony, "budget_test")
        session.queen_executor = SimpleNamespace(  # type: ignore[attr-defined]
            node_registry={"queen": SimpleNamespace(_last_ctx=None)}
        )
        registry = ToolRegistry()
        register_queen_lifecycle_tools(registry, session=session, session_id=session.id)
        executor = registry.get_executor()

        result = executor(
            ToolUse(
                id="tu",
                name="run_parallel_workers",
                input={
                    "tasks": [
                        {"task": "a"},  # batch defaults apply
                        {
                            "task": "b",
                            "max_iterations": 10,
                            "max_context_tokens": 16000,
                        },  # per-task override
                    ],
                    "max_iterations": 50,
                    "max_tool_calls_per_turn": 30,
                },
            )
        )
        if asyncio.iscoroutine(result):
            result = await result

        assert not result.is_error, result.content
        # Batch-level forwarded.
        assert captured["batch_loop"] == {
            "max_iterations": 50,
            "max_tool_calls_per_turn": 30,
        }
        # Task 0: no per-task override.
        assert captured["task_loops"][0] is None
        # Task 1: only the fields she set are there; spawn_batch will
        # fall back to batch for the missing one.
        assert captured["task_loops"][1] == {
            "max_iterations": 10,
            "max_context_tokens": 16000,
        }
    finally:
        await colony.stop()


@pytest.mark.asyncio
async def test_spawn_batch_per_task_loop_overrides_supersede_batch(
    tmp_path: Path,
) -> None:
    """Direct dispatcher test: per-task loop_config_overrides replace the
    batch default for that one worker; missing per-task config falls
    back to the batch."""
    bus = EventBus()
    colony = ColonyRuntime(
        agent_spec=AgentSpec(id="t", name="t", description="t", system_prompt="t", agent_type="event_loop"),
        goal=Goal(id="g", name="g", description="g"),
        storage_path=tmp_path / "colony",
        llm=_ByTaskMockLLM({}),
        tools=[],
        tool_executor=_stub_executor,
        event_bus=bus,
        colony_id="dispatch_loop_test",
        pipeline_stages=[],
    )
    await colony.start()
    try:
        seen: list[dict | None] = []

        async def _capturing_spawn(*args, **kwargs):
            seen.append(kwargs.get("loop_config_overrides"))
            return ["w"]

        colony.spawn = _capturing_spawn  # type: ignore[assignment]

        await colony.spawn_batch(
            [
                {"task": "uses-batch"},
                {"task": "overrides", "loop_config_overrides": {"max_iterations": 5}},
            ],
            loop_config_overrides={"max_iterations": 100, "max_context_tokens": 64000},
        )

        # Task 0 uses the batch default verbatim.
        assert seen[0] == {"max_iterations": 100, "max_context_tokens": 64000}
        # Task 1 fully replaces with its own dict (max_context_tokens is
        # NOT inherited — per-task is opt-in atomic).
        assert seen[1] == {"max_iterations": 5}
    finally:
        await colony.stop()


class _AlwaysTextOnlyLLM(LLMProvider):
    """Mock LLM that NEVER calls tools — every turn is text-only.

    Used to exercise the worker stall detector: with grace=2 a parallel
    worker should auto-fail (synthesizing report_to_parent(status='failed'))
    on the third consecutive text-only turn.
    """

    model: str = "mock-text-only"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[Tool] | None = None,
        max_tokens: int = 4096,
        **kwargs,
    ) -> AsyncIterator:
        yield TextDeltaEvent(content="Hmm, let me think...", snapshot="Hmm, let me think...")
        yield FinishEvent(stop_reason="stop", input_tokens=10, output_tokens=5, model="mock")

    def complete(self, messages, system="", **kwargs) -> LLMResponse:
        return LLMResponse(content="", model="mock", stop_reason="stop")


@pytest.mark.asyncio
async def test_run_parallel_workers_immediate_return_includes_batch_breadcrumbs(
    tmp_path: Path,
) -> None:
    """The immediate response must carry batch_id, per-worker breadcrumbs
    (worker_id, task_index, task_preview, output_file), and the three
    discipline rules in the message text. Locked in so a future drift
    can't silently strip the queen's correlation handles."""
    from types import SimpleNamespace

    bus = EventBus()
    colony = ColonyRuntime(
        agent_spec=AgentSpec(
            id="t",
            name="t",
            description="t",
            system_prompt="t",
            agent_type="event_loop",
        ),
        goal=Goal(id="g", name="g", description="g"),
        storage_path=tmp_path / "colony",
        llm=_ByTaskMockLLM({}),
        tools=[],
        tool_executor=_stub_executor,
        event_bus=bus,
        colony_id="bc_test",
        pipeline_stages=[],
    )
    await colony.start()
    try:
        # Stub spawn_batch so we don't actually spawn — but we DO want
        # spawn() to populate _workers map with breadcrumbs the tool
        # reads after spawn_batch returns. Easiest: override spawn_batch
        # to populate _workers with stub objects exposing output_file.
        async def _stub_spawn_batch(
            tasks,
            *,
            tools_override=None,
            profile_name=None,
            skills=None,
            loop_config_overrides=None,
            batch_id=None,
        ):
            ids = [f"w_{i}" for i in range(len(tasks))]

            async def _noop_stop():  # colony.stop() iterates _workers and awaits .stop()
                return None

            for wid in ids:
                # Mimic Worker shape: output_file + is_active +
                # the lifecycle methods colony.stop walks the registry to call.
                colony._workers[wid] = SimpleNamespace(
                    output_file=f"/tmp/colony/workers/{wid}/conversations/parts",
                    is_active=False,  # already "done" so stop() doesn't iterate
                    stop=_noop_stop,
                    _task_handle=None,
                )
            return ids

        colony.spawn_batch = _stub_spawn_batch  # type: ignore[assignment]

        session = _FakeSession(colony, "bc_test")
        session.queen_executor = SimpleNamespace(  # type: ignore[attr-defined]
            node_registry={"queen": SimpleNamespace(_last_ctx=None)}
        )
        registry = ToolRegistry()
        register_queen_lifecycle_tools(registry, session=session, session_id=session.id)
        executor = registry.get_executor()

        result = executor(
            ToolUse(
                id="tu",
                name="run_parallel_workers",
                input={
                    "tasks": [
                        {"task": "fill rows: a, b"},
                        {"task": "fill rows: c, d, e — way longer prose " * 10},
                    ],
                },
            )
        )
        if asyncio.iscoroutine(result):
            result = await result
        assert not result.is_error, result.content

        payload = json.loads(result.content)
        assert payload["status"] == "started"
        # batch_id must be a non-empty string and follow the rpw_*
        # prefix so logs / queen prompts can identify it.
        assert isinstance(payload["batch_id"], str) and payload["batch_id"].startswith("rpw_")
        # workers list mirrors per-task breadcrumbs.
        assert isinstance(payload["workers"], list)
        assert len(payload["workers"]) == 2
        bc0 = payload["workers"][0]
        assert bc0["worker_id"] == "w_0"
        assert bc0["task_index"] == 1
        assert bc0["task_preview"] == "fill rows: a, b"
        assert bc0["output_file"].endswith("conversations/parts")
        bc1 = payload["workers"][1]
        # Long task strings get truncated to ≤200 chars + ellipsis.
        assert bc1["task_index"] == 2
        assert len(bc1["task_preview"]) <= 201
        # The three disciplines must be present in the message — these
        # are what teach the queen the structured-report contract.
        msg = payload["message"]
        assert "batch_remaining" in msg
        assert "Don't poll" in msg
        assert "Don't fabricate" in msg
        assert "Don't peek" in msg
    finally:
        await colony.stop()


@pytest.mark.asyncio
async def test_subagent_report_carries_batch_metadata(tmp_path: Path) -> None:
    """A worker spawned via spawn_batch must emit a SUBAGENT_REPORT with
    batch_id, batch_index, batch_size, output_file populated. This is
    what the queen-side handler reads to render the structured block."""
    bus = EventBus()
    llm = _ByTaskMockLLM(
        by_task={
            "task-A": _report("success", "A done", {"x": 1}),
            "task-B": _report("success", "B done", {"y": 2}),
        }
    )
    colony = ColonyRuntime(
        agent_spec=AgentSpec(
            id="t",
            name="t",
            description="t",
            system_prompt="t",
            agent_type="event_loop",
            output_keys=[],
            tool_access_policy="all",
        ),
        goal=Goal(id="g", name="g", description="g"),
        storage_path=tmp_path / "colony",
        llm=llm,
        tools=[],
        tool_executor=_stub_executor,
        event_bus=bus,
        colony_id="meta_test",
        pipeline_stages=[],
    )
    await colony.start()
    captured: list[dict] = []

    async def _on_report(event: AgentEvent) -> None:
        captured.append(event.data or {})

    bus.subscribe(event_types=[EventType.SUBAGENT_REPORT], handler=_on_report)
    try:
        worker_ids = await colony.spawn_batch(
            [{"task": "task-A"}, {"task": "task-B"}],
        )
        assert len(worker_ids) == 2

        for _ in range(50):
            if len(captured) >= 2:
                break
            await asyncio.sleep(0.1)
        assert len(captured) == 2

        # Both reports share the same batch_id.
        batch_ids = {r.get("batch_id") for r in captured}
        assert len(batch_ids) == 1, f"expected one batch_id, got {batch_ids}"
        # Indices are 1 and 2 in some order.
        assert sorted(r.get("batch_index") for r in captured) == [1, 2]
        for r in captured:
            assert r.get("batch_size") == 2
            assert r.get("output_file", "").endswith("conversations/parts")
    finally:
        await colony.stop()


@pytest.mark.asyncio
async def test_parallel_worker_stall_auto_fails_with_synthetic_report(
    tmp_path: Path,
) -> None:
    """A parallel worker that produces text-only turns past grace must
    auto-fail via a synthesized report_to_parent(status='failed') —
    NOT by emitting ESCALATION_REQUESTED. Per BRD fail-fast model.

    This locks in: (a) no ESCALATION_REQUESTED fires for parallel
    workers; (b) SUBAGENT_REPORT fires with status='failed' and a
    summary containing the auto-fail reason; (c) worker terminates
    cleanly (no synchronous queen-input wait).
    """
    bus = EventBus()
    colony = ColonyRuntime(
        agent_spec=AgentSpec(
            id="stall_test",
            name="Stall Test Colony",
            description="Mock colony for stall detection tests.",
            system_prompt="You are a worker.",
            agent_type="event_loop",
            output_keys=[],
            tool_access_policy="all",
        ),
        goal=Goal(id="g", name="g", description="g"),
        storage_path=tmp_path / "colony",
        llm=_AlwaysTextOnlyLLM(),
        tools=[],
        tool_executor=_stub_executor,
        event_bus=bus,
        colony_id="stall_test",
        pipeline_stages=[],
    )
    await colony.start()

    # Capture both event types so we can assert what fired and what didn't.
    reports: list[dict] = []
    escalations: list[dict] = []

    async def _on_report(event: AgentEvent) -> None:
        reports.append(event.data or {})

    async def _on_escalation(event: AgentEvent) -> None:
        escalations.append(event.data or {})

    bus.subscribe(event_types=[EventType.SUBAGENT_REPORT], handler=_on_report)
    bus.subscribe(event_types=[EventType.ESCALATION_REQUESTED], handler=_on_escalation)

    try:
        # Spawn directly (bypasses run_parallel_workers tool, which would
        # also work but adds a layer of session/registry mocking we don't
        # need for this assertion).
        worker_ids = await colony.spawn_batch(
            [{"task": "do something"}],
        )
        assert len(worker_ids) == 1

        # Wait for the auto-fail report. With grace=2 the worker fails
        # on the 3rd text-only turn; each turn is a no-op LLM call so
        # the whole sequence should land in well under 5s.
        for _ in range(50):
            if reports:
                break
            await asyncio.sleep(0.1)

        assert len(reports) == 1, (
            f"expected one SUBAGENT_REPORT, got {len(reports)}: {reports}"
        )
        report = reports[0]
        assert report.get("status") == "failed"
        # Summary must explain WHY (so the queen can re-dispatch
        # informedly) and include the auto-fail signature.
        assert "auto-failed" in report.get("summary", "").lower() or \
            "stall" in report.get("summary", "").lower()
        # Critical BRD invariant: parallel workers MUST NOT emit
        # ESCALATION_REQUESTED. They fail-fast or report success;
        # there is no escalation channel.
        assert escalations == [], (
            f"Parallel worker fired ESCALATION_REQUESTED — should be "
            f"impossible after BRD escalation removal. Got: {escalations}"
        )
    finally:
        await colony.stop()


@pytest.mark.asyncio
async def test_run_parallel_workers_validates_tasks_input() -> None:
    """Empty / non-list / missing-task-string inputs return structured errors."""
    bus = EventBus()
    colony = ColonyRuntime(
        agent_spec=AgentSpec(
            id="t",
            name="t",
            description="t",
            system_prompt="t",
            agent_type="event_loop",
        ),
        goal=Goal(id="g", name="g", description="g"),
        storage_path=Path("/tmp/_phase4_validation_test_colony"),
        llm=_ByTaskMockLLM({}),
        tools=[],
        tool_executor=_stub_executor,
        event_bus=bus,
        colony_id="phase4_validation",
        pipeline_stages=[],
    )
    await colony.start()
    session = _FakeSession(colony, "phase4_validation")
    registry = ToolRegistry()
    register_queen_lifecycle_tools(registry, session=session, session_id=session.id)
    executor = registry.get_executor()

    async def _call(payload: dict) -> dict:
        r = executor(ToolUse(id="tu", name="run_parallel_workers", input=payload))
        if asyncio.iscoroutine(r):
            r = await r
        return json.loads(r.content)

    try:
        # Empty list
        assert "error" in await _call({"tasks": []})
        # Missing task string
        assert "error" in await _call({"tasks": [{"data": {}}]})
    finally:
        await colony.stop()


# ---------------------------------------------------------------------------
# Soft-timeout inject reaches slow workers; explicit-report preservation
# ---------------------------------------------------------------------------


class _SlowLLM(LLMProvider):
    """Mock LLM that stalls on _await_user_input by never yielding a finish.

    Each call to ``stream`` awaits the ``stall_event`` before emitting any
    tokens — tests drive it via ``release()``. When the worker's LLM is
    stuck waiting, the watcher's inject message arrives at ``_input_queue``
    but the LLM turn doesn't see it until the current stream finishes.
    We simulate "worker is stuck mid-turn" by holding the stall until the
    test explicitly releases it.
    """

    model: str = "mock-slow"

    def __init__(self) -> None:
        self.stall_event = asyncio.Event()
        self.release_after_inject: bool = False
        self.report_on_release: tuple[str, str, dict] | None = None
        self.inject_seen = asyncio.Event()
        self._turn_count = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[Tool] | None = None,
        max_tokens: int = 4096,
        **kwargs,
    ) -> AsyncIterator:
        self._turn_count += 1
        # On the second call (after the watcher's inject), check whether the
        # SOFT TIMEOUT message arrived in the conversation.
        if self._turn_count >= 2:
            for m in messages:
                content = m.get("content", "")
                if isinstance(content, str) and "[SOFT TIMEOUT]" in content:
                    self.inject_seen.set()
            if self.report_on_release:
                st, summary, data = self.report_on_release
                yield ToolCallEvent(
                    tool_use_id=f"tu_report_{self._turn_count}",
                    tool_name="report_to_parent",
                    tool_input={"status": st, "summary": summary, "data": data},
                )
                yield FinishEvent(stop_reason="tool_calls", input_tokens=1, output_tokens=1, model="mock-slow")
                return
            # Otherwise loop forever (ignore warning).
            await self.stall_event.wait()
            yield FinishEvent(stop_reason="stop", input_tokens=1, output_tokens=1, model="mock-slow")
            return

        # First turn: stall until released.
        await self.stall_event.wait()
        yield TextDeltaEvent(content="thinking...", snapshot="thinking...")
        yield FinishEvent(stop_reason="stop", input_tokens=1, output_tokens=1, model="mock-slow")

    def complete(self, messages, system="", **kwargs) -> LLMResponse:
        return LLMResponse(content="", model="mock-slow", stop_reason="stop")


async def _build_colony(tmp_path: Path, llm: LLMProvider, colony_id: str) -> ColonyRuntime:
    bus = EventBus()
    colony = ColonyRuntime(
        agent_spec=AgentSpec(
            id="t",
            name="t",
            description="t",
            system_prompt="t",
            agent_type="event_loop",
            tool_access_policy="all",
        ),
        goal=Goal(id="g", name="g", description="g"),
        storage_path=tmp_path / colony_id,
        llm=llm,
        tools=[],
        tool_executor=_stub_executor,
        event_bus=bus,
        colony_id=colony_id,
        pipeline_stages=[],
    )
    await colony.start()
    return colony


@pytest.mark.asyncio
async def test_watch_batch_timeouts_soft_inject_only_hits_stragglers(
    tmp_path: Path,
) -> None:
    """Workers that already filed an explicit report must NOT receive the
    SOFT TIMEOUT warning inject."""
    fast_llm = _ByTaskMockLLM(by_task={"fast": _report("success", "fast done", {})})
    colony = await _build_colony(tmp_path, fast_llm, "soft_fast")

    try:
        ids = await colony.spawn_batch([{"task": "fast"}])
        worker = colony._workers[ids[0]]

        # Wait for the worker to finish naturally.
        for _ in range(50):
            if not worker.is_active:
                break
            await asyncio.sleep(0.05)
        assert not worker.is_active
        assert worker._explicit_report is not None  # it did call report_to_parent

        # Snapshot input-queue depth, then schedule watcher with short soft.
        before = worker._input_queue.qsize()
        task = colony.watch_batch_timeouts(
            ids,
            soft_timeout=0.1,
            hard_timeout=0.2,
        )
        await task
        # Worker already finished + reported — watcher must skip the inject.
        assert worker._input_queue.qsize() == before
    finally:
        await colony.stop()


@pytest.mark.asyncio
async def test_explicit_report_survives_cancel(tmp_path: Path) -> None:
    """A worker that set _explicit_report right before being cancelled must
    emit a SUBAGENT_REPORT carrying the explicit payload, not the canned
    'Worker was cancelled' stub."""
    llm = _ByTaskMockLLM(by_task={"cancel-me": _report("success", "partial wrap-up", {"items_done": 3})})
    colony = await _build_colony(tmp_path, llm, "cancel_survives")

    collected: list[dict] = []

    async def _on_report(event: AgentEvent) -> None:
        collected.append(event.data or {})

    colony.event_bus.subscribe(event_types=[EventType.SUBAGENT_REPORT], handler=_on_report)

    try:
        ids = await colony.spawn_batch([{"task": "cancel-me"}])
        worker = colony._workers[ids[0]]

        # Let worker finish first turn so _explicit_report is set,
        # then cancel it.
        for _ in range(50):
            if worker._explicit_report is not None:
                break
            await asyncio.sleep(0.05)
        assert worker._explicit_report is not None, "Worker never set _explicit_report — test precondition not met"

        # Cancel the already-reported worker.
        await colony.stop_worker(ids[0])

        # Drain any pending events.
        for _ in range(20):
            if collected:
                break
            await asyncio.sleep(0.05)

        # The report we receive should be the explicit one.
        assert collected, "No SUBAGENT_REPORT emitted"
        # Find the cancel-survives worker's report (there should only be one).
        report = collected[0]
        assert report.get("summary") == "partial wrap-up", report
        assert report.get("data", {}).get("items_done") == 3, report
    finally:
        await colony.stop()
