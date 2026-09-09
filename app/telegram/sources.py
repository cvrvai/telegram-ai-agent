"""Owner-private Telegram directory, consent and date-bounded history tools."""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


LOCAL_TZ = timezone(timedelta(hours=7), name="Asia/Phnom_Penh")


class SourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hint: str = Field(default="", max_length=200, description="Person/group name or Saved Messages; empty to let the user choose.")
    kind: Literal["all", "private", "group", "channel"] = "all"
    period: Literal["recent", "this_month", "last_month", "last_7_days", "today", "custom"] = "recent"
    start_date: str | None = Field(default=None, description="Inclusive YYYY-MM-DD for a custom period")
    end_date: str | None = Field(default=None, description="Inclusive YYYY-MM-DD for a custom period")


class InteractionRequired(Exception):
    def __init__(self, state, payload):
        self.state, self.payload = state, payload
        super().__init__(state)


def normalize(value):
    return "".join(c for c in value.casefold() if c.isalnum())


def window(args, current=None):
    current = (current or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)
    midnight = datetime.combine(current.date(), time.min, LOCAL_TZ)
    end = current
    start = None
    if args.period == "this_month":
        start = midnight.replace(day=1)
    elif args.period == "last_month":
        end = midnight.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
    elif args.period == "today":
        start = midnight
    elif args.period == "last_7_days":
        start = current - timedelta(days=7)
    elif args.period == "custom":
        if not args.start_date or not args.end_date:
            raise ValueError("Please provide both a start and end date.")
        start = datetime.combine(date.fromisoformat(args.start_date), time.min, LOCAL_TZ)
        end = min(datetime.combine(date.fromisoformat(args.end_date) + timedelta(days=1), time.min, LOCAL_TZ), current)
    if start and start >= end:
        raise ValueError("The start date must be before the end date.")
    return {"start": start.isoformat() if start else None, "end": end.isoformat(), "timezone": "Asia/Phnom_Penh"}


class TelegramSources:
    def __init__(self, client_provider, owner_id, store, focused_ids, access):
        self.client_provider, self.owner_id, self.store = client_provider, owner_id, store
        self.focused_ids, self.access = focused_ids, access

    def authorize_actor(self, actor, chat, kind):
        if actor != self.owner_id or kind != "private" or not self.access.can_use(actor, chat, kind):
            raise PermissionError("Please ask the owner in their private bot conversation to access Telegram sources.")

    async def directory(self):
        client = self.client_provider()
        if client is None:
            raise RuntimeError("Connect the Telegram user account before selecting chats.")
        me = await client.get_me()
        if not me or not getattr(me, "id", None):
            raise RuntimeError("Telegram could not identify the connected account.")
        account = int(me.id)
        rows = {account: {"id": account, "account_id": account, "title": "Saved Messages", "kind": "private", "username": "me", "self": True}}
        async for dialog in client.iter_dialogs(limit=None):
            entity = dialog.entity
            if getattr(entity, "migrated_to", None) is not None or getattr(entity, "bot", False):
                continue
            peer = int(dialog.id)
            if peer in rows:
                continue
            if getattr(dialog, "is_user", False):
                kind = "private"
            elif getattr(dialog, "is_group", False):
                kind = "group"
            elif getattr(dialog, "is_channel", False):
                kind = "channel"
            else:
                continue
            rows[peer] = {"id": peer, "account_id": account, "title": str(dialog.name or f"Chat {peer}"), "kind": kind,
                          "username": str(getattr(entity, "username", None) or ""), "self": False}
        return sorted(rows.values(), key=lambda row: (not row["self"], row["title"].casefold(), row["id"]))

    @staticmethod
    def matches(rows, hint, kind="all"):
        candidates = [row for row in rows if kind == "all" or row["kind"] == kind]
        if not hint or not str(hint).strip():
            return candidates

        raw_hint = str(hint).strip().casefold()
        clean_hint = re.sub(r'\b(can\s+you|please|check|read|look\s+at|open|view|the|group|chat|channel|team)\b', '', raw_hint).strip()
        needle = normalize(clean_hint or raw_hint)

        if needle in {"saved", "save", "savedmessage", "savedmessages", "savemessages", "me"}:
            return [row for row in candidates if row["self"]]

        # 1. Exact match on title or username
        exact = [row for row in candidates if needle in {normalize(row["title"]), normalize(row["username"])}]
        if exact:
            return exact

        # 2. Substring match on entire needle
        substr = [row for row in candidates if needle and (needle in normalize(row["title"]) or needle in normalize(row["username"]))]
        if substr:
            return substr

        # 3. Multi-token match: all tokens present in normalized title or username
        tokens = [normalize(t) for t in (clean_hint or raw_hint).split() if normalize(t)]
        if len(tokens) > 1:
            token_matches = [
                row for row in candidates
                if all(t in normalize(row["title"]) or t in normalize(row["username"]) for t in tokens)
            ]
            if token_matches:
                return token_matches

        # 4. Ranked token match: any token present, ordered by score
        if tokens:
            scored = []
            for row in candidates:
                title_norm = normalize(row["title"])
                user_norm = normalize(row["username"])
                score = sum(1 for t in tokens if t in title_norm or t in user_norm)
                if score > 0:
                    scored.append((score, row))
            if scored:
                scored.sort(key=lambda x: -x[0])
                return [row for _, row in scored]

        return []

    async def permitted(self, actor, source, run):
        # A one-run approval is bound to the exact source AND connected account.
        fresh = await self.store.get(run["_id"])
        if not fresh or fresh["state"] != "running":
            raise asyncio.CancelledError()
        grant = fresh.get("once")
        if grant and grant == {"account_id": source["account_id"], "source_id": source["id"]}:
            return True
        return source["id"] in self.focused_ids() or await self.store.has_grant(actor, source)

    async def request(self, args, context, choose=False):
        self.authorize_actor(context.actor_id, context.chat_id, context.chat_type)
        run = await self.store.get(context.session["_run_id"])
        rows = await self.directory()
        if not args.hint and not choose:
            args = args.model_copy(update={"hint": context.session.get("last_source_hint", "")})
        dates = window(args)
        candidates = self.matches(rows, args.hint, args.kind)
        payload = {"args": args.model_dump(), "window": dates, "directory": rows, "candidates": candidates}
        if choose or len(candidates) != 1:
            raise InteractionRequired("selecting", payload)
        return await self.selected(candidates[0], payload, context, run)

    async def selected(self, source, payload, context, run):
        if not await self.permitted(context.actor_id, source, run):
            raise InteractionRequired("awaiting_approval", {**payload, "source": source})
        return await self.read(source, payload["window"], context, run)

    async def read(self, source, dates, context, run):
        self.authorize_actor(context.actor_id, context.chat_id, context.chat_type)
        client = self.client_provider()
        me = await client.get_me()
        if not me or int(me.id) != source["account_id"]:
            raise PermissionError("The connected Telegram account changed. Select the chat again.")
        if not await self.permitted(context.actor_id, source, run):
            raise PermissionError("Reading permission has been revoked.")
        start = datetime.fromisoformat(dates["start"]) if dates["start"] else None
        end = datetime.fromisoformat(dates["end"])
        limit = 500 if start else 100
        rows, scanned, omitted, chars, partial = [], 0, 0, 0, False
        # Telethon paginates internally, newest first; +1 detects truncation.
        async for message in client.iter_messages(source["id"], offset_date=end, limit=limit + 1):
            self.authorize_actor(context.actor_id, context.chat_id, context.chat_type)
            if not await self.permitted(context.actor_id, source, run):
                raise PermissionError("Reading permission has been revoked.")
            timestamp = message.date
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if timestamp >= end:
                continue
            if start and timestamp < start:
                break
            if scanned >= limit:
                partial = True
                break
            scanned += 1
            text = (getattr(message, "message", None) or "").strip()
            if not text:
                omitted += 1
                continue
            if chars + len(text) > 48000:
                partial = True
                break
            chars += len(text)
            link = None
            if source["username"] and not source["self"]:
                link = f"https://t.me/{source['username']}/{message.id}" if source["kind"] != "private" else None
            elif str(source["id"]).startswith("-100"):
                link = f"https://t.me/c/{str(source['id'])[4:]}/{message.id}"
            rows.append({"message_id": message.id, "date": timestamp.isoformat(), "sender_id": getattr(message, "sender_id", None),
                         "direction": "outgoing" if getattr(message, "out", False) else "incoming", "text": text, "link": link})
        context.session["last_source_hint"] = source["title"]
        context.session["last_window"] = dates
        context.session["_read_source"] = source
        return {"source": source, "window": dates, "messages": list(reversed(rows)), "messages_analyzed": len(rows),
                "scanned": scanned, "nontext_omitted": omitted, "partial": partial,
                "coverage_notice": "Partial history: reached the message or text budget." if partial else "All accessible messages in the requested window were examined." if start else "Latest messages only; not the entire chat history."}


async def read_telegram_chat(args, context):
    if context.telegram is None:
        raise RuntimeError("Telegram source tools are not connected.")
    return await context.telegram.request(args, context)


async def select_telegram_chat(args, context):
    if context.telegram is None:
        raise RuntimeError("Telegram source tools are not connected.")
    return await context.telegram.request(args, context, choose=True)


async def telegram_access(args, context):
    context.telegram.authorize_actor(context.actor_id, context.chat_id, context.chat_type)
    raise InteractionRequired("selecting", {"access_view": True, "grants": await context.telegram.store.list_grants(context.actor_id)})


class SendTelegramMessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str = Field(description="Name, username, or numeric ID of the Telegram contact or chat to message (e.g. 'Dai Vai', '@DaivaiCheong', or '1001924732').")
    message: str = Field(min_length=1, max_length=4000, description="The message text to send.")


async def draft_telegram_message(args: SendTelegramMessageInput, context: Any):
    if context.telegram is None:
        raise RuntimeError("Telegram source tools are not connected.")
    context.telegram.authorize_actor(context.actor_id, context.chat_id, context.chat_type)
    rows = await context.telegram.directory()
    candidates = context.telegram.matches(rows, args.recipient)
    if not candidates:
        target_id = args.recipient
        target_title = args.recipient
    else:
        target_id = str(candidates[0]["id"])
        target_title = candidates[0]["title"]

    action_id = await context.service.draft_action(
        context.actor_id, context.chat_id, context.chat_type,
        "send_message", target_id, args.message, 15
    )
    context.session["pending_draft_action"] = {"id": action_id, "label": f"message to {target_title}"}
    return {
        "drafted": True,
        "action_id": action_id,
        "recipient": target_title,
        "message": args.message,
    }

