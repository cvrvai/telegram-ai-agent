"""Durable run/consent records; no source message bodies are stored here."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError


def now():
    return datetime.now(timezone.utc)


class MongoAgentState:
    def __init__(self, db):
        self.runs = db["agent_runs"]
        self.sessions = db["agent_sessions"]
        self.grants = db["agent_source_grants"]

    async def init(self):
        await self.runs.create_index("event_key", unique=True)
        await self.runs.create_index([("actor_id", 1), ("chat_id", 1), ("state", 1)])
        await self.grants.create_index([("actor_id", 1), ("account_id", 1), ("source_id", 1)], unique=True)
        # Single bot process: a previous process cannot still own these runs.
        await self.runs.update_many({"state": {"$in": ["running", "queued", "cancelling"]}}, {"$set": {"state": "interrupted", "updated_at": now()}})

    async def create(self, actor, chat, kind, text, event_key):
        record = {"_id": uuid4().hex[:20], "actor_id": actor, "chat_id": chat,
                  "chat_type": kind, "request": text, "event_key": event_key,
                  "state": "queued", "created_at": now(), "updated_at": now(),
                  "expires_at": now() + timedelta(minutes=30)}
        try:
            await self.runs.insert_one(record)
        except DuplicateKeyError:
            return None
        return record

    async def get(self, run_id):
        return await self.runs.find_one({"_id": run_id})

    async def transition(self, record, expected, state, **fields):
        return await self.runs.find_one_and_update(
            {"_id": record["_id"], "actor_id": record["actor_id"], "chat_id": record["chat_id"], "state": {"$in": list(expected)}},
            {"$set": {"state": state, "updated_at": now(), **fields}}, return_document=ReturnDocument.AFTER)

    async def pending(self, actor, chat):
        return await self.runs.find_one({"actor_id": actor, "chat_id": chat,
            "state": {"$in": ["selecting", "awaiting_approval"]}, "expires_at": {"$gt": now()}}, sort=[("created_at", -1)])

    async def cancel(self, actor, chat):
        result = await self.runs.update_many({"actor_id": actor, "chat_id": chat,
            "state": {"$in": ["running", "queued", "selecting", "awaiting_approval"]}},
            {"$set": {"state": "cancelled", "updated_at": now()}})
        return result.modified_count

    async def load_session(self, actor, chat):
        row = await self.sessions.find_one({"_id": f"{actor}:{chat}"})
        return (row or {}).get("context", {})

    async def save_session(self, actor, chat, context):
        safe = {key: value for key, value in context.items() if not key.startswith("_")}
        await self.sessions.update_one({"_id": f"{actor}:{chat}"}, {"$set": {"context": safe, "updated_at": now()}}, upsert=True)

    async def grant(self, actor, source):
        await self.grants.update_one({"actor_id": actor, "account_id": source["account_id"], "source_id": source["id"]},
            {"$set": {"enabled": True, "source": source, "updated_at": now()}}, upsert=True)

    async def has_grant(self, actor, source):
        return bool(await self.grants.find_one({"actor_id": actor, "account_id": source["account_id"], "source_id": source["id"], "enabled": True}))

    async def list_grants(self, actor):
        return await self.grants.find({"actor_id": actor, "enabled": True}, {"_id": 0}).to_list(length=1000)

    async def revoke(self, actor, account, source_id):
        await self.grants.update_one({"actor_id": actor, "account_id": account, "source_id": source_id}, {"$set": {"enabled": False, "updated_at": now()}})
