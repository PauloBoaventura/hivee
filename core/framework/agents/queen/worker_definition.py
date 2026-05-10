"""Worker agent definition — runtime identity + worker.json serialization.

What a worker IS (section 1): a focused, ephemeral task executor spawned
by the queen inside a colony. No escalation, no reflection, no client
facing, fail-fast. Mirrors the BRD parallel-agent spec.

How a worker is SERIALIZED to ``worker.json`` (section 2): the dict
shape that ``fork_session_into_colony`` writes to disk at colony-fork
time, and that the agent_loader reads back when spawning worker
AgentLoops. These two sections live together because the disk format
IS the runtime identity — adding a field in one without the other is
a silent contract break.

Imported by:
- ``routes_execution.fork_session_into_colony`` — builds + writes worker.json
- ``colony_runtime.spawn`` — reads the default loop config + goal
- ``test_worker_definition`` — unit coverage
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Section 1 — Runtime identity
# ---------------------------------------------------------------------------

from framework.schemas.goal import Goal

worker_goal = Goal(
    id="queen-worker",
    name="Queen Worker",
    description=(
        "Ephemeral parallel worker spawned by the queen to carry out one "
        "unit of delegated work. No memory of prior runs, no reflection, "
        "no client-facing tools, no escalation channel — completes the "
        "assigned task and reports back via report_to_parent."
    ),
    success_criteria=[],
    constraints=[],
)

# Default loop config for a worker. The queen's own config (999_999
# iterations, 180k context) is tuned for long-running conversational
# oversight; a single-task worker should have tighter defaults. The
# queen can override these per-batch via ``run_parallel_workers``
# budget knobs (max_iterations / max_tool_calls_per_turn /
# max_context_tokens).
DEFAULT_LOOP_CONFIG: dict[str, int] = {
    "max_iterations": 50,
    "max_tool_calls_per_turn": 30,
    "max_context_tokens": 64_000,
}

WORKER_SYSTEM_PROMPT = """\
You are a focused worker agent spawned by the queen to carry out one \
specific task. Read the goal carefully, use your available tools to \
make progress, and call ``report_to_parent`` when you finish — that \
is your terminal channel back to the queen. Send \
``report_to_parent(status='success', summary=<one-paragraph result>, \
data=<structured payload, optional>)`` on completion. Your loop ends \
after this call; do not call any other tool afterwards.

If the queen attached one or more skills to this spawn (you'll see them \
in your skill catalog), follow that protocol — it carries the per-batch \
schema, output format, quality bar, and tool conventions she factored \
out so each task string only carries the unique slice. The skill is \
the operational truth; the task string is the per-worker arguments.

If the colony has a tracker (``tracker.db``, signalled by the \
tracker-write tools in your toolset), prefer ``tracker_upsert`` for \
recording structured findings — the queen reads rows directly and \
validates progress via SQL. Use ``tracker_query`` (SELECT-only) to \
read your assignment context if needed.

FAIL FAST. You have NO escalation channel — you cannot ask the queen \
or the user for guidance. If you can't complete the task (missing \
info, blocked, repeated tool failure, scope ambiguity), call \
``report_to_parent(status='failed', summary=<one-paragraph reason>, \
data=<any partial state>)`` and stop. The queen reads the failure \
and either re-dispatches you with different parameters or takes over. \
Do NOT loop trying alternative workarounds for more than 2-3 \
attempts; surface the failure cleanly.

Status values for ``report_to_parent``:
- ``success``  — task complete, results in summary/data
- ``partial``  — some progress, but you couldn't finish (explain in summary)
- ``failed``   — could not make meaningful progress
"""


def build_system_prompt(task: str | None) -> str:
    """Templated system prompt with the task string appended.

    The header is identity-free — workers are not the queen's persona
    and inheriting identity_prompt makes them greet the user in first
    person with no memory of the assigned work.
    """
    worker_task = task or "Continue the work from the queen's current session."
    return f"{WORKER_SYSTEM_PROMPT}\nTask: {worker_task}"


# ---------------------------------------------------------------------------
# Section 2 — worker.json serialization (consumed by fork_session_into_colony)
# ---------------------------------------------------------------------------


def build_input_data(
    *,
    db_path: str,
    tracker_db_path: str,
    colony_id: str,
    seeded_task_id: str | None = None,
) -> dict[str, Any]:
    """Threaded into the worker's first user message via
    ``_format_spawn_task_message`` so the worker can claim queue rows
    (progress.db) and write tracker rows (tracker.db) without deriving
    paths from layout assumptions. ``seeded_task_id`` pins the worker
    to a specific progress.db row (colony-progress-tracker assigned-
    task-id branch).
    """
    data: dict[str, Any] = {
        "db_path": db_path,
        "tracker_db_path": tracker_db_path,
        "colony_id": colony_id,
    }
    if seeded_task_id:
        data["task_id"] = seeded_task_id
    return data


def build_loop_config_dict(queen_loop_config: Any | None) -> dict[str, Any]:
    """Distil the queen's LoopConfig into a worker.json-compatible dict.

    Typed as ``Any`` to avoid importing the agent-loop package (cycle
    avoidance — callers already have the LoopConfig in hand). Falls
    back to ``DEFAULT_LOOP_CONFIG`` when the queen's config isn't
    available.

    Returns JSON-serialisable. Only the fields the worker runtime
    propagates are included; everything else falls back to
    LoopConfig() defaults at spawn time.
    """
    cfg = dict(DEFAULT_LOOP_CONFIG)
    if queen_loop_config is None:
        return cfg
    cfg["max_iterations"] = queen_loop_config.max_iterations
    cfg["max_tool_calls_per_turn"] = queen_loop_config.max_tool_calls_per_turn
    cfg["max_context_tokens"] = queen_loop_config.max_context_tokens
    cfg["max_tool_result_chars"] = queen_loop_config.max_tool_result_chars
    return cfg


def build_meta(
    *,
    worker_name: str,
    source_session_id: str,
    task: str | None,
    tool_names: list[str],
    skills_catalog_prompt: str,
    protocols_prompt: str,
    skill_dirs: list[str],
    queen_loop_config: Any | None,
    queen_phase: str,
    queen_id: str,
    input_data: dict[str, Any],
    concurrency_hint: int | None = None,
) -> dict[str, Any]:
    """Assemble the dict serialised to ``worker.json`` at colony fork.

    ``identity_prompt`` and ``memory_prompt`` are empty by construction
    — workers are NOT the queen's persona. Per-profile clones use
    ``dict(worker_meta)`` + per-profile overrides (task, prompt
    suffix, tool filter, concurrency_hint) from the calling loop in
    ``fork_session_into_colony``.
    """
    meta: dict[str, Any] = {
        "name": worker_name,
        "version": "1.0.0",
        "description": f"Worker clone from queen session {source_session_id}",
        "input_data": input_data,
        "goal": {
            "description": task or "Continue the work from the queen's current session.",
            "success_criteria": [],
            "constraints": [],
        },
        "system_prompt": build_system_prompt(task),
        "tools": list(tool_names),
        "skills_catalog_prompt": skills_catalog_prompt,
        "protocols_prompt": protocols_prompt,
        "skill_dirs": list(skill_dirs),
        "identity_prompt": "",
        "memory_prompt": "",
        "queen_phase": queen_phase,
        "queen_id": queen_id,
        "loop_config": build_loop_config_dict(queen_loop_config),
        "spawned_from": source_session_id,
        "spawned_at": datetime.now(UTC).isoformat(),
    }
    # Concurrency hint: copied into the per-worker spec so per-profile
    # clones inherit it. The colony-level max_concurrent_workers (set
    # on metadata.json from this same value at fork time) is what the
    # runtime actually enforces.
    if isinstance(concurrency_hint, int) and concurrency_hint > 0:
        meta["concurrency_hint"] = concurrency_hint
    return meta


__all__ = [
    # Runtime identity
    "DEFAULT_LOOP_CONFIG",
    "WORKER_SYSTEM_PROMPT",
    "build_system_prompt",
    "worker_goal",
    # Serialization
    "build_input_data",
    "build_loop_config_dict",
    "build_meta",
]
