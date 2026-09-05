"""MongoDB implementation of the legacy Telegram message database interface."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.models import DigestStats, IncomingMessage, MessageRecord, PriorityClassification, Situation, SituationDecision


class MongoMessageDatabase:
    """Store classified Telegram messages and digests in MongoDB."""

    def __init__(self, uri: str, database: str = "telegram_business") -> None:
        self.client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5_000)
        self.db = self.client[database]
        self.messages = self.db["messages"]
        self.digests = self.db["digests"]
        self.counters = self.db["counters"]
        self.situations = self.db["situations"]

    async def init_db(self) -> None:
        await self.client.admin.command("ping")
        await self.messages.create_index([("chat_id", 1), ("message_id", 1)], unique=True)
        await self.messages.create_index([("digest_sent", 1), ("priority", 1), ("id", 1)])
        await self.messages.create_index([("chat_id", 1), ("id", -1)])
        await self.digests.create_index([("created_at", -1)])
        await self.situations.create_index([("chat_id", 1), ("status", 1), ("last_update_at", -1)])

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

    async def search_messages(self, query_text: str, limit: int = 20, allowed_chat_ids: Optional[set[int]] = None) -> List[MessageRecord]:
        safe_query = re.escape(str(query_text or "")[:200])
        query: Dict[str, Any] = {"$or": [{"text": {"$regex": safe_query, "$options": "i"}}, {"summary": {"$regex": safe_query, "$options": "i"}}, {"chat_title": {"$regex": safe_query, "$options": "i"}}]}
        if allowed_chat_ids is not None:
            query["chat_id"] = {"$in": [int(chat_id) for chat_id in allowed_chat_ids]}
        cursor = self.messages.find(query).sort("id", -1).limit(limit)
        return [self._record(doc) async for doc in cursor]

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

    @staticmethod
    def _situation(doc: Dict[str, Any]) -> Situation:
        return Situation(
            id=int(doc["id"]),
            chat_id=int(doc["chat_id"]),
            chat_title=doc.get("chat_title") or "Chat",
            title=doc.get("title") or "",
            status=doc.get("status") or "open",
            priority=doc.get("priority") or "P2",
            summary=doc.get("summary") or "",
            current_action=doc.get("current_action"),
            dependency=doc.get("dependency"),
            guest_affected=bool(doc.get("guest_affected")),
            responsible=doc.get("responsible"),
            message_count=int(doc.get("message_count") or 1),
            source_message_ids=list(doc.get("source_message_ids") or []),
            last_message_link=doc.get("last_message_link"),
            started_at=doc.get("started_at") or "",
            last_update_at=doc.get("last_update_at") or "",
            resolved_at=doc.get("resolved_at"),
        )

    async def get_open_situations(self, chat_id: int, limit: int = 5) -> List[Situation]:
        cursor = self.situations.find({"chat_id": chat_id, "status": {"$in": ["open", "monitoring"]}}).sort("last_update_at", -1).limit(limit)
        return [self._situation(doc) async for doc in cursor]

    async def create_situation(self, msg: IncomingMessage, decision: SituationDecision, cls: PriorityClassification) -> int:
        record_id = await self._next_id()
        now = msg.date.isoformat()
        await self.situations.insert_one(
            {
                "id": record_id,
                "chat_id": msg.chat_id,
                "chat_title": msg.chat_title,
                "title": decision.title or cls.summary[:120] or msg.text[:120],
                "status": decision.status if decision.status != "resolved" else "open",
                "priority": cls.priority,
                "summary": cls.summary,
                "current_action": decision.current_action or cls.action,
                "dependency": decision.dependency,
                "guest_affected": decision.guest_affected,
                "responsible": decision.responsible or cls.category or msg.chat_title,
                "message_count": 1,
                "source_message_ids": [msg.message_id],
                "last_message_link": msg.message_link,
                "started_at": now,
                "last_update_at": now,
                "resolved_at": None,
            }
        )
        return record_id

    async def update_situation(self, situation_id: int, msg: IncomingMessage, decision: SituationDecision, cls: PriorityClassification) -> None:
        existing = await self.situations.find_one({"id": situation_id})
        if not existing:
            return
        source_ids = list(existing.get("source_message_ids") or [])
        if msg.message_id not in source_ids:
            source_ids.append(msg.message_id)
        source_ids = source_ids[-20:]
        status = "resolved" if decision.action == "resolve" else decision.status
        priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        priority = cls.priority if priority_rank.get(cls.priority, 3) < priority_rank.get(existing.get("priority", "P3"), 3) else existing.get("priority")
        update = {
            "status": status,
            "priority": priority,
            "summary": cls.summary,
            "current_action": decision.current_action or cls.action or existing.get("current_action"),
            "dependency": decision.dependency if decision.dependency is not None else existing.get("dependency"),
            "guest_affected": bool(existing.get("guest_affected")) or decision.guest_affected,
            "responsible": decision.responsible or existing.get("responsible"),
            "message_count": int(existing.get("message_count") or 1) + 1,
            "source_message_ids": source_ids,
            "last_message_link": msg.message_link or existing.get("last_message_link"),
            "last_update_at": msg.date.isoformat(),
        }
        if status == "resolved":
            update["resolved_at"] = msg.date.isoformat()
        await self.situations.update_one({"id": situation_id}, {"$set": update})

    async def get_situation(self, situation_id: int) -> Optional[Situation]:
        doc = await self.situations.find_one({"id": situation_id})
        return self._situation(doc) if doc else None

    async def resolve_situation(self, situation_id: int) -> bool:
        """Manual GM override, independent of the message-driven fusion flow."""
        now = datetime.now(timezone.utc).isoformat()
        result = await self.situations.update_one({"id": situation_id}, {"$set": {"status": "resolved", "resolved_at": now}})
        return result.matched_count > 0

    async def list_situations(self, status: Optional[str] = None, limit: int = 20, allowed_chat_ids: Optional[set[int]] = None) -> List[Situation]:
        query: Dict[str, Any] = {"status": status} if status else {"status": {"$in": ["open", "monitoring"]}}
        if allowed_chat_ids is not None:
            query["chat_id"] = {"$in": [int(chat_id) for chat_id in allowed_chat_ids]}
        cursor = self.situations.find(query).sort("last_update_at", -1).limit(max(limit, 50))
        rows = [self._situation(doc) async for doc in cursor]
        priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        # Stable sort: ties keep the last_update_at-descending order from Mongo.
        rows.sort(key=lambda situation: priority_rank.get(situation.priority, 3))
        return rows[:limit]

    async def list_situations_active_since(self, since_iso: str, allowed_chat_ids: Optional[set[int]] = None) -> List[Situation]:
        """Situations with any activity (open or newly resolved) since a given
        instant -- the basis for "what happened today/this week" retrospectives,
        as distinct from list_situations()'s "what's open right now" snapshot."""
        query: Dict[str, Any] = {"$or": [{"last_update_at": {"$gte": since_iso}}, {"started_at": {"$gte": since_iso}}]}
        if allowed_chat_ids is not None:
            query = {"$and": [query, {"chat_id": {"$in": [int(chat_id) for chat_id in allowed_chat_ids]}}]}
        cursor = self.situations.find(query).sort("last_update_at", -1)
        rows = [self._situation(doc) async for doc in cursor]
        priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        rows.sort(key=lambda situation: priority_rank.get(situation.priority, 3))
        return rows
