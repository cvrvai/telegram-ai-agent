"""MongoDB-backed business-assistant storage.

This repository implements the transport-independent business service contract
for conversations, access, projects, work items, and AI usage. SQLite remains
only as a migration and test compatibility adapter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import secrets
from typing import Any, Dict, List, Optional
from app.projects.workflow import normalize_status, validate_priority, validate_work_item_type, can_transition

from motor.motor_asyncio import AsyncIOMotorClient


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MongoBusinessRepository:
    """Mongo implementation of the business repository contract."""

    def __init__(self, uri: str, database: str = "telegram_business") -> None:
        self.client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5_000)
        self.db = self.client[database]
        self.users = self.db["assistant_users"]
        self.profiles = self.db["assistant_profiles"]
        self.memberships = self.db["assistant_memberships"]
        self.conversations = self.db["assistant_conversations"]
        self.turns = self.db["assistant_turns"]
        self.pairings = self.db["assistant_pairings"]
        self.groups = self.db["assistant_groups"]
        self.focus = self.db["assistant_focus"]
        self.usage = self.db["ai_usage"]
        self.tasks = self.db["assistant_tasks"]
        self.reminders = self.db["assistant_reminders"]
        self.actions = self.db["assistant_actions"]
        self.departments = self.db["assistant_departments"]
        self.projects = self.db["assistant_projects"]
        self.project_members = self.db["assistant_project_members"]
        self.dependencies = self.db["assistant_task_dependencies"]
        self.milestones = self.db["assistant_milestones"]
        self.comments = self.db["assistant_task_comments"]
        self.status_history = self.db["work_item_status_history"]
        self.agent_audits = self.db["agent_audit_events"]
        self.google_tokens = self.db["assistant_google_tokens"]
        self.google_oauth_states = self.db["assistant_google_oauth_states"]

    async def init(self) -> None:
        """Verify connectivity and create the indexes used by the service."""
        await self.client.admin.command("ping")
        await self.users.create_index("user_id", unique=True)
        await self.profiles.create_index("assistant_id", unique=True)
        await self.memberships.create_index([("assistant_id", 1), ("user_id", 1)], unique=True)
        await self.conversations.create_index([("assistant_id", 1), ("user_id", 1), ("chat_id", 1), ("updated_at", -1)])
        await self.turns.create_index([("conversation_id", 1), ("created_at", -1)])
        await self.pairings.create_index("code", unique=True)
        await self.pairings.create_index([("status", 1), ("created_at", 1)])
        await self.usage.create_index([("period", 1), ("assistant_id", 1), ("feature", 1)])
        await self.tasks.create_index([("user_id", 1), ("status", 1), ("created_at", -1)])
        await self.reminders.create_index([("status", 1), ("remind_at", 1)])
        await self.actions.create_index([("status", 1), ("expires_at", 1)])
        await self.focus.create_index([("assistant_id", 1), ("chat_id", 1)], unique=True)
        await self.focus.create_index([("assistant_id", 1), ("enabled", 1)])
        await self.projects.create_index([("user_id", 1), ("status", 1), ("updated_at", -1)])
        await self.projects.create_index("project_key", unique=True, sparse=True)
        await self.tasks.create_index([("project_id", 1), ("status", 1)])
        await self.tasks.create_index("display_key", unique=True, sparse=True)
        await self.status_history.create_index([("work_item_id", 1), ("created_at", 1)])
        await self.google_tokens.create_index("user_id", unique=True)
        await self.google_oauth_states.create_index("state", unique=True)
        await self.google_oauth_states.create_index("expires_at")
        await self.agent_audits.create_index([("actor_id", 1), ("created_at", -1)])
        await self.profiles.update_one(
            {"assistant_id": "business"},
            {
                "$setOnInsert": {
                    "assistant_id": "business",
                    "name": "Business Assistant",
                    "instructions": "Answer business questions clearly and use only authorized context.",
                    "created_at": _now(),
                    "updated_at": _now(),
                }
            },
            upsert=True,
        )

    async def close(self) -> None:
        self.client.close()

    async def upsert_user(
        self,
        user_id: int,
        display_name: str = "",
        role: str = "user",
        approved: bool = False,
    ) -> None:
        now = _now()
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "display_name": display_name,
                    "role": role,
                    "approved": bool(approved),
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    async def mark_user_seen(self, user_id: int) -> None:
        now = _now()
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"approved": True, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    async def create_pairing(self, user_id: int, display_name: str = "") -> str:
        code = secrets.token_hex(3).upper()
        now = datetime.now(timezone.utc)
        await self.pairings.insert_one(
            {
                "code": code,
                "user_id": user_id,
                "display_name": display_name,
                "status": "pending",
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=24)).isoformat(),
            }
        )
        await self.upsert_user(user_id, display_name=display_name, approved=False)
        return code

    async def resolve_pairing(self, code: str, approved: bool) -> Optional[int]:
        normalized = code.strip().upper()
        pairing = await self.pairings.find_one({"code": normalized, "status": "pending"})
        if not pairing:
            return None
        now = _now()
        if pairing.get("expires_at") and pairing["expires_at"] < now:
            await self.pairings.update_one(
                {"_id": pairing["_id"], "status": "pending"},
                {"$set": {"status": "expired", "resolved_at": now}},
            )
            return None
        result = await self.pairings.update_one(
            {"_id": pairing["_id"], "status": "pending"},
            {"$set": {"status": "approved" if approved else "denied", "resolved_at": now}},
        )
        if result.modified_count != 1:
            return None
        await self.users.update_one(
            {"user_id": int(pairing["user_id"])},
            {"$set": {"approved": bool(approved), "updated_at": now}},
            upsert=True,
        )
        return int(pairing["user_id"])

    async def pending_pairings(self) -> List[Dict[str, Any]]:
        cursor = self.pairings.find({"status": "pending"}, {"_id": 0}).sort("created_at", 1)
        return [doc async for doc in cursor]

    async def create_or_get_conversation(
        self,
        assistant_id: str,
        user_id: int,
        chat_id: int,
        title: str = "",
    ) -> str:
        conversation = await self.conversations.find_one(
            {"assistant_id": assistant_id, "user_id": user_id, "chat_id": chat_id},
            sort=[("updated_at", -1)],
        )
        if conversation:
            await self.conversations.update_one({"_id": conversation["_id"]}, {"$set": {"updated_at": _now()}})
            return str(conversation["_id"])
        conversation_id = secrets.token_hex(12)
        now = _now()
        await self.conversations.insert_one(
            {
                "_id": conversation_id,
                "assistant_id": assistant_id,
                "user_id": user_id,
                "chat_id": chat_id,
                "title": title,
                "created_at": now,
                "updated_at": now,
            }
        )
        return conversation_id

    async def add_turn(
        self,
        conversation_id: str,
        role: str,
        content: str,
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        now = _now()
        await self.turns.insert_one(
            {
                "conversation_id": str(conversation_id),
                "role": role,
                "content": content,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "created_at": now,
            }
        )
        await self.conversations.update_one({"_id": str(conversation_id)}, {"$set": {"updated_at": now}})

    async def recent_turns(self, conversation_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.turns.find(
            {"conversation_id": str(conversation_id)},
            {"_id": 0, "role": 1, "content": 1, "model": 1, "input_tokens": 1, "output_tokens": 1, "created_at": 1},
        ).sort("created_at", -1).limit(limit)
        rows = [doc async for doc in cursor]
        rows.reverse()
        return rows

    async def search_turns(self, user_id: int, assistant_id: str, query: str, limit: int = 20, chat_id: int | None = None) -> List[Dict[str, Any]]:
        """Search saved assistant turns within the caller's own conversations."""
        conversations = self.conversations.find(
            {"assistant_id": assistant_id, "user_id": user_id, **({"chat_id": chat_id} if chat_id is not None else {})},
            {"_id": 1, "chat_id": 1, "title": 1},
        )
        conversation_ids = [doc["_id"] async for doc in conversations]
        if not conversation_ids:
            return []
        cursor = self.turns.find(
            {"conversation_id": {"$in": [str(value) for value in conversation_ids]}, "content": {"$regex": re.escape(query), "$options": "i"}},
            {"_id": 0, "role": 1, "content": 1, "model": 1, "created_at": 1, "conversation_id": 1},
        ).sort("created_at", -1).limit(limit)
        rows = [doc async for doc in cursor]
        by_id = {str(doc["_id"]): doc for doc in await self.conversations.find({"_id": {"$in": conversation_ids}}).to_list(None)}
        for row in rows:
            conversation = by_id.get(str(row.get("conversation_id")), {})
            row["chat_id"] = conversation.get("chat_id")
            row["title"] = conversation.get("title", "")
            row.pop("conversation_id", None)
        return rows

    async def clear_conversation(self, user_id: int, chat_id: int, assistant_id: str = "business") -> int:
        """Delete one assistant conversation and its turns."""
        conversations = await self.conversations.find(
            {"assistant_id": assistant_id, "user_id": user_id, "chat_id": chat_id},
            {"_id": 1},
        ).to_list(None)
        ids = [str(doc["_id"]) for doc in conversations]
        if ids:
            await self.turns.delete_many({"conversation_id": {"$in": ids}})
            await self.conversations.delete_many({"_id": {"$in": ids}})
        return len(ids)

    async def clear_user_memory(self, user_id: int, assistant_id: Optional[str] = None) -> int:
        """Delete all saved assistant conversations for one user."""
        query: Dict[str, Any] = {"user_id": user_id}
        if assistant_id:
            query["assistant_id"] = assistant_id
        conversations = await self.conversations.find(query, {"_id": 1}).to_list(None)
        ids = [str(doc["_id"]) for doc in conversations]
        if ids:
            await self.turns.delete_many({"conversation_id": {"$in": ids}})
            await self.conversations.delete_many({"_id": {"$in": ids}})
        return len(ids)

    async def purge_turns_older_than(self, cutoff_iso: str) -> int:
        """Delete old turns and empty conversations for retention enforcement."""
        result = await self.turns.delete_many({"created_at": {"$lt": cutoff_iso}})
        conversation_ids = await self.turns.distinct("conversation_id")
        await self.conversations.delete_many({"_id": {"$nin": conversation_ids}})
        return int(result.deleted_count)

    async def create_task(self, user_id: int, title: str, details: str = "", due_at: Optional[str] = None, project_id: Optional[Any] = None, assignee_id: Optional[int] = None, priority: str = "P2", duration_days: int = 1, item_type: str = "task", parent_id: Optional[Any] = None, reporter_id: Optional[int] = None, source_chat_id: Optional[int] = None, source_message_id: Optional[int] = None, source_message_url: Optional[str] = None, ai_confidence: float = 0.0) -> str:
        priority = validate_priority(priority); item_type = validate_work_item_type(item_type); now = _now()
        display_key = None; sequence = None
        if project_id is not None:
            project = await self._project_for_user(user_id, project_id)
            if not project: raise ValueError("Project not found or not owned by this user")
            if parent_id is not None:
                parent = await self._find_task(parent_id)
                if not parent or parent.get("project_id") != project.get("_id") and parent.get("project_id") != project_id: raise ValueError("Parent work item must belong to the same project")
            sequence = int(project.get("next_sequence") or 1)
            display_key = f"{project.get('project_key') or 'PROJ'}-{sequence}"
            await self.projects.update_one({"_id": project["_id"]}, {"$set": {"next_sequence": sequence + 1, "updated_at": now}})
        result = await self.tasks.insert_one({"user_id": user_id, "project_id": project_id, "assignee_id": assignee_id, "title": title, "details": details, "status": "todo", "priority": priority, "duration_days": max(1, duration_days), "due_at": due_at, "created_at": now, "updated_at": now, "completed_at": None, "parent_id": parent_id, "sequence_number": sequence, "display_key": display_key, "type": item_type, "reporter_id": reporter_id or user_id, "remind_at": due_at, "rank": sequence or 0, "source_chat_id": source_chat_id, "source_message_id": source_message_id, "source_message_url": source_message_url, "ai_confidence": max(0.0, min(1.0, float(ai_confidence)))})
        return str(result.inserted_id)

    async def list_tasks(self, user_id: int, status: str = "open", limit: int = 20, project_id: Optional[Any] = None, assignee_id: Optional[int] = None) -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {"$or": [{"user_id": user_id}, {"assignee_id": user_id}]}
        if status == "open": query["status"] = {"$in": ["open", "to_do", "todo", "in_progress", "blocked", "waiting", "review"]}
        elif status != "all": query["status"] = normalize_status(status)
        if project_id is not None: query["project_id"] = project_id
        if assignee_id is not None: query["assignee_id"] = assignee_id
        cursor = self.tasks.find(query).sort([("rank", -1), ("created_at", -1)]).limit(limit)
        rows = [doc async for doc in cursor]
        for row in rows:
            row["id"] = str(row.pop("_id"))
        return rows

    async def _find_task(self, task_id: Any) -> Optional[Dict[str, Any]]:
        from bson import ObjectId
        try: key: Any = ObjectId(str(task_id))
        except Exception: key = None
        query = {"_id": key} if key is not None else {"display_key": str(task_id)}
        return await self.tasks.find_one(query)

    async def _project_for_user(self, user_id: int, project_id: Any) -> Optional[Dict[str, Any]]:
        from bson import ObjectId
        try: key: Any = ObjectId(str(project_id))
        except Exception: key = project_id
        return await self.projects.find_one({"_id": key, "user_id": user_id})

    async def get_work_item(self, user_id: int, item_id: Any) -> Optional[Dict[str, Any]]:
        row = await self._find_task(item_id)
        if not row or (row.get("user_id") != user_id and row.get("assignee_id") != user_id): return None
        row["id"] = str(row.pop("_id")); return row

    async def list_work_items(self, user_id: int, status: str = "all", limit: int = 50, offset: int = 0, project_id: Optional[Any] = None, assignee_id: Optional[int] = None, due_before: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = await self.list_tasks(user_id, status, limit + offset, project_id, assignee_id)
        if due_before: rows = [row for row in rows if row.get("due_at") and row["due_at"] <= due_before]
        return rows[offset:offset + limit]

    async def complete_task(self, user_id: int, task_id: str) -> bool:
        row = await self._find_task(task_id)
        if not row or (row.get("user_id") != user_id and row.get("assignee_id") != user_id): return False
        key = row["_id"]
        result = await self.tasks.update_one({"$or": [{"user_id": user_id}, {"assignee_id": user_id}], "status": {"$in": ["open", "to_do", "todo", "in_progress", "blocked", "waiting", "review"]}, "_id": key}, {"$set": {"status": "done", "completed_at": _now(), "updated_at": _now()}})
        return result.modified_count == 1

    async def create_reminder(self, user_id: int, text: str, remind_at: str) -> str:
        result = await self.reminders.insert_one({"user_id": user_id, "text": text, "remind_at": remind_at, "status": "pending", "created_at": _now(), "delivered_at": None})
        return str(result.inserted_id)

    async def list_reminders(self, user_id: int, status: str = "pending", limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.reminders.find({"user_id": user_id, "status": status}, {"_id": 1, "text": 1, "remind_at": 1, "status": 1, "created_at": 1}).sort("remind_at", 1).limit(limit)
        rows = [doc async for doc in cursor]
        for row in rows:
            row["id"] = str(row.pop("_id"))
        return rows

    async def claim_due_reminders(self, now_iso: str, limit: int = 50) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for _ in range(limit):
            row = await self.reminders.find_one_and_update({"status": "pending", "remind_at": {"$lte": now_iso}}, {"$set": {"status": "delivered", "delivered_at": _now()}}, sort=[("remind_at", 1)])
            if not row:
                break
            row["id"] = str(row.pop("_id"))
            rows.append(row)
        return rows

    async def create_action(self, user_id: int, action_type: str, target: str, payload: str, expires_at: str) -> str:
        result = await self.actions.insert_one({"user_id": user_id, "action_type": action_type, "target": target, "payload": payload, "status": "pending", "created_at": _now(), "expires_at": expires_at, "resolved_at": None})
        return str(result.inserted_id)

    async def resolve_action(self, action_id: str, user_id: int, status: str) -> Optional[Dict[str, Any]]:
        from bson import ObjectId
        try:
            key: Any = ObjectId(action_id)
        except Exception:
            key = action_id
        action = await self.actions.find_one({"_id": key, "user_id": user_id, "status": "pending"})
        if not action or action.get("expires_at", "") < _now():
            return None
        result = await self.actions.update_one({"_id": key, "status": "pending"}, {"$set": {"status": status, "resolved_at": _now()}})
        if result.modified_count != 1:
            return None
        action["id"] = str(action.pop("_id"))
        return action

    async def list_actions(self, status: str = "pending", limit: int = 100) -> List[Dict[str, Any]]:
        cursor = self.actions.find({"status": status}, {"_id": 1, "user_id": 1, "action_type": 1, "target": 1, "status": 1, "created_at": 1, "expires_at": 1, "resolved_at": 1}).sort("created_at", -1).limit(limit)
        rows = [doc async for doc in cursor]
        for row in rows:
            row["id"] = str(row.pop("_id"))
        return rows

    async def save_google_token(self, user_id: int, token_json: str, scopes: str) -> None:
        await self.google_tokens.update_one(
            {"user_id": user_id},
            {"$set": {"token_json": token_json, "scopes": scopes, "updated_at": _now()}},
            upsert=True,
        )

    async def get_google_token(self, user_id: int) -> Optional[Dict[str, Any]]:
        return await self.google_tokens.find_one({"user_id": user_id})

    async def delete_google_token(self, user_id: int) -> None:
        await self.google_tokens.delete_one({"user_id": user_id})

    async def create_google_oauth_state(self, user_id: int, ttl_minutes: int = 10) -> str:
        state = secrets.token_urlsafe(24)
        expires = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat()
        await self.google_oauth_states.insert_one({"state": state, "user_id": user_id, "created_at": _now(), "expires_at": expires})
        return state

    async def attach_google_oauth_verifier(self, state: str, code_verifier: Optional[str]) -> None:
        await self.google_oauth_states.update_one({"state": state}, {"$set": {"code_verifier": code_verifier}})

    async def consume_google_oauth_state(self, state: str) -> Optional[Dict[str, Any]]:
        row = await self.google_oauth_states.find_one_and_delete({"state": state})
        if not row or row["expires_at"] < _now():
            return None
        return {"user_id": int(row["user_id"]), "code_verifier": row.get("code_verifier")}

    async def record_usage(
        self,
        period: str,
        assistant_id: str,
        feature: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        user_id: Optional[int] = None,
        model: str = "",
    ) -> None:
        await self.usage.insert_one(
            {
                "period": period,
                "assistant_id": assistant_id,
                "user_id": user_id,
                "feature": feature,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": cost_usd,
                "created_at": _now(),
            }
        )

    async def usage_total(self, period: str) -> float:
        cursor = self.usage.find({"period": period}, {"estimated_cost_usd": 1})
        total = 0.0
        async for row in cursor:
            total += float(row.get("estimated_cost_usd") or 0.0)
        return total

    async def list_users(self, limit: int = 100) -> List[Dict[str, Any]]:
        cursor = self.users.find({}, {"_id": 0}).sort("updated_at", -1).limit(limit)
        return [doc async for doc in cursor]

    async def set_user_approval(self, user_id: int, approved: bool) -> None:
        await self.users.update_one({"user_id": user_id}, {"$set": {"approved": bool(approved), "updated_at": _now()}}, upsert=True)

    async def list_profiles(self) -> List[Dict[str, Any]]:
        cursor = self.profiles.find({}, {"_id": 0}).sort("assistant_id", 1)
        return [doc async for doc in cursor]

    async def upsert_profile(self, assistant_id: str, name: str, instructions: str = "", enabled: bool = True) -> None:
        await self.profiles.update_one({"assistant_id": assistant_id}, {"$set": {"name": name, "instructions": instructions, "enabled": bool(enabled), "updated_at": _now()}, "$setOnInsert": {"created_at": _now()}}, upsert=True)

    async def list_groups(self, limit: int = 100) -> List[Dict[str, Any]]:
        cursor = self.groups.find({}, {"_id": 0}).sort("updated_at", -1).limit(limit)
        return [doc async for doc in cursor]

    async def list_focus_scopes(self, assistant_id: str = "business", enabled_only: bool = True) -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {"assistant_id": assistant_id}
        if enabled_only:
            query["enabled"] = True
        cursor = self.focus.find(query, {"_id": 0}).sort("chat_title", 1)
        return [doc async for doc in cursor]

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
        await self.focus.update_one(
            {"assistant_id": assistant_id, "chat_id": int(chat_id)},
            {
                "$set": {
                    "chat_title": chat_title,
                    "chat_type": chat_type,
                    "mode": mode,
                    "enabled": bool(enabled),
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    async def disable_focus_scope(self, assistant_id: str, chat_id: int) -> None:
        await self.focus.update_one(
            {"assistant_id": assistant_id, "chat_id": int(chat_id)},
            {"$set": {"enabled": False, "updated_at": _now()}},
        )

    async def create_department(self, name: str, description: str = "") -> str:
        now = _now()
        await self.departments.update_one({"name": name}, {"$set": {"description": description, "enabled": True, "updated_at": now}, "$setOnInsert": {"created_at": now}}, upsert=True)
        row = await self.departments.find_one({"name": name}, {"_id": 1})
        return str(row["_id"])

    async def list_departments(self) -> List[Dict[str, Any]]:
        cursor = self.departments.find({}, {"_id": 1, "name": 1, "description": 1, "enabled": 1}).sort("name", 1)
        rows = [doc async for doc in cursor]
        for row in rows: row["id"] = str(row.pop("_id"))
        return rows

    async def create_project(self, user_id: int, name: str, description: str = "", department_id: Optional[Any] = None, start_date: Optional[str] = None, target_date: Optional[str] = None) -> str:
        now = _now()
        base = re.sub(r"[^A-Za-z0-9]+", "", (name or "").upper())[:10] or "PROJ"
        key = base; suffix = 2
        while await self.projects.find_one({"project_key": key}, {"_id": 1}):
            tail = str(suffix); key = f"{base[:max(1, 10-len(tail))]}{tail}"; suffix += 1
        result = await self.projects.insert_one({"user_id": user_id, "department_id": department_id, "name": name, "description": description, "status": "active", "project_key": key, "next_sequence": 1, "start_date": start_date, "target_date": target_date, "created_at": now, "updated_at": now})
        return str(result.inserted_id)

    async def list_projects(self, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        memberships = await self.project_members.find({"user_id": user_id}, {"project_id": 1}).to_list(None)
        ids = [row["project_id"] for row in memberships]
        query = {"$or": [{"user_id": user_id}, {"_id": {"$in": ids}}]}
        cursor = self.projects.find(query, {"_id": 1, "project_key": 1, "name": 1, "description": 1, "status": 1, "start_date": 1, "target_date": 1, "created_at": 1, "updated_at": 1, "next_sequence": 1}).sort("updated_at", -1).limit(limit)
        rows = [doc async for doc in cursor]
        for row in rows: row["id"] = str(row.pop("_id"))
        return rows

    async def get_project(self, user_id: int, project_id: Any) -> Optional[Dict[str, Any]]:
        from bson import ObjectId
        try: key: Any = ObjectId(str(project_id))
        except Exception: key = project_id
        row = await self.projects.find_one({"_id": key, "$or": [{"user_id": user_id}, {"_id": {"$in": await self.project_members.distinct("project_id", {"user_id": user_id})}}]})
        if not row: return None
        row["id"] = str(row.pop("_id")); return row

    async def find_projects(self, user_id: int, hint: str, limit: int = 10) -> List[Dict[str, Any]]:
        rows = await self.list_projects(user_id, 100)
        needle = (hint or "").casefold()
        return [row for row in rows if needle in str(row.get("name", "")).casefold() or needle in str(row.get("project_key", "")).casefold()][:limit]

    async def add_project_member(self, project_id: Any, user_id: int, role: str = "member") -> None:
        await self.project_members.update_one({"project_id": project_id, "user_id": user_id}, {"$set": {"role": role}}, upsert=True)

    async def update_task(self, user_id: int, task_id: str, status: Optional[str] = None, assignee_id: Optional[int] = None, priority: Optional[str] = None, due_at: Optional[str] = None) -> bool:
        row = await self._find_task(task_id)
        if not row or (row.get("user_id") != user_id and row.get("assignee_id") != user_id): return False
        key = row["_id"]
        previous_status = None
        if status is not None:
            status = normalize_status(status)
            current = await self.tasks.find_one({"_id": key}, {"status": 1})
            if not current: return False
            previous_status = normalize_status(current.get("status"))
            if not can_transition(previous_status, status): raise ValueError(f"Cannot move work item from {previous_status} to {status}")
        if priority is not None: priority = validate_priority(priority)
        changes = {key_name: value for key_name, value in (("status", status), ("assignee_id", assignee_id), ("priority", priority), ("due_at", due_at), ("updated_at", _now())) if value is not None}
        if not changes: return False
        result = await self.tasks.update_one({"_id": key, "$or": [{"user_id": user_id}, {"assignee_id": user_id}]}, {"$set": changes})
        if result.modified_count == 1 and status is not None and previous_status != status:
            await self.status_history.insert_one({"work_item_id": str(task_id), "from_status": previous_status, "to_status": status, "changed_by": user_id, "created_at": _now()})
        return result.modified_count == 1

    async def record_agent_audit(self, *, actor_id: int, tool_name: str, arguments_summary: Dict[str, Any], policy_result: str, execution_result: Any = None, target: Any = None) -> None:
        await self.agent_audits.insert_one({
            "actor_id": int(actor_id),
            "tool_name": tool_name,
            "arguments": arguments_summary,
            "target": target,
            "policy_result": policy_result,
            "execution_result": execution_result,
            "created_at": _now(),
        })

    async def add_dependency(self, task_id: Any, predecessor_id: Any) -> None:
        await self.dependencies.update_one({"task_id": task_id, "predecessor_id": predecessor_id}, {"$set": {"task_id": task_id, "predecessor_id": predecessor_id}}, upsert=True)

    async def add_milestone(self, project_id: Any, name: str, target_date: str) -> str:
        result = await self.milestones.insert_one({"project_id": project_id, "name": name, "target_date": target_date, "status": "open", "created_at": _now()})
        return str(result.inserted_id)

    async def add_task_comment(self, task_id: Any, user_id: int, comment: str) -> str:
        result = await self.comments.insert_one({"task_id": task_id, "user_id": user_id, "comment": comment, "created_at": _now()})
        return str(result.inserted_id)

    async def project_plan_data(self, user_id: int, project_id: str) -> Dict[str, Any]:
        from bson import ObjectId
        try: key: Any = ObjectId(project_id)
        except Exception: key = project_id
        project = await self.projects.find_one({"_id": key, "user_id": user_id}, {"_id": 1, "project_key": 1, "name": 1, "description": 1, "status": 1, "start_date": 1, "target_date": 1})
        if not project: return {}
        tasks = [doc async for doc in self.tasks.find({"project_id": {"$in": [key, project_id]}}, {"_id": 1, "user_id": 1, "project_id": 1, "assignee_id": 1, "title": 1, "details": 1, "status": 1, "priority": 1, "duration_days": 1, "due_at": 1, "display_key": 1, "type": 1, "parent_id": 1})]
        for task in tasks: task["id"] = str(task.pop("_id"))
        task_keys = [task["id"] for task in tasks]
        dependencies = [doc async for doc in self.dependencies.find({"task_id": {"$in": task_keys}}, {"_id": 0, "task_id": 1, "predecessor_id": 1})]
        milestones = [doc async for doc in self.milestones.find({"project_id": {"$in": [key, project_id]}}, {"_id": 0, "name": 1, "target_date": 1, "status": 1})]
        project["id"] = str(project.pop("_id"))
        return {"project": project, "tasks": tasks, "dependencies": dependencies, "milestones": milestones}
