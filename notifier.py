"""Notification dispatcher for instant alerts and periodic digests via Telegram."""

from __future__ import annotations

import logging
import httpx
from typing import Optional
from config import AppConfig, config
from models import IncomingMessage, PriorityClassification

logger = logging.getLogger("notifier")


class Notifier:
    """Delivers immediate priority alerts and digests to the user's notification channel."""

    def __init__(self, cfg: Optional[AppConfig] = None):
        self.cfg = cfg or config
        self.bot_token = self.cfg.telegram_bot_token
        self.chat_id = self.cfg.notification_chat_id

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """Sends a message via the Telegram Bot API."""
        if not self.is_configured:
            logger.info(f"[NOTIFIER DRY-RUN] Would send message:\n{text}")
            return True

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup


        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, json=payload)
                data = res.json()
                if not data.get("ok"):
                    logger.error(f"Failed to send Telegram notification: {data}")
                    return False
                return True
        except Exception as e:
            logger.error(f"HTTP error sending Telegram notification: {e}")
            return False

    def format_instant_alert(self, msg: IncomingMessage, cls: PriorityClassification) -> str:
        """Formats a high-priority incoming message into a clean alert box."""
        badge = "🚨 <b>URGENT ALERT (P0)</b>" if cls.priority == "P0" else "🔴 <b>IMPORTANT (P1)</b>"

        lines = [
            f"{badge} • <i>Score: {cls.score}/100</i>",
            "",
            f"📍 <b>Chat:</b> {self._escape_html(msg.chat_title)}",
            f"👤 <b>From:</b> {self._escape_html(msg.sender_name)}",
            "",
            f"📝 <b>Summary:</b> {self._escape_html(cls.summary)}",
        ]

        if cls.needs_action and cls.action:
            lines.append(f"⚡ <b>Action:</b> <u>{self._escape_html(cls.action)}</u>")

        if cls.deadline:
            lines.append(f"⏰ <b>Deadline:</b> <code>{self._escape_html(cls.deadline)}</code>")

        lines.append(f"💡 <b>Reason:</b> <i>{self._escape_html(cls.reason)}</i>")

        if msg.message_link:
            lines.append(f"\n🔗 <a href=\"{msg.message_link}\">Open Message in Telegram</a>")

        return "\n".join(lines)

    async def send_instant_alert(self, msg: IncomingMessage, cls: PriorityClassification) -> bool:
        """Dispatches an immediate notification for a high-priority message with action buttons."""
        text = self.format_instant_alert(msg, cls)
        markup = {
            "inline_keyboard": [
                [
                    {"text": "📋 Instant Digest", "callback_data": "btn_digest"},
                    {"text": "🔴 Priority Only", "callback_data": "btn_priority"},
                ],
                [
                    {"text": "📊 Stats", "callback_data": "btn_stats"},
                ],
            ]
        }
        return await self.send_message(text, reply_markup=markup)

    async def send_digest(self, digest_text: str) -> bool:
        """Dispatches a periodic digest with interactive action buttons."""
        markup = {
            "inline_keyboard": [
                [
                    {"text": "🔄 Refresh Digest", "callback_data": "btn_digest"},
                    {"text": "🔴 Priority Messages", "callback_data": "btn_priority"},
                ],
                [
                    {"text": "📊 View Stats", "callback_data": "btn_stats"},
                ],
            ]
        }
        return await self.send_message(digest_text, reply_markup=markup)


    @staticmethod
    def _escape_html(text: Optional[str]) -> str:
        if not text:
            return ""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
