"""Turns "what happened today/yesterday/this week/this month?" and "what do
I need to do?" into one structured answer built on Situations, instead of a
fresh summarization pass over raw messages every time they're asked.

Two distinct queries, both case-study first-class asks:
- snapshot(): "what's true right now" -- currently open/critical situations
  plus pending approvals. This is the proactive DAILY MANAGEMENT BRIEF.
- period_summary(period): "what happened in this window" -- a retrospective
  over situations with activity in that window, including ones resolved
  during it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Optional

LOCAL_TZ = timezone(timedelta(hours=7), name="Asia/Phnom_Penh")
PERIOD_LABELS = {"today": "Today", "yesterday": "Yesterday", "week": "This week", "month": "This month"}


def _period_bounds(period: str, now: Optional[datetime] = None) -> tuple[datetime, Optional[datetime], str]:
    """Returns (start, end, label). end is None for "up to now" periods;
    "yesterday" is the one period that needs an explicit upper bound too."""
    current = (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "yesterday":
        start = midnight - timedelta(days=1)
        return start, midnight, PERIOD_LABELS["yesterday"]
    if period == "week":
        return midnight - timedelta(days=current.weekday()), None, PERIOD_LABELS["week"]
    if period == "month":
        return midnight.replace(day=1), None, PERIOD_LABELS["month"]
    return midnight, None, PERIOD_LABELS["today"]


def _clip(text: str, limit: int = 90) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


class BriefEngine:
    def __init__(self, message_db: Any, business_repository: Any):
        self.db = message_db
        self.business_repository = business_repository

    async def snapshot(self, allowed_chat_ids: Optional[set[int]] = None) -> str:
        situations = await self.db.list_situations(limit=50, allowed_chat_ids=allowed_chat_ids)
        pending_actions = await self.business_repository.list_actions("pending", limit=20)
        return self._format(
            situations, pending_actions,
            title="📋 <b>Daily Management Brief</b>",
            empty_note="🟢 <i>All caught up. No critical or pending situations right now.</i>",
        )

    async def period_summary(self, period: str, allowed_chat_ids: Optional[set[int]] = None) -> str:
        start, end, label = _period_bounds(period)
        situations = await self.db.list_situations_active_since(start.astimezone(timezone.utc).isoformat(), allowed_chat_ids)
        if end is not None:
            end_iso = end.astimezone(timezone.utc).isoformat()
            situations = [s for s in situations if s.last_update_at < end_iso or s.started_at < end_iso]
        pending_actions = await self.business_repository.list_actions("pending", limit=20)
        return self._format(
            situations, pending_actions,
            title=f"📋 <b>{label}</b>",
            empty_note=f"🟢 <i>Nothing significant happened {label.lower()}.</i>",
        )

    def _format(self, situations, pending_actions, *, title: str, empty_note: str) -> str:
        open_situations = [s for s in situations if s.status != "resolved"]
        resolved = [s for s in situations if s.status == "resolved"]
        critical = [s for s in open_situations if s.priority == "P0"]
        pending = [s for s in open_situations if s.priority in ("P1", "P2")]
        general = [s for s in open_situations if s.priority == "P3"]

        lines = [title, "━━━━━━━━━━━━━━━━━━━━━━"]
        if critical:
            lines.append(f"\n🔴 <b>CRITICAL — {len(critical)}</b>")
            for s in critical[:8]:
                lines.append(f"• <b>{escape(s.chat_title)}</b> — {escape(_clip(s.title))}")
        if pending:
            lines.append(f"\n🟠 <b>PENDING — {len(pending)}</b>")
            for s in pending[:10]:
                waiting_note = f" (waiting on {escape(_clip(s.dependency, 40))})" if s.dependency else ""
                lines.append(f"• {escape(s.chat_title)} — {escape(_clip(s.title))}{waiting_note}")
        if resolved:
            lines.append(f"\n✅ <b>RESOLVED — {len(resolved)}</b>")
            for s in resolved[:8]:
                lines.append(f"• {escape(_clip(s.title))}")
        if pending_actions:
            lines.append(f"\n📌 <b>YOUR ACTIONS — {len(pending_actions)}</b>")
            for action in pending_actions[:10]:
                lines.append(f"• Approve {escape(str(action.get('action_type', 'draft')))} (#{action.get('id')})")
        if general and not (critical or pending or pending_actions):
            lines.append("\n🟢 <b>GENERAL STATUS</b>")
            lines.append(f"{len(general)} lower-priority item(s) tracked; no management attention required.")
        if not critical and not pending and not pending_actions and not resolved:
            lines.append(f"\n{empty_note}")
        return "\n".join(lines)
