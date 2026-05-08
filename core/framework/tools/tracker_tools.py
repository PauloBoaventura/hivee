"""Queen + worker tools for the per-colony tracker DB.

Three tools are wired here:

- ``tracker_sql(sql)`` — **queen-only**. Raw SQL against ``tracker.db``,
  denylist enforced (see :mod:`framework.host.tracker_db`). Returns rows
  for ``SELECT`` and ``{rowcount, last_insert_rowid}`` for DDL/DML.
  This is the queen's primary tool for designing the tracker schema and
  validating progress.

- ``tracker_register_writable(table, write_columns, key_columns?, mode?)``
  — **queen-only**. Records a row in the ``_tracker_registry`` so workers
  may call :func:`tracker_upsert` against ``table``. Validates that the
  table exists, the columns exist, and (for ``upsert`` mode) that the key
  columns have a unique index that covers them.

- ``tracker_upsert(table, row)`` — **shared**. The narrow worker tool.
  Looks up ``table`` in ``_tracker_registry`` and either does
  ``INSERT ... ON CONFLICT(<keys>) DO UPDATE`` (mode=``upsert``) or a
  plain INSERT (mode=``append``). Refuses unregistered tables and
  ``_*`` framework tables.

The colony's ``tracker.db`` path is derived from ``colony_id`` in the
calling agent's execution context, so the tools work the same for the
queen (post-fork) and for workers spawned into the colony.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from framework.host.tracker_db import (
    DenylistError,
    PROTECTED_PREFIX,
    ensure_tracker_db,
    execute_sql,
)
from framework.llm.provider import Tool
from framework.loader.tool_registry import ToolRegistry
from framework.tasks.tools._context import current_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _resolve_tracker_db_path() -> Path | None:
    """Return the absolute path to the calling agent's ``tracker.db``.

    Resolution order:
      1. ``tracker_db_path`` from the agent's input_data (preferred — the
         spawn flow injects this absolute path so workers don't need to
         re-derive it from layout assumptions).
      2. ``colony_id`` from the execution context, joined under
         ``framework.config.COLONIES_DIR / colony_id / data / tracker.db``.

    Returns ``None`` when neither is available — caller should surface a
    clear error rather than silently ATTACHing to the wrong DB. The
    framework guarantees the file exists once the colony has been forked
    (``ensure_tracker_db`` is called during fork and on host startup).
    """
    ctx = current_context()
    raw = ctx.get("tracker_db_path")
    if isinstance(raw, str) and raw:
        return Path(raw)

    colony_id = ctx.get("colony_id")
    if not colony_id:
        return None

    from framework.config import COLONIES_DIR

    return COLONIES_DIR / str(colony_id) / "data" / "tracker.db"


# ---------------------------------------------------------------------------
# tracker_sql (queen-only)
# ---------------------------------------------------------------------------


_TRACKER_SQL_DESC = (
    "Run raw SQL against this colony's tracker.db. The tracker is your "
    "queen-owned domain model — design the table(s) that describe progress "
    "toward the goal, then validate worker output by querying the table.\n\n"
    "Typical workflow after the colony is created:\n"
    "  1. CREATE TABLE <name> (...)  -- design columns that describe one "
    "unit of progress (one row = one company, one paper, one row, etc.).\n"
    "  2. INSERT INTO <name> (key_col, ...) VALUES (...)  -- seed primary "
    "keys you already know so workers fan out across disjoint rows.\n"
    "  3. tracker_register_writable(...)  -- whitelist which columns "
    "workers may write to; without this they cannot upsert.\n"
    "  4. run_parallel_workers(...)  -- delegate row-fill work.\n"
    "  5. SELECT ... WHERE <col> IS NULL  -- find gaps; re-dispatch if needed.\n\n"
    "Returns:\n"
    "  - SELECT: {kind: 'rows', columns: [...], rows: [[...], ...], "
    "rowcount, truncated}. Rows past row_cap are dropped (truncated=true); "
    "paginate with LIMIT/OFFSET.\n"
    "  - DDL/DML: {kind: 'exec', rowcount, last_insert_rowid}.\n"
    "  - Multi-statement script (statements separated by ';'): "
    "{kind: 'script', results: [<per-stmt result>, ...]}.\n\n"
    "Forbidden: ATTACH, DETACH, PRAGMA, VACUUM, REINDEX, load_extension(). "
    "Tables starting with '_' are framework-owned (DDL/DML rejected; "
    "SELECT is fine). Cap of 20 statements per call."
)


def _tracker_sql_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": (
                    "Raw SQL. May be a single statement or a script of "
                    "statements separated by ';'. CTEs, transactions "
                    "(BEGIN/COMMIT), and views are allowed."
                ),
            },
            "row_cap": {
                "type": "integer",
                "description": "Max rows returned per SELECT (default 1000).",
                "minimum": 1,
                "maximum": 10000,
            },
        },
        "required": ["sql"],
    }


def _make_tracker_sql_executor():
    async def execute(inputs: dict) -> dict[str, Any]:
        db_path = _resolve_tracker_db_path()
        if db_path is None:
            return {
                "success": False,
                "error": (
                    "tracker_sql: no colony context — this tool only works "
                    "inside a colony (after create_colony). Current "
                    "execution context has no colony_id and no "
                    "tracker_db_path."
                ),
            }
        sql = inputs.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            return {"success": False, "error": "tracker_sql: 'sql' is required."}
        # Make sure tracker.db exists. The fork flow already calls
        # ensure_tracker_db, so this is just defensive (e.g. when the
        # queen runs tracker_sql before any worker has spawned).
        try:
            ensure_tracker_db(db_path.parent.parent)
        except Exception as e:
            logger.exception("tracker_sql: ensure_tracker_db failed")
            return {"success": False, "error": f"tracker_sql: {e}"}

        row_cap = int(inputs.get("row_cap") or 1000)
        try:
            result = execute_sql(db_path, sql, row_cap=row_cap)
        except DenylistError as e:
            return {"success": False, "error": f"tracker_sql denied: {e}"}
        except sqlite3.Error as e:
            # Surface SQLite errors verbatim so the queen can debug her schema.
            return {"success": False, "error": f"tracker_sql sqlite error: {e}"}
        return {"success": True, **result}

    return execute


# ---------------------------------------------------------------------------
# tracker_register_writable (queen-only)
# ---------------------------------------------------------------------------


_TRACKER_REGISTER_DESC = (
    "Whitelist a tracker table for worker writes. Workers cannot call "
    "tracker_upsert against an unregistered table — without registration "
    "the table is invisible to them. Call this after CREATE TABLE.\n\n"
    "Args:\n"
    "  table         — the table name in tracker.db (must already exist).\n"
    "  write_columns — list of columns workers may set on each row.\n"
    "                  Columns NOT in this list (e.g. an internal "
    "                  'reviewed_at') stay queen-only.\n"
    "  key_columns   — list of columns that uniquely identify a row "
    "                  (used for ON CONFLICT). Empty list / omitted ⇒ "
    "                  append mode (every upsert call inserts a new row).\n"
    "  mode          — 'upsert' (default when key_columns given) or "
    "                  'append' (default when key_columns empty).\n\n"
    "Validation: the table must exist in tracker.db, every named column "
    "must exist, and for upsert mode every key_column must be covered by "
    "a UNIQUE index (a PRIMARY KEY counts). Re-registering a table is "
    "fine — the new spec replaces the old one."
)


def _tracker_register_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "table": {
                "type": "string",
                "description": "Tracker table name (must already exist).",
            },
            "write_columns": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "Columns workers may write. Other columns stay queen-only."
                ),
            },
            "key_columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Columns that uniquely identify a row (for ON CONFLICT). "
                    "Empty / omitted ⇒ append mode."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["upsert", "append"],
                "description": (
                    "Override the default mode inferred from key_columns."
                ),
            },
        },
        "required": ["table", "write_columns"],
    }


def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    """Return ordered column names for ``table``, or [] if missing."""
    # Direct PRAGMA call from framework code — denylist applies only to
    # user-supplied SQL routed through validate_sql.
    rows = con.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    return [r[1] for r in rows]


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _quote_ident(name: str) -> str:
    """Quote a SQLite identifier safely (double the embedded double-quotes)."""
    return '"' + name.replace('"', '""') + '"'


def _key_columns_uniquely_indexed(
    con: sqlite3.Connection, table: str, keys: list[str]
) -> bool:
    """True if some unique index on ``table`` covers exactly ``keys``.

    Order matters in SQLite indices but not for our purposes (ON CONFLICT
    doesn't care about declaration order). We compare as sets.
    """
    keys_set = set(keys)
    idx_rows = con.execute(
        f"PRAGMA index_list({_quote_ident(table)})"
    ).fetchall()
    for idx in idx_rows:
        # PRAGMA index_list columns: seq, name, unique, origin, partial
        idx_name, is_unique = idx[1], bool(idx[2])
        if not is_unique:
            continue
        cols = [
            r[2]
            for r in con.execute(
                f"PRAGMA index_info({_quote_ident(idx_name)})"
            ).fetchall()
        ]
        if set(cols) == keys_set:
            return True
    return False


def _make_tracker_register_executor():
    async def execute(inputs: dict) -> dict[str, Any]:
        from framework.host.tracker_db import _connect, _now_iso

        db_path = _resolve_tracker_db_path()
        if db_path is None:
            return {
                "success": False,
                "error": "tracker_register_writable: no colony context.",
            }

        table = (inputs.get("table") or "").strip()
        if not table:
            return {"success": False, "error": "table is required"}
        if table.startswith(PROTECTED_PREFIX):
            return {
                "success": False,
                "error": (
                    f"table '{table}' is in the protected '{PROTECTED_PREFIX}*' "
                    "namespace and cannot be registered for worker writes."
                ),
            }

        write_columns = inputs.get("write_columns") or []
        if not isinstance(write_columns, list) or not all(
            isinstance(c, str) and c for c in write_columns
        ):
            return {
                "success": False,
                "error": "write_columns must be a non-empty list of strings",
            }

        key_columns = inputs.get("key_columns") or []
        if not isinstance(key_columns, list) or not all(
            isinstance(c, str) and c for c in key_columns
        ):
            return {
                "success": False,
                "error": "key_columns must be a list of strings",
            }

        mode = (inputs.get("mode") or "").strip()
        if not mode:
            mode = "upsert" if key_columns else "append"
        if mode not in ("upsert", "append"):
            return {"success": False, "error": "mode must be 'upsert' or 'append'"}
        if mode == "upsert" and not key_columns:
            return {
                "success": False,
                "error": "mode='upsert' requires non-empty key_columns",
            }

        # Make sure the DB exists; the fork flow does this, but a fresh
        # tracker_register_writable call before any other tracker activity
        # should still succeed.
        ensure_tracker_db(db_path.parent.parent)

        con = _connect(db_path)
        try:
            if not _table_exists(con, table):
                return {
                    "success": False,
                    "error": (
                        f"table '{table}' does not exist in tracker.db. "
                        "CREATE the table via tracker_sql first."
                    ),
                }

            actual_cols = _table_columns(con, table)
            actual_set = set(actual_cols)
            missing_write = [c for c in write_columns if c not in actual_set]
            if missing_write:
                return {
                    "success": False,
                    "error": (
                        f"write_columns not found on '{table}': "
                        f"{missing_write}. Existing: {actual_cols}"
                    ),
                }
            missing_key = [c for c in key_columns if c not in actual_set]
            if missing_key:
                return {
                    "success": False,
                    "error": (
                        f"key_columns not found on '{table}': {missing_key}"
                    ),
                }

            if mode == "upsert" and not _key_columns_uniquely_indexed(
                con, table, key_columns
            ):
                return {
                    "success": False,
                    "error": (
                        f"key_columns {key_columns} are not covered by a "
                        f"UNIQUE index on '{table}'. Add PRIMARY KEY or "
                        "CREATE UNIQUE INDEX before registering for upsert."
                    ),
                }

            con.execute(
                """
                INSERT INTO _tracker_registry
                    (table_name, write_columns, key_columns, mode, registered_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(table_name) DO UPDATE SET
                    write_columns = excluded.write_columns,
                    key_columns   = excluded.key_columns,
                    mode          = excluded.mode,
                    registered_at = excluded.registered_at
                """,
                (
                    table,
                    json.dumps(list(write_columns)),
                    json.dumps(list(key_columns)),
                    mode,
                    _now_iso(),
                ),
            )
        finally:
            con.close()

        return {
            "success": True,
            "table": table,
            "write_columns": list(write_columns),
            "key_columns": list(key_columns),
            "mode": mode,
            "message": (
                f"Registered '{table}' for worker writes "
                f"(mode={mode}, key_columns={key_columns or 'none — append'})."
            ),
        }

    return execute


# ---------------------------------------------------------------------------
# tracker_upsert (worker-facing)
# ---------------------------------------------------------------------------


_TRACKER_UPSERT_DESC = (
    "Write a row to a tracker table the queen has registered for worker "
    "writes. This is your channel for reporting findings — prefer it over "
    "embedding structured data in your final-message text, because the "
    "queen reads tracker rows directly and can validate them.\n\n"
    "Args:\n"
    "  table — the registered tracker table.\n"
    "  row   — dict of column→value. Must include all key_columns the "
    "          queen registered (for upsert mode). Columns not in the "
    "          registered write_columns are rejected.\n\n"
    "Behavior depends on the table's registered mode:\n"
    "  - upsert: INSERT ... ON CONFLICT(<keys>) DO UPDATE — call again "
    "    with the same key to update the row.\n"
    "  - append: plain INSERT — every call adds a new row.\n\n"
    "Refuses unregistered tables and any '_*' framework table."
)


def _tracker_upsert_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "table": {"type": "string"},
            "row": {
                "type": "object",
                "description": (
                    "Column→value pairs. Values may be string, number, "
                    "boolean, or null. Lists/objects are JSON-encoded "
                    "before write."
                ),
            },
        },
        "required": ["table", "row"],
    }


# ---------------------------------------------------------------------------
# tracker_query (shared — SELECT-only)
# ---------------------------------------------------------------------------


_TRACKER_QUERY_DESC = (
    "Read rows from the colony's tracker.db. SELECT-only — DDL, INSERT, "
    "UPDATE, and DELETE are rejected (use tracker_upsert for writes).\n\n"
    "Workers: use this to read your assignment context (e.g. \"which "
    "rows still need work\", \"what columns are expected\") instead of "
    "asking the queen. The queen has already designed the table; you "
    "can introspect via SELECT against ``sqlite_master`` or the table "
    "directly.\n\n"
    "Queen: also fine to use for read-only checks; tracker_sql covers "
    "the same ground with broader powers.\n\n"
    "Returns ``{kind: 'rows', columns: [...], rows: [[...], ...], "
    "rowcount, truncated}``. Rows past row_cap are dropped (truncated="
    "true); paginate with LIMIT/OFFSET.\n\n"
    "Allowed: SELECT, WITH, EXPLAIN. Forbidden: ATTACH, DETACH, PRAGMA, "
    "VACUUM, REINDEX, load_extension(), and ALL writes/DDL."
)


def _tracker_query_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": (
                    "SELECT (or WITH … SELECT) statement. Single statement; "
                    "scripts not allowed."
                ),
            },
            "row_cap": {
                "type": "integer",
                "description": "Max rows returned (default 1000).",
                "minimum": 1,
                "maximum": 10000,
            },
        },
        "required": ["sql"],
    }


# Statement keywords that count as "read" — anything else is rejected.
_READ_KEYWORDS = frozenset({"SELECT", "WITH", "EXPLAIN"})


def _make_tracker_query_executor():
    async def execute(inputs: dict) -> dict[str, Any]:
        from framework.host.tracker_db import (
            _split_statements,
            _leading_keyword,
        )

        db_path = _resolve_tracker_db_path()
        if db_path is None:
            return {
                "success": False,
                "error": "tracker_query: no colony context.",
            }
        sql = inputs.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            return {"success": False, "error": "tracker_query: 'sql' is required."}

        # Reject anything that isn't a pure read. Multi-statement scripts
        # are also rejected — the worker should issue one SELECT per call
        # so write-attempts disguised in a script can't slip through.
        statements = _split_statements(sql)
        if not statements:
            return {"success": False, "error": "tracker_query: no statements"}
        if len(statements) > 1:
            return {
                "success": False,
                "error": (
                    "tracker_query accepts ONE statement per call (got "
                    f"{len(statements)}). Use tracker_sql for scripts."
                ),
            }
        kw = _leading_keyword(statements[0])
        if kw not in _READ_KEYWORDS:
            return {
                "success": False,
                "error": (
                    f"tracker_query is SELECT-only; rejected leading "
                    f"keyword '{kw}'. For writes, use tracker_upsert."
                ),
            }

        # The denylist (ATTACH/PRAGMA/load_extension/etc.) still applies
        # via execute_sql → validate_sql. The leading-keyword check above
        # is stricter than validate_sql (which permits write DML); the
        # denylist sits underneath as belt-and-suspenders.
        row_cap = int(inputs.get("row_cap") or 1000)
        try:
            result = execute_sql(db_path, sql, row_cap=row_cap)
        except DenylistError as e:
            return {"success": False, "error": f"tracker_query denied: {e}"}
        except sqlite3.Error as e:
            return {"success": False, "error": f"tracker_query sqlite error: {e}"}
        return {"success": True, **result}

    return execute


# ---------------------------------------------------------------------------
# tracker_upsert (worker-facing)
# ---------------------------------------------------------------------------


def _make_tracker_upsert_executor():
    async def execute(inputs: dict) -> dict[str, Any]:
        from framework.host.tracker_db import _connect

        db_path = _resolve_tracker_db_path()
        if db_path is None:
            return {
                "success": False,
                "error": "tracker_upsert: no colony context.",
            }

        table = (inputs.get("table") or "").strip()
        if not table:
            return {"success": False, "error": "table is required"}
        if table.startswith(PROTECTED_PREFIX):
            return {
                "success": False,
                "error": (
                    f"refusing to write to framework-owned table '{table}' "
                    f"({PROTECTED_PREFIX}* is reserved)."
                ),
            }

        row = inputs.get("row")
        if not isinstance(row, dict) or not row:
            return {
                "success": False,
                "error": "row must be a non-empty object of column→value",
            }

        con = _connect(db_path)
        try:
            reg = con.execute(
                "SELECT write_columns, key_columns, mode FROM _tracker_registry "
                "WHERE table_name = ?",
                (table,),
            ).fetchone()
            if reg is None:
                return {
                    "success": False,
                    "error": (
                        f"table '{table}' is not registered for worker writes. "
                        "The queen must call tracker_register_writable first."
                    ),
                }
            write_columns_raw, key_columns_raw, mode = reg
            try:
                write_columns = list(json.loads(write_columns_raw))
                key_columns = list(json.loads(key_columns_raw))
            except (json.JSONDecodeError, TypeError):
                return {
                    "success": False,
                    "error": "registry row is corrupt; re-register the table",
                }

            allowed_for_writes = set(write_columns) | set(key_columns)
            unknown = [c for c in row.keys() if c not in allowed_for_writes]
            if unknown:
                return {
                    "success": False,
                    "error": (
                        f"columns not in write/key list: {unknown}. "
                        f"Allowed: {sorted(allowed_for_writes)}"
                    ),
                }

            if mode == "upsert":
                missing_keys = [k for k in key_columns if k not in row]
                if missing_keys:
                    return {
                        "success": False,
                        "error": (
                            f"row is missing key_columns {missing_keys} "
                            "required for upsert"
                        ),
                    }

            # Encode complex values as JSON text so the row is always
            # column-shaped from SQLite's view.
            cols = list(row.keys())
            values = []
            for c in cols:
                v = row[c]
                if isinstance(v, list | dict):
                    values.append(json.dumps(v, ensure_ascii=False))
                elif isinstance(v, bool):
                    values.append(1 if v else 0)
                else:
                    values.append(v)

            quoted_cols = ", ".join(_quote_ident(c) for c in cols)
            placeholders = ", ".join(["?"] * len(cols))
            base_sql = (
                f"INSERT INTO {_quote_ident(table)} ({quoted_cols}) "
                f"VALUES ({placeholders})"
            )

            if mode == "upsert":
                update_cols = [c for c in cols if c not in key_columns]
                if update_cols:
                    set_clause = ", ".join(
                        f"{_quote_ident(c)} = excluded.{_quote_ident(c)}"
                        for c in update_cols
                    )
                    conflict = ", ".join(_quote_ident(k) for k in key_columns)
                    sql = (
                        f"{base_sql} ON CONFLICT ({conflict}) DO UPDATE SET {set_clause}"
                    )
                else:
                    # Row carried only key columns -- nothing to update.
                    conflict = ", ".join(_quote_ident(k) for k in key_columns)
                    sql = f"{base_sql} ON CONFLICT ({conflict}) DO NOTHING"
            else:
                sql = base_sql

            try:
                cur = con.execute(sql, values)
            except sqlite3.Error as e:
                return {
                    "success": False,
                    "error": f"tracker_upsert sqlite error: {e}",
                }

            return {
                "success": True,
                "table": table,
                "mode": mode,
                "rowcount": cur.rowcount,
                "last_insert_rowid": cur.lastrowid,
            }
        finally:
            con.close()

    return execute


# ---------------------------------------------------------------------------
# Public registration
# ---------------------------------------------------------------------------


# Tools that should NEVER appear in worker.json — they're the queen's
# levers (full SQL, registry writes). The fork flow filters worker tool
# inheritance against this set.
QUEEN_ONLY_TRACKER_TOOLS: frozenset[str] = frozenset(
    {"tracker_sql", "tracker_register_writable"}
)


def build_tracker_tools() -> list[tuple[Tool, Any]]:
    """Build (Tool, executor) pairs for the four tracker tools."""
    return [
        (
            Tool(
                name="tracker_sql",
                description=_TRACKER_SQL_DESC,
                parameters=_tracker_sql_schema(),
                concurrency_safe=False,
            ),
            _make_tracker_sql_executor(),
        ),
        (
            Tool(
                name="tracker_register_writable",
                description=_TRACKER_REGISTER_DESC,
                parameters=_tracker_register_schema(),
                concurrency_safe=False,
            ),
            _make_tracker_register_executor(),
        ),
        (
            Tool(
                name="tracker_upsert",
                description=_TRACKER_UPSERT_DESC,
                parameters=_tracker_upsert_schema(),
                concurrency_safe=False,
            ),
            _make_tracker_upsert_executor(),
        ),
        (
            Tool(
                name="tracker_query",
                description=_TRACKER_QUERY_DESC,
                parameters=_tracker_query_schema(),
                concurrency_safe=True,
            ),
            _make_tracker_query_executor(),
        ),
    ]


def _wrap_async_executor(async_executor):
    """Mirror the adapter used by other tool-registration helpers."""

    def executor(inputs: dict) -> Any:
        return async_executor(inputs)

    return executor


def register_tracker_tools(registry: ToolRegistry, *, role: str = "queen") -> None:
    """Register the tracker tools on ``registry``.

    Idempotent: re-registering replaces the previous executor.

    Args:
        registry: The ToolRegistry instance to register on.
        role: Which subset to register.
            - ``"queen"`` (default): all three tools.
            - ``"worker"``: only ``tracker_upsert``. Even though the
              worker.json ``tools`` list filters by name, registering
              the queen-only pair on a worker's registry would still
              let any non-LLM caller invoke them through the executor.
              Restricting registration is defense-in-depth.

    Raises:
        ValueError: ``role`` is not ``"queen"`` or ``"worker"``.
    """
    if role not in ("queen", "worker"):
        raise ValueError(f"role must be 'queen' or 'worker', got {role!r}")

    pairs = build_tracker_tools()
    registered: list[str] = []
    for tool, async_executor in pairs:
        if role == "worker" and tool.name in QUEEN_ONLY_TRACKER_TOOLS:
            continue
        registry.register(tool.name, tool, _wrap_async_executor(async_executor))
        registered.append(tool.name)
    logger.debug(
        "Registered tracker tools on %s (role=%s): %s",
        registry,
        role,
        registered,
    )


__all__ = [
    "QUEEN_ONLY_TRACKER_TOOLS",
    "build_tracker_tools",
    "register_tracker_tools",
]
