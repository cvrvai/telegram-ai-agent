"""Thin async wrapper around the Gmail API for inbox reading and sending.

googleapiclient is synchronous; every call runs off the event loop via
asyncio.to_thread so it never blocks the Telethon loop.
"""

from __future__ import annotations

import asyncio
import base64
from email.mime.text import MIMEText
from typing import Any

from googleapiclient.discovery import build


def _service(credentials):
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _list_messages_sync(credentials, max_results: int, query: str) -> list[dict[str, Any]]:
    service = _service(credentials)
    response = service.users().messages().list(userId="me", maxResults=max_results, q=query).execute()
    messages = []
    for item in response.get("messages", []):
        detail = (
            service.users()
            .messages()
            .get(userId="me", id=item["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        messages.append(
            {
                "id": item["id"],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject") or "(no subject)",
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", ""),
            }
        )
    return messages


def _send_message_sync(credentials, *, to: str, subject: str, body: str) -> dict[str, Any]:
    service = _service(credentials)
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"id": sent.get("id"), "to": to, "subject": subject}


async def list_recent_messages(credentials, max_results: int = 10, query: str = "is:unread") -> list[dict[str, Any]]:
    return await asyncio.to_thread(_list_messages_sync, credentials, max_results, query)


async def send_message(credentials, *, to: str, subject: str, body: str) -> dict[str, Any]:
    return await asyncio.to_thread(_send_message_sync, credentials, to=to, subject=subject, body=body)
