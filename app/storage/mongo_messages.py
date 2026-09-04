"""MongoDB implementation of the legacy Telegram message database interface."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from models import DigestStats, IncomingMessage, MessageRecord, PriorityClassification


class MongoMessageDatabase:
    """Store classified Telegram messages and digests in MongoDB."""

    def __init__(self, uri: str, database: str = "telegram_business") -> None:
        self.client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5_000)
        self.db = self.client[database]
        self.messages = self.db["messages"]
        self.digests = self.db["digests"]
        self.counters = self.db["counters"]

    async def init_db(self) -> None:
        await self.client.admin.command("ping")
        await self.messages.create_index([("chat_id", 1), ("message_id", 1)], unique=True)
        await self.messages.create_index([("digest_sent", 1), ("priority", 1), ("id", 1)])
        await self.messages.create_index([("chat_id", 1), ("id", -1)])
        await self.digests.create_index([("created_at", -1)])

    async def close(self) -> None:
        self.client.close()

    async def _next_id(self) -> int:
        counter = await self.counters.find_one_and_update(
            {"_id": "messages"},
            {"$inc": {"value": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(counter["value"])

    @staticmethod
    def _record(doc: Dict[str, Any]) -> MessageRecord:
        return MessageRecord(
            id=int(doc["id"]),
            message_id=int(doc["message_id"]),
            chat_id=int(doc["chat_id"]),
            chat_title=doc.get("chat_title") or "Direct Message",
            chat_type=doc.get("chat_type") or "private",
            sender_id=doc.get("sender_id"),
            sender_name=doc.get("sender_name") or "Unknown",
            sender_username=doc.get("sender_username"),
            text=doc.get("text") or "",
            date=doc.get("date") or "",
            media_type=doc.get("media_type"),
            message_link=doc.get("message_link"),
            priority=doc.get("priority", "P3"),
            score=int(doc.get("score", 0)),
            reason=doc.get("reason") or "",
            needs_action=bool(doc.get("needs_action")),
            action=doc.get("action"),
            deadline=doc.get("deadline"),
            category=doc.get("category") or "general",
            summary=doc.get("summary") or "",
            message_type=doc.get("message_type") or "unknown",
            ai_confidence=float(doc.get("ai_confidence") or 0),
            is_prefiltered=bool(doc.get("is_prefiltered")),
            alert_sent=bool(doc.get("alert_sent")),
            digest_sent=bool(doc.get("digest_sent")),
            created_at=doc.get("created_at") or "",
        )

    async def save_message(
        self,
        msg: IncomingMessage,
        cls: PriorityClassification,
        is_prefiltered: bool = False,
        alert_sent: bool = False,
    ) -> int:
        existing = await self.messages.find_one({"chat_id": msg.chat_id, "message_id": msg.message_id}, {"id": 1})
        if existing:
            return int(existing["id"])
        record_id = await self._next_id()
        doc = {
            "id": record_id,
            "message_id": msg.message_id,
            "chat_id": msg.chat_id,
            "chat_title": msg.chat_title,
            "chat_type": msg.chat_type,
            "sender_id": msg.sender_id,
            "sender_name": msg.sender_name,
            "sender_username": msg.sender_username,
            "text": msg.text,
            "date": msg.date.isoformat(),
            "media_type": msg.media_type,
            "message_link": msg.message_link,
            "priority": cls.priority,
            "score": cls.score,
            "reason": cls.reason,
            "needs_action": cls.needs_action,
            "action": cls.action,
            "deadline": cls.deadline,
            "category": cls.category,
            "summary": cls.summary,
            "message_type": cls.message_type,
            "ai_confidence": cls.ai_confidence,
            "is_prefiltered": is_prefiltered,
            "alert_sent": alert_sent,
            "digest_sent": False,
            "created_at": msg.date.isoformat(),
        }
        try:
            await self.messages.insert_one(doc)
        except DuplicateKeyError:
            existing = await self.messages.find_one({"chat_id": msg.chat_id, "message_id": msg.message_id}, {"id": 1})
            return int(existing["id"]) if existing else record_id
        return record_id

    async def message_exists(self, chat_id: int, message_id: int) -> bool:
        return await self.messages.count_documents({"chat_id": chat_id, "message_id": message_id}, limit=1) > 0

    async def mark_alert_sent(self, record_id: int) -> None:
        await self.messages.update_one({"id": record_id}, {"$set": {"alert_sent": True}})

    async def get_pending_digest_messages(self, allowed_chat_ids: Optional[set[int]] = None) -> List[MessageRecord]:
        query: Dict[str, Any] = {"digest_sent": False}
        if allowed_chat_ids is not None:
            query["chat_id"] = {"$in": [int(chat_id) for chat_id in allowed_chat_ids]}
        cursor = self.messages.find(query).sort([("score", -1), ("id", 1)])
        return [self._record(doc) async for doc in cursor]

    async def mark_messages_digested(self, record_ids: List[int]) -> None:
        if record_ids:
            await self.messages.update_many({"id": {"$in": record_ids}}, {"$set": {"digest_sent": True}})

    async def save_digest(self, stats: DigestStats, digest_text: str) -> int:
        record_id = await self._next_id()
        await self.digests.insert_one(
            {
                "id": record_id,
                "digest_text": digest_text,
                "total_messages": stats.total_messages,
                "p0_count": stats.p0_count,
                "p1_count": stats.p1_count,
                "p2_count": stats.p2_count,
                "p3_count": stats.p3_count,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return record_id

    async def get_priority_messages(self, limit: int = 20, allowed_chat_ids: Optional[set[int]] = None) -> List[MessageRecord]:
        query: Dict[str, Any] = {"priority": {"$in": ["P0", "P1"]}}
        if allowed_chat_ids is not None:
            query["chat_id"] = {"$in": [int(chat_id) for chat_id in allowed_chat_ids]}
        cursor = self.messages.find(query).sort("id", -1).limit(limit)
        return [self._record(doc) async for doc in cursor]

    async def get_messages_by_tier(self, tier: str, limit: int = 15, allowed_chat_ids: Optional[set[int]] = None) -> List[MessageRecord]:
        query: Dict[str, Any] = {"priority": tier.upper()}
        if allowed_chat_ids is not None:
            query["chat_id"] = {"$in": [int(chat_id) for chat_id in allowed_chat_ids]}
        cursor = self.messages.find(query).sort("id", -1).limit(limit)
        return [self._record(doc) async for doc in cursor]

    async def get_recent_messages(self, limit: int = 40, allowed_chat_ids: Optional[set[int]] = None) -> List[MessageRecord]:
        query: Dict[str, Any] = {}
        if allowed_chat_ids is not None:
            query["chat_id"] = {"$in": [int(chat_id) for chat_id in allowed_chat_ids]}
        cursor = self.messages.find(query).sort("id", -1).limit(limit)
        rows = [self._record(doc) async for doc in cursor]
        return rows

    async def get_active_groups(self, limit: int = 10, allowed_chat_ids: Optional[set[int]] = None) -> List[Dict[str, Any]]:
        match: Dict[str, Any] = {"chat_title": {"$nin": [None, ""]}}
        if allowed_chat_ids is not None:
            match["chat_id"] = {"$in": [int(chat_id) for chat_id in allowed_chat_ids]}
        pipeline = [
            {"$match": match},
            {"$group": {"_id": {"chat_id": "$chat_id", "chat_title": "$chat_title", "chat_type": "$chat_type"}, "total_count": {"$sum": 1}, "priority_count": {"$sum": {"$cond": [{"$in": ["$priority", ["P0", "P1"]]}, 1, 0]}}, "last_activity": {"$max": "$date"}}},
            {"$sort": {"last_activity": -1}},
            {"$limit": limit},
        ]
        rows = []
        async for row in self.messages.aggregate(pipeline):
            group = row["_id"]
            rows.append({**group, "total_count": row["total_count"], "priority_count": row["priority_count"], "last_activity": row["last_activity"]})
        return rows

    async def get_messages_for_chat(self, chat_id: int, limit: int = 15, allowed_chat_ids: Optional[set[int]] = None) -> List[MessageRecord]:
        if allowed_chat_ids is not None and int(chat_id) not in {int(value) for value in allowed_chat_ids}:
            return []
        cursor = self.messages.find({"chat_id": chat_id}).sort("id", -1).limit(limit)
        return [self._record(doc) async for doc in cursor]

    async def get_stats(self, allowed_chat_ids: Optional[set[int]] = None) -> Dict[str, Any]:
        stats = {"total_messages": 0, "p0_urgent": 0, "p1_important": 0, "p2_useful": 0, "p3_noise": 0, "actions_needed": 0, "alerts_sent": 0, "pending_digest": 0}
        query: Dict[str, Any] = {}
        if allowed_chat_ids is not None:
            query["chat_id"] = {"$in": [int(chat_id) for chat_id in allowed_chat_ids]}
        async for doc in self.messages.find(query, {"priority": 1, "needs_action": 1, "alert_sent": 1, "digest_sent": 1}):
            stats["total_messages"] += 1
            priority_key = {"P0": "p0_urgent", "P1": "p1_important", "P2": "p2_useful", "P3": "p3_noise"}.get(doc.get("priority"))
            if priority_key:
                stats[priority_key] += 1
            stats["actions_needed"] += int(bool(doc.get("needs_action")))
            stats["alerts_sent"] += int(bool(doc.get("alert_sent")))
            stats["pending_digest"] += int(not bool(doc.get("digest_sent")))
        return stats
