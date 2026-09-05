from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from .registry import ToolRegistry
from .resolution import resolve_person, resolve_project, resolve_work_item
from .schemas import RiskLevel, ToolDefinition


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LimitInput(_Input):
    limit: int = Field(default=20, ge=1, le=100)


class HintInput(_Input):
    hint: str = Field(min_length=1, max_length=200, validation_alias=AliasChoices("hint", "query", "name", "text"))


class IdInput(_Input):
    work_item_id: str = Field(min_length=1, max_length=80)


class StatusInput(IdInput):
    status: str = Field(min_length=2, max_length=30)


class PriorityInput(IdInput):
    priority: str = Field(min_length=2, max_length=10)


class DueInput(IdInput):
    due_at: str = Field(min_length=4, max_length=80)


class CommentInput(IdInput):
    comment: str = Field(min_length=1, max_length=2000)


class AssignmentInput(IdInput):
    assignee: str = Field(min_length=1, max_length=200)


class BriefInput(_Input):
    period: Literal["now", "today", "yesterday", "week", "month"] = Field(
        default="now",
        description="'now' for the current critical/pending situations, or a retrospective window.",
    )


class JsonOutput(BaseModel):
    # Preserve handler payload keys instead of silently dropping them.
    model_config = ConfigDict(extra="allow")


def _service_context(context: Any) -> tuple[Any, int, int, str]:
    return context.service, context.actor_id, context.chat_id, context.chat_type


async def list_projects(args: LimitInput, context: Any) -> Any:
    service, actor, chat_id, chat_type = _service_context(context)
    rows = await service.list_projects(actor, chat_id, chat_type)
    return {"projects": rows[: args.limit]}


async def find_project(args: HintInput, context: Any) -> Any:
    service, actor, chat_id, chat_type = _service_context(context)
    return {"projects": await service.find_projects(actor, chat_id, chat_type, args.hint)}


async def get_project(args: HintInput, context: Any) -> Any:
    service, actor, chat_id, chat_type = _service_context(context)
    resolution = await resolve_project(service, actor, chat_id, chat_type, args.hint)
    return {"resolution": resolution.status, "project": resolution.value, "candidates": resolution.candidates or []}


async def find_work_item(args: HintInput, context: Any) -> Any:
    service, actor, chat_id, chat_type = _service_context(context)
    resolution = await resolve_work_item(service, actor, chat_id, chat_type, args.hint, context.active_work_item)
    return {"resolution": resolution.status, "work_item": resolution.value, "candidates": resolution.candidates or []}


async def get_work_item(args: IdInput, context: Any) -> Any:
    service, actor, chat_id, chat_type = _service_context(context)
    return {"work_item": await service.get_work_item(actor, chat_id, chat_type, args.work_item_id)}


async def get_my_work(args: LimitInput, context: Any) -> Any:
    service, actor, chat_id, chat_type = _service_context(context)
    return {"work_items": await service.list_work_items(actor, chat_id, chat_type, "open", args.limit)}


async def get_due_soon(args: LimitInput, context: Any) -> Any:
    service, actor, chat_id, chat_type = _service_context(context)
    cutoff = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    return {"work_items": await service.list_work_items(actor, chat_id, chat_type, "open", args.limit, due_before=cutoff)}


async def search_messages(args: HintInput, context: Any) -> Any:
    db = context.message_database
    allowed = getattr(context, "allowed_chat_ids", None)
    if not hasattr(db, "search_messages"):
        return {"messages": [], "scope_notice": "Message search is unavailable."}
    rows = await db.search_messages(args.hint, limit=20, allowed_chat_ids=allowed)
    return {
        "messages": [row.model_dump(mode="json") if hasattr(row, "model_dump") else row for row in rows],
        "scope_notice": "Search is limited to the owner's selected source chats.",
    }


async def search_memory(args: HintInput, context: Any) -> Any:
    service, actor, chat_id, chat_type = _service_context(context)
    return {"memory": await service.search_memory(actor, chat_id, chat_type, args.hint, 10)}


async def change_work_status(args: StatusInput, context: Any) -> Any:
    service, actor, chat_id, chat_type = _service_context(context)
    item = await service.get_work_item(actor, chat_id, chat_type, args.work_item_id)
    if not item:
        return {"error": "WORK_ITEM_NOT_FOUND"}
    changed = await service.update_task(actor, chat_id, chat_type, item["id"], status=args.status)
    return {"updated": changed, "work_item": await service.get_work_item(actor, chat_id, chat_type, item["id"])}


async def change_priority(args: PriorityInput, context: Any) -> Any:
    service, actor, chat_id, chat_type = _service_context(context)
    item = await service.get_work_item(actor, chat_id, chat_type, args.work_item_id)
    if not item: return {"error": "WORK_ITEM_NOT_FOUND"}
    changed = await service.update_task(actor, chat_id, chat_type, item["id"], priority=args.priority)
    return {"updated": changed, "work_item": await service.get_work_item(actor, chat_id, chat_type, item["id"])}


async def set_due_date(args: DueInput, context: Any) -> Any:
    service, actor, chat_id, chat_type = _service_context(context)
    item = await service.get_work_item(actor, chat_id, chat_type, args.work_item_id)
    if not item: return {"error": "WORK_ITEM_NOT_FOUND"}
    changed = await service.update_task(actor, chat_id, chat_type, item["id"], due_at=args.due_at)
    return {"updated": changed, "work_item": await service.get_work_item(actor, chat_id, chat_type, item["id"])}


async def add_comment(args: CommentInput, context: Any) -> Any:
    service, actor, chat_id, chat_type = _service_context(context)
    item = await service.get_work_item(actor, chat_id, chat_type, args.work_item_id)
    if not item: return {"error": "WORK_ITEM_NOT_FOUND"}
    comment_id = await service.add_comment(actor, chat_id, chat_type, item["id"], args.comment)
    return {"comment_id": comment_id, "work_item": item}


async def assign_work_item(args: AssignmentInput, context: Any) -> Any:
    service, actor, chat_id, chat_type = _service_context(context)
    item = await service.get_work_item(actor, chat_id, chat_type, args.work_item_id)
    if not item:
        return {"error": "WORK_ITEM_NOT_FOUND"}
    resolution = await resolve_person(service, actor, args.assignee)
    if resolution.status != "RESOLVED" or not resolution.value:
        return {"resolution": resolution.status, "candidates": resolution.candidates or []}
    assignee_id = int(resolution.value.get("user_id") or resolution.value.get("id"))
    changed = await service.update_task(actor, chat_id, chat_type, item["id"], assignee_id=assignee_id)
    return {"updated": changed, "assignee": resolution.value, "work_item": await service.get_work_item(actor, chat_id, chat_type, item["id"])}


async def get_management_brief(args: BriefInput, context: Any) -> Any:
    if context.message_database is None:
        return {"brief": "Situation history is not available in this context."}
    from app.priority.briefs import BriefEngine

    engine = BriefEngine(context.message_database, context.repository)
    allowed = getattr(context, "allowed_chat_ids", None) or None
    if args.period == "now":
        return {"brief": await engine.snapshot(allowed)}
    return {"brief": await engine.period_summary(args.period, allowed)}


def build_registry(telegram: bool = False) -> ToolRegistry:
    read = RiskLevel.READ
    write = RiskLevel.SAFE_WRITE
    definitions = [
        ToolDefinition(name="list_projects", description="List projects visible to the user.", input_schema=LimitInput, output_schema=JsonOutput, risk=read, required_permission="work.read", side_effect=False, approval_required=False, handler=list_projects),
        ToolDefinition(name="find_project", description="Find projects by human key or name.", input_schema=HintInput, output_schema=JsonOutput, risk=read, required_permission="work.read", side_effect=False, approval_required=False, handler=find_project),
        ToolDefinition(name="get_project", description="Resolve and return one project by key or name.", input_schema=HintInput, output_schema=JsonOutput, risk=read, required_permission="work.read", side_effect=False, approval_required=False, handler=get_project),
        ToolDefinition(name="find_work_item", description="Find work items by human key or title.", input_schema=HintInput, output_schema=JsonOutput, risk=read, required_permission="work.read", side_effect=False, approval_required=False, handler=find_work_item),
        ToolDefinition(name="get_work_item", description="Return one work item by public key or ID.", input_schema=IdInput, output_schema=JsonOutput, risk=read, required_permission="work.read", side_effect=False, approval_required=False, handler=get_work_item),
        ToolDefinition(name="get_my_work", description="List open work assigned to or owned by the user.", input_schema=LimitInput, output_schema=JsonOutput, risk=read, required_permission="work.read", side_effect=False, approval_required=False, handler=get_my_work),
        ToolDefinition(name="get_due_soon", description="List open work due within 48 hours.", input_schema=LimitInput, output_schema=JsonOutput, risk=read, required_permission="work.read", side_effect=False, approval_required=False, handler=get_due_soon),
        ToolDefinition(name="search_messages", description="Search only approved source messages.", input_schema=HintInput, output_schema=JsonOutput, risk=read, required_permission="source.read", side_effect=False, approval_required=False, handler=search_messages),
        ToolDefinition(name="get_management_brief", description="Get the current management brief (critical/pending situations, pending approvals) or a 'what happened' retrospective for today/yesterday/this week/this month.", input_schema=BriefInput, output_schema=JsonOutput, risk=read, required_permission="source.read", side_effect=False, approval_required=False, handler=get_management_brief),
        ToolDefinition(name="search_memory", description="Search the user's saved assistant conversation memory.", input_schema=HintInput, output_schema=JsonOutput, risk=read, required_permission="memory.read", side_effect=False, approval_required=False, handler=search_memory),
        ToolDefinition(name="change_work_status", description="Move a work item through the legal workflow.", input_schema=StatusInput, output_schema=JsonOutput, risk=write, required_permission="work.update", side_effect=True, approval_required=False, handler=change_work_status),
        ToolDefinition(name="change_priority", description="Change a work item's P0-P3 priority.", input_schema=PriorityInput, output_schema=JsonOutput, risk=write, required_permission="work.update", side_effect=True, approval_required=False, handler=change_priority),
        ToolDefinition(name="set_due_date", description="Set a work item's due date.", input_schema=DueInput, output_schema=JsonOutput, risk=write, required_permission="work.update", side_effect=True, approval_required=False, handler=set_due_date),
        ToolDefinition(name="add_comment", description="Add a comment to a work item.", input_schema=CommentInput, output_schema=JsonOutput, risk=write, required_permission="work.comment", side_effect=True, approval_required=False, handler=add_comment),
        ToolDefinition(name="assign_work_item", description="Assign a work item to one approved user by name or ID.", input_schema=AssignmentInput, output_schema=JsonOutput, risk=write, required_permission="work.update", side_effect=True, approval_required=False, handler=assign_work_item),
    ]
    if telegram:
        from app.telegram.sources import SourceInput, read_telegram_chat, select_telegram_chat, telegram_access
        # Conversational release exposes read tools; existing explicit business
        # commands remain available through their service authorization.
        definitions = [tool for tool in definitions if tool.risk == read and tool.name != "search_messages"]
        for name, description, schema, handler in [
            ("read_telegram_chat", "Read/summarize one Telegram personal chat, group or Saved Messages. Resolves the name, asks permission, and retrieves the requested dates.", SourceInput, read_telegram_chat),
            ("select_telegram_chat", "Open Telegram buttons to search and select a group or personal chat, then grant read access and continue the request.", SourceInput, select_telegram_chat),
            ("telegram_access", "Show persistent history-read permissions with Telegram revoke buttons. Monitoring is managed separately in Setup.", LimitInput, telegram_access),
        ]:
            definitions.append(ToolDefinition(name=name, description=description, input_schema=schema, output_schema=JsonOutput,
                risk=read, required_permission="source.read", side_effect=False, approval_required=False, handler=handler))
    return ToolRegistry(definitions)


