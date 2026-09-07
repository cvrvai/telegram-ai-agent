"""Telegram interaction adapter for durable, cancellable conversational runs."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timezone
from types import SimpleNamespace

import httpx
from telethon import Button

from app.agent.state import now
from app.telegram.sources import InteractionRequired
from app.usage.budget import BudgetExceeded


logger = logging.getLogger(__name__)
CANCEL_WORDS = {"cancel", "stop", "never mind", "nevermind", "cancel this", "stop this", "please cancel", "please stop", "/cancel"}


def chunks(text, limit=3500):
    part, size = [], 0
    for char in text:
        count = 2 if ord(char) > 0xFFFF else 1
        if size + count > limit:
            yield "".join(part)
            part, size = [], 0
        part.append(char)
        size += count
    if part:
        yield "".join(part)


class TelegramConversation:
    def __init__(self, runtime, sources, store, timeout=600):
        self.runtime, self.sources, self.store, self.timeout = runtime, sources, store, timeout
        self.runtime.telegram = sources
        self.locks = {}
        self.tasks = {}
        self.slots = asyncio.Semaphore(3)

    async def controls(self, event):
        """Called before legacy forms so cancellation never becomes form input."""
        text = (event.raw_text or "").strip()
        actor, chat = event.sender_id, event.chat_id
        key = (actor, chat)
        if text.casefold() in CANCEL_WORDS:
            count = await self.store.cancel(actor, chat)
            active = list(self.tasks.get(key, {}).values())
            for task in active:
                task.cancel()
            if count or active:
                await event.respond("Stopped. I won't continue that request.", parse_mode=None)
                return True
            return False
        pending = await self.store.pending(actor, chat)
        if not pending:
            return False
        if pending["state"] == "awaiting_approval" and text.casefold() in {"yes", "allow", "allow once", "yes please"}:
            await self._approve(event, pending, persistent=False)
            return True
        if pending["state"] == "awaiting_approval" and text.casefold() in {"no", "no thanks", "deny"}:
            await self.store.transition(pending, ["awaiting_approval"], "cancelled")
            await event.respond("Cancelled. No new access was granted.", parse_mode=None)
            return True
        if pending["state"] == "selecting" and not pending.get("payload", {}).get("access_view") and not text.startswith("/"):
            # Clear new requests leave selection; otherwise plain text searches.
            if not any(word in text.casefold().split() for word in {"summarize", "summarise", "check", "read", "hello", "hi"}):
                payload = pending["payload"]
                payload["args"]["hint"] = text
                payload["candidates"] = self.sources.matches(payload["directory"], text, payload["args"]["kind"])
                updated = await self.store.transition(pending, ["selecting"], "selecting", payload=payload)
                if updated:
                    await self.render(event, updated)
                return True
        return False

    async def handle(self, event, text):
        actor, chat = int(event.sender_id), int(event.chat_id)
        kind = "private" if event.is_private else "group"
        if not self.runtime.service.access.can_use(actor, chat, kind):
            await event.respond("You don't have access to this assistant.")
            return
        record = await self.store.create(actor, chat, kind, text, f"telegram:{chat}:{event.id}")
        if record:
            await self.execute(event, record)

    async def execute(self, event, record, resume=False):
        key = (record["actor_id"], record["chat_id"])
        run_id = record["_id"]
        task = asyncio.current_task()
        self.tasks.setdefault(key, {})[record["_id"]] = task
        progress = None
        try:
            async with self.locks.setdefault(key, asyncio.Lock()):
                async with self.slots:
                    record = await self.store.transition(record, ["queued"], "running")
                    if not record:
                        return
                    # A newer conversation turn supersedes old unanswered buttons.
                    pending = await self.store.pending(*key)
                    if pending:
                        await self.store.transition(pending, ["selecting", "awaiting_approval"], "cancelled")
                    progress = await event.respond("Reading the selected chat…" if resume else "Thinking…", parse_mode=None)
                    session = await self.store.load_session(*key)
                    session["_run_id"] = record["_id"]
                    # Cleared on load, not just after use: a stale marker from
                    # a run that never reached the button-rendering step below
                    # must never resurface on a later, unrelated turn.
                    session.pop("pending_draft_action", None)
                    session.pop("pending_file", None)
                    initial = []

                    async def work():
                        if resume:
                            context = SimpleNamespace(actor_id=key[0], chat_id=key[1], chat_type=record["chat_type"], session=session)
                            payload = record["payload"]
                            data = await self.sources.selected(payload["source"], payload, context, record)
                            initial.append({"tool": "read_telegram_chat", "arguments": payload["args"], "result": {"ok": True, "data": data}})
                        answer = await self.runtime.handle_turn(*key, record["chat_type"], record["request"], session_state=session, initial_results=initial)
                        if session.get("_read_source"):
                            self.sources.authorize_actor(*key, record["chat_type"])
                            if not await self.sources.permitted(key[0], session["_read_source"], record):
                                raise PermissionError("Reading permission was revoked before the reply was ready.")
                        await self.store.save_session(*key, session)
                        return answer

                    answer = await asyncio.wait_for(work(), self.timeout)
                    completed = await self.store.transition(record, ["running"], "completed")
                    if completed:
                        # Reject accidental internal payloads even from a native final reply.
                        if '"tool_call"' in answer or '"tool_name"' in answer:
                            answer = "The AI returned an invalid reply. Please try again."
                        parts = list(chunks(answer)) or ["I couldn't generate a response."]
                        # A tool this turn (create_calendar_event, send_email) may have
                        # drafted a pending action -- attach the same Approve/Reject
                        # buttons the manual /event and /email commands use, so the
                        # owner confirms through the one existing approval path.
                        pending_draft = session.pop("pending_draft_action", None)
                        buttons = None
                        if pending_draft:
                            action_id = pending_draft["id"]
                            buttons = [[Button.inline("✅ Approve", data=f"action_approve_{action_id}".encode()),
                                        Button.inline("❌ Reject", data=f"action_deny_{action_id}".encode())]]
                        await progress.edit(parts[0], parse_mode=None, buttons=buttons)
                        for part in parts[1:]:
                            await event.respond(part, parse_mode=None)
                        # A tool may have produced a document (the weekly deck).
                        # Agents can only return text, so the file is handed over
                        # via the session and sent here.
                        pending_file = session.pop("pending_file", None)
                        if pending_file:
                            try:
                                await event.respond(pending_file.get("caption") or "", file=pending_file["path"], parse_mode=None)
                            except Exception:
                                logger.warning("Could not deliver generated file %s", pending_file.get("path"))
                            finally:
                                try:
                                    os.unlink(pending_file["path"])
                                except OSError:
                                    pass
        except InteractionRequired as interaction:
            if progress:
                await progress.edit("Choose the Telegram chat below." if interaction.state == "selecting" else "Please choose how to allow access below.", parse_mode=None)
            suspended = await self.store.transition(record, ["running"], interaction.state, payload=interaction.payload)
            if suspended:
                await self.render(event, suspended)
        except asyncio.CancelledError:
            if record:
                await self.store.transition(record, ["queued", "running"], "cancelled")
            if progress:
                await progress.edit("Stopped.", parse_mode=None)
        except Exception as exc:
            logger.warning("Agent run %s failed (%s)", record and record["_id"], type(exc).__name__)
            if isinstance(exc, httpx.HTTPStatusError):
                code = exc.response.status_code
                message = {401: "Ollama rejected the API key. Update OLLAMA_API_KEY and restart the bot.",
                           403: "Ollama denied access to this model. Check your account's model access.",
                           429: "Ollama's usage limit was reached. Please try again later.",
                           404: "Ollama could not find the configured model or endpoint."}.get(code, "Ollama could not complete the request. Please try again.")
            elif isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
                message = "That request took too long. Try a shorter date range, or try again."
            elif isinstance(exc, PermissionError):
                message = str(exc)
            elif isinstance(exc, BudgetExceeded):
                message = "The AI allowance is currently used. Please contact the administrator."
            else:
                message = "I couldn't complete that request. Check the connected Telegram account and AI service, then try again."
            failed = await self.store.transition(record, ["running", "queued"], "failed", error_code=type(exc).__name__)
            if failed:
                if progress:
                    await progress.edit(message, parse_mode=None)
                else:
                    await event.respond(message, parse_mode=None)
        finally:
            self.tasks.get(key, {}).pop(run_id, None)

    async def render(self, event, record, page=0):
        payload, run_id = record["payload"], record["_id"]
        def button(label, action):
            return Button.inline(label, f"agent:{run_id}:{action}".encode())
        if payload.get("access_view"):
            rows = payload["grants"]
            buttons = [[button(f"Revoke {row['source']['title'][:28]}", f"revoke:{i}")] for i, row in enumerate(rows[:20])]
            buttons.append([button("Done", "cancel")])
            await event.respond("Persistent read permissions. These do not enable monitoring.\n" + ("No persistent read permissions." if not rows else "Choose a permission to revoke.") + "\nMonitoring permissions are managed in Setup.", buttons=buttons, parse_mode=None)
            return
        if record["state"] == "awaiting_approval":
            source, dates = payload["source"], payload["window"]
            text = (f"Allow reading {source['title']} ({source['kind']})?\n"
                    f"Range: {dates['start'] or 'latest messages'} → {dates['end']} ({dates['timezone']}).\n"
                    "Only this chat will be read. Approved text is processed by Ollama Cloud when configured.\n"
                    "Allow once covers this request only. Always allow reading does not enable monitoring.")
            buttons = [[button("Allow once", "once"), button("Always allow reading", "always")], [button("Cancel", "cancel")]]
        else:
            rows = payload["candidates"]
            page = max(0, min(page, max(0, (len(rows) - 1) // 8)))
            text = f"Choose a Telegram chat · {page + 1}/{max(1, (len(rows) + 7) // 8)}\nType a name here to search. Your original request will continue after selection."
            if not rows:
                text += "\nNo matches. Try another name or choose All."
            buttons = [[button(f"{row['title'][:28]} · {row['kind']} · {str(row['id'])[-6:]}", f"pick:{row['id']}")] for row in rows[page * 8:(page + 1) * 8]]
            buttons.append([button("People", "kind:private"), button("Groups", "kind:group"), button("All", "kind:all")])
            navigation = []
            if page:
                navigation.append(button("Previous", f"page:{page-1}"))
            if (page + 1) * 8 < len(rows):
                navigation.append(button("Next", f"page:{page+1}"))
            if navigation:
                buttons.append(navigation)
            buttons.append([button("Cancel", "cancel")])
        await event.respond(text, buttons=buttons, parse_mode=None)

    async def callback(self, event):
        raw = event.data.decode("utf-8")
        if not raw.startswith("agent:"):
            return False
        _, run_id, action = raw.split(":", 2)
        record = await self.store.get(run_id)
        if not record or record["actor_id"] != event.sender_id or record["chat_id"] != event.chat_id:
            await event.answer("This request belongs to another conversation.", alert=True)
            return True
        self.sources.authorize_actor(event.sender_id, event.chat_id, record["chat_type"])
        expiry = record["expires_at"]
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= now() or record["state"] not in {"selecting", "awaiting_approval"}:
            await event.answer("This request has expired or already finished.", alert=True)
            return True
        await event.answer()
        if action == "cancel":
            if await self.store.transition(record, ["selecting", "awaiting_approval"], "cancelled"):
                await event.respond("Cancelled.", parse_mode=None)
        elif action in {"once", "always"} and record["state"] == "awaiting_approval":
            await self._approve(event, record, persistent=action == "always")
        elif action.startswith("pick:") and record["state"] == "selecting" and not record["payload"].get("access_view"):
            source = next((row for row in record["payload"]["candidates"] if str(row["id"]) == action[5:]), None)
            if source:
                payload = {**record["payload"], "source": source}
                updated = await self.store.transition(record, ["selecting"], "queued", payload=payload)
                if updated:
                    await self.execute(event, updated, resume=True)
        elif action.startswith("page:") and record["state"] == "selecting":
            await self.render(event, record, int(action[5:]))
        elif action.startswith("kind:") and record["state"] == "selecting" and not record["payload"].get("access_view"):
            kind = action[5:]
            if kind in {"all", "private", "group"}:
                payload = record["payload"]
                payload["args"].update(kind=kind, hint="")
                payload["candidates"] = self.sources.matches(payload["directory"], "", kind)
                updated = await self.store.transition(record, ["selecting"], "selecting", payload=payload)
                if updated:
                    await self.render(event, updated)
        elif action.startswith("revoke:") and record["payload"].get("access_view"):
            index = int(action[7:])
            rows = record["payload"]["grants"]
            if 0 <= index < len(rows):
                item = rows[index]
                claimed = await self.store.transition(record, ["selecting"], "completed")
                if claimed:
                    await self.store.revoke(event.sender_id, item["account_id"], item["source_id"])
                    await event.respond("Read permission revoked. If this chat is also monitored, remove it in Setup to stop that separate access.", parse_mode=None)
        return True

    async def _approve(self, event, record, persistent):
        self.sources.authorize_actor(record["actor_id"], record["chat_id"], record["chat_type"])
        source = record["payload"]["source"]
        # Only the atomic winner may consume approval and resume this exact run.
        claimed = await self.store.transition(record, ["awaiting_approval"], "queued",
            once=None if persistent else {"account_id": source["account_id"], "source_id": source["id"]})
        if claimed:
            if persistent:
                await self.store.grant(record["actor_id"], source)
            await self.execute(event, claimed, resume=True)
