from __future__ import annotations

from typing import Any

import httpx


class IntegrationHub:
    """Webhook based email, calendar, CRM and task integrations."""

    def __init__(self, endpoints: dict[str, str] | None = None, secret: str | None = None):
        self.endpoints = {key: value for key, value in (endpoints or {}).items() if value}
        self.secret = secret

    def configured(self, kind: str) -> bool:
        return bool(self.endpoints.get(kind))

    async def dispatch(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = self.endpoints.get(kind)
        if not endpoint:
            raise RuntimeError(f"{kind} integration is not configured")
        headers = {"User-Agent": "TelegramBusinessAssistant/1.0"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                return {"ok": True, "status_code": response.status_code}

    async def send_email(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        return await self.dispatch("email", {"to": to, "subject": subject, "body": body})

    async def create_calendar_event(self, *, title: str, start: str, end: str | None = None, notes: str = "") -> dict[str, Any]:
        return await self.dispatch("calendar", {"title": title, "start": start, "end": end, "notes": notes})

    async def create_crm_record(self, *, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        return await self.dispatch("crm", {"kind": kind, "data": data})

    async def create_task(self, *, title: str, details: str = "", due_at: str | None = None) -> dict[str, Any]:
        return await self.dispatch("task", {"title": title, "details": details, "due_at": due_at})
