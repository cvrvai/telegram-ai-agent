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
    telegram: Any = None
    google_account: Any = None
    impact_label: str = "Critical impact"
    chat_names: dict = field(default_factory=dict)

    def history_messages(self) -> list[dict[str, Any]]:
        """Conversation turns as role-tagged messages for providers that take
        real multi-turn input, rather than a flattened reference blob."""
        return [{"role": row.get("role", "user"), "content": str(row.get("content", ""))[:2000]} for row in self.recent_turns[-8:]]

    def prompt_lines(self, include_turns: bool = True) -> list[str]:
        lines = [f"Actor: {self.actor_id}"]
        if self.chat_names:
            named = ", ".join(f"{title} (id {cid})" for cid, title in sorted(self.chat_names.items(), key=lambda kv: kv[1]))
            lines.append(f"Authorized sources: {named}")
            lines.append("Always refer to a chat by its name. Never show a numeric chat id to the user.")
        else:
            lines.append(f"Authorized source chat IDs: {sorted(self.allowed_chat_ids)}")
        if self.session.get("last_source_hint"):
            lines.append(f"Last selected Telegram source: {self.session['last_source_hint']}")
        if self.session.get("last_window"):
            lines.append(f"Last requested date window: {self.session['last_window']}")
        if include_turns:
            # Only for providers fed a single flat context block; the native
            # path passes these as real messages via history_messages().
            lines.extend(f"Previous {row.get('role', 'user')}: {str(row.get('content', ''))[:2000]}" for row in self.recent_turns[-8:])
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
    def __init__(self, assistant_service: Any, repository: Any, message_database: Any = None, allowed_chat_ids_provider: Callable[[], set[int]] | None = None, google_account: Any = None, chat_names_provider: Callable[[], dict] | None = None) -> None:
        self.service = assistant_service
        self.repository = repository
        self.message_database = message_database
        self.allowed_chat_ids_provider = allowed_chat_ids_provider
        self.google_account = google_account
        self.chat_names_provider = chat_names_provider

    async def retrieve(self, actor_id: int, chat_id: int, chat_type: str, query: str, session: dict[str, Any] | None = None) -> AgentContext:
        try:
            from config import config as _cfg
            impact_label = _cfg.profile.business.impact_label
        except Exception:
            impact_label = "Critical impact"
        context = AgentContext(actor_id, chat_id, chat_type, session=session if session is not None else {}, service=self.service, repository=self.repository, message_database=self.message_database, google_account=self.google_account,
                               impact_label=impact_label)
        # Owner-selected sources are not grants to other bot members or groups.
        if self.allowed_chat_ids_provider is not None and actor_id == self.service.access.owner_id and chat_type == "private":
            context.allowed_chat_ids = {int(value) for value in self.allowed_chat_ids_provider()}
            if self.chat_names_provider is not None:
                context.chat_names = {int(k): v for k, v in self.chat_names_provider().items()}
        conversation_id = await self.repository.create_or_get_conversation("business", actor_id, chat_id)
        context.recent_turns = await self.repository.recent_turns(conversation_id, limit=8)
        # Business and Telegram data are retrieved by explicit tools on demand.
        active_project_id = context.session.get("active_project_id")
        active_work_item_id = context.session.get("active_work_item_id")
        if active_project_id:
            context.active_project = next((p for p in context.relevant_projects if str(p.get("id")) == str(active_project_id)), None)
        if active_work_item_id:
            context.active_work_item = await self.service.get_work_item(actor_id, chat_id, chat_type, active_work_item_id)
        if query.strip():
            context.relevant_memory = await self.service.search_memory(actor_id, chat_id, chat_type, query, 6)
        return context


