"""Agent tools for Situations and the weekly deck -- the natural-language
equivalents of /situations and /weeklyreport.

The client asked for conversation, not commands: "what is still unresolved?",
"who is responsible for the pump issue?", "prepare this week's presentation".
Every one of those has to reach the same code the slash commands use, so
these are thin wrappers over the existing storage and builders.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SituationListInput(_Input):
    status: Literal["open", "monitoring", "resolved", "any"] = Field(
        default="open", description="Which situations to return; 'open' is the default view."
    )
    limit: int = Field(default=20, ge=1, le=50)


class SituationIdInput(_Input):
    situation_id: int = Field(description="Numeric id from list_situations.")


class WeeklyReportInput(_Input):
    title: str = Field(default="Weekly Management Brief", max_length=120)
    format: Literal["pptx", "pdf"] = Field(
        default="pptx",
        description="Report format: 'pptx' for PowerPoint presentation slides, or 'pdf' for executive printable PDF document.",
    )


def _row(s: Any) -> dict:
    return {
        "situation_id": s.id,
        "title": s.title,
        "status": s.status,
        "priority": s.priority,
        "critical_impact": s.critical_impact,
        "responsible": s.responsible,
        "current_action": s.current_action,
        "dependency": s.dependency,
        "message_count": s.message_count,
        "source": s.chat_title,
        "started_at": s.started_at,
        "last_update_at": s.last_update_at,
    }


async def list_situations(args: SituationListInput, context: Any) -> Any:
    db = context.message_database
    if db is None:
        return {"error": "Situation history is not available here."}
    allowed = getattr(context, "allowed_chat_ids", None) or None
    status = None if args.status == "any" else args.status
    rows = await db.list_situations(status=status, limit=args.limit, allowed_chat_ids=allowed)
    impact_label = getattr(context, "impact_label", "Critical impact")
    return {"impact_label": impact_label, "count": len(rows), "situations": [_row(s) for s in rows]}


async def get_situation(args: SituationIdInput, context: Any) -> Any:
    db = context.message_database
    if db is None:
        return {"error": "Situation history is not available here."}
    situation = await db.get_situation(args.situation_id)
    if not situation:
        return {"error": f"No situation with id {args.situation_id}."}
    allowed = getattr(context, "allowed_chat_ids", None) or None
    if allowed and situation.chat_id not in allowed:
        return {"error": "That situation is outside the authorized source chats."}
    detail = _row(situation)
    # Source traceability: the case study requires every conclusion to be
    # answerable with "where did you get this?".
    detail["summary"] = situation.summary
    detail["source_message_ids"] = situation.source_message_ids
    detail["last_message_link"] = situation.last_message_link
    return detail


async def resolve_situation(args: SituationIdInput, context: Any) -> Any:
    db = context.message_database
    if db is None:
        return {"error": "Situation history is not available here."}
    ok = await db.resolve_situation(args.situation_id)
    return {"resolved": bool(ok), "situation_id": args.situation_id}


async def generate_weekly_report(args: WeeklyReportInput, context: Any) -> Any:
    """Builds the .pptx or .pdf and hands the path to the Telegram layer, which sends
    it as a document once the turn finishes (agents can only return text)."""
    db = context.message_database
    if db is None or context.repository is None:
        return {"error": "Reporting data is not available here."}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    allowed = getattr(context, "allowed_chat_ids", None) or None
    is_pdf = (args.format or "pptx").lower() == "pdf"

    if is_pdf:
        from app.reporting.weekly_pdf import WeeklyPdfBuilder

        path = os.path.join(tempfile.gettempdir(), f"weekly-brief-{stamp}.pdf")
        builder = WeeklyPdfBuilder(db, context.repository)
        await builder.build(path, allowed, title=args.title)
        caption = f"📄 {args.title} (PDF) — review before the meeting."
        fmt_label = "PDF executive report"
    else:
        from app.reporting.weekly_deck import WeeklyDeckBuilder

        path = os.path.join(tempfile.gettempdir(), f"weekly-brief-{stamp}.pptx")
        builder = WeeklyDeckBuilder(db, context.repository)
        await builder.build(path, allowed, title=args.title)
        caption = f"📊 {args.title} (PowerPoint) — review before the meeting."
        fmt_label = "PowerPoint presentation"

    context.session["pending_file"] = {"path": path, "caption": caption}
    return {"generated": True, "format": "pdf" if is_pdf else "pptx", "note": f"The {fmt_label} file is being sent to this chat now."}
