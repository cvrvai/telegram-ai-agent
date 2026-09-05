"""Async SQLite database manager for messages, classifications, and digests."""

from __future__ import annotations

import json

import aiosqlite
from typing import List, Optional, Dict, Any
from app.core.models import MessageRecord, DigestStats, PriorityClassification, IncomingMessage, Situation, SituationDecision


def _situation_from_row(row: aiosqlite.Row) -> Situation:
    return Situation(
        id=row["id"],
        chat_id=row["chat_id"],
        chat_title=row["chat_title"] or "Chat",
        title=row["title"] or "",
        status=row["status"] or "open",
        priority=row["priority"] or "P2",
        summary=row["summary"] or "",
        current_action=row["current_action"],
        dependency=row["dependency"],
        guest_affected=bool(row["guest_affected"]),
        responsible=row["responsible"],
        message_count=int(row["message_count"] or 1),
        source_message_ids=json.loads(row["source_message_ids"] or "[]"),
        last_message_link=row["last_message_link"],
        started_at=row["started_at"] or "",
        last_update_at=row["last_update_at"] or "",
        resolved_at=row["resolved_at"],
    )


def _chat_scope(allowed_chat_ids: Optional[set[int]], prefix: str = " AND ") -> tuple[str, list[int]]:
    """Build a safe SQL scope for owner-selected Telegram chats."""
    if allowed_chat_ids is None:
        return "", []
    ids = sorted({int(chat_id) for chat_id in allowed_chat_ids})
    if not ids:
        return f"{prefix}1=0", []
    placeholders = ",".join("?" for _ in ids)
    return f"{prefix}chat_id IN ({placeholders})", ids


class Database:
    def __init__(self, db_path: str = "telegram_bot.db"):
        self.db_path = db_path

    async def init_db(self) -> None:
        """Initialize database tables and indexes."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    chat_title TEXT,
                    chat_type TEXT,
                    sender_id INTEGER,
                    sender_name TEXT,
                    sender_username TEXT,
                    text TEXT,
                    date TEXT,
                    media_type TEXT,
                    message_link TEXT,
                    priority TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    reason TEXT,
                    needs_action INTEGER DEFAULT 0,
                    action TEXT,
                    deadline TEXT,
                    category TEXT,
                    summary TEXT,
                    is_prefiltered INTEGER DEFAULT 0,
                    alert_sent INTEGER DEFAULT 0,
                    digest_sent INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_chat_msg
                ON messages(chat_id, message_id)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_digest_pending
                ON messages(digest_sent, priority, score)
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS digests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    digest_text TEXT NOT NULL,
                    total_messages INTEGER NOT NULL,
                    p0_count INTEGER NOT NULL,
                    p1_count INTEGER NOT NULL,
                    p2_count INTEGER NOT NULL,
                    p3_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS situations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    chat_title TEXT,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    priority TEXT NOT NULL DEFAULT 'P2',
                    summary TEXT,
                    current_action TEXT,
                    dependency TEXT,
                    guest_affected INTEGER DEFAULT 0,
                    responsible TEXT,
                    message_count INTEGER DEFAULT 1,
                    source_message_ids TEXT NOT NULL DEFAULT '[]',
                    last_message_link TEXT,
                    started_at TEXT NOT NULL,
                    last_update_at TEXT NOT NULL,
                    resolved_at TEXT
                )
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_situations_chat_status
                ON situations(chat_id, status, last_update_at)
            """)

            await db.commit()

        # Business assistant tables are intentionally maintained separately
        # from the legacy message tables so both schemas can be migrated
        # without changing existing alert or digest records.
        from app.storage.business import init_business_schema
        await init_business_schema(self.db_path)

    async def save_message(
        self,
        msg: IncomingMessage,
        classification: PriorityClassification,
        is_prefiltered: bool = False,
        alert_sent: bool = False,
    ) -> int:
        """Insert a processed message into the database."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO messages (
                    message_id, chat_id, chat_title, chat_type,
                    sender_id, sender_name, sender_username, text,
                    date, media_type, message_link,
                    priority, score, reason, needs_action,
                    action, deadline, category, summary, message_type, ai_confidence,
                    is_prefiltered, alert_sent, digest_sent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.message_id,
                    msg.chat_id,
                    msg.chat_title,
                    msg.chat_type,
                    msg.sender_id,
                    msg.sender_name,
                    msg.sender_username,
                    msg.text,
                    msg.date.isoformat(),
                    msg.media_type,
                    msg.message_link,
                    classification.priority,
                    classification.score,
                    classification.reason,
                    1 if classification.needs_action else 0,
                    classification.action,
                    classification.deadline,
                    classification.category,
                    classification.summary,
                    classification.message_type,
                    classification.ai_confidence,
                    1 if is_prefiltered else 0,
                    1 if alert_sent else 0,
                    0,
                    msg.date.isoformat(),
                ),
            )
            await db.commit()
            return cursor.lastrowid

    async def mark_alert_sent(self, record_id: int) -> None:
        """Mark that an immediate alert was sent for this record."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE messages SET alert_sent = 1 WHERE id = ?",
                (record_id,),
            )
            await db.commit()

    async def message_exists(self, chat_id: int, message_id: int) -> bool:
        """Return whether a Telegram message has already been imported."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM messages WHERE chat_id = ? AND message_id = ? LIMIT 1",
                (chat_id, message_id),
            )
            return await cursor.fetchone() is not None

    async def get_pending_digest_messages(self, allowed_chat_ids: Optional[set[int]] = None) -> List[MessageRecord]:
        """Fetch all messages that have not yet been included in a digest."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            scope, scope_params = _chat_scope(allowed_chat_ids)
            cursor = await db.execute(
                f"SELECT * FROM messages WHERE digest_sent = 0{scope} ORDER BY score DESC, id ASC",
                scope_params,
            )
            rows = await cursor.fetchall()
            return [
                MessageRecord(
                    id=row["id"],
                    message_id=row["message_id"],
                    chat_id=row["chat_id"],
                    chat_title=row["chat_title"] or "Direct Message",
                    chat_type=row["chat_type"] or "private",
                    sender_id=row["sender_id"],
                    sender_name=row["sender_name"] or "Unknown",
                    sender_username=row["sender_username"],
                    text=row["text"] or "",
                    date=row["date"],
                    media_type=row["media_type"],
                    message_link=row["message_link"],
                    priority=row["priority"],
                    score=row["score"],
                    reason=row["reason"] or "",
                    needs_action=bool(row["needs_action"]),
                    action=row["action"],
                    deadline=row["deadline"],
                    category=row["category"] or "general",
                    summary=row["summary"] or "",
                    message_type=row["message_type"] or "unknown",
                    ai_confidence=float(row["ai_confidence"] or 0),
                    is_prefiltered=bool(row["is_prefiltered"]),
                    alert_sent=bool(row["alert_sent"]),
                    digest_sent=bool(row["digest_sent"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def mark_messages_digested(self, record_ids: List[int]) -> None:
        """Mark a list of message records as included in digest."""
        if not record_ids:
            return
        placeholders = ",".join("?" for _ in record_ids)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE messages SET digest_sent = 1 WHERE id IN ({placeholders})",
                record_ids,
            )
            await db.commit()

    async def save_digest(self, stats: DigestStats, digest_text: str) -> int:
        """Record a generated digest into history."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO digests (
                    digest_text, total_messages, p0_count, p1_count, p2_count, p3_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    digest_text,
                    stats.total_messages,
                    stats.p0_count,
                    stats.p1_count,
                    stats.p2_count,
                    stats.p3_count,
                ),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_priority_messages(self, limit: int = 20, allowed_chat_ids: Optional[set[int]] = None) -> List[MessageRecord]:
        """Fetch latest high-priority messages (P0 and P1)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            scope, scope_params = _chat_scope(allowed_chat_ids)
            cursor = await db.execute(
                f"SELECT * FROM messages WHERE priority IN ('P0', 'P1'){scope} ORDER BY id DESC LIMIT ?",
                [*scope_params, limit],
            )
            rows = await cursor.fetchall()
            return [
                MessageRecord(
                    id=row["id"],
                    message_id=row["message_id"],
                    chat_id=row["chat_id"],
                    chat_title=row["chat_title"] or "Direct Message",
                    chat_type=row["chat_type"] or "private",
                    sender_id=row["sender_id"],
                    sender_name=row["sender_name"] or "Unknown",
                    sender_username=row["sender_username"],
                    text=row["text"] or "",
                    date=row["date"],
                    media_type=row["media_type"],
                    message_link=row["message_link"],
                    priority=row["priority"],
                    score=row["score"],
                    reason=row["reason"] or "",
                    needs_action=bool(row["needs_action"]),
                    action=row["action"],
                    deadline=row["deadline"],
                    category=row["category"] or "general",
                    summary=row["summary"] or "",
                    message_type=row["message_type"] or "unknown",
                    ai_confidence=float(row["ai_confidence"] or 0),
                    is_prefiltered=bool(row["is_prefiltered"]),
                    alert_sent=bool(row["alert_sent"]),
                    digest_sent=bool(row["digest_sent"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def get_messages_by_tier(self, tier: str, limit: int = 15, allowed_chat_ids: Optional[set[int]] = None) -> List[MessageRecord]:
        """Fetch messages filtered by a specific priority tier (P0, P1, P2, P3)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            scope, scope_params = _chat_scope(allowed_chat_ids)
            cursor = await db.execute(
                f"SELECT * FROM messages WHERE priority = ?{scope} ORDER BY id DESC LIMIT ?",
                [tier.upper(), *scope_params, limit],
            )
            rows = await cursor.fetchall()
            return [
                MessageRecord(
                    id=row["id"],
                    message_id=row["message_id"],
                    chat_id=row["chat_id"],
                    chat_title=row["chat_title"] or "Direct Message",
                    chat_type=row["chat_type"] or "private",
                    sender_id=row["sender_id"],
                    sender_name=row["sender_name"] or "Unknown",
                    sender_username=row["sender_username"],
                    text=row["text"] or "",
                    date=row["date"],
                    media_type=row["media_type"],
                    message_link=row["message_link"],
                    priority=row["priority"],
                    score=row["score"],
                    reason=row["reason"] or "",
                    needs_action=bool(row["needs_action"]),
                    action=row["action"],
                    deadline=row["deadline"],
                    category=row["category"] or "general",
                    summary=row["summary"] or "",
                    message_type=row["message_type"] or "unknown",
                    ai_confidence=float(row["ai_confidence"] or 0),
                    is_prefiltered=bool(row["is_prefiltered"]),
                    alert_sent=bool(row["alert_sent"]),
                    digest_sent=bool(row["digest_sent"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def get_recent_messages(self, limit: int = 40, allowed_chat_ids: Optional[set[int]] = None) -> List[MessageRecord]:
        """Fetch most recent messages for conversational AI Q&A."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            scope, scope_params = _chat_scope(allowed_chat_ids)
            cursor = await db.execute(
                f"SELECT * FROM messages WHERE 1=1{scope} ORDER BY id DESC LIMIT ?",
                [*scope_params, limit],
            )
            rows = await cursor.fetchall()
            return [
                MessageRecord(
                    id=row["id"],
                    message_id=row["message_id"],
                    chat_id=row["chat_id"],
                    chat_title=row["chat_title"] or "Direct Message",
                    chat_type=row["chat_type"] or "private",
                    sender_id=row["sender_id"],
                    sender_name=row["sender_name"] or "Unknown",
                    sender_username=row["sender_username"],
                    text=row["text"] or "",
                    date=row["date"],
                    media_type=row["media_type"],
                    message_link=row["message_link"],
                    priority=row["priority"],
                    score=row["score"],
                    reason=row["reason"] or "",
                    needs_action=bool(row["needs_action"]),
                    action=row["action"],
                    deadline=row["deadline"],
                    category=row["category"] or "general",
                    summary=row["summary"] or "",
                    message_type=row["message_type"] or "unknown",
                    ai_confidence=float(row["ai_confidence"] or 0),
                    is_prefiltered=bool(row["is_prefiltered"]),
                    alert_sent=bool(row["alert_sent"]),
                    digest_sent=bool(row["digest_sent"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]



    async def get_active_groups(self, limit: int = 10, allowed_chat_ids: Optional[set[int]] = None) -> List[Dict[str, Any]]:
        """Fetch active chats/groups with message and priority counts."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            scope, scope_params = _chat_scope(allowed_chat_ids)
            cursor = await db.execute(
                f"""
                SELECT
                    chat_id,
                    chat_title,
                    chat_type,
                    COUNT(*) as total_count,
                    SUM(CASE WHEN priority IN ('P0', 'P1') THEN 1 ELSE 0 END) as priority_count,
                    MAX(date) as last_activity
                FROM messages
                WHERE chat_title IS NOT NULL AND chat_title != ''{scope}
                GROUP BY chat_id, chat_title, chat_type
                ORDER BY last_activity DESC
                LIMIT ?
                """,
                [*scope_params, limit],
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_messages_for_chat(self, chat_id: int, limit: int = 15, allowed_chat_ids: Optional[set[int]] = None) -> List[MessageRecord]:
        """Fetch recent messages for a specific chat/group."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if allowed_chat_ids is not None and int(chat_id) not in {int(value) for value in allowed_chat_ids}:
                return []
            cursor = await db.execute(
                """
                SELECT * FROM messages
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            )
            rows = await cursor.fetchall()
            return [
                MessageRecord(
                    id=row["id"],
                    message_id=row["message_id"],
                    chat_id=row["chat_id"],
                    chat_title=row["chat_title"] or "Direct Message",
                    chat_type=row["chat_type"] or "private",
                    sender_id=row["sender_id"],
                    sender_name=row["sender_name"] or "Unknown",
                    sender_username=row["sender_username"],
                    text=row["text"] or "",
                    date=row["date"],
                    media_type=row["media_type"],
                    message_link=row["message_link"],
                    priority=row["priority"],
                    score=row["score"],
                    reason=row["reason"] or "",
                    needs_action=bool(row["needs_action"]),
                    action=row["action"],
                    deadline=row["deadline"],
                    category=row["category"] or "general",
                    summary=row["summary"] or "",
                    message_type=row["message_type"] or "unknown",
                    ai_confidence=float(row["ai_confidence"] or 0),
                    is_prefiltered=bool(row["is_prefiltered"]),
                    alert_sent=bool(row["alert_sent"]),
                    digest_sent=bool(row["digest_sent"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def get_stats(self, allowed_chat_ids: Optional[set[int]] = None) -> Dict[str, Any]:
        """Fetch overall classification statistics."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            scope, scope_params = _chat_scope(allowed_chat_ids)
            cursor = await db.execute(f"""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN priority = 'P0' THEN 1 ELSE 0 END) as p0,
                    SUM(CASE WHEN priority = 'P1' THEN 1 ELSE 0 END) as p1,
                    SUM(CASE WHEN priority = 'P2' THEN 1 ELSE 0 END) as p2,
                    SUM(CASE WHEN priority = 'P3' THEN 1 ELSE 0 END) as p3,
                    SUM(CASE WHEN needs_action = 1 THEN 1 ELSE 0 END) as action_needed,
                    SUM(CASE WHEN alert_sent = 1 THEN 1 ELSE 0 END) as alerts_sent,
                    SUM(CASE WHEN digest_sent = 0 THEN 1 ELSE 0 END) as pending_digest
                FROM messages
                WHERE 1=1{scope}
            """, scope_params)
            row = await cursor.fetchone()
            if not row:
                return {}
            return {
                "total_messages": row["total"] or 0,
                "p0_urgent": row["p0"] or 0,
                "p1_important": row["p1"] or 0,
                "p2_useful": row["p2"] or 0,
                "p3_noise": row["p3"] or 0,
                "actions_needed": row["action_needed"] or 0,
                "alerts_sent": row["alerts_sent"] or 0,
                "pending_digest": row["pending_digest"] or 0,
            }

    async def get_open_situations(self, chat_id: int, limit: int = 5) -> List[Situation]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM situations WHERE chat_id = ? AND status IN ('open','monitoring') ORDER BY last_update_at DESC LIMIT ?",
                (chat_id, limit),
            )
            return [_situation_from_row(row) for row in await cursor.fetchall()]

    async def create_situation(self, msg: IncomingMessage, decision: SituationDecision, cls: PriorityClassification) -> int:
        now = msg.date.isoformat()
        title = decision.title or cls.summary[:120] or msg.text[:120]
        status = decision.status if decision.status != "resolved" else "open"
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO situations (chat_id, chat_title, title, status, priority, summary, current_action, "
                "dependency, guest_affected, responsible, message_count, source_message_ids, last_message_link, "
                "started_at, last_update_at, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    msg.chat_id, msg.chat_title, title, status, cls.priority, cls.summary,
                    decision.current_action or cls.action, decision.dependency,
                    1 if decision.guest_affected else 0, decision.responsible or cls.category or msg.chat_title,
                    1, json.dumps([msg.message_id]), msg.message_link, now, now, None,
                ),
            )
            await db.commit()
            return cursor.lastrowid

    async def update_situation(self, situation_id: int, msg: IncomingMessage, decision: SituationDecision, cls: PriorityClassification) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM situations WHERE id = ?", (situation_id,))
            existing = await cursor.fetchone()
            if not existing:
                return
            source_ids = json.loads(existing["source_message_ids"] or "[]")
            if msg.message_id not in source_ids:
                source_ids.append(msg.message_id)
            source_ids = source_ids[-20:]
            status = "resolved" if decision.action == "resolve" else decision.status
            priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
            priority = cls.priority if priority_rank.get(cls.priority, 3) < priority_rank.get(existing["priority"], 3) else existing["priority"]
            resolved_at = msg.date.isoformat() if status == "resolved" else existing["resolved_at"]
            await db.execute(
                "UPDATE situations SET status=?, priority=?, summary=?, current_action=?, dependency=?, "
                "guest_affected=?, responsible=?, message_count=?, source_message_ids=?, last_message_link=?, "
                "last_update_at=?, resolved_at=? WHERE id=?",
                (
                    status, priority, cls.summary,
                    decision.current_action or cls.action or existing["current_action"],
                    decision.dependency if decision.dependency is not None else existing["dependency"],
                    1 if (bool(existing["guest_affected"]) or decision.guest_affected) else 0,
                    decision.responsible or existing["responsible"],
                    int(existing["message_count"] or 1) + 1,
                    json.dumps(source_ids),
                    msg.message_link or existing["last_message_link"],
                    msg.date.isoformat(),
                    resolved_at,
                    situation_id,
                ),
            )
            await db.commit()

    async def get_situation(self, situation_id: int) -> Optional[Situation]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM situations WHERE id = ?", (situation_id,))
            row = await cursor.fetchone()
            return _situation_from_row(row) if row else None

    async def resolve_situation(self, situation_id: int) -> bool:
        """Manual GM override, independent of the message-driven fusion flow."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("UPDATE situations SET status='resolved', resolved_at=? WHERE id=?", (now, situation_id))
            await db.commit()
            return cursor.rowcount > 0

    async def list_situations(self, status: Optional[str] = None, limit: int = 20, allowed_chat_ids: Optional[set[int]] = None) -> List[Situation]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            scope, scope_params = _chat_scope(allowed_chat_ids)
            if status:
                where, params = "status = ?", [status]
            else:
                where, params = "status IN ('open','monitoring')", []
            cursor = await db.execute(
                f"SELECT * FROM situations WHERE {where}{scope} ORDER BY last_update_at DESC",
                [*params, *scope_params],
            )
            rows = [_situation_from_row(row) for row in await cursor.fetchall()]
            priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
            # Stable sort: ties keep the last_update_at-descending SQL order.
            rows.sort(key=lambda situation: priority_rank.get(situation.priority, 3))
            return rows[:limit]

    async def list_situations_active_since(self, since_iso: str, allowed_chat_ids: Optional[set[int]] = None) -> List[Situation]:
        """Situations with any activity (open or newly resolved) since a given
        instant -- the basis for "what happened today/this week" retrospectives,
        as distinct from list_situations()'s "what's open right now" snapshot."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            scope, scope_params = _chat_scope(allowed_chat_ids)
            cursor = await db.execute(
                f"SELECT * FROM situations WHERE (last_update_at >= ? OR started_at >= ?){scope} ORDER BY last_update_at DESC",
                [since_iso, since_iso, *scope_params],
            )
            rows = [_situation_from_row(row) for row in await cursor.fetchall()]
            priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
            rows.sort(key=lambda situation: priority_rank.get(situation.priority, 3))
            return rows


