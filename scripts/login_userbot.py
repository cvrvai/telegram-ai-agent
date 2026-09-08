"""Interactive login utility for the personal Telethon Userbot.

Run this script once to authenticate your personal Telegram account
and create the authorized session in runtime/telegram_ai_session.session.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

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

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from config import AppConfig


async def main() -> None:
    cfg = AppConfig()
    session_dir = REPO_ROOT / "runtime"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_path = str(session_dir / "telegram_ai_session")

    print("=" * 60)
    print("Telegram Personal Account Login (Telethon Userbot)")
    print("=" * 60)
    print(f"API ID: {cfg.telegram_api_id}")
    print(f"Session Path: {session_path}.session")

    client = TelegramClient(session_path, cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"\n✅ Already authorized!")
        print(f"Name: {me.first_name} {me.last_name or ''}")
        print(f"Username: @{me.username or 'None'}")
        print(f"Phone: +{me.phone}")
        print(f"User ID: {me.id}")
        await client.disconnect()
        return

    phone = cfg.telegram_phone or input("Enter your phone number (with country code, e.g. +855...): ").strip()
    print(f"\nSending login code to Telegram for {phone}...")
    await client.send_code_request(phone)

    code = input("\nEnter the 5-digit verification code you received in Telegram: ").strip()
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        pw = input("Two-step verification (2FA) is enabled. Enter your 2FA password: ")
        await client.sign_in(password=pw)

    me = await client.get_me()
    print(f"\n🎉 Successfully logged in!")
    print(f"Name: {me.first_name} {me.last_name or ''}")
    print(f"User ID: {me.id}")
    print(f"Session saved to: {session_path}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
