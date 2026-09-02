"""Async SQLite database manager for messages, classifications, and digests."""

from __future__ import annotations

import aiosqlite
from typing import List, Optional, Dict, Any
from models import MessageRecord, DigestStats, PriorityClassification, IncomingMessage


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

            await db.commit()

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
                    action, deadline, category, summary,
                    is_prefiltered, alert_sent, digest_sent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))
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
                    1 if is_prefiltered else 0,
                    1 if alert_sent else 0,
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

    async def get_pending_digest_messages(self) -> List[MessageRecord]:
        """Fetch all messages that have not yet been included in a digest."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM messages
                WHERE digest_sent = 0
                ORDER BY score DESC, id ASC
            """)
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

    async def get_priority_messages(self, limit: int = 20) -> List[MessageRecord]:
        """Fetch latest high-priority messages (P0 and P1)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM messages
                WHERE priority IN ('P0', 'P1')
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
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
                    is_prefiltered=bool(row["is_prefiltered"]),
                    alert_sent=bool(row["alert_sent"]),
                    digest_sent=bool(row["digest_sent"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def get_messages_by_tier(self, tier: str, limit: int = 15) -> List[MessageRecord]:
        """Fetch messages filtered by a specific priority tier (P0, P1, P2, P3)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM messages
                WHERE priority = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (tier.upper(), limit),
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
                    is_prefiltered=bool(row["is_prefiltered"]),
                    alert_sent=bool(row["alert_sent"]),
                    digest_sent=bool(row["digest_sent"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def get_recent_messages(self, limit: int = 40) -> List[MessageRecord]:
        """Fetch most recent messages for conversational AI Q&A."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM messages
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
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
                    is_prefiltered=bool(row["is_prefiltered"]),
                    alert_sent=bool(row["alert_sent"]),
                    digest_sent=bool(row["digest_sent"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]



    async def get_active_groups(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetch active chats/groups with message and priority counts."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    chat_id,
                    chat_title,
                    chat_type,
                    COUNT(*) as total_count,
                    SUM(CASE WHEN priority IN ('P0', 'P1') THEN 1 ELSE 0 END) as priority_count,
                    MAX(date) as last_activity
                FROM messages
                WHERE chat_title IS NOT NULL AND chat_title != ''
                GROUP BY chat_id, chat_title, chat_type
                ORDER BY last_activity DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_messages_for_chat(self, chat_id: int, limit: int = 15) -> List[MessageRecord]:
        """Fetch recent messages for a specific chat/group."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
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
                    is_prefiltered=bool(row["is_prefiltered"]),
                    alert_sent=bool(row["alert_sent"]),
                    digest_sent=bool(row["digest_sent"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def get_stats(self) -> Dict[str, Any]:
        """Fetch overall classification statistics."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
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
            """)
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


