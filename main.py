"""Main Application Entrypoint for Telegram AI Priority Filter & Digest Bot."""

from __future__ import annotations

import argparse
import asyncio
import json
from html import escape
import logging
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from app.core.models import IncomingMessage, PriorityClassification
from app.priority.prefilter import evaluate_prefilter
from app.priority.classifier import AIClassifier
from app.priority.situations import SituationLinker
from app.telegram.notifier import Notifier
from app.priority.digest import DigestEngine
from app.telegram.scheduler import DigestScheduler
from app.ai.provider import OpenAICompatibleProvider
from app.security.access import AccessController
from app.services.assistant import AssistantService
from app.bot.navigation import route, parse
from app.usage.budget import UsageBudget, BudgetExceeded
from app.dashboard.http import DashboardServer
from app.integrations import IntegrationHub, GoogleAccount
from app.agent.runtime import AgentRuntime
from app.agent.tools import build_registry

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
        from app.storage.mongo_messages import MongoMessageDatabase
        if self.cfg.storage_backend.lower() != "mongo":
            raise RuntimeError("SQLite runtime storage has been removed. Set STORAGE_BACKEND=mongo and configure MONGO_URI.")
        if not self.cfg.mongo_uri:
            raise RuntimeError("MongoDB is required: set MONGO_URI before starting the assistant")
        self.db = MongoMessageDatabase(self.cfg.mongo_uri, self.cfg.mongo_database)
        self.classifier = AIClassifier(self.cfg)
        self.situation_linker = SituationLinker(self.cfg)
        self.notifier = Notifier(self.cfg)
        self.digest_engine = DigestEngine(self.db)
        self.scheduler: Optional[DigestScheduler] = None
        self.telethon_client = None
        self._last_alert_time_by_chat: dict[int, float] = {}
        # Owner-selected chat focus. An empty map preserves the legacy
        # behaviour of monitoring every non-ignored chat until setup is saved.
        self._focus_settings: dict[int, dict] = {}
        self._setup_state: dict[int, dict] = {}
        self._pm_state: dict[int, dict] = {}
        # Explicit, owner-approved requests to read a chat outside the saved
        # monitoring scope. Entries are removed after a one-time request.
        self._pending_access_requests: dict[tuple[int, int], dict] = {}
        # Business-assistant services are transport independent. Telegram
        # handlers below only translate events into this use case.
        from app.storage.mongo import MongoBusinessRepository
        self.business_repository = MongoBusinessRepository(self.cfg.mongo_uri, self.cfg.mongo_database)
        self.business_access = AccessController(
            owner_id=self.cfg.owner_user_id or self.cfg.notification_chat_id,
            approved_users=set(self.cfg.approved_user_id_list),
            approved_groups=set(self.cfg.approved_group_id_list),
        )
        self.business_budget = UsageBudget(monthly_limit_usd=self.cfg.ai_monthly_budget_usd)
        self.integrations = IntegrationHub(
            {"email": self.cfg.email_webhook_url, "calendar": self.cfg.calendar_webhook_url, "crm": self.cfg.crm_webhook_url, "task": self.cfg.task_webhook_url},
            self.cfg.integration_secret,
        )
        # Google Calendar/Gmail are owner-only and, when connected, take
        # priority over the generic webhook stub above for the same two
        # action types (see the action_approve_ callback).
        self.google_account = GoogleAccount(
            self.business_repository,
            self.cfg.google_client_id,
            self.cfg.google_client_secret,
            self.cfg.google_oauth_redirect_uri,
        )
        if self.cfg.ai_provider.lower() != "ollama":
            raise RuntimeError("Only Ollama is supported. Set AI_PROVIDER=ollama and configure OLLAMA_BASE_URL, OLLAMA_MODEL, and OLLAMA_API_KEY.")
        business_provider = OpenAICompatibleProvider(
            self.cfg.ollama_base_url,
            self.cfg.ollama_api_key,
            self.cfg.ollama_model,
            timeout_seconds=self.cfg.ai_request_timeout_seconds,
        )
        input_rate = output_rate = 0.0
        self.business_provider = business_provider
        self.business_assistant = AssistantService(
            self.business_repository,
            self.business_access,
            business_provider,
            self.business_budget,
            input_price_per_million=input_rate,
            output_price_per_million=output_rate,
        )
        self.assistant_services = {"business": self.business_assistant}
        self._assistant_selection: dict[int, str] = {}
        self.dashboard_server = None
        self.dashboard_thread = None
        for assistant_id in self.cfg.assistant_id_list:
            if assistant_id != "business":
                self.assistant_services[assistant_id] = AssistantService(
                    self.business_repository,
                    self.business_access,
                    business_provider,
                    self.business_budget,
                    assistant_id=assistant_id,
                    input_price_per_million=input_rate,
                    output_price_per_million=output_rate,
                )
        selected_model = self.cfg.ollama_model
        logger.info("Business AI provider: %s | model: %s", self.cfg.ai_provider, selected_model)
        logger.info(
            "Business access owner: %s | approved users: %d | approved groups: %d",
            self.business_access.owner_id,
            len(self.business_access.approved_users),
            len(self.business_access.approved_groups),
        )

    async def initialize(self) -> None:
        """Initialize database and subcomponents."""
        await self.db.init_db()
        await self.business_repository.init()
        for focus in await self.business_repository.list_focus_scopes("business"):
            self._focus_settings[int(focus["chat_id"])] = focus
            if focus.get("chat_type") in {"group", "channel"}:
                self.business_access.enable_group(int(focus["chat_id"]))
        # Restore approvals created by the setup wizard after a restart. The
        # environment variables remain useful as bootstrap defaults.
        for user in await self.business_repository.list_users():
            if user.get("approved"):
                self.business_access.approve_user(int(user["user_id"]))
        if not self._focus_settings:
            logger.info("Workspace setup required: Telegram message processing is paused until the owner selects chats.")
        if self.business_access.owner_id is not None:
            await self.business_repository.upsert_user(self.business_access.owner_id, role="admin", approved=True)
        for user_id in self.business_access.approved_users:
            await self.business_repository.upsert_user(user_id, approved=True)
        for assistant_id in self.assistant_services:
            await self.business_repository.upsert_profile(
                assistant_id,
                assistant_id.replace("_", " ").title() + " Assistant",
                "Answer clearly from the authorized business context.",
                True,
            )
        current_period = self.business_budget.period
        self.business_budget.spent_usd = await self.business_repository.usage_total(current_period)

    async def process_incoming_message(self, msg: IncomingMessage) -> PriorityClassification:
        """Pipeline to filter, classify, save, and route an incoming message."""
        import time

        # 1. Fast rule-based pre-filter (Free / No AI cost)
        prefiltered_cls = evaluate_prefilter(msg, self.cfg.profile.vip_senders)
        if prefiltered_cls:
            logger.info(f"[PRE-FILTER NOISE] {msg.chat_title} | {msg.sender_name}: {msg.text[:50]}")
            await self.db.save_message(msg, prefiltered_cls, is_prefiltered=True, alert_sent=False)
            return prefiltered_cls

        # 2. AI Priority Classification, admitted through the same allowance
        # used by business chat and documents. The classifier does not expose
        # provider usage yet, so use a conservative bounded estimate here.
        estimated_input = max(1000, (len(msg.text or "") + 3) // 4)
        estimated_output = 150
        reserved = (
            estimated_input * self.cfg.ai_input_price_per_million / 1_000_000
            + estimated_output * self.cfg.ai_output_price_per_million / 1_000_000
        )
        try:
            self.business_budget.reserve(reserved)
            cls = await self.classifier.classify(msg)
        except BudgetExceeded:
            logger.warning("AI allowance reached; using heuristic priority classification")
            cls = self.classifier._fallback_classification(msg, reason="AI allowance reached")
            reserved = 0.0
        except Exception:
            self.business_budget.settle(0.0, reserved)
            raise
        else:
            self.business_budget.settle(reserved, reserved)
            period = datetime.now(timezone.utc).strftime("%Y-%m")
            await self.business_repository.record_usage(
                period,
                "business",
                "classification",
                estimated_input,
                estimated_output,
                reserved,
                model=self.cfg.ollama_model,
            )
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

        # 3b. Situation fusion: fold this message into a persistent, evolving
        # issue instead of leaving it as an unrelated scored message. Only
        # for messages that could plausibly be part of an ongoing issue --
        # chatter, questions, and P3 noise never create or touch one.
        if self.situation_linker.eligible(cls):
            try:
                open_situations = await self.db.get_open_situations(msg.chat_id, limit=5)
                decision = await self.situation_linker.link(msg, cls, open_situations)
                if decision.action in {"continue", "resolve"} and decision.situation_id:
                    await self.db.update_situation(decision.situation_id, msg, decision, cls)
                else:
                    await self.db.create_situation(msg, decision, cls)
            except Exception:
                logger.exception(f"Situation linking failed for record #{record_id}")

        # 4. Dispatch Instant Notification if high priority
        if should_alert:
            logger.info(f"🚨 Sending Instant Alert for record #{record_id} (Score {cls.score})")
            await self.notifier.send_instant_alert(msg, cls)

        return cls


    async def run_digest_job(self) -> None:
        """Periodic job to generate and dispatch digest."""
        if not self._focus_settings:
            logger.info("Digest skipped: workspace setup is incomplete and no chats are selected.")
            return
        logger.info("Executing scheduled digest generation...")
        due_reminders = await self.business_repository.claim_due_reminders(datetime.now(timezone.utc).isoformat(), 50)
        for reminder in due_reminders:
            await self.notifier.send_message(f"⏰ <b>Reminder</b>\n\n{escape(str(reminder.get('text', '')))}")
        digest_text, stats = await self.digest_engine.generate_digest(self.focused_chat_ids())
        if digest_text:
            logger.info(f"Generated digest for {stats.total_messages} messages (P0:{stats.p0_count}, P1:{stats.p1_count}, P2:{stats.p2_count}, P3:{stats.p3_count})")
            await self.notifier.send_digest(digest_text)
        else:
            logger.info("No pending messages for digest.")

    def selected_assistant(self, user_id: int) -> AssistantService:
        return self.assistant_services.get(self._assistant_selection.get(user_id, "business"), self.business_assistant)

    def focused_chat_ids(self) -> set[int]:
        """Return the explicit chat scope for message reads and summaries."""
        return {int(chat_id) for chat_id, row in self._focus_settings.items() if row.get("enabled", True)}

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

        if self.cfg.dashboard_public_url and self.cfg.dashboard_token:
            self.dashboard_server = DashboardServer(
                self.business_repository,
                self.business_budget,
                self.cfg.dashboard_host,
                self.cfg.dashboard_port,
                self.cfg.dashboard_token,
                self.cfg.schedule_times_list,
                self.business_access.owner_id,
                google_account=self.google_account,
            )
            self.dashboard_thread = threading.Thread(target=self.dashboard_server.serve_forever, name="dashboard", daemon=True)
            self.dashboard_thread.start()
            logger.info("Telegram Web App dashboard listening on %s:%s", self.cfg.dashboard_host, self.cfg.dashboard_port)

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
            # Privacy-first default: before the owner completes setup, do not
            # inspect chat metadata or message content from any conversation.
            # The interactive bot remains available so /setup can be opened.
            if not self._focus_settings:
                return

            # Reject unselected peers before resolving senders or reading content.
            if int(event.chat_id) not in self.focused_chat_ids():
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
                # Once the owner saves a focus setup, only selected chats are
                # sent through classification and alerts. This keeps private
                # supplier/customer conversations out of the workspace unless
                # they were explicitly selected.
                focus = self._focus_settings.get(int(chat_id))
                if self._focus_settings and not focus:
                    return
                if focus and focus.get("mode") == "mention":
                    message = getattr(event, "message", None)
                    if not (getattr(message, "mentioned", False) or getattr(message, "is_reply", False)):
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
            def report_bot_task(task: asyncio.Task) -> None:
                if task.cancelled():
                    return
                error = task.exception()
                if error:
                    logger.error("Interactive bot stopped before it could receive messages", exc_info=(type(error), error, error.__traceback__))
            bot_task.add_done_callback(report_bot_task)

        # Run until disconnected
        await self.telethon_client.run_until_disconnected()
        if bot_task:
            bot_task.cancel()

    async def _start_interactive_bot(self) -> None:
        """Starts the interactive Telegram Bot to chat and respond to button clicks."""
        from telethon import TelegramClient, events, Button

        bot_client = TelegramClient(
            self.cfg.telegram_bot_session_name,
            self.cfg.telegram_api_id,
            self.cfg.telegram_api_hash,
        )
        await bot_client.start(bot_token=self.cfg.telegram_bot_token)
        console.print("[bold green]✓ Interactive Bot Assistant started! You can now chat with your bot on Telegram.[/bold green]")
        # Used by the owner-only dialog picker to exclude the notification bot
        # itself from the list of people and groups.
        bot_id = int(self.cfg.telegram_bot_token.split(":")[0]) if self.cfg.telegram_bot_token and ":" in self.cfg.telegram_bot_token else None

        # Menu Navigation Buttons
        def get_webapp_keyboard():
            if not self.cfg.dashboard_public_url or not self.cfg.dashboard_token:
                return None
            from urllib.parse import urlencode
            url = self.cfg.dashboard_public_url.rstrip("/") + "/webapp?" + urlencode({"token": self.cfg.dashboard_token})
            from telethon.tl import types
            return types.ReplyKeyboardMarkup(
                rows=[types.KeyboardButtonRow(buttons=[types.KeyboardButtonWebView(text="📊 Open Project Workspace", url=url)])],
                resize=True,
                single_use=False,
            )

        def get_main_menu():
            if not self._focus_settings:
                return [[Button.inline("⚙️ Complete setup first", data=b"btn_setup")]]
            return [
                [Button.inline("➕ New Task", data=b"btn_new_task"), Button.inline("📁 Projects", data=b"btn_projects")],
                [Button.inline("⏰ Due Soon", data=b"btn_due"), Button.inline("📋 Digest", data=b"btn_digest")],
                [Button.inline("✅ My Work", data=b"btn_mywork")],
                [Button.inline("⋯ More", data=b"btn_more")],
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
                    Button.inline("💬 Sources", data=b"btn_groups"),
                    Button.inline("🏠 Home", data=b"btn_menu"),
                ],
            ]

        def get_groups_menu(groups: list):
            buttons = []
            # Add up to 4 quick chat drill-down buttons (2 per row)
            row = []
            for g in groups[:4]:
                title = g.get("chat_title", "Chat")
                short_title = (title[:11].rstrip() + "…") if len(title) > 12 else title
                icon = "👤" if g.get("chat_type") == "private" else ("📣" if g.get("chat_type") == "channel" else "👥")
                row.append(Button.inline(f"{icon} {short_title}", data=f"chat_{g['chat_id']}".encode()))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)

            buttons.append([Button.inline("📋 Digest", data=b"btn_digest"), Button.inline("🏠 Home", data=b"btn_menu")])
            return buttons

        def get_sub_menu():
            return [
                [Button.inline("🚨 P0 Urgent", data=b"tier_P0"),
                 Button.inline("🔴 P1 Important", data=b"tier_P1")],
                [Button.inline("💬 Sources", data=b"btn_groups"),
                 Button.inline("📋 Digest", data=b"btn_digest")],
                [Button.inline("🏠 Home", data=b"btn_menu")],
            ]

        def clip_text(value: str, limit: int = 150) -> str:
            """Keep Telegram cards readable while preserving complete records in storage."""
            clean = " ".join((value or "").split())
            return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"

        def get_welcome_text():
            if not self._focus_settings:
                return (
                    f"👋 <b>Hi {self.cfg.profile.user_name}!</b>\n\n"
                    "🔒 <b>Setup required before use.</b>\n\n"
                    "For privacy, I will not inspect or process messages yet. "
                    "First choose the people and groups I should focus on.\n\n"
                    "Tap <b>Complete setup first</b> to select your groups, "
                    "supplier/customer chats, and approved members."
                )
            return (
                f"👋 <b>Hi {self.cfg.profile.user_name}!</b>\n\n"
                "Here is your business message hub.\n"
                "Choose a view below to review what arrived:\n\n"
                "• 🚨 <b>P0 Urgent:</b> Critical blockers & emergencies\n"
                "• 🔴 <b>P1 Important:</b> Deadlines & questions for you\n"
                "• 🟡 <b>P2 Updates:</b> Project progress & useful notes\n"
                "• 🟢 <b>P3 Chatter:</b> Memes, greetings & casual chat\n"
                "• 💬 <b>Sources:</b> Approved personal and group conversations\n\n"
                "• 📁 <b>Projects:</b> Owners, due dates, dependencies, and CPM plans\n\n"
                "<i>💬 Or type any question below to ask AI!</i>"
            )

        async def safe_edit_or_respond(event, text: str, buttons=None):
            try:
                await event.edit(text, parse_mode="HTML", buttons=buttons)
            except Exception:
                await event.respond(text, parse_mode="HTML", buttons=buttons)

        def setup_state(user_id: int) -> dict:
            """Return the owner's in-progress focus setup."""
            state = self._setup_state.get(user_id)
            if state is None:
                state = {
                    "assistant_id": self._assistant_selection.get(user_id, "business"),
                    "chats": {
                        chat_id: {
                            "chat_title": row.get("chat_title") or "Chat",
                            "chat_type": row.get("chat_type") or "private",
                            "mode": row.get("mode") or "monitor",
                        }
                        for chat_id, row in self._focus_settings.items()
                    },
                    "users": set(self.business_access.approved_users),
                    "awaiting_user": False,
                    "awaiting_search": False,
                    "picker_query": "",
                    "picker_kind": "all",
                    "picker_page": 0,
                }
                self._setup_state[user_id] = state
            return state

        def setup_home_buttons(state: dict):
            buttons = []
            assistants = list(self.assistant_services)
            for start in range(0, len(assistants), 2):
                buttons.append([
                    Button.inline(
                        ("✅ " if state.get("assistant_id") == assistant_id else "🤖 ") + assistant_id,
                        data=f"setup_assistant_{assistant_id}".encode(),
                    )
                    for assistant_id in assistants[start:start + 2]
                ])
            buttons.extend([
                [Button.inline("💬 Manage Sources", data=b"setup_chats"), Button.inline("👥 Manage Access", data=b"setup_add_user")],
                [Button.inline("💾 Save Changes", data=b"setup_save")],
                [Button.inline("🏠 Home", data=b"btn_menu")],
            ])
            return buttons

        async def render_setup_home(event, user_id: int):
            state = setup_state(user_id)
            selected = state.get("chats", {})
            lines = [
                "⚙️ <b>Workspace setup</b>",
                "<i>Choose which chats this assistant should focus on.</i>",
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"🤖 <b>Assistant:</b> {escape(state.get('assistant_id', 'business'))}",
                "",
                "<b>💬 Sources</b>",
            ]
            if selected:
                for chat_id, row in selected.items():
                    mode = row.get("mode", "monitor")
                    mode_label = {"monitor": "Everything", "mention": "Mentions only", "orders": "Action items"}.get(mode, "Active")
                    lines.append(f"🟢 {escape(clip_text(row.get('chat_title', 'Chat'), 35))} · {mode_label}")
            else:
                lines.append("• No chats selected yet")
            members = sorted(int(uid) for uid in state.get("users", set()))
            lines.extend([
                "",
                f"👥 <b>Workspace access:</b> {len(members)} approved member(s)",
                "",
                "Choose which conversations to monitor and who can use this assistant.",
            ])
            await safe_edit_or_respond(event, "\n".join(lines), setup_home_buttons(state))

        async def setup_dialogs():
            rows = await self.conversation.sources.directory()
            return [{"chat_id": row["id"], "chat_title": row["title"],
                     "chat_type": row["kind"], "chat_username": row["username"]} for row in rows]

        async def render_setup_chats(event, user_id: int, kind: str = "all", page: int = 0):
            state = setup_state(user_id)
            state["picker_kind"] = kind
            load_error = ""
            try:
                dialogs = await setup_dialogs()
            except Exception as exc:
                logger.exception("Could not load Telegram dialogs for setup")
                dialogs = []
                load_error = f" ({type(exc).__name__})"
            if kind == "groups":
                dialogs = [row for row in dialogs if row["chat_type"] in {"group", "channel"}]
            elif kind == "people":
                dialogs = [row for row in dialogs if row["chat_type"] == "private"]
            query = (state.get("picker_query") or "").strip().casefold()
            if query:
                dialogs = [
                    row for row in dialogs
                    if query in str(row.get("chat_title", "")).casefold()
                    or query in str(row.get("chat_username", "")).casefold()
                ]
            selected_ids = set(state.get("chats", {}))
            dialogs.sort(key=lambda row: (row["chat_id"] not in selected_ids, row["chat_title"].lower()))
            page_size = 12
            page_count = max(1, (len(dialogs) + page_size - 1) // page_size)
            page = max(0, min(int(page), page_count - 1))
            state["picker_page"] = page
            page_dialogs = dialogs[page * page_size:(page + 1) * page_size]
            lines = [
                f"{'👥' if kind == 'groups' else '👤' if kind == 'people' else '📂'} <b>Select {'groups' if kind == 'groups' else 'people' if kind == 'people' else 'chats'}</b> · {page + 1}/{page_count}",
                "Tap a name to select it. Choose what the assistant should watch.",
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"Selected chats (total): <b>{len(selected_ids)}</b>",
            ]
            if query:
                lines.append(f"Search: <code>{escape(query)}</code>")
            buttons = []
            for dialog in page_dialogs:
                chat_id = dialog["chat_id"]
                row = state.get("chats", {}).get(chat_id)
                marker = "✅" if row else "⬜"
                mode = row.get("mode", "monitor") if row else "off"
                mode_label = {"monitor": "Everything", "mention": "Mentions only", "orders": "Action items"}.get(mode, "Off")
                lines.append(f"{marker} <b>{escape(clip_text(dialog['chat_title'], 34))}</b> · {mode_label}")
                buttons.append([
                    Button.inline(f"{marker} {clip_text(dialog['chat_title'], 22)}", data=f"setup_pick_{chat_id}".encode()),
                    Button.inline("⚙️ Watch", data=f"setup_modes_{chat_id}".encode()),
                ])
            if not dialogs:
                lines.append(f"No Telegram chats were found{load_error}. Keep the user account connected and try again.")
            nav = []
            if page > 0:
                nav.append(Button.inline("⬅️ Previous", data=f"setup_page_{kind}_{page - 1}".encode()))
            if page < page_count - 1:
                nav.append(Button.inline("Next ➡️", data=f"setup_page_{kind}_{page + 1}".encode()))
            if nav:
                buttons.append(nav)
            search_buttons = [Button.inline("🔍 Search", data=b"setup_search")]
            if query:
                search_buttons.append(Button.inline("✖ Clear", data=b"setup_clear_search"))
            buttons.append(search_buttons)
            buttons.append([Button.inline("✅ Done", data=b"setup_home")])
            await safe_edit_or_respond(event, "\n".join(lines), buttons)

        async def render_setup_mode(event, user_id: int, chat_id: int):
            state = setup_state(user_id)
            row = state.get("chats", {}).get(chat_id)
            if not row:
                await event.answer("Select this chat first", alert=True)
                return
            title = escape(row.get("chat_title", "Chat"))
            text = (
                f"💬 <b>{title}</b>\n\n"
                "<b>What should I watch?</b>\n"
                "• Everything — read all messages in this source\n"
                "• Mentions only — pay attention when you are mentioned\n"
                "• Action items — look mainly for requests, tasks, and deadlines"
            )
            buttons = [
                [Button.inline("🔘 Everything", data=f"setup_setmode_{chat_id}_monitor".encode())],
                [Button.inline("@ Mentions only", data=f"setup_setmode_{chat_id}_mention".encode())],
                [Button.inline("✅ Action items", data=f"setup_setmode_{chat_id}_orders".encode())],
                [Button.inline("⏸ Stop monitoring", data=f"setup_remove_{chat_id}".encode())],
                [Button.inline("⬅️ Sources", data=f"setup_page_{state.get('picker_kind', 'all')}_{state.get('picker_page', 0)}".encode())],
            ]
            await safe_edit_or_respond(event, text, buttons)

        async def save_setup(user_id: int) -> tuple[bool, str]:
            state = setup_state(user_id)
            selected = state.get("chats", {})
            if not selected:
                return False, "Select at least one chat before saving."

            selected_ids = set(selected)
            existing = await self.business_repository.list_focus_scopes("business", enabled_only=False)
            for row in existing:
                if int(row["chat_id"]) not in selected_ids:
                    await self.business_repository.disable_focus_scope("business", int(row["chat_id"]))
            for chat_id, row in selected.items():
                await self.business_repository.upsert_focus_scope(
                    "business",
                    int(chat_id),
                    row.get("chat_title", "Chat"),
                    row.get("chat_type", "private"),
                    row.get("mode", "monitor"),
                    True,
                )
            self._focus_settings = {
                int(chat_id): {"chat_id": int(chat_id), **row}
                for chat_id, row in selected.items()
            }

            # The selected chats become the explicit group allowlist. Owner
            # access remains unconditional; member approvals are persisted.
            self.business_access.approved_groups = {
                int(chat_id)
                for chat_id, row in selected.items()
                if row.get("chat_type") in {"group", "channel"}
            }
            selected_users = {int(uid) for uid in state.get("users", set())}
            self.business_access.approved_users = selected_users
            for uid in selected_users:
                await self.business_repository.upsert_user(uid, approved=True)
            self._assistant_selection[user_id] = state.get("assistant_id", "business")
            return True, f"Saved {len(selected)} focused chat(s) and {len(selected_users)} approved member(s)."

        async def render_groups_view(event):
            # Start with the owner-selected scope so a newly selected chat is
            # visible even before its first message is captured. Enrich it
            # with activity counts when records are available.
            active = await self.db.get_active_groups(limit=50, allowed_chat_ids=self.focused_chat_ids())
            active_by_id = {int(row["chat_id"]): row for row in active}
            groups = []
            for chat_id, focus in self._focus_settings.items():
                activity = active_by_id.get(int(chat_id), {})
                groups.append({
                    "chat_id": int(chat_id),
                    "chat_title": activity.get("chat_title") or focus.get("chat_title") or "Chat",
                    "chat_type": activity.get("chat_type") or focus.get("chat_type") or "private",
                    "total_count": int(activity.get("total_count") or 0),
                    "priority_count": int(activity.get("priority_count") or 0),
                    "last_activity": activity.get("last_activity"),
                    "mode": focus.get("mode") or "monitor",
                })
            groups.sort(key=lambda row: str(row.get("last_activity") or ""), reverse=True)
            if not groups:
                text = (
                    "💬 <b>Sources</b>\n"
                    "<i>These conversations are monitored by your assistant.</i>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "🟢 <i>No messages have been captured yet. Keep the listener running and refresh this view.</i>"
                )
                await safe_edit_or_respond(event, text, get_main_menu())
                return

            lines = [
                "💬 <b>Sources</b>",
                "<i>These conversations are monitored by your assistant.</i>",
                "━━━━━━━━━━━━━━━━━━━━━━",
            ]

            for g in groups:
                chat_id = g["chat_id"]
                chat_title = g.get("chat_title") or "Direct Message"
                total = g.get("total_count", 0)
                p_count = g.get("priority_count", 0)

                badge = "🚨 " if p_count > 0 else "💬 "
                chat_type = g.get("chat_type") or "private"
                if chat_type == "private":
                    chat_icon, chat_kind = "👤", "Personal chat"
                elif chat_type == "channel":
                    chat_icon, chat_kind = "📣", "Channel"
                else:
                    chat_icon, chat_kind = "👥", "Group chat"
                lines.append(
                    f"\n{badge}{chat_icon} <b>{escape(clip_text(chat_title, 42))}</b> "
                    f"<i>· {chat_kind} · {total} messages captured</i>"
                )

                # Get latest 3 messages for this chat
                chat_msgs = await self.db.get_messages_for_chat(chat_id, limit=3, allowed_chat_ids=self.focused_chat_ids())
                if not chat_msgs:
                    lines.append("  <i>No messages captured yet for this focused chat.</i>")
                for m in chat_msgs:
                    clean = m.summary or m.text
                    prefix = f"[{m.chat_title}]"
                    if clean.startswith(prefix):
                        clean = clean[len(prefix):].strip()

                    p_icon = "🔴 " if m.priority in ("P0", "P1") else ("🟡 " if m.priority == "P2" else "• ")
                    lines.append(f"  {p_icon}{escape(clip_text(clean))}")
                    if m.needs_action and m.action:
                        lines.append(f"    👉 <b>Action:</b> <u>{escape(clip_text(m.action, 90))}</u>")
                    if m.deadline:
                        lines.append(f"    ⏰ <b>Due:</b> <code>{escape(m.deadline)}</code>")

                lines.append("")

            await safe_edit_or_respond(event, "\n".join(lines), get_groups_menu(groups))

        async def render_situations_view(event):
            situations = await self.db.list_situations(limit=20, allowed_chat_ids=self.focused_chat_ids())
            header = ["🔥 <b>Situations</b>", "<i>Issues fused from multiple messages, not a raw message list.</i>", "━━━━━━━━━━━━━━━━━━━━━━"]
            if not situations:
                await safe_edit_or_respond(event, "\n".join(header) + "\n\n🟢 <i>Nothing open right now.</i>", get_main_menu())
                return
            priority_icon = {"P0": "🚨", "P1": "🔴", "P2": "🟡", "P3": "🟢"}
            lines = list(header)
            buttons, row = [], []
            for s in situations[:10]:
                icon = priority_icon.get(s.priority, "🟡")
                guest = " 🧑‍🤝‍🧑" if s.guest_affected else ""
                lines.append(f"\n{icon} <b>{escape(clip_text(s.title, 60))}</b>{guest}")
                lines.append(f"  <i>{escape(clip_text(s.chat_title, 40))} · {s.message_count} message(s) · updated {escape(s.last_update_at[:16])}</i>")
                if s.current_action:
                    lines.append(f"  👉 {escape(clip_text(s.current_action, 90))}")
                short_title = (s.title[:14].rstrip() + "…") if len(s.title) > 15 else s.title
                row.append(Button.inline(f"{icon} {short_title}", data=f"situation_{s.id}".encode()))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            buttons.append([Button.inline("🏠 Home", data=b"btn_menu")])
            await safe_edit_or_respond(event, "\n".join(lines), buttons)

        async def render_situation_detail_view(event, situation_id: int):
            situation = await self.db.get_situation(situation_id)
            if not situation:
                await safe_edit_or_respond(event, "🔥 That situation could not be found.", get_main_menu())
                return
            status_label = {"open": "🔴 Open", "monitoring": "🟡 Monitoring", "resolved": "🟢 Resolved"}.get(situation.status, situation.status)
            lines = [
                f"🔥 <b>{escape(situation.title)}</b>",
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"<b>Status:</b> {status_label}",
                f"<b>Priority:</b> {situation.priority}",
                f"<b>Source:</b> {escape(situation.chat_title)}",
                f"<b>Guest affected:</b> {'Yes' if situation.guest_affected else 'No'}",
            ]
            if situation.responsible:
                lines.append(f"<b>Responsible:</b> {escape(situation.responsible)}")
            if situation.current_action:
                lines.append(f"<b>Current action:</b> {escape(situation.current_action)}")
            if situation.dependency:
                lines.append(f"<b>Dependency:</b> {escape(situation.dependency)}")
            lines.append(f"<b>Started:</b> {escape(situation.started_at[:16])}")
            lines.append(f"<b>Last update:</b> {escape(situation.last_update_at[:16])}")
            lines.append(f"<b>Messages linked:</b> {situation.message_count}")
            if situation.last_message_link:
                lines.append(f"<b>Latest source:</b> <a href=\"{escape(situation.last_message_link)}\">Open in Telegram</a>")
            buttons = []
            if situation.status != "resolved":
                buttons.append([Button.inline("✅ Mark resolved", data=f"situationresolve_{situation.id}".encode())])
            buttons.append([Button.inline("🔥 All Situations", data=b"btn_situations"), Button.inline("🏠 Home", data=b"btn_menu")])
            await safe_edit_or_respond(event, "\n".join(lines), buttons)

        async def render_memory_view(event):
            user_id = getattr(event, "sender_id", None)
            if user_id is None:
                await safe_edit_or_respond(event, "🧠 No conversation memory is available for this account.", get_main_menu())
                return
            conversation_id = await self.business_repository.create_or_get_conversation("business", user_id, event.chat_id)
            turns = await self.business_repository.recent_turns(conversation_id, limit=8)
            if not turns:
                text = "🧠 <b>Knowledge</b>\n\nNo saved context yet. Ask the assistant a question to begin."
            else:
                lines = ["🧠 <b>Knowledge</b>", "<i>Your saved assistant context</i>", "━━━━━━━━━━━━━━━━━━━━━━"]
                for turn in turns:
                    role = "You" if turn.get("role") == "user" else "Assistant"
                    lines.append(f"<b>{role}:</b> {escape(clip_text(turn.get('content', ''), 180))}")
                text = "\n\n".join(lines)
            await safe_edit_or_respond(event, text, get_main_menu())

        async def render_tasks_view(event):
            user_id = getattr(event, "sender_id", None)
            rows = await self.business_assistant.list_tasks(user_id, event.chat_id, "private", "open", 20)
            if not rows:
                text = "✅ <b>My Work</b>\n\nNo open work items yet."
            else:
                lines = ["✅ <b>Open tasks</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
                for row in rows:
                    due = f" · due {escape(str(row['due_at']))}" if row.get("due_at") else ""
                    lines.append(f"<b>#{escape(str(row['id']))}</b> {escape(clip_text(row['title'], 120))}{due}")
                lines.append("\nComplete one with <code>/done ID</code>.")
                text = "\n".join(lines)
            await safe_edit_or_respond(event, text, get_main_menu())

        async def render_home(event):
            text = "Hello! How can I help you today?"
            await safe_edit_or_respond(event, text)

        async def render_my_work(event, status="open"):
            user_id = getattr(event, "sender_id", None)
            query_status = "all" if status in {"urgent", "blocked"} else status
            rows = await self.business_assistant.list_work_items(user_id, event.chat_id, "private", query_status, 30)
            if status == "urgent":
                rows = [row for row in rows if row.get("priority") in {"P0", "P1"} and row.get("status") not in {"done", "completed"}]
            elif status == "blocked":
                rows = [row for row in rows if row.get("status") == "blocked"]
            title = "My Work" if status == "open" else f"My Work · {status.replace('_', ' ').title()}"
            if not rows:
                text = f"✅ <b>{title}</b>\n\nNothing is assigned to you yet.\n\nCreate a task or select work from one of your projects."
            else:
                lines = [f"✅ <b>{title}</b>", "<i>Real work items saved in the workspace</i>", "━━━━━━━━━━━━━━━━━━━━━━"]
                for row in rows:
                    key = row.get("display_key") or f"#{row.get('id')}"
                    due = f" · due {escape(str(row['due_at']))}" if row.get("due_at") else ""
                    lines.append(f"<b>{escape(str(key))}</b> · {escape(str(row.get('status','todo')))} · {escape(str(row.get('priority','P2')))}\n{escape(clip_text(row.get('title','Untitled'), 120))}{due}")
                text = "\n\n".join(lines)
            buttons = [[Button.inline("🚨 Urgent", data=b"mywork_urgent"), Button.inline("🔵 In Progress", data=b"mywork_in_progress")], [Button.inline("🚧 Blocked", data=b"mywork_blocked"), Button.inline("⏰ Due Soon", data=b"btn_due")], [Button.inline("📋 All Work", data=b"mywork_all"), Button.inline("🏠 Home", data=b"btn_menu")]]
            await safe_edit_or_respond(event, text, buttons)

        async def render_due_soon(event):
            user_id = getattr(event, "sender_id", None)
            cutoff = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
            rows = await self.business_assistant.list_work_items(user_id, event.chat_id, "private", "open", 30, due_before=cutoff)
            if not rows:
                text = "⏰ <b>Due Soon</b>\n\nNo open work items are due in the next 48 hours."
            else:
                lines = ["⏰ <b>Due Soon</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
                for row in rows:
                    lines.append(f"<b>{escape(str(row.get('display_key') or '#'+str(row.get('id'))))}</b> · {escape(str(row.get('due_at')))}\n{escape(clip_text(row.get('title','Untitled'), 140))}")
                text = "\n\n".join(lines)
            await safe_edit_or_respond(event, text, [[Button.inline("✅ My Work", data=b"btn_mywork"), Button.inline("🏠 Home", data=b"btn_menu")]])

        async def render_more(event):
            await safe_edit_or_respond(
                event,
                "⋯ <b>More</b>\n\nChoose an additional workspace view.",
                [
                    [Button.inline("💬 Sources", data=b"btn_groups"), Button.inline("🔥 Situations", data=b"btn_situations")],
                    [Button.inline("🔐 Access", data=b"btn_access"), Button.inline("🧠 Knowledge", data=b"btn_memory")],
                    [Button.inline("📊 Reports", data=b"btn_stats"), Button.inline("⚙️ Settings", data=b"btn_setup")],
                    [Button.inline("🏠 Home", data=b"btn_menu")],
                ],
            )

        async def render_access_view(event):
            user_id = getattr(event, "sender_id", None)
            if user_id != self.business_access.owner_id:
                await event.respond("🔒 Only the owner can view bot access settings.")
                return
            users = await self.business_repository.list_users(100)
            approved_users = [str(row.get("user_id")) for row in users if row.get("approved")]
            sources = await self.business_repository.list_focus_scopes("business")
            source_lines = []
            group_lines = []
            for row in sources:
                chat_type = row.get("chat_type") or "private"
                icon = "👤" if chat_type == "private" else ("📣" if chat_type == "channel" else "👥")
                mode = {"monitor": "Everything", "mention": "Mentions only", "orders": "Action items"}.get(row.get("mode"), "Active")
                label = f"{icon} {row.get('chat_title') or row.get('chat_id')} · {mode}"
                source_lines.append(f"• {label}")
                if chat_type in {"group", "channel"}:
                    group_lines.append(f"• {label}")
            text = (
                "🔐 <b>Bot access</b>\n\n"
                f"<b>Owner:</b> <code>{escape(str(self.business_access.owner_id))}</code>\n"
                f"<b>Approved people:</b> {escape(', '.join(approved_users) or 'none')}\n"
                "<b>Approved group chats:</b>\n"
                + ("\n".join(group_lines) if group_lines else "• none")
                + "\n\n<b>Message sources the bot may read:</b>\n"
                + ("\n".join(source_lines) if source_lines else "• none")
                + "\n\nA selected personal chat is a read source; it does not grant that person permission to use the bot. Group access and message-source access are also managed separately."
            )
            await safe_edit_or_respond(event, text, [[Button.inline("⚙️ Manage setup", data=b"btn_setup")], [Button.inline("🏠 Home", data=b"btn_menu")]])

        async def render_reminders_view(event):
            user_id = getattr(event, "sender_id", None)
            rows = await self.business_assistant.list_reminders(user_id, event.chat_id, "private", 20)
            if not rows:
                text = "⏰ <b>Reminders</b>\n\nNo pending reminders. Create one with <code>/remind 2026-09-04T18:00:00+07:00 Call client</code>."
            else:
                lines = ["⏰ <b>Pending reminders</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
                for row in rows:
                    lines.append(f"<b>#{escape(str(row['id']))}</b> {escape(str(row['remind_at']))} · {escape(clip_text(row['text'], 120))}")
                text = "\n".join(lines)
            await safe_edit_or_respond(event, text, get_main_menu())

        async def render_projects_view(event):
            user_id = getattr(event, "sender_id", None)
            projects = await self.business_assistant.list_projects(user_id, event.chat_id, "private")
            if not projects:
                text = (
                    "📁 <b>Projects</b>\n"
                    "<i>Plan work, assign owners, and track delivery.</i>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "No projects yet.\n\nProjects organize tasks, deadlines, and team activity in one place."
                )
                buttons = [[Button.inline("➕ Create Project", data=b"btn_new_project")], [Button.inline("🏠 Home", data=b"btn_menu")]]
            else:
                lines = ["📁 <b>Projects</b>", "<i>One place for backlog, status, and CPM planning.</i>", "━━━━━━━━━━━━━━━━━━━━━━"]
                buttons = []
                for project in projects:
                    project_id = project["id"]
                    project_tasks = await self.business_repository.list_tasks(user_id, "all", 100, project_id=project_id)
                    counts = {status: sum(1 for task in project_tasks if ("todo" if task.get("status") == "to_do" else "done" if task.get("status") == "completed" else task.get("status")) == status) for status in ("todo", "in_progress", "review", "waiting", "blocked", "done")}
                    due = f" · due {escape(str(project['target_date']))}" if project.get("target_date") else ""
                    lines.append(
                        f"\n📁 <b>{escape(str(project.get('project_key') or project_id))}</b> · {escape(project['name'])} · "
                        f"{escape(project.get('status', 'active'))}{due}\n"
                        f"   📝 {counts['todo']} to do · 🔄 {counts['in_progress']} in progress · "
                        f"🕒 {counts['review']} review · 🚧 {counts['blocked']} blocked · ✅ {counts['done']} done"
                    )
                    buttons.append([
                        Button.inline(f"📁 View {project.get('project_key') or project_id}", data=route("project", "view", project_id)),
                        Button.inline(f"📈 CPM {project.get('project_key') or project_id}", data=route("project", "plan", project_id)),
                    ])
                lines.append("\nCreate work with <code>/ptask PROJECT_ID | Task name | duration days | assignee ID | priority | due date</code>.")
                text = "\n".join(lines)
                buttons.append([Button.inline("🏠 Home", data=b"btn_menu")])
            await safe_edit_or_respond(event, text, buttons)

        async def render_project_board_view(event, project_id):
            user_id = getattr(event, "sender_id", None)
            try:
                plan = await self.business_assistant.project_plan(user_id, event.chat_id, "private", project_id)
            except (PermissionError, ValueError) as exc:
                await safe_edit_or_respond(event, f"⚠️ {escape(str(exc))}", [[Button.inline("📁 Projects", data=b"btn_projects")]])
                return
            if not plan:
                await safe_edit_or_respond(event, "⚠️ Project not found.", [[Button.inline("📁 Projects", data=b"btn_projects")]])
                return
            project = plan.get("project", {})
            columns = {"todo": [], "in_progress": [], "review": [], "waiting": [], "blocked": [], "done": []}
            for task in plan.get("tasks", []):
                status = {"to_do": "todo", "completed": "done", "open": "todo"}.get(task.get("status"), task.get("status", "todo"))
                columns.setdefault(status, []).append(task)
            lines = [
                f"🗂 <b>{escape(str(project.get('name', 'Project')))} · Board</b>",
                "<i>Track work through the shared project workflow.</i>",
                "━━━━━━━━━━━━━━━━━━━━━━",
            ]
            labels = [("todo", "📝 To do"), ("in_progress", "🔄 In progress"), ("review", "🔎 Review"), ("waiting", "⏳ Waiting"), ("blocked", "🚧 Blocked"), ("done", "✅ Done")]
            for status, label in labels:
                lines.append(f"\n<b>{label}</b> · {len(columns.get(status, []))}")
                for task in columns.get(status, [])[:8]:
                    owner = f" · @{task.get('assignee_id')}" if task.get("assignee_id") else ""
                    lines.append(f"  • <b>{escape(str(task.get('display_key') or '#'+str(task.get('id'))))}</b> {escape(clip_text(task.get('title', 'Untitled'), 100))}{owner}")
            item_buttons = []
            for task in plan.get("tasks", [])[:12]:
                item_buttons.append([Button.inline(str(task.get("display_key") or task.get("id")), data=route("work", "view", task.get("id")))])
            buttons = item_buttons + [
                [Button.inline(f"📈 CPM {project.get('project_key') or project_id}", data=route("project", "plan", project_id)), Button.inline("📁 Projects", data=b"btn_projects")],
                [Button.inline("🏠 Home", data=b"btn_menu")],
            ]
            await safe_edit_or_respond(event, "\n".join(lines), buttons)

        async def render_work_item_view(event, item_id):
            user_id = getattr(event, "sender_id", None)
            item = await self.business_assistant.get_work_item(user_id, event.chat_id, "private", item_id)
            if not item:
                await safe_edit_or_respond(event, "⚠️ Work item not found.", [[Button.inline("🏠 Home", data=b"btn_menu")]])
                return
            key = item.get("display_key") or f"#{item.get('id')}"
            text = (f"✅ <b>{escape(str(key))}</b>\n\n<b>{escape(str(item.get('title','Untitled')))}</b>\n"
                    f"Status: <b>{escape(str(item.get('status','todo')))}</b>\nPriority: <b>{escape(str(item.get('priority','P2')))}</b>\n"
                    f"Type: <b>{escape(str(item.get('type','task')))}</b>\nAssignee: <b>{escape(str(item.get('assignee_id') or 'Unassigned'))}</b>\n"
                    f"Due: <b>{escape(str(item.get('due_at') or 'Not set'))}</b>")
            await safe_edit_or_respond(event, text, [[Button.inline("▶️ Start", data=route("work", "status", item.get("id"), "in_progress")), Button.inline("🚧 Block", data=route("work", "status", item.get("id"), "blocked"))], [Button.inline("🔎 Review", data=route("work", "status", item.get("id"), "review")), Button.inline("🏠 Home", data=b"btn_menu")]])

        async def render_project_detail_view(event, project_id):
            user_id = getattr(event, "sender_id", None)
            projects = await self.business_assistant.list_projects(user_id, event.chat_id, "private")
            project = next((row for row in projects if str(row.get("id")) == str(project_id)), None)
            if not project:
                await safe_edit_or_respond(event, "⚠️ Project not found.", [[Button.inline("📁 Projects", data=b"btn_projects")]])
                return
            rows = await self.business_repository.list_work_items(user_id, "all", 200, project_id=project_id)
            open_count = sum(1 for row in rows if row.get("status") not in {"done", "completed"})
            blocked_count = sum(1 for row in rows if row.get("status") == "blocked")
            text = (f"📁 <b>{escape(str(project.get('project_key') or project_id))} · {escape(project.get('name','Project'))}</b>\n\n"
                    f"Status: <b>{escape(str(project.get('status','active')))}</b>\n"
                    f"Open work: <b>{open_count}</b> · Blocked: <b>{blocked_count}</b>\n"
                    f"Target: <b>{escape(str(project.get('target_date') or 'Not set'))}</b>\n\n"
                    "Use the board to move work, or CPM to inspect dependencies.")
            await safe_edit_or_respond(event, text, [[Button.inline("🗂 Board", data=route("project", "board", project_id)), Button.inline("📈 CPM", data=route("project", "plan", project_id))], [Button.inline("📁 Projects", data=b"btn_projects"), Button.inline("🏠 Home", data=b"btn_menu")]])

        async def render_project_plan_view(event, project_id):
            user_id = getattr(event, "sender_id", None)
            try:
                plan = await self.business_assistant.project_plan(user_id, event.chat_id, "private", project_id)
            except (PermissionError, ValueError) as exc:
                await safe_edit_or_respond(event, f"⚠️ {escape(str(exc))}", [[Button.inline("📁 Projects", data=b"btn_projects")]])
                return
            if not plan:
                await safe_edit_or_respond(event, "⚠️ Project not found.", [[Button.inline("📁 Projects", data=b"btn_projects")]])
                return
            critical = ", ".join(str(item) for item in plan.get("critical_task_ids", [])) or "none"
            text = (
                f"📈 <b>CPM · {escape(str(plan.get('project', {}).get('name', 'Project')))}</b>\n\n"
                f"Duration: <b>{plan.get('duration_days', 0)} working days</b>\n"
                f"Completion: <b>{plan.get('completion_percent', 0)}%</b>\n"
                f"Critical work items: <code>{escape(critical)}</code>"
            )
            buttons = [
                [Button.inline(f"🗂 Board {project.get('project_key') or project_id}", data=route("project", "board", project_id)), Button.inline("📁 Projects", data=b"btn_projects")],
                [Button.inline("🏠 Home", data=b"btn_menu")],
            ]
            await safe_edit_or_respond(event, text, buttons)

        async def render_single_chat_view(event, chat_id: int):
            records = await self.db.get_messages_for_chat(chat_id, limit=10, allowed_chat_ids=self.focused_chat_ids())
            focus = self._focus_settings.get(int(chat_id), {})
            chat_title = records[0].chat_title if records else focus.get("chat_title", "Chat")
            chat_type = records[0].chat_type if records else focus.get("chat_type", "private")
            if chat_type == "private":
                chat_kind = "Personal chat"
            elif chat_type == "channel":
                chat_kind = "Channel"
            else:
                chat_kind = "Group chat"
            if not records:
                mode = {"monitor": "Everything", "mention": "Mentions only", "orders": "Action items"}.get(focus.get("mode", "monitor"), "Active")
                lines = [
                    f"💬 <b>{chat_kind}: {escape(chat_title)}</b>",
                    f"<i>Watch: {escape(mode)}</i>",
                    "━━━━━━━━━━━━━━━━━━━━━━\n",
                    "🟢 <i>No messages captured yet for this focused chat.</i>",
                    "Send a message in the chat, then return here and tap Refresh Chat.",
                ]
                menu = [
                    [Button.inline("🔄 Refresh Chat", data=f"chat_{chat_id}".encode()),
                     Button.inline("💬 Sources", data=b"btn_groups")],
                    [Button.inline("🏠 Home", data=b"btn_menu")],
                ]
                await safe_edit_or_respond(event, "\n".join(lines), menu)
                return

            lines = [
                f"💬 <b>{chat_kind}: {chat_title}</b>",
                "━━━━━━━━━━━━━━━━━━━━━━\n",
            ]

            for idx, r in enumerate(records, 1):
                clean = r.summary
                prefix = f"[{r.chat_title}]"
                if clean.startswith(prefix):
                    clean = clean[len(prefix):].strip()

                lines.append(f"<b>{idx}. {r.sender_name}</b> [{r.priority}]")
                lines.append(f"   {clean}")
                if r.needs_action and r.action:
                    lines.append(f"   👉 <b>Action:</b> <u>{r.action}</u>")
                if r.deadline:
                    lines.append(f"   ⏰ <b>Due:</b> <code>{r.deadline}</code>")
                if r.message_link:
                    lines.append(f"   🔗 <a href=\"{r.message_link}\">Open Message</a>")
                lines.append("")

            menu = [
                [Button.inline("🔄 Refresh Chat", data=f"chat_{chat_id}".encode()),
                  Button.inline("💬 Sources", data=b"btn_groups")],
                [Button.inline("🏠 Home", data=b"btn_menu")],
            ]
            await safe_edit_or_respond(event, "\n".join(lines), menu)

        async def render_tier_view(event, tier: str):
            records = await self.db.get_messages_by_tier(tier, limit=10, allowed_chat_ids=self.focused_chat_ids())
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

        from app.agent.state import MongoAgentState
        from app.telegram.sources import TelegramSources
        from app.telegram.conversation import TelegramConversation
        agent_state = MongoAgentState(self.business_repository.db)
        await agent_state.init()
        self.agent_runtime = AgentRuntime(
            self.business_assistant, self.business_repository, self.business_provider,
            message_database=self.db, registry=build_registry(telegram=True),
            timeout_seconds=self.cfg.ai_request_timeout_seconds,
            allowed_chat_ids_provider=self.focused_chat_ids,
        )
        sources = TelegramSources(lambda: self.telethon_client, self.business_access.owner_id,
                                  agent_state, self.focused_chat_ids, self.business_access)
        self.conversation = TelegramConversation(self.agent_runtime, sources, agent_state)
        logger.info("Interactive bot message and callback handlers registered")

        @bot_client.on(events.NewMessage)
        async def on_bot_message(event):
            # Group conversations are opt-in and only respond to a mention or
            # reply. Ordinary group traffic remains silent.
            setup_here = (
                not event.is_private
                and getattr(event, "sender_id", None) == self.business_access.owner_id
                and (event.raw_text or "").strip().lower() in {"/setup here", "/setup add"}
            )
            if not event.is_private:
                if setup_here:
                    pass
                elif not self._focus_settings:
                    return
                elif event.chat_id not in self.business_access.approved_groups:
                    return
                elif not (getattr(getattr(event, "message", None), "mentioned", False) or getattr(getattr(event, "message", None), "is_reply", False)):
                    return

            text = (event.raw_text or "").strip()
            text_lower = text.lower()

            user_id = getattr(event, "sender_id", None)
            chat_type = "private" if event.is_private else ("channel" if event.is_channel else "group")
            if event.is_private and text_lower in {"/id", "/whoami"}:
                await event.respond(f"Your Telegram user ID is <code>{user_id}</code>.", parse_mode="HTML")
                return
            if setup_here:
                chat = await event.get_chat()
                state = setup_state(user_id)
                title = getattr(chat, "title", None) or "Telegram group"
                state.setdefault("chats", {})[int(event.chat_id)] = {
                    "chat_id": int(event.chat_id),
                    "chat_title": title,
                    "chat_type": chat_type,
                    "mode": "monitor",
                }
                await event.respond(
                    f"✅ <b>{escape(title)}</b> was added to your setup.\n\nReturn to the bot and press <b>Save setup</b>.",
                    parse_mode="HTML",
                )
                return
            access = self.business_access.decide(user_id, event.chat_id, chat_type)
            if not access.allowed:
                # A private /pair request records a request for administrator
                # review; it never grants access automatically.
                if event.is_private and text_lower in {"/pair", "pair", "/request-access"} and user_id is not None:
                    code = await self.business_repository.create_pairing(user_id, getattr(event, "sender", None) and getattr(event.sender, "first_name", ""))
                    await self.notifier.send_message(f"🔐 Pairing request: <b>{code}</b> (user {user_id})")
                    await event.respond(f"Your access request was sent for administrator approval. Request code: {code}")
                else:
                    await event.respond("🔒 This assistant is available only to approved users and groups.")
                return

            if await self.conversation.controls(event):
                return
            if text_lower in {"cancel", "stop", "never mind", "nevermind", "/cancel"}:
                self._setup_state.pop(user_id, None)
                self._pm_state.pop(user_id, None)
                await event.respond("Cancelled." if text_lower != "stop" else "There is no active request to stop.")
                return

            # Direct search input: after pressing Search, a plain message such
            # as "daivai" is treated as the query instead of an AI question.
            state = self._setup_state.get(user_id) if user_id is not None else None
            if (
                user_id == self.business_access.owner_id
                and state
                and state.get("awaiting_search")
                and text
                and not text_lower.startswith("/setup")
            ):
                state["picker_query"] = text
                state["picker_page"] = 0
                state["awaiting_search"] = False
                await render_setup_chats(event, user_id, state.get("picker_kind", "all"), 0)
                return

            # Button-first creation flows. Slash commands remain supported for
            # power users, but normal users can create work without syntax.
            pm_state = self._pm_state.get(user_id) if user_id is not None else None
            if pm_state and pm_state.get("kind") in {"project", "task"} and text:
                kind = pm_state["kind"]
                self._pm_state.pop(user_id, None)
                try:
                    if kind == "project":
                        project_id = await self.business_assistant.create_project(user_id, event.chat_id, chat_type, text[:120])
                        projects = await self.business_assistant.list_projects(user_id, event.chat_id, chat_type)
                        project = next((row for row in projects if str(row.get("id")) == str(project_id)), {})
                        key = project.get("project_key") or "assigned automatically"
                        await event.respond(f"✅ <b>Project created</b>\n\n📁 {escape(text[:120])}\nKey: <b>{escape(str(key))}</b>", parse_mode="HTML", buttons=[[Button.inline("📁 Open Project", data=route("project", "view", project_id))], [Button.inline("🏠 Home", data=b"btn_menu")]])
                    else:
                        task_id = await self.business_assistant.create_task(user_id, event.chat_id, chat_type, text[:240])
                        await event.respond(f"✅ <b>Task created</b>\n\n{escape(text[:240])}", parse_mode="HTML", buttons=[[Button.inline("✅ My Work", data=b"btn_mywork")], [Button.inline("🏠 Home", data=b"btn_menu")]])
                except (PermissionError, ValueError) as exc:
                    await event.respond(f"⚠️ {escape(str(exc))}", parse_mode="HTML", buttons=get_main_menu())
                return

            # Owner-only focus setup. This is intentionally separate from the
            # ordinary business commands so the owner can configure the
            # workspace before any supplier/customer chat is processed.
            if text_lower == "/setup":
                if user_id != self.business_access.owner_id:
                    await event.respond("🔒 Only the owner can configure focused chats.")
                    return
                # Preserve chats added with `/setup here` from a group while
                # reopening the private setup screen.
                if user_id not in self._setup_state:
                    setup_state(user_id)
                await render_setup_home(event, user_id)
                return
            if text_lower.startswith("/setup search"):
                if user_id != self.business_access.owner_id:
                    await event.respond("🔒 Only the owner can search focused chats.")
                    return
                parts = text.split(maxsplit=2)
                if len(parts) < 3 or not parts[2].strip():
                    await event.respond("Usage: <code>/setup search NAME</code>", parse_mode="HTML")
                    return
                state = setup_state(user_id)
                state["picker_query"] = parts[2].strip()
                state["picker_page"] = 0
                state["awaiting_search"] = False
                await render_setup_chats(event, user_id, state.get("picker_kind", "all"), 0)
                return
            if text_lower.startswith("/setup user"):
                if user_id != self.business_access.owner_id:
                    await event.respond("🔒 Only the owner can add members.")
                    return
                parts = text.split()
                if len(parts) != 3 or not parts[2].lstrip("-").isdigit():
                    await event.respond("Usage: <code>/setup user TELEGRAM_USER_ID</code>", parse_mode="HTML")
                    return
                member_id = int(parts[2])
                state = setup_state(user_id)
                state.setdefault("users", set()).add(member_id)
                state["awaiting_user"] = False
                self.business_access.approve_user(member_id)
                await self.business_repository.upsert_user(member_id, approved=True)
                await event.respond(f"✅ Member <code>{member_id}</code> added to the setup.", parse_mode="HTML", buttons=setup_home_buttons(state))
                return

            # Cancel only an active consent or task/project draft. A normal
            # conversational use of these words still reaches the AI.
            pending_key = (int(user_id), int(getattr(event, "chat_id", user_id))) if user_id is not None else None
            cancel_phrases = {"cancel", "cancel this", "never mind", "nevermind", "stop", "no thanks", "don't grant", "do not grant"}
            has_pending_flow = pending_key is not None and (pending_key in self._pending_access_requests or user_id in self._pm_state)
            natural_cancel = text_lower in cancel_phrases or text_lower.startswith(("cancel ", "please cancel", "stop "))
            if text_lower == "/cancel" or (natural_cancel and has_pending_flow):
                cancelled_access = self._pending_access_requests.pop(pending_key, None) if pending_key else None
                cancelled_draft = self._pm_state.pop(user_id, None) if user_id is not None else None
                if cancelled_access or cancelled_draft:
                    await event.respond("❌ Cancelled. No new chat access or work item was created.")
                else:
                    await event.respond("There is nothing waiting to be cancelled.")
                return

            # Administrator-only pairing controls. The target user's access is
            # updated in both the database and the in-memory transport policy.
            if text_lower.startswith("/approve ") or text_lower.startswith("/deny "):
                if self.business_access.owner_id != user_id:
                    await event.respond("🔒 Administrator access required.")
                    return
                approve = text_lower.startswith("/approve ")
                code = text.split(maxsplit=1)[1].strip()
                paired_user = await self.business_repository.resolve_pairing(code, approve)
                if paired_user is None:
                    await event.respond("No pending pairing was found for that code.")
                elif approve:
                    self.business_access.approve_user(paired_user)
                    await event.respond(f"✅ User {paired_user} approved.")
                else:
                    await event.respond(f"❌ Pairing {code.upper()} denied.")
                return

            # Analyze a document or image attached to an authorized message.
            # The temporary file is removed after the provider consumes it.
            attached_media = getattr(getattr(event, "message", None), "media", None)
            if attached_media is not None:
                inbox = Path(tempfile.gettempdir()) / "telegram-ai-inbox"
                inbox.mkdir(parents=True, exist_ok=True)
                media_file = getattr(getattr(event, "message", None), "file", None)
                media_name = getattr(media_file, "name", None) or ""
                media_suffix = Path(media_name).suffix.lower()
                if not media_suffix and getattr(getattr(event, "message", None), "photo", None) is not None:
                    media_suffix = ".jpg"
                if not media_suffix:
                    mime_type = getattr(media_file, "mime_type", "") or ""
                    media_suffix = {"application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md"}.get(mime_type, ".bin")
                media_path = inbox / f"{event.chat_id}-{getattr(event, 'id', 'message')}{media_suffix}"
                try:
                    downloaded = await event.download_media(file=str(media_path))
                    if downloaded:
                        answer = await self.business_assistant.answer_file(
                            user_id=user_id,
                            chat_id=event.chat_id,
                            chat_type=chat_type,
                            file_path=downloaded,
                            question=text or "Summarize this file and list the important business points.",
                        )
                        await event.respond(answer, parse_mode="HTML", buttons=get_main_menu())
                    else:
                        await event.respond("⚠️ I could not download that attachment.")
                except ValueError:
                    await event.respond("Usage: <code>/project Name | department_id | description | start date | target date</code>", parse_mode="HTML")
                except PermissionError:
                    await event.respond("🔒 Access not approved.")
                except BudgetExceeded:
                    await event.respond("⏸️ The AI allowance is currently used. Please contact the administrator.")
                except RuntimeError as exc:
                    await event.respond(f"⚠️ {exc}")
                finally:
                    try:
                        Path(media_path).unlink(missing_ok=True)
                    except OSError:
                        logger.warning("Could not remove temporary Telegram attachment %s", media_path)
                return

            # 1. Start or Menu command
            if (
                text_lower.startswith(("/start", "/menu", "/help"))
                or text in ("❓ Help", "menu", "help", "start")
            ):
                if self._focus_settings:
                    await render_home(event)
                else:
                    await event.respond(get_welcome_text(), parse_mode="HTML", buttons=get_main_menu())
                webapp_keyboard = get_webapp_keyboard()
                if webapp_keyboard:
                    await event.respond("Use the button below to open the project workspace inside Telegram.", buttons=webapp_keyboard)
                return

            # 2. Priority check shortcuts
            if text_lower.startswith("/assistant"):
                parts = text.split(maxsplit=1)
                if len(parts) == 1:
                    available = ", ".join(sorted(self.assistant_services))
                    await event.respond(f"🤖 <b>Assistants:</b> {escape(available)}\nUse <code>/assistant ID</code> to switch. Each assistant has separate memory.", parse_mode="HTML", buttons=get_main_menu())
                elif parts[1].strip() in self.assistant_services:
                    self._assistant_selection[user_id] = parts[1].strip()
                    await event.respond(f"🤖 Switched to <b>{escape(parts[1].strip())}</b>. This assistant uses its own conversation context.", parse_mode="HTML", buttons=get_main_menu())
                else:
                    await event.respond("⚠️ Unknown assistant. Send <code>/assistant</code> to see available assistants.", parse_mode="HTML")
                return
            if text_lower in {"/integrations", "integrations"}:
                statuses = "\n".join(f"• {name.title()}: {'configured' if self.integrations.configured(name) else 'not configured'}" for name in ("email", "calendar", "crm", "task"))
                await event.respond(f"🔌 <b>Business integrations</b>\n\n{statuses}\n\nConnectors are used only after an explicit approval.", parse_mode="HTML", buttons=get_main_menu())
                return
            if text_lower.startswith("/department "):
                try:
                    item_id = await self.business_assistant.create_department(user_id, event.chat_id, chat_type, text.split(maxsplit=1)[1].strip())
                    await event.respond(f"🏢 Department created: <b>#{escape(str(item_id))}</b>", parse_mode="HTML", buttons=get_main_menu())
                except PermissionError:
                    await event.respond("🔒 Access not approved.")
                return
            if text_lower in {"/departments", "departments"}:
                rows = await self.business_assistant.list_departments(user_id, event.chat_id, chat_type)
                body = "\n".join(f"• <b>#{escape(str(row['id']))}</b> {escape(row['name'])}" for row in rows) or "No departments yet."
                await event.respond("🏢 <b>Departments</b>\n\n" + body, parse_mode="HTML", buttons=get_main_menu())
                return
            if text_lower.startswith("/project "):
                try:
                    project_parts = [part.strip() for part in text.split(maxsplit=1)[1].split("|", 4)]
                    project_id = await self.business_assistant.create_project(user_id, event.chat_id, chat_type, project_parts[0], project_parts[2] if len(project_parts) > 2 else "", int(project_parts[1]) if len(project_parts) > 1 and project_parts[1] else None, project_parts[3] if len(project_parts) > 3 and project_parts[3] else None, project_parts[4] if len(project_parts) > 4 and project_parts[4] else None)
                    await event.respond(f"📁 Project created: <b>#{escape(str(project_id))}</b>", parse_mode="HTML", buttons=get_main_menu())
                except ValueError:
                    await event.respond("Usage: <code>/project Name | department_id | description | start date | target date</code>", parse_mode="HTML")
                except PermissionError:
                    await event.respond("🔒 Access not approved.")
                return
            if text_lower in {"/projects", "projects"}:
                await render_projects_view(event)
                return
            if text_lower.startswith("/ptask "):
                parts = [part.strip() for part in text.split(maxsplit=1)[1].split("|")]
                if len(parts) < 3:
                    await event.respond("Usage: <code>/ptask PROJECT_ID | Task name | duration days | assignee ID | priority | due date</code>", parse_mode="HTML")
                    return
                try:
                    task_id = await self.business_assistant.create_project_task(user_id, event.chat_id, chat_type, parts[0], parts[1], int(parts[2]), int(parts[3]) if len(parts) > 3 and parts[3] else None, parts[4] if len(parts) > 4 and parts[4] else "normal", parts[5] if len(parts) > 5 and parts[5] else None)
                    await event.respond(f"✅ Project task created: <b>#{escape(str(task_id))}</b>", parse_mode="HTML", buttons=get_main_menu())
                except (ValueError, PermissionError):
                    await event.respond("Usage: <code>/ptask PROJECT_ID | Task name | duration days | assignee ID | priority | due date</code>", parse_mode="HTML")
                return
            if text_lower.startswith("/assign "):
                parts = text.split()
                if len(parts) != 3:
                    await event.respond("Usage: <code>/assign TASK_ID USER_ID</code>", parse_mode="HTML")
                    return
                try:
                    updated = await self.business_assistant.update_task(user_id, event.chat_id, chat_type, parts[1], assignee_id=int(parts[2]))
                    await event.respond("✅ Task assigned." if updated else "⚠️ Task not found.", buttons=get_main_menu())
                except (ValueError, PermissionError):
                    await event.respond("Usage: <code>/assign TASK_ID USER_ID</code>", parse_mode="HTML")
                return
            if text_lower.startswith("/status "):
                parts = text.split()
                if len(parts) != 3 or parts[2].lower() not in {"to_do", "in_progress", "blocked", "completed"}:
                    await event.respond("Usage: <code>/status TASK_ID to_do|in_progress|blocked|completed</code>", parse_mode="HTML")
                    return
                updated = await self.business_assistant.update_task(user_id, event.chat_id, chat_type, parts[1], status=parts[2].lower())
                await event.respond("✅ Task status updated." if updated else "⚠️ Task not found.", buttons=get_main_menu())
                return
            if text_lower.startswith("/dep "):
                parts = text.split()
                if len(parts) != 3:
                    await event.respond("Usage: <code>/dep TASK_ID PREDECESSOR_TASK_ID</code>", parse_mode="HTML")
                    return
                try:
                    await self.business_assistant.add_dependency(user_id, event.chat_id, chat_type, parts[1], parts[2])
                    await event.respond("🔗 Dependency added. Run <code>/plan PROJECT_ID</code> to recalculate CPM.", parse_mode="HTML", buttons=get_main_menu())
                except PermissionError:
                    await event.respond("🔒 Access not approved.")
                return
            if text_lower.startswith("/milestone "):
                parts = [part.strip() for part in text.split(maxsplit=1)[1].split("|", 2)]
                if len(parts) != 3:
                    await event.respond("Usage: <code>/milestone PROJECT_ID | milestone name | YYYY-MM-DD</code>", parse_mode="HTML")
                    return
                try:
                    item_id = await self.business_assistant.add_milestone(user_id, event.chat_id, chat_type, parts[0], parts[1], parts[2])
                    await event.respond(f"🏁 Milestone added: <b>#{escape(str(item_id))}</b>", parse_mode="HTML", buttons=get_main_menu())
                except PermissionError:
                    await event.respond("🔒 Access not approved.")
                return
            if text_lower.startswith("/comment "):
                parts = [part.strip() for part in text.split(maxsplit=1)[1].split("|", 1)]
                if len(parts) != 2:
                    await event.respond("Usage: <code>/comment TASK_ID | update</code>", parse_mode="HTML")
                    return
                try:
                    await self.business_assistant.add_comment(user_id, event.chat_id, chat_type, parts[0], parts[1])
                    await event.respond("💬 Comment added.", buttons=get_main_menu())
                except PermissionError:
                    await event.respond("🔒 Access not approved.")
                return
            if text_lower.startswith("/plan "):
                try:
                    plan = await self.business_assistant.project_plan(user_id, event.chat_id, chat_type, text.split(maxsplit=1)[1].strip())
                    if not plan:
                        await event.respond("⚠️ Project not found.", buttons=get_main_menu())
                    else:
                        critical = ", ".join(str(item) for item in plan["critical_task_ids"]) or "none"
                        await event.respond(f"📈 <b>CPM project plan</b>\n\nDuration: <b>{plan['duration_days']} days</b>\nCompletion: <b>{plan['completion_percent']}%</b>\nCritical tasks: <code>{escape(critical)}</code>", parse_mode="HTML", buttons=get_main_menu())
                except (ValueError, PermissionError) as exc:
                    await event.respond(f"⚠️ {escape(str(exc))}", parse_mode="HTML")
                return
            if text_lower.startswith("/task "):
                try:
                    task_id = await self.business_assistant.create_task(user_id, event.chat_id, "private" if event.is_private else "group", text.split(maxsplit=1)[1].strip())
                    await event.respond(f"✅ Task created: <b>#{escape(str(task_id))}</b>", parse_mode="HTML", buttons=get_main_menu())
                except PermissionError:
                    await event.respond("🔒 This assistant is available only to approved users and groups.")
                return
            if text_lower in {"/tasks", "tasks", "✅ tasks", "my work", "work", "✅ my work"}:
                await render_tasks_view(event)
                return
            if text_lower in {"/access", "access"}:
                await render_access_view(event)
                return
            if text_lower.startswith("/done "):
                try:
                    done = await self.business_assistant.complete_task(user_id, event.chat_id, "private" if event.is_private else "group", text.split(maxsplit=1)[1].strip())
                    await event.respond("✅ Task completed." if done else "⚠️ Open task not found.", buttons=get_main_menu())
                except (PermissionError, ValueError):
                    await event.respond("Usage: <code>/done TASK_ID</code>", parse_mode="HTML")
                return
            if text_lower.startswith("/remind "):
                parts = text.split(maxsplit=2)
                if len(parts) < 3:
                    await event.respond("Usage: <code>/remind 2026-09-04T18:00:00+07:00 Call client</code>", parse_mode="HTML")
                    return
                try:
                    remind_at = datetime.fromisoformat(parts[1]).astimezone(timezone.utc).isoformat()
                    reminder_id = await self.business_assistant.create_reminder(user_id, event.chat_id, "private" if event.is_private else "group", parts[2].strip(), remind_at)
                    await event.respond(f"⏰ Reminder created for <code>{escape(remind_at)}</code> (#{escape(str(reminder_id))}).", parse_mode="HTML", buttons=get_main_menu())
                except (ValueError, PermissionError):
                    await event.respond("Usage: <code>/remind 2026-09-04T18:00:00+07:00 Call client</code>", parse_mode="HTML")
                return
            if text_lower in {"/reminders", "reminders", "⏰ reminders"}:
                await render_reminders_view(event)
                return
            if text_lower.startswith("/web "):
                from app.tools.web import fetch_live
                try:
                    query_parts = text.split(maxsplit=2)
                    live = await fetch_live(query_parts[1])
                    if len(query_parts) == 2:
                        await event.respond(f"🌐 <b>Live source</b>\n\n{escape(clip_text(live, 3500))}", parse_mode="HTML", buttons=get_main_menu())
                    else:
                        answer = await asyncio.wait_for(self.business_assistant.answer(user_id, event.chat_id, "private" if event.is_private else "group", query_parts[2], [live]), timeout=self.cfg.ai_request_timeout_seconds)
                        await event.respond(answer, parse_mode="HTML", buttons=get_main_menu())
                except Exception as exc:
                    logger.warning("Live fetch failed: %s", exc)
                    await event.respond("⚠️ I could not fetch that public source. Check the URL and try again.", buttons=get_main_menu())
                return
            if text_lower.startswith("/draft "):
                parts = text.split(maxsplit=2)
                if len(parts) < 3:
                    await event.respond("Usage: <code>/draft CHAT_ID message</code>", parse_mode="HTML")
                    return
                try:
                    action_id = await self.business_assistant.draft_action(user_id, event.chat_id, "private", "send_message", parts[1], parts[2], 15)
                    await event.respond(f"📝 Draft ready for <code>{escape(parts[1])}</code>. Approve sending?", parse_mode="HTML", buttons=[[Button.inline("✅ Approve", data=f"action_approve_{action_id}".encode()), Button.inline("❌ Reject", data=f"action_deny_{action_id}".encode())]])
                except PermissionError:
                    await event.respond("🔒 Only approved users can create outbound drafts.")
                return
            if text_lower.startswith("/email "):
                parts = text.split(maxsplit=1)[1].split("|", 2)
                if len(parts) != 3:
                    await event.respond("Usage: <code>/email recipient | subject | message</code>", parse_mode="HTML")
                    return
                try:
                    payload = json.dumps({"to": parts[0].strip(), "subject": parts[1].strip(), "body": parts[2].strip()})
                    action_id = await self.business_assistant.draft_action(user_id, event.chat_id, "private", "email", "email", payload, 15)
                    await event.respond(f"📝 Email draft ready (#{escape(str(action_id))}). Approve sending?", parse_mode="HTML", buttons=[[Button.inline("✅ Approve", data=f"action_approve_{action_id}".encode()), Button.inline("❌ Reject", data=f"action_deny_{action_id}".encode())]])
                except PermissionError:
                    await event.respond("🔒 Only approved users can create outbound drafts.")
                return
            if text_lower.startswith("/event "):
                parts = text.split(maxsplit=1)[1].split("|", 2)
                if len(parts) < 2:
                    await event.respond("Usage: <code>/event start | title | optional end/notes</code>", parse_mode="HTML")
                    return
                try:
                    payload = json.dumps({"start": parts[0].strip(), "title": parts[1].strip(), "end": parts[2].strip() if len(parts) > 2 else None})
                    action_id = await self.business_assistant.draft_action(user_id, event.chat_id, "private", "calendar", "calendar", payload, 15)
                    await event.respond(f"📝 Calendar event draft ready (#{escape(str(action_id))}). Approve creating it?", parse_mode="HTML", buttons=[[Button.inline("✅ Approve", data=f"action_approve_{action_id}".encode()), Button.inline("❌ Reject", data=f"action_deny_{action_id}".encode())]])
                except PermissionError:
                    await event.respond("🔒 Only approved users can create outbound drafts.")
                return
            if text_lower in {"/connectgoogle", "connect google"}:
                if user_id != self.business_access.owner_id or not event.is_private:
                    await event.respond("🔒 Only the owner can connect Google, from the private bot chat.")
                    return
                try:
                    url = await self.google_account.connect_url(user_id)
                    await event.respond("🔗 Connect your Google account (Calendar + Gmail):", buttons=[[Button.url("Connect Google", url)]])
                except RuntimeError as exc:
                    await event.respond(f"⚠️ {escape(str(exc))}", parse_mode="HTML")
                return
            if text_lower in {"/googlestatus", "google status"}:
                if user_id != self.business_access.owner_id or not event.is_private:
                    await event.respond("🔒 Only the owner can view the Google connection status.")
                    return
                connected = await self.google_account.is_connected(user_id)
                await event.respond("✅ Google account connected." if connected else "⚪ Google account not connected. Use /connectgoogle.")
                return
            if text_lower == "/disconnectgoogle":
                if user_id != self.business_access.owner_id or not event.is_private:
                    await event.respond("🔒 Only the owner can disconnect Google.")
                    return
                await self.google_account.disconnect(user_id)
                await event.respond("🔌 Google account disconnected.")
                return
            if text_lower in {"/calendar", "calendar", "📅 calendar"}:
                if user_id != self.business_access.owner_id or not event.is_private:
                    await event.respond("🔒 Only the owner can view the connected Google Calendar.")
                    return
                credentials = await self.google_account.get_credentials(user_id)
                if not credentials:
                    await event.respond("⚪ Google Calendar is not connected. Use /connectgoogle first.")
                    return
                try:
                    from app.integrations.google_calendar import list_upcoming_events
                    events = await list_upcoming_events(credentials, max_results=10)
                except Exception:
                    logger.exception("Google Calendar list failed")
                    await event.respond("⚠️ Could not reach Google Calendar. Try /connectgoogle again if this continues.")
                    return
                if not events:
                    await event.respond("📅 No upcoming events.")
                    return
                lines = ["📅 <b>Upcoming events</b>"] + [f"• {escape(item['summary'])} — {escape(item['start'] or '')}" for item in events]
                await event.respond("\n".join(lines), parse_mode="HTML", buttons=get_main_menu())
                return
            if text_lower in {"/inbox", "inbox"}:
                if user_id != self.business_access.owner_id or not event.is_private:
                    await event.respond("🔒 Only the owner can view the connected Gmail inbox.")
                    return
                credentials = await self.google_account.get_credentials(user_id)
                if not credentials:
                    await event.respond("⚪ Gmail is not connected. Use /connectgoogle first.")
                    return
                try:
                    from app.integrations.google_gmail import list_recent_messages
                    messages = await list_recent_messages(credentials, max_results=10)
                except Exception:
                    logger.exception("Gmail list failed")
                    await event.respond("⚠️ Could not reach Gmail. Try /connectgoogle again if this continues.")
                    return
                if not messages:
                    await event.respond("📧 No unread messages.")
                    return
                context_lines = [f"{m['from']} — {m['subject']}: {m['snippet']}" for m in messages]
                try:
                    answer = await asyncio.wait_for(
                        self.business_assistant.answer(user_id, event.chat_id, "private", "Summarize these unread emails and flag anything that needs a reply.", context_lines),
                        timeout=self.cfg.ai_request_timeout_seconds,
                    )
                    await event.respond(f"📧 <b>Inbox summary</b>\n\n{answer}", parse_mode="HTML", buttons=get_main_menu())
                except Exception:
                    logger.exception("Inbox summarization failed")
                    await event.respond("⚠️ Could not summarize the inbox right now.")
                return
            if text_lower.startswith("/crm "):
                parts = text.split(maxsplit=1)[1].split("|", 1)
                if len(parts) != 2:
                    await event.respond("Usage: <code>/crm lead | {\"name\":\"Client\"}</code>", parse_mode="HTML")
                    return
                try:
                    crm_data = json.loads(parts[1].strip())
                    payload = json.dumps({"kind": parts[0].strip(), "data": crm_data})
                    action_id = await self.business_assistant.draft_action(user_id, event.chat_id, "private", "crm", "crm", payload, 15)
                    await event.respond(f"📝 CRM draft ready (#{escape(str(action_id))}). Approve creating it?", parse_mode="HTML", buttons=[[Button.inline("✅ Approve", data=f"action_approve_{action_id}".encode()), Button.inline("❌ Reject", data=f"action_deny_{action_id}".encode())]])
                except (PermissionError, ValueError, json.JSONDecodeError):
                    await event.respond("Usage: <code>/crm lead | {\"name\":\"Client\"}</code>", parse_mode="HTML")
                return
            if text_lower.startswith("/sync-task "):
                parts = text.split(maxsplit=1)[1].split("|", 2)
                if not parts:
                    await event.respond("Usage: <code>/sync-task title | details | optional due_at</code>", parse_mode="HTML")
                    return
                try:
                    payload = json.dumps({"title": parts[0].strip(), "details": parts[1].strip() if len(parts) > 1 else "", "due_at": parts[2].strip() if len(parts) > 2 else None})
                    action_id = await self.business_assistant.draft_action(user_id, event.chat_id, "private", "task", "task", payload, 15)
                    await event.respond(f"📝 External task draft ready (#{escape(str(action_id))}). Approve syncing it?", parse_mode="HTML", buttons=[[Button.inline("✅ Approve", data=f"action_approve_{action_id}".encode()), Button.inline("❌ Reject", data=f"action_deny_{action_id}".encode())]])
                except PermissionError:
                    await event.respond("🔒 Only approved users can create outbound drafts.")
                return
            assistant = self.selected_assistant(user_id)
            if text_lower.startswith(("/memory", "memory")):
                parts = text.split(maxsplit=1)
                if len(parts) == 1:
                    await render_memory_view(event)
                    return
                try:
                    matches = await assistant.search_memory(user_id, event.chat_id, chat_type, parts[1].strip(), limit=10)
                except PermissionError:
                    await event.respond("🔒 This assistant is available only to approved users and groups.")
                    return
                if not matches:
                    await event.respond("🧠 No saved conversation turns matched that search.", buttons=get_main_menu())
                    return
                lines = ["🧠 <b>Knowledge search</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
                for match in matches:
                    role = "You" if match.get("role") == "user" else "Assistant"
                    lines.append(f"<b>{role}:</b> {escape(clip_text(match.get('content', ''), 220))}")
                await event.respond("\n\n".join(lines), parse_mode="HTML", buttons=get_main_menu())
                return
            if text_lower in {"/new", "new", "new chat"}:
                try:
                    await assistant.reset_conversation(user_id, event.chat_id, chat_type)
                    await event.respond("💬 New conversation started. Previous turns were removed from this chat memory.", buttons=get_main_menu())
                except PermissionError:
                    await event.respond("🔒 This assistant is available only to approved users and groups.")
                return
            if text_lower in {"/forget", "forget"}:
                await event.respond("To delete all of your assistant memory, send `/forget all`.", parse_mode="HTML", buttons=get_main_menu())
                return
            if text_lower in {"/forget all", "forget all"}:
                try:
                    deleted = await assistant.forget_memory(user_id, event.chat_id, chat_type)
                    await event.respond(f"🗑️ Deleted {deleted} saved conversation(s) for your account.", buttons=get_main_menu())
                except PermissionError:
                    await event.respond("🔒 This assistant is available only to approved users and groups.")
                return
            if text_lower in {"/p0", "p0", "/urgent", "urgent", "/emergency", "emergency"}:
                await render_tier_view(event, "P0")
                return
            if text_lower in {"/p1", "p1", "/important", "important"}:
                await render_tier_view(event, "P1")
                return
            if text_lower in {"/p2", "p2", "/updates", "updates"}:
                await render_tier_view(event, "P2")
                return
            if text_lower in {"/p3", "p3", "/noise", "noise", "/chatter", "chatter"}:
                await render_tier_view(event, "P3")
                return

            # 3. Group summary command
            if text_lower in ("/groups", "groups", "group", "by group", "/group"):
                await render_groups_view(event)
                return

            # 3a. Situations: issues fused from multiple messages, not raw message list
            if text_lower in {"/situations", "situations", "🔥 situations"}:
                await render_situations_view(event)
                return

            # 4. Instant Summary / Digest command
            if (
                text_lower.startswith(("/digest", "/summary"))
                or text in ("📋 Instant Digest", "digest", "summary", "summarize")
            ):
                digest_text, stats = await self.digest_engine.generate_digest(self.focused_chat_ids())
                if digest_text:
                    await event.respond(digest_text, parse_mode="HTML", buttons=get_sub_menu())
                else:
                    await event.respond(
                        "✅ <b>All caught up!</b>\n\nNo unread messages to summarize right now.",
                        parse_mode="HTML",
                        buttons=get_main_menu(),
                    )
                return

            # 5. Stats command
            if text in ("📊 Today Stats", "stats", "/stats"):
                stats = await self.db.get_stats(allowed_chat_ids=self.focused_chat_ids())
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

            # 6. Business conversational assistant. Group chats use their own
            # recent context; the owner can query recently captured messages
            # from private chat. Staff remain scoped until source access is
            # explicitly configured.
            chat_type = "private" if event.is_private else ("channel" if event.is_channel else "group")

            # Keep greetings conversational. They should not trigger the work
            # agent or advertise task/project features before the user asks
            # for them.
            if assistant is self.business_assistant and text_lower.strip() in {
                "hi", "hello", "hey", "hiya", "good morning", "good afternoon", "good evening",
            }:
                await event.respond("Hello! How can I help you today?")
                return

            if assistant is self.business_assistant:
                await self.conversation.handle(event, text)
                return
            # Other profiles do not inherit the owner's Telegram history.
            progress = await event.respond("Thinking…")
            try:
                answer = await asyncio.wait_for(
                    assistant.answer(user_id, event.chat_id, chat_type, text),
                    timeout=self.cfg.ai_request_timeout_seconds,
                )
                from app.telegram.conversation import chunks
                parts = list(chunks(answer))
                await progress.edit(parts[0], parse_mode=None)
                for part in parts[1:]:
                    await event.respond(part, parse_mode=None)
            except Exception:
                logger.warning("Assistant profile request failed")
                await progress.edit("I couldn't complete that request. Please try again.", parse_mode=None)

        # Callback queries for inline buttons - EDIT IN PLACE (NO SPAM)
        @bot_client.on(events.CallbackQuery)
        async def on_callback(event):
            callback_user_id = getattr(event, "sender_id", None)
            callback_chat_id = getattr(event, "chat_id", None)
            callback_chat_type = "private"
            if not self.business_access.can_use(callback_user_id, callback_chat_id, callback_chat_type):
                await event.answer("🔒 Access not approved", alert=True)
                return
            data = event.data
            if data.startswith(b"agent:"):
                try:
                    await self.conversation.callback(event)
                except PermissionError:
                    await event.answer("Access is no longer available for this request.", alert=True)
                return


            if data.startswith(b"setup_") or data == b"btn_setup":
                if callback_user_id != self.business_access.owner_id:
                    await event.answer("Only the owner can change workspace setup", alert=True)
                    return
                if data in {b"btn_setup", b"setup_home"}:
                    setup_state(callback_user_id)["awaiting_search"] = False
                    await event.answer()
                    await render_setup_home(event, callback_user_id)
                    return
                if data == b"setup_chats":
                    state = setup_state(callback_user_id)
                    state["picker_query"] = ""
                    state["awaiting_search"] = False
                    await event.answer("Loading your Telegram chats...")
                    await render_setup_chats(event, callback_user_id)
                    return
                if data == b"setup_groups":
                    state = setup_state(callback_user_id)
                    state["picker_query"] = ""
                    state["awaiting_search"] = False
                    await event.answer("Loading groups...")
                    await render_setup_chats(event, callback_user_id, "groups")
                    return
                if data == b"setup_people":
                    state = setup_state(callback_user_id)
                    state["picker_query"] = ""
                    state["awaiting_search"] = False
                    await event.answer("Loading people...")
                    await render_setup_chats(event, callback_user_id, "people")
                    return
                if data == b"setup_search":
                    state = setup_state(callback_user_id)
                    state["awaiting_search"] = True
                    await event.answer()
                    await safe_edit_or_respond(
                        event,
                        "🔍 <b>Search chats</b>\n\nType a person or group name in this chat, for example <code>daivai</code>.\nSearch matches contact, group, and username text.",
                        [[Button.inline("⬅️ Back to results", data=f"setup_page_{state.get('picker_kind', 'all')}_{state.get('picker_page', 0)}".encode())]],
                    )
                    return
                if data == b"setup_clear_search":
                    state = setup_state(callback_user_id)
                    state["picker_query"] = ""
                    state["awaiting_search"] = False
                    await event.answer("Search cleared")
                    await render_setup_chats(event, callback_user_id, state.get("picker_kind", "all"), 0)
                    return
                if data.startswith(b"setup_page_"):
                    parts = data.decode().split("_")
                    kind, page = parts[2], int(parts[3])
                    await event.answer()
                    await render_setup_chats(event, callback_user_id, kind, page)
                    return
                if data == b"setup_add_user":
                    state = setup_state(callback_user_id)
                    state["awaiting_user"] = True
                    await event.answer()
                    await safe_edit_or_respond(
                        event,
                        "👤 <b>Add a member</b>\n\nSend <code>/setup user TELEGRAM_USER_ID</code> in this private chat.\nUse the member's numeric Telegram ID; the member will still need owner approval to use the assistant.",
                        [[Button.inline("⬅️ Setup", data=b"setup_home")]],
                    )
                    return
                if data.startswith(b"setup_assistant_"):
                    assistant_id = data.decode().split("_", 2)[2]
                    if assistant_id in self.assistant_services:
                        setup_state(callback_user_id)["assistant_id"] = assistant_id
                    await event.answer("Assistant selected")
                    await render_setup_home(event, callback_user_id)
                    return
                if data.startswith(b"setup_pick_"):
                    chat_id = int(data.decode().split("_", 2)[2])
                    state = setup_state(callback_user_id)
                    chats = state.setdefault("chats", {})
                    if chat_id in chats:
                        chats.pop(chat_id, None)
                    else:
                        try:
                            dialogs = await setup_dialogs()
                        except Exception:
                            logger.exception("Could not reload Telegram dialogs while selecting a chat")
                            dialogs = []
                        dialog = next((row for row in dialogs if row["chat_id"] == chat_id), None)
                        if dialog:
                            chats[chat_id] = {**dialog, "mode": "monitor"}
                    await event.answer("Chat selection updated")
                    state = setup_state(callback_user_id)
                    await render_setup_chats(event, callback_user_id, state.get("picker_kind", "all"), state.get("picker_page", 0))
                    return
                if data.startswith(b"setup_modes_"):
                    chat_id = int(data.decode().split("_", 2)[2])
                    await event.answer()
                    await render_setup_mode(event, callback_user_id, chat_id)
                    return
                if data.startswith(b"setup_setmode_"):
                    parts = data.decode().split("_")
                    chat_id, mode = int(parts[2]), parts[3]
                    state = setup_state(callback_user_id)
                    if chat_id in state.get("chats", {}):
                        state["chats"][chat_id]["mode"] = mode
                    await event.answer("Focus mode updated")
                    await render_setup_chats(event, callback_user_id, state.get("picker_kind", "all"), state.get("picker_page", 0))
                    return
                if data.startswith(b"setup_remove_"):
                    chat_id = int(data.decode().split("_", 2)[2])
                    state = setup_state(callback_user_id)
                    state.get("chats", {}).pop(chat_id, None)
                    await event.answer("Chat removed")
                    await render_setup_chats(event, callback_user_id, state.get("picker_kind", "all"), state.get("picker_page", 0))
                    return
                if data == b"setup_save":
                    try:
                        saved, message = await save_setup(callback_user_id)
                    except Exception:
                        logger.exception("Could not save workspace setup")
                        saved, message = False, "Could not save setup. Check the database connection and try again."
                    await event.answer("Setup saved" if saved else "Setup incomplete", alert=not saved)
                    if saved:
                        self._setup_state.pop(callback_user_id, None)
                        await safe_edit_or_respond(event, f"✅ <b>Workspace ready</b>\n\n{escape(message)}\n\nChoonvai Priority Assistant will now focus only on the selected chats.", get_main_menu())
                    else:
                        await render_setup_home(event, callback_user_id)
                    return

            if not self._focus_settings:
                await event.answer("Complete workspace setup before using the assistant", alert=True)
                return

            if data.startswith((b"access_once_", b"access_always_")) or data == b"access_cancel":
                await event.answer("This old request has expired. Ask your question again.", alert=True)
                return

            if data == b"btn_new_project":
                self._pm_state[callback_user_id] = {"kind": "project"}
                await event.answer()
                await safe_edit_or_respond(event, "➕ <b>New Project</b>\n\nWhat should we call this project?\n\n<i>Example: RerngAI</i>", [[Button.inline("❌ Cancel", data=b"pm_cancel")]])
                return
            if data == b"btn_new_task":
                self._pm_state[callback_user_id] = {"kind": "task"}
                await event.answer()
                await safe_edit_or_respond(event, "➕ <b>New Task</b>\n\nWhat needs to be done?\n\n<i>Example: Confirm red craw customer order</i>", [[Button.inline("❌ Cancel", data=b"pm_cancel")]])
                return
            if data == b"pm_cancel":
                self._pm_state.pop(callback_user_id, None)
                await event.answer("Cancelled")
                await render_home(event)
                return

            # Versioned PM callback routes. Older underscore callbacks below
            # remain supported for already-rendered Telegram messages.
            if data.startswith(b"project:"):
                parts = parse(data)
                if len(parts) >= 3 and parts[1] in {"view", "board", "plan"}:
                    await event.answer("Loading project..." if parts[1] == "view" else ("Loading project board..." if parts[1] == "board" else "Calculating CPM..."))
                    if parts[1] == "view":
                        await render_project_detail_view(event, parts[2])
                    elif parts[1] == "board":
                        await render_project_board_view(event, parts[2])
                    else:
                        await render_project_plan_view(event, parts[2])
                    return

            if data.startswith(b"work:"):
                parts = parse(data)
                if len(parts) >= 3 and parts[1] == "view":
                    await event.answer("Loading work item...")
                    await render_work_item_view(event, parts[2])
                    return
                if len(parts) >= 4 and parts[1] == "status":
                    try:
                        updated = await self.business_assistant.update_task(callback_user_id, callback_chat_id, callback_chat_type, parts[2], status=parts[3])
                        await event.answer("Status updated" if updated else "Work item not found")
                        await render_work_item_view(event, parts[2])
                    except ValueError as exc:
                        await event.answer(str(exc), alert=True)
                    return

            if data.startswith(b"action_"):
                parts = data.decode().split("_", 2)
                action_status = "approved" if parts[1] == "approve" else "rejected"
                action_id = parts[2]
                # Outbound sends are always explicit and single-use.
                if callback_user_id != self.business_access.owner_id:
                    await event.answer("Only the owner can approve outbound messages", alert=True)
                    return
                action = await self.business_repository.resolve_action(action_id, callback_user_id, action_status)
                if not action:
                    await event.answer("Draft expired or already resolved", alert=True)
                    return
                if action_status == "approved":
                    try:
                        if action.get("action_type") == "send_message":
                            await bot_client.send_message(action["target"], action["payload"])
                            confirmation = "✅ Message sent after approval."
                        else:
                            payload = json.loads(action["payload"])
                            action_type = action.get("action_type")
                            # Google Calendar/Gmail take priority over the
                            # generic webhook stub once the owner has
                            # connected an account with /connectgoogle.
                            google_credentials = await self.google_account.get_credentials(callback_user_id) if action_type in {"email", "calendar"} else None
                            if action_type == "email":
                                if google_credentials:
                                    from app.integrations.google_gmail import send_message as google_send_message
                                    await google_send_message(google_credentials, to=payload["to"], subject=payload["subject"], body=payload["body"])
                                    confirmation = "✅ Email sent via Gmail after approval."
                                else:
                                    await self.integrations.send_email(**payload)
                                    confirmation = "✅ Email sent after approval."
                            elif action.get("action_type") == "calendar":
                                if google_credentials:
                                    from app.integrations.google_calendar import create_event as google_create_event
                                    created = await google_create_event(google_credentials, summary=payload["title"], start=payload["start"], end=payload.get("end"))
                                    confirmation = f"✅ Calendar event created: <a href=\"{escape(created['link'] or '')}\">{escape(created['summary'] or '')}</a>"
                                else:
                                    await self.integrations.create_calendar_event(**payload)
                                    confirmation = "✅ Calendar event created after approval."
                            elif action.get("action_type") == "crm":
                                await self.integrations.create_crm_record(**payload)
                                confirmation = "✅ CRM record created after approval."
                            else:
                                await self.integrations.create_task(**payload)
                                confirmation = "✅ External task created after approval."
                        await safe_edit_or_respond(event, confirmation, get_main_menu())
                    except Exception:
                        logger.exception("Approved outbound send failed")
                        await safe_edit_or_respond(event, "⚠️ Approved, but Telegram could not deliver the message.", get_main_menu())
                else:
                    await safe_edit_or_respond(event, "❌ Draft rejected; nothing was sent.", get_main_menu())
                return

            # Check priority tier buttons (tier_P0, tier_P1, tier_P2, tier_P3)
            if data.startswith(b"tier_"):
                tier = data.decode().split("_")[1]
                await event.answer(f"Loading {tier}...")
                await render_tier_view(event, tier)

            # View summaries by group
            elif data == b"btn_groups":
                await event.answer("Loading group summaries...")
                await render_groups_view(event)

            # Drill down into a specific chat
            elif data.startswith(b"chat_"):
                chat_id = int(data.decode().split("_")[1])
                await event.answer("Opening chat...")
                await render_single_chat_view(event, chat_id)

            elif data == b"btn_situations":
                await event.answer("Loading situations...")
                await render_situations_view(event)

            elif data.startswith(b"situationresolve_"):
                situation_id = int(data.decode().split("_", 1)[1])
                if callback_user_id != self.business_access.owner_id:
                    await event.answer("Only the owner can resolve situations", alert=True)
                    return
                await self.db.resolve_situation(situation_id)
                await event.answer("Marked resolved")
                await render_situation_detail_view(event, situation_id)

            elif data.startswith(b"situation_"):
                situation_id = int(data.decode().split("_", 1)[1])
                await event.answer("Opening situation...")
                await render_situation_detail_view(event, situation_id)

            elif data == b"btn_digest":
                await event.answer("Updating summary...")
                digest_text, stats = await self.digest_engine.generate_digest(self.focused_chat_ids())
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
                stats = await self.db.get_stats(allowed_chat_ids=self.focused_chat_ids())
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

            elif data == b"btn_memory":
                await event.answer("Loading memory...")
                await render_memory_view(event)

            elif data == b"btn_projects":
                await event.answer("Loading projects...")
                await render_projects_view(event)

            elif data == b"btn_mywork":
                await event.answer("Loading your work...")
                await render_my_work(event)

            elif data.startswith(b"mywork_"):
                await event.answer("Filtering work...")
                await render_my_work(event, data.decode().split("_", 1)[1])

            elif data == b"btn_due":
                await event.answer("Checking due dates...")
                await render_due_soon(event)

            elif data == b"btn_more":
                await event.answer()
                await render_more(event)

            elif data == b"btn_access":
                await event.answer("Loading access settings...")
                await render_access_view(event)

            elif data.startswith(b"project_board_"):
                await event.answer("Loading project board...")
                await render_project_board_view(event, data.decode().split("_", 2)[2])

            elif data.startswith(b"project_plan_"):
                await event.answer("Calculating CPM...")
                await render_project_plan_view(event, data.decode().split("_", 2)[2])

            elif data == b"btn_tasks":
                await event.answer("Loading tasks...")
                await render_tasks_view(event)

            elif data == b"btn_reminders":
                await event.answer("Loading reminders...")
                await render_reminders_view(event)

            elif data == b"btn_new":
                await event.answer("Starting a new conversation...")
                try:
                    deleted = await self.business_assistant.reset_conversation(
                        callback_user_id,
                        callback_chat_id,
                        callback_chat_type,
                    )
                    await safe_edit_or_respond(
                        event,
                        f"💬 <b>New conversation</b>\n\nPrevious saved turns removed: {deleted} conversation(s).",
                        get_main_menu(),
                    )
                except PermissionError:
                    await event.answer("Access not approved", alert=True)

            elif data == b"btn_menu":
                await event.answer()
                if self._focus_settings:
                    await render_home(event)
                else:
                    await safe_edit_or_respond(event, get_welcome_text(), get_main_menu())

        await bot_client.run_until_disconnected()






async def run_daemon():
    app = TelegramPriorityApp()
    await app.initialize()
    await app.start_userbot()


async def run_manual_digest():
    app = TelegramPriorityApp()
    await app.initialize()
    if not app._focus_settings:
        console.print("[bold yellow]Workspace setup is required before generating a digest.[/bold yellow]")
        return
    console.print("[bold yellow]Triggering manual digest generation...[/bold yellow]")
    digest_text, stats = await app.digest_engine.generate_digest(app.focused_chat_ids())
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


async def run_backfill(days: int = 7, limit_per_chat: int = 50, chat_ids: Optional[list[int]] = None) -> None:
    """Import a bounded window of older Telegram messages for the owner."""
    app = TelegramPriorityApp()
    await app.initialize()
    if not app._focus_settings:
        console.print("[bold yellow]History import skipped: select at least one chat with /setup first.[/bold yellow]")
        return
    if not app.cfg.telegram_api_id or not app.cfg.telegram_api_hash:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required for history import")

    from telethon import TelegramClient

    client = TelegramClient(
        app.cfg.telegram_session_name,
        app.cfg.telegram_api_id,
        app.cfg.telegram_api_hash,
    )
    await client.start(phone=app.cfg.telegram_phone)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    selected = set(chat_ids or [])
    imported = 0
    skipped = 0
    try:
        async for dialog in client.iter_dialogs():
            chat_id = int(dialog.id)
            if chat_id not in app._focus_settings:
                continue
            if selected and chat_id not in selected:
                continue
            chat = dialog.entity
            chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or "Private DM"
            if chat_title in app.cfg.profile.ignore_chats or str(chat_id) in app.cfg.profile.ignore_chats:
                continue

            chat_type = "group" if dialog.is_group else ("channel" if dialog.is_channel else "private")
            async for message in client.iter_messages(chat, limit=max(1, limit_per_chat)):
                message_date = message.date
                if message_date and message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=timezone.utc)
                if message_date and message_date < cutoff:
                    break
                if not message.message and not message.media:
                    skipped += 1
                    continue
                if await app.db.message_exists(chat_id, int(message.id)):
                    skipped += 1
                    continue
                sender = await message.get_sender()
                if getattr(sender, "bot", False):
                    skipped += 1
                    continue
                sender_id = getattr(sender, "id", None)
                sender_name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "Unknown"
                message_link = None
                username = getattr(chat, "username", None)
                if username:
                    message_link = f"https://t.me/{username}/{message.id}"
                elif dialog.is_group or dialog.is_channel:
                    message_link = f"https://t.me/c/{str(chat_id).replace('-100', '')}/{message.id}"
                media_type = "text"
                if message.photo:
                    media_type = "photo"
                elif message.document:
                    media_type = "document"
                incoming = IncomingMessage(
                    message_id=int(message.id),
                    chat_id=chat_id,
                    chat_title=chat_title,
                    chat_type=chat_type,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    sender_username=getattr(sender, "username", None),
                    text=message.message or "",
                    date=message_date or datetime.now(timezone.utc),
                    is_reply=bool(message.reply_to),
                    reply_to_msg_id=getattr(message.reply_to, "reply_to_msg_id", None),
                    media_type=media_type,
                    message_link=message_link,
                )
                await app.process_incoming_message(incoming)
                imported += 1
            logger.info("History import checked %s", chat_title)
    finally:
        await client.disconnect()
    logger.info("History import complete: imported=%d skipped=%d", imported, skipped)


async def show_stats():
    app = TelegramPriorityApp()
    await app.initialize()
    stats = await app.db.get_stats(allowed_chat_ids=app.focused_chat_ids())

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
    subparsers.add_parser("dashboard", help="Start the authenticated business status dashboard")
    backfill_parser = subparsers.add_parser("backfill", help="Import a bounded window of older Telegram messages")
    backfill_parser.add_argument("--days", type=int, default=7, help="How many days back to import (default: 7)")
    backfill_parser.add_argument("--limit", type=int, default=50, help="Maximum messages per chat (default: 50)")
    backfill_parser.add_argument("--chat-id", type=int, action="append", help="Only import this chat ID; repeat for multiple chats")

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
    elif args.command == "dashboard":
        app = TelegramPriorityApp()
        asyncio.run(app.initialize())
        DashboardServer(
            app.business_repository,
            app.business_budget,
            app.cfg.dashboard_host,
            app.cfg.dashboard_port,
            app.cfg.dashboard_token,
            app.cfg.schedule_times_list,
            app.business_access.owner_id,
            google_account=app.google_account,
        ).serve_forever()
    elif args.command == "backfill":
        asyncio.run(run_backfill(args.days, args.limit, args.chat_id))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
