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

    async def initialize(self) -> None:
        """Initialize database and subcomponents."""
        await self.db.init_db()

    async def process_incoming_message(self, msg: IncomingMessage) -> PriorityClassification:
        """Pipeline to filter, classify, save, and route an incoming message."""
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

        # Menu Keyboards
        reply_keyboard = [
            [Button.text("📋 Instant Digest", resize=True), Button.text("🔴 Priority Only", resize=True)],
            [Button.text("📊 Today Stats", resize=True), Button.text("❓ Help", resize=True)],
        ]

        def get_inline_menu():
            return [
                [Button.inline("📋 Generate Digest Now", data=b"btn_digest"),
                 Button.inline("🔴 Priority Messages", data=b"btn_priority")],
                [Button.inline("📊 System Stats", data=b"btn_stats"),
                 Button.inline("🔄 Refresh Menu", data=b"btn_menu")],
            ]

        welcome_text = (
            f"👋 <b>Hello {self.cfg.profile.user_name}!</b>\n\n"
            "I am your <b>AI Priority Gatekeeper & Executive Assistant</b>.\n\n"
            "• I monitor all your incoming chats in real-time.\n"
            "• Urgent alerts (P0/P1) are sent to you immediately.\n"
            "• All other updates are organized for scheduled digests.\n\n"
            "<i>💡 Use the buttons below or ask me any question like:\n"
            "\"What happened in CloudKH today?\"\n"
            "\"Did anyone mention deadline?\"</i>"
        )

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
                    buttons=get_inline_menu(),
                )
                return

            # 2. Instant Digest button/text
            if (
                text_lower.startswith(("/digest", "/summary"))
                or text in ("📋 Instant Digest", "digest", "summary", "summarize")
            ):
                await event.respond("⏳ <i>Generating digest from pending messages...</i>", parse_mode="HTML")
                digest_text, stats = await self.digest_engine.generate_digest()
                if digest_text:
                    await event.respond(digest_text, parse_mode="HTML", buttons=get_inline_menu())
                else:
                    await event.respond(
                        "✅ <b>All caught up!</b>\n\nNo unread or pending messages to summarize right now.",
                        parse_mode="HTML",
                        buttons=get_inline_menu(),
                    )
                return

            # 3. Priority Only button/text
            if (
                text_lower.startswith(("/priority", "/urgent"))
                or text in ("🔴 Priority Only", "priority", "urgent", "alerts")
            ):
                records = await self.db.get_priority_messages(limit=10)
                if not records:
                    await event.respond("🟢 No urgent or priority messages currently logged.", parse_mode="HTML", buttons=get_inline_menu())
                    return


                lines = ["🔴 <b>Latest Priority Messages (P0/P1)</b>\n"]
                for idx, r in enumerate(records, 1):
                    lines.append(f"<b>{idx}. {r.chat_title}</b> ({r.sender_name})")
                    lines.append(f"   {r.summary}")
                    if r.needs_action and r.action:
                        lines.append(f"   ⚡ <b>Action:</b> <u>{r.action}</u>")
                    if r.deadline:
                        lines.append(f"   ⏰ <b>Deadline:</b> <code>{r.deadline}</code>")
                    if r.message_link:
                        lines.append(f"   🔗 <a href=\"{r.message_link}\">Open Message</a>")
                    lines.append("")
                await event.respond("\n".join(lines), parse_mode="HTML", buttons=get_inline_menu())
                return

            # 4. Stats button/text
            if text in ("📊 Today Stats", "stats", "/stats"):
                stats = await self.db.get_stats()
                stat_text = (
                    "📊 <b>Telegram AI Filter Statistics</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Total Messages Ingested:</b> {stats.get('total_messages', 0)}\n"
                    f"• <b>🔴 P0 Urgent:</b> {stats.get('p0_urgent', 0)}\n"
                    f"• <b>🔴 P1 Important:</b> {stats.get('p1_important', 0)}\n"
                    f"• <b>🟡 P2 Useful Updates:</b> {stats.get('p2_useful', 0)}\n"
                    f"• <b>🟢 P3 Filtered Noise:</b> {stats.get('p3_noise', 0)}\n"
                    f"• <b>⚡ Action Items:</b> {stats.get('actions_needed', 0)}\n"
                    f"• <b>🚨 Instant Alerts Sent:</b> {stats.get('alerts_sent', 0)}\n"
                    f"• <b>⏳ Pending in Digest Queue:</b> {stats.get('pending_digest', 0)}\n"
                )
                await event.respond(stat_text, parse_mode="HTML", buttons=get_inline_menu())
                return

            # 5. Natural Language Conversational AI Q&A
            await event.respond("🤔 <i>Searching recent messages and thinking...</i>", parse_mode="HTML")
            recent = await self.db.get_recent_messages(limit=35)
            answer = await self.classifier.answer_user_query(text, recent)
            await event.respond(answer, buttons=get_inline_menu())

        # Callback queries for inline buttons
        @bot_client.on(events.CallbackQuery)
        async def on_callback(event):
            data = event.data

            if data == b"btn_digest":
                await event.answer("Generating digest...")
                digest_text, stats = await self.digest_engine.generate_digest()
                if digest_text:
                    await event.respond(digest_text, parse_mode="HTML", buttons=get_inline_menu())
                else:
                    await event.respond("✅ <b>No pending messages!</b> You are all caught up.", parse_mode="HTML", buttons=get_inline_menu())

            elif data == b"btn_priority":
                await event.answer("Loading priority items...")
                records = await self.db.get_priority_messages(limit=10)
                if not records:
                    await event.respond("🟢 No priority messages logged yet.", parse_mode="HTML", buttons=get_inline_menu())
                else:
                    lines = ["🔴 <b>Latest Priority Messages (P0/P1)</b>\n"]
                    for idx, r in enumerate(records, 1):
                        lines.append(f"<b>{idx}. {r.chat_title}</b> ({r.sender_name})")
                        lines.append(f"   {r.summary}")
                        if r.needs_action and r.action:
                            lines.append(f"   ⚡ <b>Action:</b> <u>{r.action}</u>")
                        if r.deadline:
                            lines.append(f"   ⏰ <b>Deadline:</b> <code>{r.deadline}</code>")
                        if r.message_link:
                            lines.append(f"   🔗 <a href=\"{r.message_link}\">Open Message</a>")
                        lines.append("")
                    await event.respond("\n".join(lines), parse_mode="HTML", buttons=get_inline_menu())

            elif data == b"btn_stats":
                await event.answer("Loading stats...")
                stats = await self.db.get_stats()
                stat_text = (
                    "📊 <b>Telegram AI Filter Statistics</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Total Messages:</b> {stats.get('total_messages', 0)}\n"
                    f"• <b>🔴 P0 Urgent:</b> {stats.get('p0_urgent', 0)}\n"
                    f"• <b>🔴 P1 Important:</b> {stats.get('p1_important', 0)}\n"
                    f"• <b>🟡 P2 Useful:</b> {stats.get('p2_useful', 0)}\n"
                    f"• <b>🟢 P3 Noise:</b> {stats.get('p3_noise', 0)}\n"
                    f"• <b>⚡ Action Items:</b> {stats.get('actions_needed', 0)}\n"
                    f"• <b>🚨 Alerts Sent:</b> {stats.get('alerts_sent', 0)}\n"
                    f"• <b>⏳ Pending Queue:</b> {stats.get('pending_digest', 0)}\n"
                )
                await event.respond(stat_text, parse_mode="HTML", buttons=get_inline_menu())

            elif data == b"btn_menu":
                await event.answer()
                await event.respond(welcome_text, parse_mode="HTML", buttons=get_inline_menu())

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
