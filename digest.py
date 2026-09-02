"""Periodic Digest generation and aggregation engine."""

from __future__ import annotations

import html
from datetime import datetime
from typing import List, Tuple
from database import Database
from models import MessageRecord, DigestStats


class DigestEngine:
    """Aggregates unread/pending messages into a structured periodic digest."""

    def __init__(self, db: Database):
        self.db = db

    async def generate_digest(self) -> Tuple[Optional[str], DigestStats]:
        """
        Gathers all pending messages, formats the 3-tier digest,
        marks messages as digested, and saves history.
        """
        records = await self.db.get_pending_digest_messages()
        if not records:
            return None, DigestStats()

        # Group records by priority
        p0_p1_records: List[MessageRecord] = []
        p2_records: List[MessageRecord] = []
        p3_records: List[MessageRecord] = []

        for r in records:
            if r.priority in ("P0", "P1"):
                p0_p1_records.append(r)
            elif r.priority == "P2":
                p2_records.append(r)
            else:
                p3_records.append(r)

        stats = DigestStats(
            total_messages=len(records),
            p0_count=sum(1 for r in records if r.priority == "P0"),
            p1_count=sum(1 for r in records if r.priority == "P1"),
            p2_count=len(p2_records),
            p3_count=len(p3_records),
            action_items_count=sum(1 for r in records if r.needs_action),
        )

        digest_text = self._format_digest_text(p0_p1_records, p2_records, p3_records, stats)

        # Mark all pending messages as digested
        record_ids = [r.id for r in records if r.id is not None]
        await self.db.mark_messages_digested(record_ids)

        # Save to digest history table
        await self.db.save_digest(stats, digest_text)

        return digest_text, stats

    def _format_digest_text(
        self,
        p0_p1: List[MessageRecord],
        p2: List[MessageRecord],
        p3: List[MessageRecord],
        stats: DigestStats,
    ) -> str:
        """Formats the digest into the user's requested clean structure."""
        now_str = datetime.now().strftime("%I:%M %p")
        lines = [
            f"📋 <b>Telegram Priority Digest</b> • <i>{now_str}</i>",
            f"━━━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        # 1. High Priority Section (P0 & P1)
        if p0_p1:
            lines.append(f"🔴 <b>{len(p0_p1)} Priority Messages</b>\n")
            for idx, item in enumerate(p0_p1, start=1):
                chat_title = html.escape(item.chat_title)
                summary = html.escape(item.summary)
                sender = html.escape(item.sender_name)

                lines.append(f"<b>{idx}. {chat_title}</b> <i>({sender})</i>")
                lines.append(f"   {summary}")

                if item.needs_action and item.action:
                    lines.append(f"   → <b>Action:</b> <u>{html.escape(item.action)}</u>")

                if item.deadline:
                    lines.append(f"   → <b>Deadline:</b> <code>{html.escape(item.deadline)}</code>")

                if item.message_link:
                    lines.append(f"   → <a href=\"{item.message_link}\">Open Message</a>")

                lines.append("")
        else:
            lines.append("🔴 <b>0 Urgent / Actionable Messages</b>\n")

        # 2. Medium Priority Section (P2 - Useful Updates)
        if p2:
            lines.append(f"🟡 <b>{len(p2)} Important Updates</b>")
            for item in p2[:15]:  # Cap at 15 items to prevent oversized telegram messages
                chat = html.escape(item.chat_title)
                summary = html.escape(item.summary)
                lines.append(f"• <b>[{chat}]</b> {summary}")
            if len(p2) > 15:
                lines.append(f"<i>... and {len(p2) - 15} more updates.</i>")
            lines.append("")
        else:
            lines.append("🟡 <b>0 Important Updates</b>\n")

        # 3. Low Priority Section (P3 - Noise Summary)
        lines.append(f"🟢 <b>{len(p3)} Low-priority messages summarized</b>")
        lines.append("<i>(greetings, memes, stickers, reactions, casual chat)</i>")

        return "\n".join(lines)
