from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Resolution:
    status: str
    value: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] | None = None


def _match(rows: list[dict[str, Any]], hint: str, fields: tuple[str, ...]) -> Resolution:
    needle = (hint or "").strip().casefold()
    if not needle:
        return Resolution("NOT_FOUND")
    exact = [row for row in rows if any(str(row.get(field, "")).casefold() == needle for field in fields)]
    if len(exact) == 1:
        return Resolution("RESOLVED", exact[0], exact)
    matches = exact or [row for row in rows if any(needle in str(row.get(field, "")).casefold() for field in fields)]
    if len(matches) == 1:
        return Resolution("RESOLVED", matches[0], matches)
    if matches:
        return Resolution("AMBIGUOUS", candidates=matches[:10])
    return Resolution("NOT_FOUND")


async def resolve_project(service: Any, actor_id: int, chat_id: int, chat_type: str, hint: str) -> Resolution:
    rows = await service.list_projects(actor_id, chat_id, chat_type)
    return _match(rows, hint, ("id", "project_key", "name"))


async def resolve_work_item(service: Any, actor_id: int, chat_id: int, chat_type: str, hint: str, active: dict[str, Any] | None = None) -> Resolution:
    if active and hint.strip().casefold() in {"it", "that task", "this task", "this issue", "that issue"}:
        return Resolution("RESOLVED", active, [active])
    rows = await service.list_work_items(actor_id, chat_id, chat_type, "all", 100)
    return _match(rows, hint, ("id", "display_key", "title"))


async def resolve_person(service: Any, actor_id: int, hint: str) -> Resolution:
    """Resolve an assignee from the existing approved user directory."""
    repository = getattr(service, "repository", None)
    if repository is None or not hasattr(repository, "list_users"):
        return Resolution("NOT_FOUND")
    rows = [row for row in await repository.list_users(100) if bool(row.get("approved")) or int(row.get("user_id") or row.get("id") or -1) == int(actor_id)]
    return _match(rows, hint, ("user_id", "display_name", "username", "name"))


