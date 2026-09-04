from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class AgentContext:
    actor_id: int
    chat_id: int
    chat_type: str
    assistant_id: str = "business"
    recent_turns: list[dict[str, Any]] = field(default_factory=list)
    relevant_projects: list[dict[str, Any]] = field(default_factory=list)
    relevant_work_items: list[dict[str, Any]] = field(default_factory=list)
    relevant_messages: list[Any] = field(default_factory=list)
    relevant_memory: list[dict[str, Any]] = field(default_factory=list)
    active_project: dict[str, Any] | None = None
    active_work_item: dict[str, Any] | None = None
    session: dict[str, Any] = field(default_factory=dict)
    service: Any = None
    repository: Any = None
    message_database: Any = None
    allowed_chat_ids: set[int] = field(default_factory=set)

    def prompt_lines(self) -> list[str]:
        lines = [f"Actor: {self.actor_id}"]
        lines.append(f"Authorized source chat IDs: {sorted(self.allowed_chat_ids)}")
        if self.active_project:
            lines.append(f"Active project: {self.active_project.get('project_key') or self.active_project.get('name')}")
        if self.active_work_item:
            lines.append(f"Active work item: {self.active_work_item.get('display_key') or self.active_work_item.get('title')}")
        lines.extend(f"Project: {row.get('project_key') or row.get('name')}" for row in self.relevant_projects[:10])
        lines.extend(f"Work: {row.get('display_key') or row.get('title')} [{row.get('status')}]" for row in self.relevant_work_items[:10])
        for row in self.relevant_messages[:10]:
            if hasattr(row, "text"):
                lines.append(f"Message: [{getattr(row, 'chat_title', '')}] {getattr(row, 'text', '')[:240]}")
            elif isinstance(row, dict):
                lines.append(f"Message: [{row.get('chat_title', '')}] {str(row.get('text', ''))[:240]}")
        lines.extend(f"Memory: {row.get('content', '')[:240]}" for row in self.relevant_memory[:6])
        return lines


class ContextRetriever:
    def __init__(self, assistant_service: Any, repository: Any, message_database: Any = None, allowed_chat_ids_provider: Callable[[], set[int]] | None = None) -> None:
        self.service = assistant_service
        self.repository = repository
        self.message_database = message_database
        self.allowed_chat_ids_provider = allowed_chat_ids_provider

    async def retrieve(self, actor_id: int, chat_id: int, chat_type: str, query: str, session: dict[str, Any] | None = None) -> AgentContext:
        context = AgentContext(actor_id, chat_id, chat_type, session=session or {}, service=self.service, repository=self.repository, message_database=self.message_database)
        if self.allowed_chat_ids_provider is not None:
            context.allowed_chat_ids = {int(value) for value in self.allowed_chat_ids_provider()}
        conversation_id = await self.repository.create_or_get_conversation("business", actor_id, chat_id)
        context.recent_turns = await self.repository.recent_turns(conversation_id, limit=8)
        context.relevant_projects = await self.service.list_projects(actor_id, chat_id, chat_type)
        context.relevant_work_items = await self.service.list_work_items(actor_id, chat_id, chat_type, "open", 20)
        if self.message_database is not None and context.allowed_chat_ids and hasattr(self.message_database, "get_recent_messages"):
            context.relevant_messages = await self.message_database.get_recent_messages(limit=20, allowed_chat_ids=context.allowed_chat_ids)
        active_project_id = context.session.get("active_project_id")
        active_work_item_id = context.session.get("active_work_item_id")
        if active_project_id:
            context.active_project = next((p for p in context.relevant_projects if str(p.get("id")) == str(active_project_id)), None)
        if active_work_item_id:
            context.active_work_item = await self.service.get_work_item(actor_id, chat_id, chat_type, active_work_item_id)
        if query.strip():
            context.relevant_memory = await self.service.search_memory(actor_id, chat_id, chat_type, query, 6)
        return context


