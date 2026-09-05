"""SQLite persistence kept separate from the legacy message pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiosqlite
import secrets
from app.storage.migrations import run_migrations
from app.projects.workflow import normalize_status, validate_priority, validate_work_item_type, can_transition
import re


SCHEMA = """
CREATE TABLE IF NOT EXISTS assistant_users (
    user_id INTEGER PRIMARY KEY,
    display_name TEXT,
    role TEXT NOT NULL DEFAULT 'user',
    approved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_profiles (
    assistant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_memberships (
    assistant_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (assistant_id, user_id),
    FOREIGN KEY (assistant_id) REFERENCES assistant_profiles(assistant_id),
    FOREIGN KEY (user_id) REFERENCES assistant_users(user_id)
);
CREATE TABLE IF NOT EXISTS assistant_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assistant_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assistant_conversations_scope
    ON assistant_conversations(assistant_id, user_id, chat_id, updated_at);
CREATE TABLE IF NOT EXISTS assistant_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES assistant_conversations(id)
);
CREATE TABLE IF NOT EXISTS assistant_pairings (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    display_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS assistant_groups (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    enabled INTEGER NOT NULL DEFAULT 0,
    mention_only INTEGER NOT NULL DEFAULT 1,
    trigger_pattern TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_focus (
    assistant_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    chat_title TEXT NOT NULL DEFAULT '',
    chat_type TEXT NOT NULL DEFAULT 'private',
    mode TEXT NOT NULL DEFAULT 'monitor',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (assistant_id, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_assistant_focus_enabled ON assistant_focus(assistant_id, enabled);
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,
    assistant_id TEXT NOT NULL,
    user_id INTEGER,
    feature TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_period ON ai_usage(period);
CREATE TABLE IF NOT EXISTS assistant_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    project_id INTEGER,
    assignee_id INTEGER,
    title TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'to_do',
    priority TEXT NOT NULL DEFAULT 'normal',
    duration_days INTEGER NOT NULL DEFAULT 1,
    due_at TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_assistant_tasks_user_status ON assistant_tasks(user_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_assistant_tasks_project ON assistant_tasks(project_id, status, created_at);
CREATE TABLE IF NOT EXISTS assistant_departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    department_id INTEGER,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    start_date TEXT,
    target_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assistant_projects_owner ON assistant_projects(user_id, status, updated_at);
CREATE TABLE IF NOT EXISTS assistant_project_members (
    project_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    PRIMARY KEY(project_id, user_id)
);
CREATE TABLE IF NOT EXISTS assistant_task_dependencies (
    task_id INTEGER NOT NULL,
    predecessor_id INTEGER NOT NULL,
    PRIMARY KEY(task_id, predecessor_id)
);
CREATE TABLE IF NOT EXISTS assistant_milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    target_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    comment TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    remind_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_assistant_reminders_due ON assistant_reminders(status, remind_at);
CREATE TABLE IF NOT EXISTS assistant_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    target TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_assistant_actions_status ON assistant_actions(status, expires_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_business_schema(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        # Keep databases created by the earlier schema usable.
        try:
            await db.execute("ALTER TABLE assistant_pairings ADD COLUMN expires_at TEXT")
        except aiosqlite.OperationalError:
            pass
        for statement in (
            "ALTER TABLE assistant_tasks ADD COLUMN project_id INTEGER",
            "ALTER TABLE assistant_tasks ADD COLUMN assignee_id INTEGER",
            "ALTER TABLE assistant_tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'",
            "ALTER TABLE assistant_tasks ADD COLUMN duration_days INTEGER NOT NULL DEFAULT 1",
        ):
            try:
                await db.execute(statement)
            except aiosqlite.OperationalError:
                pass
        await db.execute("UPDATE assistant_tasks SET status='to_do' WHERE status='open'")
        now = _now()
        await db.execute(
            "INSERT OR IGNORE INTO assistant_profiles(assistant_id, name, instructions, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("business", "Business Assistant", "Answer from authorized business context and say when information is missing.", now, now),
        )
        await db.commit()
        # New schema evolution is versioned. The compatibility ALTERs above
        # are retained only for databases created before migrations existed.
        await run_migrations(db)


class BusinessRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self) -> None:
        await init_business_schema(self.db_path)

    async def upsert_user(self, user_id: int, display_name: str = "", role: str = "user", approved: bool = False) -> None:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO assistant_users(user_id,display_name,created_at,updated_at) VALUES (?,?,?,?)",
                (user_id, display_name, now, now),
            )
            await db.execute(
                """INSERT INTO assistant_users(user_id, display_name, role, approved, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name,
                   role=excluded.role, approved=excluded.approved, updated_at=excluded.updated_at""",
                (user_id, display_name, role, int(approved), now, now),
            )
            await db.commit()

    async def mark_user_seen(self, user_id: int) -> None:
        """Record an already-authorized caller without changing its role."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO assistant_users(user_id, created_at, updated_at) VALUES (?, ?, ?)",
                (user_id, now, now),
            )
            await db.execute("UPDATE assistant_users SET approved=1, updated_at=? WHERE user_id=?", (now, user_id))
            await db.commit()

    async def create_pairing(self, user_id: int, display_name: str = "") -> str:
        """Create a time-limited administrator-review request."""
        code = secrets.token_hex(3).upper()
        now = _now()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO assistant_pairings(code,user_id,display_name,status,created_at,expires_at) VALUES (?,?,?,?,?,?)",
                (code, user_id, display_name, "pending", now, expires_at),
            )
            await db.commit()
        return code

    async def resolve_pairing(self, code: str, approved: bool) -> Optional[int]:
        """Resolve a pending request and return its user id, if it exists."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT user_id, expires_at FROM assistant_pairings WHERE code=? AND status='pending'", (code.upper(),))
            row = await cursor.fetchone()
            if not row:
                return None
            if row[1] and row[1] < now:
                await db.execute("UPDATE assistant_pairings SET status='expired', resolved_at=? WHERE code=?", (now, code.upper()))
                await db.commit()
                return None
            user_id = int(row[0])
            await db.execute(
                "UPDATE assistant_pairings SET status=?, resolved_at=? WHERE code=?",
                ("approved" if approved else "denied", now, code.upper()),
            )
            await db.execute("UPDATE assistant_users SET approved=?, updated_at=? WHERE user_id=?", (int(approved), now, user_id))
            await db.commit()
        return user_id

    async def pending_pairings(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT code,user_id,display_name,created_at FROM assistant_pairings WHERE status='pending' ORDER BY created_at")
            return [dict(row) for row in await cursor.fetchall()]

    async def create_or_get_conversation(self, assistant_id: str, user_id: int, chat_id: int, title: str = "") -> int:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT id FROM assistant_conversations WHERE assistant_id=? AND user_id=? AND chat_id=? ORDER BY id DESC LIMIT 1",
                (assistant_id, user_id, chat_id),
            )
            row = await cursor.fetchone()
            if row:
                await db.execute("UPDATE assistant_conversations SET updated_at=? WHERE id=?", (now, row[0]))
                await db.commit()
                return int(row[0])
            cursor = await db.execute(
                "INSERT INTO assistant_conversations(assistant_id,user_id,chat_id,title,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (assistant_id, user_id, chat_id, title, now, now),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def add_turn(self, conversation_id: int, role: str, content: str, model: str = "", input_tokens: int = 0, output_tokens: int = 0) -> None:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO assistant_turns(conversation_id,role,content,model,input_tokens,output_tokens,created_at) VALUES (?,?,?,?,?,?,?)",
                (conversation_id, role, content, model, input_tokens, output_tokens, now),
            )
            await db.execute("UPDATE assistant_conversations SET updated_at=? WHERE id=?", (now, conversation_id))
            await db.commit()

    async def recent_turns(self, conversation_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT role, content, model, input_tokens, output_tokens, created_at FROM assistant_turns WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                (conversation_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in reversed(rows)]

    async def search_turns(self, user_id: int, assistant_id: str, query: str, limit: int = 20, chat_id: int | None = None) -> List[Dict[str, Any]]:
        """Search saved assistant turns within the caller's own conversations."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT t.role, t.content, t.model, t.created_at, c.chat_id, c.title
                   FROM assistant_turns t
                   JOIN assistant_conversations c ON c.id = t.conversation_id
                   WHERE c.user_id=? AND c.assistant_id=? AND t.content LIKE ? AND (? IS NULL OR c.chat_id=?)
                   ORDER BY t.id DESC LIMIT ?""",
                (user_id, assistant_id, f"%{query}%", chat_id, chat_id, limit),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def clear_conversation(self, user_id: int, chat_id: int, assistant_id: str = "business") -> int:
        """Delete one assistant conversation and its turns."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT id FROM assistant_conversations WHERE assistant_id=? AND user_id=? AND chat_id=?",
                (assistant_id, user_id, chat_id),
            )
            ids = [int(row[0]) for row in await cursor.fetchall()]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                await db.execute(f"DELETE FROM assistant_turns WHERE conversation_id IN ({placeholders})", ids)
                await db.execute(f"DELETE FROM assistant_conversations WHERE id IN ({placeholders})", ids)
            await db.commit()
            return len(ids)

    async def clear_user_memory(self, user_id: int, assistant_id: Optional[str] = None) -> int:
        """Delete all saved assistant conversations for one user."""
        async with aiosqlite.connect(self.db_path) as db:
            if assistant_id:
                cursor = await db.execute(
                    "SELECT id FROM assistant_conversations WHERE assistant_id=? AND user_id=?",
                    (assistant_id, user_id),
                )
            else:
                cursor = await db.execute("SELECT id FROM assistant_conversations WHERE user_id=?", (user_id,))
            ids = [int(row[0]) for row in await cursor.fetchall()]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                await db.execute(f"DELETE FROM assistant_turns WHERE conversation_id IN ({placeholders})", ids)
                await db.execute(f"DELETE FROM assistant_conversations WHERE id IN ({placeholders})", ids)
            await db.commit()
            return len(ids)

    async def purge_turns_older_than(self, cutoff_iso: str) -> int:
        """Delete old turns and empty conversations for retention enforcement."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM assistant_turns WHERE created_at < ?", (cutoff_iso,))
            deleted = cursor.rowcount if cursor.rowcount is not None else 0
            await db.execute(
                "DELETE FROM assistant_conversations WHERE id NOT IN (SELECT DISTINCT conversation_id FROM assistant_turns)"
            )
            await db.commit()
            return deleted

    @staticmethod
    def _project_key(name: str) -> str:
        return (re.sub(r"[^A-Za-z0-9]+", "", (name or "").upper())[:10] or "PROJ")

    async def create_task(self, user_id: int, title: str, details: str = "", due_at: Optional[str] = None, project_id: Optional[int] = None, assignee_id: Optional[int] = None, priority: str = "P2", duration_days: int = 1, item_type: str = "task", parent_id: Optional[int] = None, reporter_id: Optional[int] = None, source_chat_id: Optional[int] = None, source_message_id: Optional[int] = None, source_message_url: Optional[str] = None, ai_confidence: float = 0.0) -> int:
        priority = validate_priority(priority)
        item_type = validate_work_item_type(item_type)
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            display_key = None
            sequence = None
            if project_id is not None:
                project = await (await db.execute("SELECT project_key,next_sequence FROM assistant_projects WHERE id=? AND user_id=?", (project_id, user_id))).fetchone()
                if not project:
                    raise ValueError("Project not found or not owned by this user")
                if parent_id is not None:
                    parent = await (await db.execute("SELECT project_id FROM assistant_tasks WHERE id=?", (parent_id,))).fetchone()
                    if not parent or parent[0] != project_id:
                        raise ValueError("Parent work item must belong to the same project")
                key = project[0] or self._project_key(title)
                sequence = int(project[1] or 1)
                display_key = f"{key}-{sequence}"
                await db.execute("UPDATE assistant_projects SET next_sequence=?,updated_at=? WHERE id=?", (sequence + 1, now, project_id))
            cursor = await db.execute(
                """INSERT INTO assistant_tasks(user_id,project_id,assignee_id,title,details,status,priority,duration_days,due_at,created_at,parent_id,sequence_number,display_key,type,reporter_id,remind_at,rank,source_chat_id,source_message_id,source_message_url,ai_confidence,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, project_id, assignee_id, title, details, "todo", priority, max(1, duration_days), due_at, now, parent_id, sequence, display_key, item_type, reporter_id or user_id, due_at, sequence or 0, source_chat_id, source_message_id, source_message_url, max(0.0, min(1.0, float(ai_confidence))), now),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def list_tasks(self, user_id: int, status: str = "open", limit: int = 20, project_id: Optional[int] = None, assignee_id: Optional[int] = None, due_before: Optional[str] = None, offset: int = 0) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            clauses = ["(user_id=? OR assignee_id=?)"]
            params: list[Any] = [user_id, user_id]
            if status not in {"open", "all"}:
                clauses.append("status=?")
                params.append(normalize_status(status))
            elif status == "open":
                clauses.append("status IN ('todo','to_do','in_progress','blocked','waiting','review')")
            if project_id is not None:
                clauses.append("project_id=?")
                params.append(project_id)
            if assignee_id is not None:
                clauses.append("assignee_id=?")
                params.append(assignee_id)
            if due_before is not None:
                clauses.append("due_at IS NOT NULL AND due_at<=?")
                params.append(due_before)
            params.extend([limit, max(0, offset)])
            cursor = await db.execute(
                "SELECT id,user_id,project_id,assignee_id,title,details,status,priority,duration_days,due_at,created_at,completed_at,parent_id,sequence_number,display_key,type,reporter_id,remind_at,rank,source_chat_id,source_message_id,source_message_url,ai_confidence,updated_at FROM assistant_tasks WHERE " + " AND ".join(clauses) + " ORDER BY rank DESC,id DESC LIMIT ? OFFSET ?",
                params,
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def complete_task(self, user_id: int, task_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE assistant_tasks SET status='done', completed_at=?,updated_at=? WHERE id=? AND (user_id=? OR assignee_id=?) AND status IN ('open','to_do','todo','in_progress','blocked','waiting','review')",
                (_now(), _now(), task_id, user_id, user_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def create_reminder(self, user_id: int, text: str, remind_at: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO assistant_reminders(user_id,text,remind_at,created_at) VALUES (?,?,?,?)",
                (user_id, text, remind_at, _now()),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def list_reminders(self, user_id: int, status: str = "pending", limit: int = 20) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id,text,remind_at,status,created_at FROM assistant_reminders WHERE user_id=? AND status=? ORDER BY remind_at LIMIT ?",
                (user_id, status, limit),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def claim_due_reminders(self, now_iso: str, limit: int = 50) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM assistant_reminders WHERE status='pending' AND remind_at<=? ORDER BY remind_at LIMIT ?", (now_iso, limit))
            rows = [dict(row) for row in await cursor.fetchall()]
            for row in rows:
                await db.execute("UPDATE assistant_reminders SET status='delivered', delivered_at=? WHERE id=? AND status='pending'", (_now(), row["id"]))
            await db.commit()
            return rows

    async def create_action(self, user_id: int, action_type: str, target: str, payload: str, expires_at: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO assistant_actions(user_id,action_type,target,payload,expires_at,created_at) VALUES (?,?,?,?,?,?)",
                (user_id, action_type, target, payload, expires_at, _now()),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def resolve_action(self, action_id: int, user_id: int, status: str) -> Optional[Dict[str, Any]]:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM assistant_actions WHERE id=? AND user_id=? AND status='pending'",
                (action_id, user_id),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            if row["expires_at"] < now:
                await db.execute("UPDATE assistant_actions SET status='expired', resolved_at=? WHERE id=?", (now, action_id))
                await db.commit()
                return None
            await db.execute("UPDATE assistant_actions SET status=?, resolved_at=? WHERE id=?", (status, now, action_id))
            await db.commit()
            return dict(row)

    async def list_actions(self, status: str = "pending", limit: int = 100) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT id,user_id,action_type,target,status,created_at,expires_at,resolved_at FROM assistant_actions WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
            return [dict(row) for row in await cursor.fetchall()]

    async def record_usage(self, period: str, assistant_id: str, feature: str, input_tokens: int, output_tokens: int, cost_usd: float, user_id: Optional[int] = None, model: str = "") -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO ai_usage(period,assistant_id,user_id,feature,model,input_tokens,output_tokens,estimated_cost_usd,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (period, assistant_id, user_id, feature, model, input_tokens, output_tokens, cost_usd, _now()),
            )
            await db.commit()

    async def usage_total(self, period: str) -> float:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM ai_usage WHERE period=?", (period,))
            row = await cursor.fetchone()
            return float(row[0] if row else 0.0)

    async def list_users(self, limit: int = 100) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT user_id,display_name,role,approved,created_at,updated_at FROM assistant_users ORDER BY updated_at DESC LIMIT ?", (limit,))
            return [dict(row) for row in await cursor.fetchall()]

    async def set_user_approval(self, user_id: int, approved: bool) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE assistant_users SET approved=?, updated_at=? WHERE user_id=?", (int(approved), _now(), user_id))
            await db.commit()

    async def list_profiles(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT assistant_id,name,instructions,enabled,created_at,updated_at FROM assistant_profiles ORDER BY assistant_id")
            return [dict(row) for row in await cursor.fetchall()]

    async def upsert_profile(self, assistant_id: str, name: str, instructions: str = "", enabled: bool = True) -> None:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO assistant_profiles(assistant_id,name,instructions,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(assistant_id) DO UPDATE SET name=excluded.name,instructions=excluded.instructions,enabled=excluded.enabled,updated_at=excluded.updated_at", (assistant_id, name, instructions, int(enabled), now, now))
            await db.commit()

    async def list_groups(self, limit: int = 100) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT chat_id,title,enabled,mention_only,trigger_pattern,updated_at FROM assistant_groups ORDER BY updated_at DESC LIMIT ?", (limit,))
            return [dict(row) for row in await cursor.fetchall()]

    async def list_focus_scopes(self, assistant_id: str = "business", enabled_only: bool = True) -> List[Dict[str, Any]]:
        """Return chats selected by the owner in the Telegram setup wizard."""
        query = "SELECT assistant_id,chat_id,chat_title,chat_type,mode,enabled,created_at,updated_at FROM assistant_focus WHERE assistant_id=?"
        params: list[Any] = [assistant_id]
        if enabled_only:
            query += " AND enabled=1"
        query += " ORDER BY chat_title COLLATE NOCASE"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def upsert_focus_scope(
        self,
        assistant_id: str,
        chat_id: int,
        chat_title: str,
        chat_type: str = "private",
        mode: str = "monitor",
        enabled: bool = True,
    ) -> None:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO assistant_focus(assistant_id,chat_id,chat_title,chat_type,mode,enabled,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(assistant_id,chat_id) DO UPDATE SET chat_title=excluded.chat_title,
                   chat_type=excluded.chat_type,mode=excluded.mode,enabled=excluded.enabled,updated_at=excluded.updated_at""",
                (assistant_id, int(chat_id), chat_title, chat_type, mode, int(enabled), now, now),
            )
            await db.commit()

    async def disable_focus_scope(self, assistant_id: str, chat_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE assistant_focus SET enabled=0,updated_at=? WHERE assistant_id=? AND chat_id=?",
                (_now(), assistant_id, int(chat_id)),
            )
            await db.commit()

    async def create_department(self, name: str, description: str = "") -> int:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("INSERT INTO assistant_departments(name,description,created_at,updated_at) VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET description=excluded.description,updated_at=excluded.updated_at", (name, description, now, now))
            await db.commit()
            return int(cursor.lastrowid or 0)

    async def list_departments(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT id,name,description,enabled,created_at,updated_at FROM assistant_departments ORDER BY name")
            return [dict(row) for row in await cursor.fetchall()]

    async def create_project(self, user_id: int, name: str, description: str = "", department_id: Optional[int] = None, start_date: Optional[str] = None, target_date: Optional[str] = None) -> int:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            base = self._project_key(name)
            key = base
            suffix = 2
            while await (await db.execute("SELECT 1 FROM assistant_projects WHERE project_key=?", (key,))).fetchone():
                tail = str(suffix); key = f"{base[:max(1, 10-len(tail))]}{tail}"; suffix += 1
            cursor = await db.execute("INSERT INTO assistant_projects(user_id,department_id,name,description,status,start_date,target_date,created_at,updated_at,project_key,next_sequence) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (user_id, department_id, name, description, "active", start_date, target_date, now, now, key, 1))
            await db.commit()
            return int(cursor.lastrowid)

    async def list_projects(self, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT id,project_key,name,description,status,start_date,target_date,created_at,updated_at,next_sequence FROM assistant_projects WHERE user_id=? OR id IN (SELECT project_id FROM assistant_project_members WHERE user_id=?) ORDER BY updated_at DESC LIMIT ?", (user_id, user_id, limit))
            return [dict(row) for row in await cursor.fetchall()]

    async def get_project(self, user_id: int, project_id: Any) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM assistant_projects WHERE id=? AND (user_id=? OR id IN (SELECT project_id FROM assistant_project_members WHERE user_id=?))", (project_id, user_id, user_id))).fetchone()
            return dict(row) if row else None

    async def find_projects(self, user_id: int, hint: str, limit: int = 10) -> List[Dict[str, Any]]:
        rows = await self.list_projects(user_id, 100)
        needle = (hint or "").casefold()
        return [row for row in rows if needle in str(row.get("name", "")).casefold() or needle in str(row.get("project_key", "")).casefold()][:limit]

    async def get_work_item(self, user_id: int, item_id: Any) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM assistant_tasks WHERE (id=? OR display_key=?) AND (user_id=? OR assignee_id=? OR project_id IN (SELECT project_id FROM assistant_project_members WHERE user_id=?))", (item_id, str(item_id), user_id, user_id, user_id))).fetchone()
            return dict(row) if row else None

    async def list_work_items(self, user_id: int, status: str = "all", limit: int = 50, offset: int = 0, project_id: Optional[int] = None, assignee_id: Optional[int] = None, due_before: Optional[str] = None) -> List[Dict[str, Any]]:
        return await self.list_tasks(user_id, status, limit, project_id, assignee_id, due_before, offset)

    async def add_project_member(self, project_id: int, user_id: int, role: str = "member") -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO assistant_project_members(project_id,user_id,role) VALUES (?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role", (project_id, user_id, role))
            await db.commit()

    async def update_task(self, user_id: int, task_id: int, status: Optional[str] = None, assignee_id: Optional[int] = None, priority: Optional[str] = None, due_at: Optional[str] = None) -> bool:
        fields: list[str] = []
        values: list[Any] = []
        old_status = None
        if status:
            status = normalize_status(status)
            if status not in {"todo", "in_progress", "review", "waiting", "blocked", "done", "backlog"}:
                raise ValueError("Unsupported work item status")
            fields.append("status=?"); values.append(status)
        if assignee_id is not None:
            fields.append("assignee_id=?"); values.append(assignee_id)
        if priority:
            fields.append("priority=?"); values.append(validate_priority(priority))
        if due_at is not None:
            fields.append("due_at=?"); values.append(due_at)
        if not fields:
            return False
        async with aiosqlite.connect(self.db_path) as db:
            resolved = await (await db.execute("SELECT id FROM assistant_tasks WHERE (id=? OR display_key=?) AND (user_id=? OR assignee_id=?)", (task_id, str(task_id), user_id, user_id))).fetchone()
            if not resolved:
                return False
            task_id = int(resolved[0])
            if status:
                row = await (await db.execute("SELECT status FROM assistant_tasks WHERE id=? AND (user_id=? OR assignee_id=?)", (task_id, user_id, user_id))).fetchone()
                if not row:
                    return False
                old_status = normalize_status(row[0])
                if not can_transition(old_status, status):
                    raise ValueError(f"Cannot move work item from {old_status} to {status}")
            fields.append("updated_at=?"); values.append(_now())
            cursor = await db.execute("UPDATE assistant_tasks SET " + ",".join(fields) + " WHERE id=? AND (user_id=? OR assignee_id=?)", values + [task_id, user_id, user_id])
            if cursor.rowcount == 1 and status and old_status != status:
                await db.execute("INSERT INTO work_item_status_history(work_item_id,from_status,to_status,changed_by,created_at) VALUES (?,?,?,?,?)", (task_id, old_status, status, user_id, _now()))
            await db.commit()
            return cursor.rowcount == 1

    async def record_agent_audit(self, *, actor_id: int, tool_name: str, arguments_summary: Dict[str, Any], policy_result: str, execution_result: Any = None, target: Any = None) -> None:
        import json
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO agent_audit_events(actor_id,tool_name,arguments, target,policy_result,execution_result,created_at) VALUES (?,?,?,?,?,?,?)", (actor_id, tool_name, json.dumps(arguments_summary, default=str), str(target) if target is not None else None, policy_result, json.dumps(execution_result, default=str), _now()))
            await db.commit()

    async def add_dependency(self, task_id: int, predecessor_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR IGNORE INTO assistant_task_dependencies(task_id,predecessor_id) VALUES (?,?)", (task_id, predecessor_id))
            await db.commit()

    async def add_milestone(self, project_id: int, name: str, target_date: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("INSERT INTO assistant_milestones(project_id,name,target_date,created_at) VALUES (?,?,?,?)", (project_id, name, target_date, _now()))
            await db.commit()
            return int(cursor.lastrowid)

    async def add_task_comment(self, task_id: int, user_id: int, comment: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("INSERT INTO assistant_task_comments(task_id,user_id,comment,created_at) VALUES (?,?,?,?)", (task_id, user_id, comment, _now()))
            await db.commit()
            return int(cursor.lastrowid)

    async def project_plan_data(self, user_id: int, project_id: int) -> Dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            project_cursor = await db.execute("SELECT * FROM assistant_projects WHERE id=? AND (user_id=? OR id IN (SELECT project_id FROM assistant_project_members WHERE user_id=?))", (project_id, user_id, user_id))
            project = await project_cursor.fetchone()
            if not project:
                return {}
            task_cursor = await db.execute("SELECT * FROM assistant_tasks WHERE project_id=? ORDER BY id", (project_id,))
            tasks = [dict(row) for row in await task_cursor.fetchall()]
            dep_cursor = await db.execute("SELECT task_id,predecessor_id FROM assistant_task_dependencies WHERE task_id IN (SELECT id FROM assistant_tasks WHERE project_id=?)", (project_id,))
            dependencies = [dict(row) for row in await dep_cursor.fetchall()]
            milestone_cursor = await db.execute("SELECT * FROM assistant_milestones WHERE project_id=? ORDER BY target_date", (project_id,))
            milestones = [dict(row) for row in await milestone_cursor.fetchall()]
            return {"project": dict(project), "tasks": tasks, "dependencies": dependencies, "milestones": milestones}
