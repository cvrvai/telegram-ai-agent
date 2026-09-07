"""Agent tools for Google Calendar/Gmail -- the natural-language equivalents
of /calendar, /inbox, /event, and /email.

Reads answer directly. Writes never touch Google themselves: they create a
pending draft through the exact same propose-then-approve flow the manual
commands already use (context.service.draft_action), and stash the new
action id on context.session so TelegramConversation can attach the same
Approve/Reject buttons once the turn completes. This is deliberately not
the registry's approval_required risk tier -- that tier stops the handler
from ever running at all (see runtime.py), so it can't create the draft the
approval is actually for.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListEventsInput(_Input):
    limit: int = Field(default=10, ge=1, le=25)


class CreateEventInput(_Input):
    title: str = Field(min_length=1, max_length=200)
    start: str = Field(description="ISO 8601 date-time with timezone offset, e.g. 2026-09-06T14:00:00+07:00")
    end: Optional[str] = Field(default=None, description="ISO 8601 date-time. Optional -- omit it and the event defaults to one hour; never ask the user for a duration just to fill this in.")
    # No default on purpose: the model must have this answer -- from context
    # or by asking the user -- before a draft can be prepared at all.
    meeting_type: Literal["online", "in_person"] = Field(
        description="Whether this is a video call or a physical meeting. If the user's message doesn't make this clear, ask them before calling this tool -- do not guess."
    )
    location: Optional[str] = Field(default=None, max_length=300, description="Physical location/room, only for an in_person meeting.")
    attendee_email: Optional[str] = Field(default=None, max_length=320, description="The other person's real email address, only if it is actually known -- never invent one.")


class SendEmailInput(_Input):
    to: str = Field(min_length=3, max_length=320)
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=5000)


async def _credentials(context: Any):
    if context.google_account is None:
        return None, "Google integration is not configured."
    credentials = await context.google_account.get_credentials(context.actor_id)
    if not credentials:
        return None, "Google Calendar/Gmail is not connected. Ask the owner to run /connectgoogle."
    return credentials, None


async def list_upcoming_events(args: ListEventsInput, context: Any) -> Any:
    credentials, error = await _credentials(context)
    if error:
        return {"error": error}
    from app.integrations.google_calendar import list_upcoming_events as fetch_events

    return {"events": await fetch_events(credentials, max_results=args.limit)}


async def summarize_inbox(args: ListEventsInput, context: Any) -> Any:
    credentials, error = await _credentials(context)
    if error:
        return {"error": error}
    from app.integrations.google_gmail import list_recent_messages

    return {"messages": await list_recent_messages(credentials, max_results=args.limit)}


def _default_end(start: str, end: Optional[str]) -> Optional[str]:
    """A missing duration is never a reason to interrogate the user -- an
    hour is the sane default, and a zero-length event is not."""
    if end:
        return end
    try:
        return (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat()
    except ValueError:
        return None


async def create_calendar_event(args: CreateEventInput, context: Any) -> Any:
    payload = json.dumps({
        "title": args.title,
        "start": args.start,
        "end": _default_end(args.start, args.end),
        "meeting_type": args.meeting_type,
        "location": args.location,
        "attendee_email": args.attendee_email,
    })
    action_id = await context.service.draft_action(context.actor_id, context.chat_id, context.chat_type, "calendar", "calendar", payload, 15)
    context.session["pending_draft_action"] = {"id": action_id, "label": "calendar event"}
    return {
        "drafted": True,
        "action_id": action_id,
        "title": args.title,
        "start": args.start,
        "end": args.end,
        "meeting_type": args.meeting_type,
        "will_create_meet_link": args.meeting_type == "online",
    }


async def send_email(args: SendEmailInput, context: Any) -> Any:
    payload = json.dumps({"to": args.to, "subject": args.subject, "body": args.body})
    action_id = await context.service.draft_action(context.actor_id, context.chat_id, context.chat_type, "email", "email", payload, 15)
    context.session["pending_draft_action"] = {"id": action_id, "label": "email"}
    return {"drafted": True, "action_id": action_id, "to": args.to, "subject": args.subject}
