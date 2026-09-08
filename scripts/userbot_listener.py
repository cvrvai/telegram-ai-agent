"""Dedicated Telethon Personal Inbox Listener & Contact Bridge (Userbot).

1. Listens for incoming private messages sent to the owner's personal Telegram account.
2. Respects focus approval (permanent or one-time "only this time" permission).
3. Forwards urgent messages directly to the owner via OpenClaw / Telegram Bot.
4. Exposes a local JSON API on http://127.0.0.1:18790 for OpenClaw to:
   - Search/list personal contacts & dialogs (e.g. Daivai)
   - Send Telegram messages directly to contacts from the owner's account
   - Grant permission ("only this time" vs "always monitor")
   - Read recent message history
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

from aiohttp import web
from telethon import TelegramClient, events
from config import AppConfig
from app.storage.business import BusinessRepository

cfg = AppConfig()
DEFAULT_USER_ID = int(os.getenv("NOTIFICATION_CHAT_ID") or os.getenv("OWNER_USER_ID") or "0")
DEFAULT_DB_PATH = str(Path(os.getenv("BUSINESS_DB_PATH", REPO_ROOT / "telegram_business.db")).resolve())
API_PORT = int(os.getenv("USERBOT_API_PORT", "18790"))

# In-memory set for "only this time" temporary permissions
once_permitted_ids: Set[int] = set()


def is_urgent(text: str) -> bool:
    """Fast keyword priority classifier for urgent alerts."""
    urgent_keywords = {
        "urgent", "emergency", "immediately", "asap", "down", "broken", "critical",
        "deadline", "help", "blocked", "issue", "failure", "fail", "alert", "error"
    }
    lowered = text.lower()
    return any(w in lowered for w in urgent_keywords)


async def send_owner_alert(sender_name: str, text: str, priority: str = "P0") -> None:
    """Forward high-priority alert to the owner via OpenClaw Telegram bot."""
    icon = "🚨" if priority == "P0" else "🔴"
    alert_msg = (
        f"{icon} <b>Priority {priority} Message from {sender_name}</b>\n\n"
        f"<i>\"{text[:500]}\"</i>\n\n"
        f"⚡ <i>Forwarded automatically by Choonvai Priority Assistant</i>"
    )
    try:
        subprocess.run(
            [
                "openclaw",
                "message",
                "send",
                "--channel",
                "telegram",
                "--target",
                str(DEFAULT_USER_ID),
                "--message",
                alert_msg,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        print(f"[ERROR] Could not forward alert via OpenClaw: {exc}", file=sys.stderr)


def create_web_app(client: TelegramClient, repo: BusinessRepository) -> web.Application:
    app = web.Application()

    async def get_monitored_ids() -> Set[int]:
        scopes = await repo.list_focus_scopes(enabled_only=True)
        return {int(s["chat_id"]) for s in scopes} | once_permitted_ids

    # GET /contacts?q=<query>&limit=50
    async def handle_contacts(request: web.Request) -> web.Response:
        q = request.query.get("q", "").lower().strip()
        limit = int(request.query.get("limit", "50"))
        try:
            dialogs = await client.get_dialogs(limit=limit)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

        monitored = await get_monitored_ids()
        items = []
        for d in dialogs:
            title = d.title or d.name or "Unknown"
            username = getattr(d.entity, "username", None)
            if q and q not in title.lower() and (not username or q not in username.lower()):
                continue
            is_monitored = d.id in monitored
            is_once = d.id in once_permitted_ids
            items.append({
                "id": d.id,
                "title": title,
                "username": username,
                "is_user": d.is_user,
                "is_group": d.is_group or d.is_channel,
                "monitored": is_monitored,
                "permission": "once" if is_once else ("always" if is_monitored else "none"),
            })
        return web.json_response({"contacts": items})

    # POST /send {"target": <id_or_name>, "message": "<text>"}
    async def handle_send(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        target = body.get("target")
        message = body.get("message")
        if not target or not message:
            return web.json_response({"error": "target and message are required"}, status=400)

        # Resolve entity
        entity = None
        target_str = str(target).strip()
        if target_str.lstrip("-").isdigit():
            entity = int(target_str)
        else:
            # Look up in recent dialogs by username or name
            dialogs = await client.get_dialogs(limit=100)
            lowered = target_str.lower().lstrip("@")
            for d in dialogs:
                uname = (getattr(d.entity, "username", None) or "").lower()
                title = (d.title or d.name or "").lower()
                if lowered == uname or lowered == title or lowered in title:
                    entity = d.entity
                    break

            if entity is None:
                try:
                    entity = await client.get_input_entity(target_str)
                except Exception as exc:
                    return web.json_response({"error": f"Contact '{target}' not found: {exc}"}, status=404)

        try:
            sent = await client.send_message(entity, message)
            return web.json_response({"success": True, "target": str(target), "message_id": sent.id})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    # POST /permission {"target": <id>, "title": "<name>", "scope": "once"|"always"}
    async def handle_permission(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        target = int(body.get("target", 0))
        title = body.get("title") or "Contact"
        scope = body.get("scope", "once")

        if not target:
            return web.json_response({"error": "target chat ID is required"}, status=400)

        if scope == "once":
            once_permitted_ids.add(target)
            print(f"🔓 Granted one-time ('only this time') permission for '{title}' (ID: {target})")
        elif scope == "always":
            await repo.upsert_focus_scope(
                assistant_id="business",
                chat_id=target,
                chat_title=title,
                chat_type="private",
                mode="monitor",
                enabled=True,
            )
            once_permitted_ids.discard(target)
            print(f"🟢 Granted persistent ('always monitor') permission for '{title}' (ID: {target})")
        else:
            return web.json_response({"error": f"Invalid scope: {scope}"}, status=400)

        return web.json_response({"success": True, "target": target, "title": title, "scope": scope})

    # GET /messages?target=<id>&limit=5
    async def handle_messages(request: web.Request) -> web.Response:
        target_str = request.query.get("target")
        limit = int(request.query.get("limit", "5"))
        if not target_str:
            return web.json_response({"error": "target is required"}, status=400)

        entity = int(target_str) if target_str.lstrip("-").isdigit() else target_str
        try:
            msgs = await client.get_messages(entity, limit=limit)
            result = [
                {
                    "id": m.id,
                    "sender_id": m.sender_id,
                    "text": m.raw_text or "",
                    "date": m.date.isoformat() if m.date else "",
                }
                for m in msgs
            ]
            return web.json_response({"messages": result})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    app.router.add_get("/contacts", handle_contacts)
    app.router.add_post("/send", handle_send)
    app.router.add_post("/permission", handle_permission)
    app.router.add_get("/messages", handle_messages)

    return app


async def main() -> None:
    session_path = str(REPO_ROOT / "runtime" / "telegram_ai_session")
    client = TelegramClient(session_path, cfg.telegram_api_id, cfg.telegram_api_hash)

    await client.connect()
    if not await client.is_user_authorized():
        print("=" * 65)
        print("❌ Userbot is NOT authorized yet.")
        print("Please run the one-time interactive login script first:")
        print(f"  {sys.executable} scripts\\login_userbot.py")
        print("=" * 65)
        await client.disconnect()
        sys.exit(1)

    me = await client.get_me()
    print("=" * 65)
    print(f"✓ Telethon Userbot Connected as: {me.first_name} (+{me.phone})")
    print(f"✓ Database: {DEFAULT_DB_PATH}")
    print(f"✓ Alert Target (Owner ID): {DEFAULT_USER_ID}")
    print("=" * 65)

    repo = BusinessRepository(db_path=DEFAULT_DB_PATH)
    await repo.init()

    # Cache of monitored chat IDs
    async def get_monitored_ids() -> Set[int]:
        scopes = await repo.list_focus_scopes(enabled_only=True)
        return {int(s["chat_id"]) for s in scopes} | once_permitted_ids

    # Start Local HTTP Bridge API
    app = create_web_app(client, repo)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", API_PORT)
    await site.start()
    print(f"✓ Contact & Send API listening on http://127.0.0.1:{API_PORT}")

    monitored_ids = await get_monitored_ids()
    print(f"✓ Currently Monitoring {len(monitored_ids)} Approved Contact(s)/Chat(s): {monitored_ids}")

    @client.on(events.NewMessage(incoming=True))
    async def on_new_message(event):
        if event.out:
            return

        chat_id = int(event.chat_id)
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return

        sender_id = int(getattr(sender, "id", chat_id))
        sender_name = getattr(sender, "first_name", "") or getattr(sender, "title", "Unknown")

        # Refresh monitored IDs dynamically
        current_monitored = await get_monitored_ids()

        # Check if chat or sender is in approved focus scopes (or once_permitted)
        if chat_id not in current_monitored and sender_id not in current_monitored:
            print(f"ℹ️ [Incoming from unapproved sender] {sender_name} (ID: {sender_id})")
            print(f"   To approve: .venv\\Scripts\\python.exe scripts\\biz_cli.py focus add --chat-id {sender_id} --title \"{sender_name}\"")
            return

        text = event.raw_text or ""
        print(f"\n📨 [MONITORED MESSAGE] {sender_name} (ID: {sender_id}): {text[:100]}")

        # Classify priority
        urgent = is_urgent(text)
        priority = "P0" if urgent else "P2"
        print(f"📊 Priority: {priority} (Urgent: {urgent})")

        if urgent:
            print(f"🚨 Dispatching instant alert to owner {DEFAULT_USER_ID} via OpenClaw...")
            await send_owner_alert(sender_name, text, priority)

    print(f"\n🎧 Listening for messages from approved contacts on Telegram... (Press Ctrl+C to stop)")
    try:
        await client.run_until_disconnected()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nListener stopped.")
