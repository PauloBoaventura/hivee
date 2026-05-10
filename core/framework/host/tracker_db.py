"""Per-colony tracker DB: queen-owned domain model with raw-SQL freedom.

Every colony gets its own ``tracker.db`` under
``~/.hive/colonies/{name}/data/``. The queen designs the schema with
full SQL via ``execute_sql`` (denylist enforced). Workers fill rows
through a narrower ``tracker_upsert`` tool, gated by the
``_tracker_registry`` table.

Concurrency:
- WAL mode on day one.
- Workers/queen open a fresh connection per call (``sqlite3`` CLI for
  agents, ``_connect`` for framework code).
- ``BEGIN IMMEDIATE`` for any multi-statement script that mutates state
  (the queen wraps her own transactions when she wants atomicity).

The denylist is intentionally small. The queen has been explicitly given
"raw SQL DDL" freedom; we only block what would breach the security
perimeter or break the framework:
  - ATTACH / DETACH (escape from tracker.db)
  - PRAGMA (operational; introspection works via sqlite_master)
  - VACUUM / REINDEX (operational, not domain)
  - load_extension(...) (security)
  - DDL/DML on ``_*`` tables (framework-owned namespace)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# Bootstrap schema. Only framework-owned tables; everything else is the
# queen's. ``_tracker_registry`` is what gates ``tracker_upsert`` for
# workers — without a row here, a table is invisible to workers.
_BOOTSTRAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS _tracker_registry (
    table_name      TEXT PRIMARY KEY,
    write_columns   TEXT NOT NULL,    -- JSON array of column names
    key_columns     TEXT NOT NULL,    -- JSON array; empty = append mode
    mode            TEXT NOT NULL CHECK (mode IN ('upsert', 'append')),
    registered_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS _tracker_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS _tasks (
    id              TEXT PRIMARY KEY,
    seq             INTEGER,
    priority        INTEGER NOT NULL DEFAULT 0,
    goal            TEXT NOT NULL,
    payload         TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    worker_id       TEXT,
    claim_token     TEXT,
    claimed_at      TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL DEFAULT 3,
    last_error      TEXT,
    parent_task_id  TEXT REFERENCES _tasks(id) ON DELETE SET NULL,
    source          TEXT
);

CREATE TABLE IF NOT EXISTS _steps (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES _tasks(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    title           TEXT NOT NULL,
    detail          TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    evidence        TEXT,
    worker_id       TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    UNIQUE (task_id, seq)
);

CREATE TABLE IF NOT EXISTS _sop_checklist (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES _tasks(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,
    description     TEXT NOT NULL,
    required        INTEGER NOT NULL DEFAULT 1,
    done_at         TEXT,
    done_by         TEXT,
    note            TEXT,
    UNIQUE (task_id, key)
);

CREATE INDEX IF NOT EXISTS idx_tracker_tasks_claimable
    ON _tasks(status, priority DESC, seq, created_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_tracker_steps_task_seq
    ON _steps(task_id, seq);

CREATE INDEX IF NOT EXISTS idx_tracker_sop_required_open
    ON _sop_checklist(task_id, required, done_at);

CREATE INDEX IF NOT EXISTS idx_tracker_tasks_status
    ON _tasks(status, updated_at);
"""

_PRAGMAS = (
    "PRAGMA journal_mode = WAL;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA foreign_keys = ON;",
    "PRAGMA busy_timeout = 5000;",
)

# Tables in this namespace are framework-owned. tracker_sql refuses DDL
# and write DML against them; SELECT is allowed so the queen can
# introspect the registry.
PROTECTED_PREFIX = "_"

# Cap on statements per execute_sql call. Bounds blast radius of a
# single tool call without being so low it forces awkward batching.
MAX_STATEMENTS_PER_CALL = 20

# Statement keywords rejected outright regardless of arguments.
_DENIED_LEADING_KEYWORDS = frozenset(
    {"ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX"}
)

# Function calls forbidden anywhere in the SQL (after string/comment
# stripping). load_extension is the canonical SQLite footgun: it can
# load arbitrary shared libraries.
_DENIED_FUNCTIONS = ("load_extension",)

# Keywords that mean "this statement may write/structure-change". For
# these we additionally check the target table against PROTECTED_PREFIX.
_MUTATING_KEYWORDS = frozenset(
    {"CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "REPLACE", "TRUNCATE"}
)


class DenylistError(ValueError):
    """Raised when SQL is rejected by the denylist before execution."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with the standard pragmas applied.

    WAL mode is sticky on the file once set; the others are per-connection.
    ``isolation_level=None`` puts us in autocommit mode so the caller
    controls transactions explicitly with ``BEGIN``/``COMMIT``.
    """
    con = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0)
    for pragma in _PRAGMAS:
        con.execute(pragma)
    return con


def ensure_tracker_db(colony_dir: Path) -> Path:
    """Create or migrate ``{colony_dir}/data/tracker.db``.

    Idempotent: safe to call on an already-initialized DB. Returns the
    absolute path to the DB file.
    """
    data_dir = Path(colony_dir) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "tracker.db"

    con = _connect(db_path)
    try:
        current_version = con.execute("PRAGMA user_version").fetchone()[0]
        if current_version < SCHEMA_VERSION:
            con.executescript(_BOOTSTRAP_SCHEMA)
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            con.execute(
                "INSERT OR REPLACE INTO _tracker_meta(key, value, updated_at) VALUES (?, ?, ?)",
                ("schema_version", str(SCHEMA_VERSION), _now_iso()),
            )
            logger.info(
                "tracker_db: initialized schema v%d at %s", SCHEMA_VERSION, db_path
            )
    finally:
        con.close()

    resolved = db_path.resolve()
    _patch_worker_configs(Path(colony_dir), resolved)
    return resolved


def _patch_worker_configs(colony_dir: Path, tracker_db_path: Path) -> int:
    """Inject ``input_data.tracker_db_path`` into existing ``worker.json`` files.

    Runs on every ``ensure_tracker_db`` call so worker configs always
    point at TrackerDB only. Legacy ProgressDB fields are removed so stale
    ``db_path`` values cannot activate outdated worker protocols.

    Returns the number of files modified.
    """
    abs_path = str(tracker_db_path)
    patched = 0

    for worker_cfg in colony_dir.glob("*.json"):
        # Skip colony-level files.
        if worker_cfg.name in ("metadata.json", "triggers.json"):
            continue
        try:
            data = json.loads(worker_cfg.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict) or "system_prompt" not in data:
            continue

        input_data = data.get("input_data")
        if not isinstance(input_data, dict):
            input_data = {}

        changed = False
        if input_data.get("tracker_db_path") != abs_path:
            input_data["tracker_db_path"] = abs_path
            changed = True
        for legacy_key in ("db_path", "colony_data_dir"):
            if legacy_key in input_data:
                input_data.pop(legacy_key, None)
                changed = True
        if not changed:
            continue  # already patched

        data["input_data"] = input_data

        try:
            worker_cfg.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            patched += 1
        except OSError as e:
            logger.warning(
                "tracker_db: failed to patch worker config %s: %s", worker_cfg, e
            )

    if patched:
        logger.info(
            "tracker_db: patched %d worker config(s) in colony '%s' with tracker_db_path",
            patched,
            colony_dir.name,
        )
    return patched


def ensure_all_colony_tracker_dbs(colonies_root: Path | None = None) -> list[Path]:
    """Idempotently ensure every existing colony has a tracker.db.

    Called on framework host startup to backfill colonies that were
    forked before TrackerDB landed.
    """
    if colonies_root is None:
        from framework.config import COLONIES_DIR

        colonies_root = COLONIES_DIR
    if not colonies_root.is_dir():
        return []

    initialized: list[Path] = []
    for entry in sorted(colonies_root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            initialized.append(ensure_tracker_db(entry))
        except Exception as e:
            logger.warning(
                "tracker_db: failed to ensure DB for colony '%s': %s", entry.name, e
            )
    return initialized


def enqueue_framework_task(
    db_path: Path,
    goal: str,
    *,
    payload: Any = None,
    priority: int = 0,
    parent_task_id: str | None = None,
    source: str | None = None,
) -> str:
    """Append a framework task row to tracker.db protected tables.

    This replaces the old ProgressDB queue write path. It is framework
    code, not a queen/worker SQL surface, so it may mutate ``_*`` tables.
    """
    task_id = str(uuid.uuid4())
    now = _now_iso()
    payload_text = None if payload is None else json.dumps(payload, ensure_ascii=False)
    con = _connect(Path(db_path))
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM _tasks").fetchone()
        seq = int(row[0] or 1)
        con.execute(
            """
            INSERT INTO _tasks (
                id, seq, priority, goal, payload, status, created_at,
                updated_at, parent_task_id, source
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                task_id,
                seq,
                int(priority),
                goal,
                payload_text,
                now,
                now,
                parent_task_id,
                source,
            ),
        )
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()
    return task_id


# ---------------------------------------------------------------------------
# Denylist parser
# ---------------------------------------------------------------------------


def _strip_strings_and_comments(sql: str) -> str:
    """Return SQL with string literals and comments replaced by spaces.

    Used for keyword scans where we must not match keywords that appear
    inside string contents (``'... ATTACH ...'``) or comments.

    Handles:
      - line comments: ``-- ... \\n``
      - block comments: ``/* ... */`` (non-nesting, like SQLite)
      - single-quoted strings with ``''`` escape: ``'O''Brien'``

    Double-quoted ``"col"`` and bracket-quoted ``[col]`` identifiers are
    left intact so that the table-name extractor can still see them.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            if j == -1:
                break
            i = j + 1
            continue
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                # Unterminated comment; bail. SQLite would reject this too.
                break
            i = j + 2
            continue
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _has_executable_content(stmt: str) -> bool:
    """True if the statement contains anything beyond comments/whitespace."""
    return bool(_strip_strings_and_comments(stmt).strip())


def _split_statements(sql: str) -> list[str]:
    """Split SQL on top-level ``;`` (outside strings and comments).

    Returns the *original* statement text (strings and comments preserved)
    so that the executor sees what the caller wrote. Comment-only chunks
    are skipped — they're not executable statements. Only the splitting
    logic looks past quotes/comments; the returned slices are verbatim.
    """
    statements: list[str] = []
    current: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        # Line comment: keep verbatim, copy up to and including newline.
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            if j == -1:
                current.append(sql[i:])
                i = n
                continue
            current.append(sql[i : j + 1])
            i = j + 1
            continue
        # Block comment: keep verbatim through ``*/``.
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                current.append(sql[i:])
                i = n
                continue
            current.append(sql[i : j + 2])
            i = j + 2
            continue
        # Single-quoted string with '' escape: copy verbatim.
        if c == "'":
            current.append("'")
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        current.append("''")
                        j += 2
                        continue
                    current.append("'")
                    j += 1
                    break
                current.append(sql[j])
                j += 1
            i = j
            continue
        if c == ";":
            stmt = "".join(current).strip()
            if stmt and _has_executable_content(stmt):
                statements.append(stmt)
            current = []
            i += 1
            continue
        current.append(c)
        i += 1
    tail = "".join(current).strip()
    if tail and _has_executable_content(tail):
        statements.append(tail)
    return statements


def _leading_keyword(stmt: str) -> str:
    """First whitespace-delimited token of a statement, uppercased."""
    for token in stmt.split():
        return token.upper()
    return ""


def _strip_identifier(name: str) -> str:
    """Strip common identifier quoting/punctuation so we can compare names."""
    # Trailing punctuation from tokenization (commas, parens, semicolons)
    # plus identifier quote characters: backtick, double-quote, square brackets.
    return name.strip("`\"[](),;")


def _referenced_tables_for_mutation(stmt: str) -> list[str]:
    """Best-effort extraction of table names targeted by a mutating stmt.

    Used to enforce ``PROTECTED_PREFIX``. Conservative: when we can't tell,
    we return nothing and let the SQL through. The protection here is
    defense-in-depth — workers' ``tracker_upsert`` independently refuses
    ``_*`` tables, and the queen has no legitimate reason to drop the
    registry (she has a tracker_register_writable helper for it).
    """
    tokens = stmt.split()
    if not tokens:
        return []
    head = tokens[0].upper()
    rest = tokens[1:]
    rest_upper = [t.upper() for t in rest]

    if head in ("INSERT", "REPLACE"):
        # INSERT [OR REPLACE|IGNORE|...] INTO <name>
        for i, t in enumerate(rest_upper):
            if t == "INTO" and i + 1 < len(rest):
                return [_strip_identifier(rest[i + 1])]
        return []

    if head == "UPDATE":
        # UPDATE [OR REPLACE|IGNORE|...] <name> SET ...
        i = 0
        if rest_upper[:1] == ["OR"] and len(rest_upper) >= 2:
            i = 2
        if i < len(rest):
            return [_strip_identifier(rest[i])]
        return []

    if head == "DELETE":
        # DELETE FROM <name>
        for i, t in enumerate(rest_upper):
            if t == "FROM" and i + 1 < len(rest):
                return [_strip_identifier(rest[i + 1])]
        return []

    if head == "TRUNCATE":
        # SQLite doesn't have TRUNCATE but reject for safety; treat
        # ``TRUNCATE [TABLE] <name>`` just in case.
        i = 0
        if rest_upper[:1] == ["TABLE"]:
            i = 1
        if i < len(rest):
            return [_strip_identifier(rest[i])]
        return []

    if head in ("CREATE", "DROP", "ALTER"):
        # Walk past optional modifiers: TEMP/TEMPORARY/UNIQUE/VIRTUAL.
        i = 0
        while i < len(rest_upper) and rest_upper[i] in (
            "TEMP",
            "TEMPORARY",
            "UNIQUE",
            "VIRTUAL",
        ):
            i += 1
        if i >= len(rest_upper):
            return []
        kind = rest_upper[i]
        if kind not in ("TABLE", "INDEX", "VIEW", "TRIGGER"):
            return []
        i += 1
        # Skip IF [NOT] EXISTS
        if rest_upper[i : i + 3] == ["IF", "NOT", "EXISTS"]:
            i += 3
        elif rest_upper[i : i + 2] == ["IF", "EXISTS"]:
            i += 2
        names: list[str] = []
        if i < len(rest):
            names.append(_strip_identifier(rest[i]))
        # CREATE INDEX <idx> ON <table>: protect the target table too.
        if kind == "INDEX":
            for j in range(i + 1, len(rest_upper)):
                if rest_upper[j] == "ON" and j + 1 < len(rest):
                    names.append(_strip_identifier(rest[j + 1]))
                    break
        # CREATE TRIGGER <trg> ... ON <table>: protect target table.
        if kind == "TRIGGER":
            for j in range(i + 1, len(rest_upper)):
                if rest_upper[j] == "ON" and j + 1 < len(rest):
                    names.append(_strip_identifier(rest[j + 1]))
                    break
        return [n for n in names if n]

    return []


def validate_sql(sql: str) -> None:
    """Raise :class:`DenylistError` if ``sql`` violates tracker policy.

    Policy is documented at the top of this module. Raises before any
    statement is executed; the SQL is either fully accepted or fully
    rejected (no partial execution).
    """
    if not sql or not sql.strip():
        raise DenylistError("empty SQL")

    statements = _split_statements(sql)
    if not statements:
        raise DenylistError("no executable statements")
    if len(statements) > MAX_STATEMENTS_PER_CALL:
        raise DenylistError(
            f"too many statements: {len(statements)} > {MAX_STATEMENTS_PER_CALL}"
        )

    # Function-level denylist scan, on a string/comment-stripped lowercase
    # view so keywords inside literals don't false-match.
    cleaned = _strip_strings_and_comments(sql).lower()
    for fn in _DENIED_FUNCTIONS:
        idx = 0
        while True:
            pos = cleaned.find(fn, idx)
            if pos == -1:
                break
            # Must be a function call (followed by `(`) and a word-boundary
            # before (no preceding identifier char).
            before_ok = pos == 0 or not (cleaned[pos - 1].isalnum() or cleaned[pos - 1] == "_")
            after = cleaned[pos + len(fn) :].lstrip()
            if before_ok and after.startswith("("):
                raise DenylistError(f"forbidden function: {fn}()")
            idx = pos + len(fn)

    # Per-statement leading keyword + protected-table check.
    for stmt in statements:
        kw = _leading_keyword(stmt)
        if kw in _DENIED_LEADING_KEYWORDS:
            raise DenylistError(f"forbidden statement: {kw}")
        if kw in _MUTATING_KEYWORDS:
            for tbl in _referenced_tables_for_mutation(stmt):
                if tbl.startswith(PROTECTED_PREFIX):
                    raise DenylistError(
                        f"forbidden mutation on framework table: {tbl}"
                    )


def execute_sql(
    db_path: Path,
    sql: str,
    *,
    row_cap: int = 1000,
) -> dict[str, Any]:
    """Validate and execute SQL on ``tracker.db``.

    Single-statement returns:
      - read (cursor.description is not None):
          ``{"kind": "rows", "columns": [...], "rows": [[...], ...],
             "rowcount": int, "truncated": bool}``
      - write/DDL:
          ``{"kind": "exec", "rowcount": int, "last_insert_rowid": int}``

    Multi-statement returns:
      ``{"kind": "script", "results": [<single-stmt result>, ...]}``

    Rows are returned as plain lists (JSON-serialisable). Anything past
    ``row_cap`` is dropped and ``truncated`` is set to True; the caller
    can paginate with LIMIT/OFFSET.
    """
    validate_sql(sql)
    statements = _split_statements(sql)

    con = _connect(Path(db_path))
    try:
        results: list[dict[str, Any]] = []
        for stmt in statements:
            cur = con.execute(stmt)
            if cur.description is not None:
                cols = [c[0] for c in cur.description]
                rows: list[list[Any]] = []
                truncated = False
                fetched = 0
                for row in cur:
                    if fetched >= row_cap:
                        truncated = True
                        break
                    rows.append(list(row))
                    fetched += 1
                results.append(
                    {
                        "kind": "rows",
                        "columns": cols,
                        "rows": rows,
                        "rowcount": fetched,
                        "truncated": truncated,
                    }
                )
            else:
                results.append(
                    {
                        "kind": "exec",
                        "rowcount": cur.rowcount,
                        "last_insert_rowid": cur.lastrowid,
                    }
                )
        if len(results) == 1:
            return results[0]
        return {"kind": "script", "results": results}
    finally:
        con.close()


__all__ = [
    "SCHEMA_VERSION",
    "DenylistError",
    "MAX_STATEMENTS_PER_CALL",
    "PROTECTED_PREFIX",
    "ensure_all_colony_tracker_dbs",
    "ensure_tracker_db",
    "enqueue_framework_task",
    "execute_sql",
    "validate_sql",
]
