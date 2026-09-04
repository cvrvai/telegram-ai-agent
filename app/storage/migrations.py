"""Versioned SQLite migrations for the work-management foundation.

The first migration is a recorded baseline for databases created by the
original schema bootstrap. Later migrations only add columns/indexes and
backfill derived values; they never drop or rewrite historical records.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Awaitable, Callable

import aiosqlite


Migration = Callable[[aiosqlite.Connection], Awaitable[None]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {str(row[1]) for row in await cursor.fetchall()}


async def _add_column(db: aiosqlite.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in await _columns(db, table):
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


async def migration_0001_baseline(db: aiosqlite.Connection) -> None:
    """Record the pre-migration schema as the compatibility baseline."""


async def migration_0002_work_item_domain(db: aiosqlite.Connection) -> None:
    # Legacy messages retain their original priority while gaining an
    # independent type and confidence field. Unknown is intentional for old
    # rows: historical P2/P3 values do not prove a message type.
    table_check = await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'")
    has_messages = bool(await table_check.fetchone())
    if has_messages:
        await _add_column(db, "messages", "message_type TEXT NOT NULL DEFAULT 'unknown'")
        await _add_column(db, "messages", "ai_confidence REAL NOT NULL DEFAULT 0")

    # Evolve assistant_tasks into the WorkItem shape without creating a second
    # task table. Existing details remains the description compatibility field.
    for definition in (
        "parent_id INTEGER",
        "sequence_number INTEGER",
        "display_key TEXT",
        "type TEXT NOT NULL DEFAULT 'task'",
        "reporter_id INTEGER",
        "remind_at TEXT",
        "rank INTEGER NOT NULL DEFAULT 0",
        "source_chat_id INTEGER",
        "source_message_id INTEGER",
        "source_message_url TEXT",
        "ai_confidence REAL NOT NULL DEFAULT 0",
        "updated_at TEXT",
    ):
        await _add_column(db, "assistant_tasks", definition)
    await db.execute("UPDATE assistant_tasks SET updated_at=COALESCE(updated_at, created_at)")
    await db.execute("UPDATE assistant_tasks SET type=COALESCE(NULLIF(type, ''), 'task')")
    await db.execute("UPDATE assistant_tasks SET rank=COALESCE(rank, id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_assistant_tasks_parent ON assistant_tasks(parent_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_assistant_tasks_project_status ON assistant_tasks(project_id, status, rank, id)")
    await db.execute("""CREATE TABLE IF NOT EXISTS work_item_status_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        work_item_id INTEGER NOT NULL,
        from_status TEXT,
        to_status TEXT NOT NULL,
        changed_by INTEGER,
        created_at TEXT NOT NULL
    )""")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_work_item_history_item ON work_item_status_history(work_item_id, created_at)")


def _project_key(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "", (name or "").upper())
    return (value[:10] or "PROJ")


async def migration_0003_project_keys(db: aiosqlite.Connection) -> None:
    await _add_column(db, "assistant_projects", "project_key TEXT")
    await _add_column(db, "assistant_projects", "next_sequence INTEGER NOT NULL DEFAULT 1")

    cursor = await db.execute("SELECT id,name,project_key FROM assistant_projects ORDER BY id")
    rows = await cursor.fetchall()
    used: set[str] = set()
    for project_id, name, existing_key in rows:
        key = re.sub(r"[^A-Za-z0-9]+", "", str(existing_key or "").upper()) or _project_key(str(name or ""))
        base = key[:10] or "PROJ"
        key = base
        suffix = 2
        while key in used:
            suffix_text = str(suffix)
            key = f"{base[: max(1, 10 - len(suffix_text))]}{suffix_text}"
            suffix += 1
        used.add(key)
        await db.execute("UPDATE assistant_projects SET project_key=? WHERE id=?", (key, project_id))

        task_cursor = await db.execute(
            "SELECT id,sequence_number FROM assistant_tasks WHERE project_id=? ORDER BY id",
            (project_id,),
        )
        task_rows = await task_cursor.fetchall()
        next_sequence = 1
        for task_id, sequence_number in task_rows:
            sequence = int(sequence_number or 0) or next_sequence
            next_sequence = max(next_sequence, sequence + 1)
            await db.execute(
                "UPDATE assistant_tasks SET sequence_number=?,display_key=? WHERE id=?",
                (sequence, f"{key}-{sequence}", task_id),
            )
        await db.execute("UPDATE assistant_projects SET next_sequence=? WHERE id=?", (next_sequence, project_id))
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_assistant_projects_key ON assistant_projects(project_key)")
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_assistant_tasks_display_key ON assistant_tasks(display_key) WHERE display_key IS NOT NULL")


async def migration_0004_status_history(db: aiosqlite.Connection) -> None:
    """Add transition history for databases that already applied 0002."""
    await db.execute("""CREATE TABLE IF NOT EXISTS work_item_status_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        work_item_id INTEGER NOT NULL,
        from_status TEXT,
        to_status TEXT NOT NULL,
        changed_by INTEGER,
        created_at TEXT NOT NULL
    )""")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_work_item_history_item ON work_item_status_history(work_item_id, created_at)")


async def migration_0005_agent_audit(db: aiosqlite.Connection) -> None:
    await db.execute("""CREATE TABLE IF NOT EXISTS agent_audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_id INTEGER NOT NULL,
        tool_name TEXT NOT NULL,
        arguments TEXT NOT NULL,
        target TEXT,
        policy_result TEXT NOT NULL,
        execution_result TEXT,
        created_at TEXT NOT NULL
    )""")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_agent_audit_actor_time ON agent_audit_events(actor_id, created_at)")


MIGRATIONS: tuple[tuple[int, str, Migration], ...] = (
    (1, "existing baseline", migration_0001_baseline),
    (2, "work item domain", migration_0002_work_item_domain),
    (3, "project keys", migration_0003_project_keys),
    (4, "status history", migration_0004_status_history),
    (5, "agent audit", migration_0005_agent_audit),
)


async def run_migrations(db: aiosqlite.Connection) -> int:
    """Apply pending migrations transactionally and return the current version."""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    cursor = await db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
    current = int((await cursor.fetchone())[0])
    for version, name, migration in MIGRATIONS:
        if version <= current:
            continue
        await db.execute("BEGIN")
        try:
            await migration(db)
            await db.execute(
                "INSERT INTO schema_migrations(version,name,applied_at) VALUES (?,?,?)",
                (version, name, _now()),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        current = version
    return current
