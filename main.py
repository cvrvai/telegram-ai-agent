"""Main Application Entrypoint for Telegram AI Priority Filter & Digest Bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from typing import Optional

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from rich.panel import Panel

from config import config
from models import IncomingMessage, PriorityClassification
from database import Database
from prefilter import evaluate_prefilter
from classifier import AIClassifier
from notifier import Notifier
from digest import DigestEngine
from scheduler import DigestScheduler

# Setup rich console and logging
console = Console(force_terminal=True, legacy_windows=False)
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, console=console)],
)
logger = logging.getLogger("main")


class TelegramPriorityApp:
    def __init__(self):
        self.cfg = config
        self.db = Database(self.cfg.database_path)
        self.classifier = AIClassifier(self.cfg)
        self.notifier = Notifier(self.cfg)
        self.digest_engine = DigestEngine(self.db)
        self.scheduler: Optional[DigestScheduler] = None
        self.telethon_client = None
        self._last_alert_time_by_chat: dict[int, float] = {}

    async def initialize(self) -> None:
        """Initialize database and subcomponents."""
        await self.db.init_db()

    async def process_incoming_message(self, msg: IncomingMessage) -> PriorityClassification:
        """Pipeline to filter, classify, save, and route an incoming message."""
        import time

        # 1. Fast rule-based pre-filter (Free / No AI cost)
        prefiltered_cls = evaluate_prefilter(msg, self.cfg.profile.vip_senders)
        if prefiltered_cls:
            logger.info(f"[PRE-FILTER NOISE] {msg.chat_title} | {msg.sender_name}: {msg.text[:50]}")
            await self.db.save_message(msg, prefiltered_cls, is_prefiltered=True, alert_sent=False)
            return prefiltered_cls

        # 2. AI Priority Classification
        cls = await self.classifier.classify(msg)
        logger.info(
            f"[{cls.priority} | Score: {cls.score}] {msg.chat_title} ({msg.sender_name}): {cls.summary}"
        )

        # 3. Route & Alert Check
        should_alert = (
            cls.score >= self.cfg.urgent_score_threshold
            or cls.priority in self.cfg.alert_priority_list
        )

        # Anti-spam debouncing for group chats:
        # If an alert was already pushed for this chat in the last 25s,
        # only send another instant alert if it is a critical emergency (score >= 95).
        now = time.time()
        last_alert_time = self._last_alert_time_by_chat.get(msg.chat_id, 0.0)
        if should_alert and (now - last_alert_time < 25.0) and cls.score < 95:
            logger.info(f"⏳ Burst cooldown active for '{msg.chat_title}'. Queuing for digest quietly.")
            should_alert = False

        if should_alert:
            self._last_alert_time_by_chat[msg.chat_id] = now

        record_id = await self.db.save_message(msg, cls, is_prefiltered=False, alert_sent=should_alert)

        # 4. Dispatch Instant Notification if high priority
        if should_alert:
            logger.info(f"🚨 Sending Instant Alert for record #{record_id} (Score {cls.score})")
            await self.notifier.send_instant_alert(msg, cls)

        return cls


    async def run_digest_job(self) -> None:
        """Periodic job to generate and dispatch digest."""
        logger.info("Executing scheduled digest generation...")
        digest_text, stats = await self.digest_engine.generate_digest()
        if digest_text:
            logger.info(f"Generated digest for {stats.total_messages} messages (P0:{stats.p0_count}, P1:{stats.p1_count}, P2:{stats.p2_count}, P3:{stats.p3_count})")
            await self.notifier.send_digest(digest_text)
        else:
            logger.info("No pending messages for digest.")

    async def start_userbot(self) -> None:
        """Starts the Telethon Userbot client to listen to all chats."""
        if not self.cfg.telegram_api_id or not self.cfg.telegram_api_hash:
            console.print(
                Panel.fit(
                    "[bold red]TELEGRAM_API_ID or TELEGRAM_API_HASH is missing in .env![/bold red]\n"
                    "Please configure your .env file with credentials from https://my.telegram.org\n"
                    "You can still test classification by running: [bold green]python main.py test[/bold green]",
                    title="Configuration Required",
                )
            )
            return

        from telethon import TelegramClient, events

        self.telethon_client = TelegramClient(
            self.cfg.telegram_session_name,
            self.cfg.telegram_api_id,
            self.cfg.telegram_api_hash,
        )

        bot_id = int(self.cfg.telegram_bot_token.split(":")[0]) if self.cfg.telegram_bot_token and ":" in self.cfg.telegram_bot_token else None

        @self.telethon_client.on(events.NewMessage)
        async def on_new_message(event):
            # Ignore messages sent by ourselves
            if event.out:
                return

            try:
                chat = await event.get_chat()
                sender = await event.get_sender()

                # Ignore our own notification bot chat to prevent circular loops
                if bot_id and (event.chat_id == bot_id or getattr(sender, "id", None) == bot_id):
                    return

                # Ignore other Telegram bots
                if getattr(sender, "bot", False):
                    return

                # Chat metadata
                chat_id = event.chat_id
                chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", "Private DM")
                chat_type = "private" if event.is_private else ("channel" if event.is_channel else "group")

                # Check ignore list
                if chat_title in self.cfg.profile.ignore_chats or str(chat_id) in self.cfg.profile.ignore_chats:
                    return


                # Sender metadata
                sender_id = getattr(sender, "id", None)
                sender_name = getattr(sender, "first_name", "") or getattr(sender, "title", "Unknown")
                sender_user = getattr(sender, "username", None)

                # Message link construction
                message_link = None
                if getattr(chat, "username", None):
                    message_link = f"https://t.me/{chat.username}/{event.id}"
                elif event.is_channel:
                    # Supergroups / Channels
                    clean_id = str(chat_id).replace("-100", "")
                    message_link = f"https://t.me/c/{clean_id}/{event.id}"

                media_type = "text"
                if event.sticker:
                    media_type = "sticker"
                elif event.photo:
                    media_type = "photo"
                elif event.voice:
                    media_type = "voice"
                elif event.document:
                    media_type = "document"

                incoming = IncomingMessage(
                    message_id=event.id,
                    chat_id=chat_id,
                    chat_title=chat_title,
                    chat_type=chat_type,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    sender_username=sender_user,
                    text=event.raw_text or "",
                    date=event.date or datetime.utcnow(),
                    is_reply=event.is_reply,
                    reply_to_msg_id=event.reply_to_msg_id,
                    media_type=media_type,
                    message_link=message_link,
                )

                await self.process_incoming_message(incoming)
            except Exception as e:
                logger.error(f"Error processing message {event.id}: {e}", exc_info=True)

        # Start Telethon Userbot client
        await self.telethon_client.start(phone=self.cfg.telegram_phone)
        console.print("[bold green]✓ Telethon Userbot connected and listening to all chats![/bold green]")

        # Start Scheduler for digests
        self.scheduler = DigestScheduler(self.run_digest_job, self.cfg)
        self.scheduler.start()

        # Start Interactive Bot Client if bot token is provided
        bot_task = None
        if self.cfg.telegram_bot_token:
            bot_task = asyncio.create_task(self._start_interactive_bot())

        # Run until disconnected
        await self.telethon_client.run_until_disconnected()
        if bot_task:
            bot_task.cancel()

    async def _start_interactive_bot(self) -> None:
        """Starts the interactive Telegram Bot to chat and respond to button clicks."""
        from telethon import TelegramClient, events, Button

        bot_client = TelegramClient(
            "bot_listener_session",
            self.cfg.telegram_api_id,
            self.cfg.telegram_api_hash,
        )
        await bot_client.start(bot_token=self.cfg.telegram_bot_token)
        console.print("[bold green]✓ Interactive Bot Assistant started! You can now chat with your bot on Telegram.[/bold green]")

        # Menu Navigation Buttons
        def get_main_menu():

            return [
                [Button.inline("🚨 P0 Urgent", data=b"tier_P0"),
                 Button.inline("🔴 P1 Important", data=b"tier_P1")],
                [Button.inline("🟡 P2 Updates", data=b"tier_P2"),
                 Button.inline("🟢 P3 Chatter", data=b"tier_P3")],
                [Button.inline("📋 Full Summary", data=b"btn_digest"),
                 Button.inline("📊 Stats", data=b"btn_stats")],
                [Button.inline("🔄 Refresh New Messages", data=b"btn_refresh")],
            ]

        def get_tier_menu(current_tier: str):
            return [
                [
                    Button.inline("🚨 P0" + (" ✓" if current_tier == "P0" else ""), data=b"tier_P0"),
                    Button.inline("🔴 P1" + (" ✓" if current_tier == "P1" else ""), data=b"tier_P1"),
                    Button.inline("🟡 P2" + (" ✓" if current_tier == "P2" else ""), data=b"tier_P2"),
                    Button.inline("🟢 P3" + (" ✓" if current_tier == "P3" else ""), data=b"tier_P3"),
                ],
                [
                    Button.inline("🔄 Refresh", data=f"tier_{current_tier}".encode()),
                    Button.inline("📋 Summary", data=b"btn_digest"),
                    Button.inline("🏠 Main Menu", data=b"btn_menu"),
                ],
            ]

        def get_sub_menu():
            return [
                [Button.inline("🚨 P0 Urgent", data=b"tier_P0"),
                 Button.inline("🔴 P1 Important", data=b"tier_P1")],
                [Button.inline("🔄 Refresh", data=b"btn_digest"),
                 Button.inline("🏠 Main Menu", data=b"btn_menu")],
            ]

        welcome_text = (
            f"👋 <b>Hi {self.cfg.profile.user_name}!</b>\n\n"
            "Here is your personal message hub.\n"
            "Tap any priority below to see your updates:\n\n"
            "• 🚨 <b>P0 Urgent:</b> Critical blockers & emergencies\n"
            "• 🔴 <b>P1 Important:</b> Deadlines & questions for you\n"
            "• 🟡 <b>P2 Updates:</b> Project progress & useful notes\n"
            "• 🟢 <b>P3 Chatter:</b> Memes, greetings & casual chat\n\n"
            "<i>💬 Or type any question below to ask AI!</i>"
        )

        async def safe_edit_or_respond(event, text: str, buttons=None):
            try:
                await event.edit(text, parse_mode="HTML", buttons=buttons)
            except Exception:
                await event.respond(text, parse_mode="HTML", buttons=buttons)

        async def render_tier_view(event, tier: str):
            records = await self.db.get_messages_by_tier(tier, limit=10)
            tier_titles = {
                "P0": ("🚨 <b>Urgent Messages (P0)</b>", "No urgent emergencies right now. All clear! 👍"),
                "P1": ("🔴 <b>Important Messages (P1)</b>", "No pending important tasks or deadlines right now."),
                "P2": ("🟡 <b>Useful Updates (P2)</b>", "No project updates or announcements logged yet."),
                "P3": ("🟢 <b>Chatter & Low Priority (P3)</b>", "No casual messages or banter logged yet."),
            }
            title, empty_msg = tier_titles.get(tier, ("Messages", "No messages."))

            if not records:
                text = f"{title}\n━━━━━━━━━━━━━━━━━━━━━━\n\n🟢 <i>{empty_msg}</i>"
                await safe_edit_or_respond(event, text, get_tier_menu(tier))
                return

            lines = [f"{title}", "━━━━━━━━━━━━━━━━━━━━━━\n"]
            for idx, r in enumerate(records, 1):
                clean_summary = r.summary
                prefix = f"[{r.chat_title}]"
                if clean_summary.startswith(prefix):
                    clean_summary = clean_summary[len(prefix):].strip()

                lines.append(f"<b>{idx}. {r.chat_title}</b> <i>({r.sender_name})</i>")
                lines.append(f"   {clean_summary}")

                if r.needs_action and r.action:
                    lines.append(f"   👉 <b>Action:</b> <u>{r.action}</u>")
                if r.deadline:
                    lines.append(f"   ⏰ <b>Due:</b> <code>{r.deadline}</code>")
                if r.message_link:
                    lines.append(f"   🔗 <a href=\"{r.message_link}\">Open Chat</a>")
                lines.append("")

            await safe_edit_or_respond(event, "\n".join(lines), get_tier_menu(tier))

        @bot_client.on(events.NewMessage)
        async def on_bot_message(event):
            if not event.is_private:
                return

            text = (event.raw_text or "").strip()
            text_lower = text.lower()

            # 1. Start or Menu command
            if (
                text_lower.startswith(("/start", "/menu", "/help"))
                or text in ("❓ Help", "menu", "help", "start")
            ):
                await event.respond(
                    welcome_text,
                    parse_mode="HTML",
                    buttons=get_main_menu(),
                )
                return

            # 2. Priority check shortcuts
            if "p0" in text_lower or "urgent" in text_lower or "emergency" in text_lower:
                await render_tier_view(event, "P0")
                return
            if "p1" in text_lower or "important" in text_lower or "task" in text_lower:
                await render_tier_view(event, "P1")
                return
            if "p2" in text_lower or "update" in text_lower:
                await render_tier_view(event, "P2")
                return
            if "p3" in text_lower or "noise" in text_lower or "chatter" in text_lower:
                await render_tier_view(event, "P3")
                return

            # 3. Instant Summary / Digest command
            if (
                text_lower.startswith(("/digest", "/summary"))
                or text in ("📋 Instant Digest", "digest", "summary", "summarize")
            ):
                digest_text, stats = await self.digest_engine.generate_digest()
                if digest_text:
                    await event.respond(digest_text, parse_mode="HTML", buttons=get_sub_menu())
                else:
                    await event.respond(
                        "✅ <b>All caught up!</b>\n\nNo unread messages to summarize right now.",
                        parse_mode="HTML",
                        buttons=get_main_menu(),
                    )
                return

            # 4. Stats command
            if text in ("📊 Today Stats", "stats", "/stats"):
                stats = await self.db.get_stats()
                stat_text = (
                    "📊 <b>Quick Stats</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Total Messages:</b> {stats.get('total_messages', 0)}\n"
                    f"• <b>🚨 P0 Urgent:</b> {stats.get('p0_urgent', 0)}\n"
                    f"• <b>🔴 P1 Important:</b> {stats.get('p1_important', 0)}\n"
                    f"• <b>🟡 P2 Updates:</b> {stats.get('p2_useful', 0)}\n"
                    f"• <b>🟢 P3 Chatter:</b> {stats.get('p3_noise', 0)}\n"
                    f"• <b>⚡ Tasks Needed:</b> {stats.get('actions_needed', 0)}\n"
                    f"• <b>🚨 Alerts Sent:</b> {stats.get('alerts_sent', 0)}\n"
                    f"• <b>⏳ Pending Summary:</b> {stats.get('pending_digest', 0)}\n"
                )
                await event.respond(stat_text, parse_mode="HTML", buttons=get_sub_menu())
                return

            # 5. Natural Language Conversational AI Q&A
            recent = await self.db.get_recent_messages(limit=35)
            answer = await self.classifier.answer_user_query(text, recent)
            await event.respond(answer, parse_mode="HTML", buttons=get_main_menu())

        # Callback queries for inline buttons - EDIT IN PLACE (NO SPAM)
        @bot_client.on(events.CallbackQuery)
        async def on_callback(event):
            data = event.data

            # Check priority tier buttons (tier_P0, tier_P1, tier_P2, tier_P3)
            if data.startswith(b"tier_"):
                tier = data.decode().split("_")[1]
                await event.answer(f"Loading {tier}...")
                await render_tier_view(event, tier)

            elif data in (b"btn_digest", b"btn_refresh"):
                await event.answer("Updating summary...")
                digest_text, stats = await self.digest_engine.generate_digest()
                if digest_text:
                    await safe_edit_or_respond(event, digest_text, get_sub_menu())
                else:
                    await safe_edit_or_respond(
                        event,
                        "✅ <b>All caught up!</b>\n\nNo unread messages to summarize right now.",
                        get_main_menu(),
                    )

            elif data == b"btn_stats":
                await event.answer("Loading stats...")
                stats = await self.db.get_stats()
                stat_text = (
                    "📊 <b>Quick Stats</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Total Messages:</b> {stats.get('total_messages', 0)}\n"
                    f"• <b>🚨 P0 Urgent:</b> {stats.get('p0_urgent', 0)}\n"
                    f"• <b>🔴 P1 Important:</b> {stats.get('p1_important', 0)}\n"
                    f"• <b>🟡 P2 Updates:</b> {stats.get('p2_useful', 0)}\n"
                    f"• <b>🟢 P3 Chatter:</b> {stats.get('p3_noise', 0)}\n"
                    f"• <b>⚡ Tasks Needed:</b> {stats.get('actions_needed', 0)}\n"
                    f"• <b>🚨 Alerts Sent:</b> {stats.get('alerts_sent', 0)}\n"
                    f"• <b>⏳ Pending Summary:</b> {stats.get('pending_digest', 0)}\n"
                )
                await safe_edit_or_respond(event, stat_text, get_sub_menu())

            elif data == b"btn_menu":
                await event.answer()
                await safe_edit_or_respond(event, welcome_text, get_main_menu())

        await bot_client.run_until_disconnected()





async def run_daemon():
    app = TelegramPriorityApp()
    await app.initialize()
    await app.start_userbot()


async def run_manual_digest():
    app = TelegramPriorityApp()
    await app.initialize()
    console.print("[bold yellow]Triggering manual digest generation...[/bold yellow]")
    digest_text, stats = await app.digest_engine.generate_digest()
    if digest_text:
        console.print(Panel(digest_text, title="Generated Digest Preview"))
        if app.notifier.is_configured:
            sent = await app.notifier.send_digest(digest_text)
            if sent:
                console.print("[bold green]✓ Digest successfully sent to Telegram![/bold green]")
            else:
                console.print("[bold red]✗ Failed to send digest via Telegram Bot API.[/bold red]")
        else:
            console.print("[dim]Telegram notification bot not configured, digest printed above.[/dim]")
    else:
        console.print("[dim]No pending messages to summarize.[/dim]")


async def show_stats():
    app = TelegramPriorityApp()
    await app.initialize()
    stats = await app.db.get_stats()

    table = Table(title="Telegram AI Priority Bot Statistics")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="bold white")

    for k, v in stats.items():
        label = k.replace("_", " ").title()
        table.add_row(label, str(v))

    console.print(table)


def main():
    parser = argparse.ArgumentParser(description="Telegram AI Priority Filter & Digest Bot")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("run", help="Start the live Telegram Userbot listener and digest daemon")
    subparsers.add_parser("digest", help="Trigger an immediate digest of pending messages")
    subparsers.add_parser("stats", help="Display classification and queue statistics")
    subparsers.add_parser("test", help="Run simulated test classification suite")

    args = parser.parse_args()

    if args.command == "run":
        asyncio.run(run_daemon())
    elif args.command == "digest":
        asyncio.run(run_manual_digest())
    elif args.command == "stats":
        asyncio.run(show_stats())
    elif args.command == "test":
        from test_classifier import run_tests
        asyncio.run(run_tests())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
