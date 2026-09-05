"""Thin async wrapper around the Google Calendar API for event listing and creation.

googleapiclient is synchronous; every call runs off the event loop via
asyncio.to_thread so it never blocks the Telethon loop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from googleapiclient.discovery import build


def _service(credentials):
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _list_events_sync(credentials, max_results: int) -> list[dict[str, Any]]:
    service = _service(credentials)
    now = datetime.now(timezone.utc).isoformat()
    response = (
        service.events()
        .list(calendarId="primary", timeMin=now, maxResults=max_results, singleEvents=True, orderBy="startTime")
        .execute()
    )
    events = []
    for item in response.get("items", []):
        start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
        end = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date")
        events.append(
            {
                "id": item.get("id"),
                "summary": item.get("summary") or "(no title)",
                "start": start,
                "end": end,
                "attendees": [a.get("email") for a in item.get("attendees", []) if a.get("email")],
                "link": item.get("htmlLink"),
            }
        )
    return events


def _create_event_sync(
    credentials,
    *,
    summary: str,
    start: str,
    end: Optional[str],
    description: str,
    attendees: Optional[list[str]],
    tz: str,
) -> dict[str, Any]:
    service = _service(credentials)
    body: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start, "timeZone": tz},
        "end": {"dateTime": end or start, "timeZone": tz},
    }
    if attendees:
        body["attendees"] = [{"email": email} for email in attendees]
    created = (
        service.events()
        .insert(calendarId="primary", body=body, sendUpdates="all" if attendees else "none")
        .execute()
    )
    return {
        "id": created.get("id"),
        "link": created.get("htmlLink"),
        "summary": created.get("summary"),
        "start": start,
        "end": end or start,
    }


async def list_upcoming_events(credentials, max_results: int = 10) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_list_events_sync, credentials, max_results)


async def create_event(
    credentials,
    *,
    summary: str,
    start: str,
    end: str | None = None,
    description: str = "",
    attendees: list[str] | None = None,
    timezone_name: str = "Asia/Phnom_Penh",
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _create_event_sync,
        credentials,
        summary=summary,
        start=start,
        end=end,
        description=description,
        attendees=attendees,
        tz=timezone_name,
    )
