"""Situation fusion: eligibility, LLM/heuristic linking, and storage lifecycle.

Mirrors the httpx.MockTransport pattern already used for the conversational
agent's provider tests (tests/test_telegram_agent.py) rather than hitting a
real Ollama endpoint.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from app.core.models import IncomingMessage, PriorityClassification
from app.priority.situations import SituationLinker
from app.storage.sqlite_messages import Database
from config import AppConfig


def _message(text: str, chat_id: int = 1001, message_id: int = 1, chat_title: str = "Engineering") -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_title=chat_title,
        chat_type="group",
        sender_name="Sophat",
        text=text,
        date=datetime.now(timezone.utc),
        message_link=f"https://t.me/c/{chat_id}/{message_id}",
    )


def _classification(message_type: str = "task", priority: str = "P1", summary: str = "Room 305 AC is broken") -> PriorityClassification:
    return PriorityClassification(
        priority=priority,
        score=80,
        reason="test",
        needs_action=True,
        action="Check the unit",
        category="engineering",
        summary=summary,
        message_type=message_type,
    )


class EligibilityTests(unittest.TestCase):
    def test_p0_p1_p2_task_types_are_eligible(self) -> None:
        for priority in ("P0", "P1", "P2"):
            for message_type in ("task", "update", "blocker", "waiting", "decision"):
                self.assertTrue(SituationLinker.eligible(_classification(message_type=message_type, priority=priority)))

    def test_p3_is_never_eligible(self) -> None:
        self.assertFalse(SituationLinker.eligible(_classification(priority="P3")))

    def test_chatter_and_question_are_not_situation_forming(self) -> None:
        self.assertFalse(SituationLinker.eligible(_classification(message_type="chatter")))
        self.assertFalse(SituationLinker.eligible(_classification(message_type="question")))


class LinkingWithoutOpenSituationsTests(unittest.IsolatedAsyncioTestCase):
    async def test_seeds_a_new_situation_with_no_llm_call(self) -> None:
        linker = SituationLinker(AppConfig())
        msg = _message("Room 305 AC broken, guest is unhappy")
        cls = _classification()
        with patch("httpx.AsyncClient") as fake_client:
            decision = await linker.link(msg, cls, open_situations=[])
        fake_client.assert_not_called()
        self.assertEqual(decision.action, "new")
        self.assertTrue(decision.guest_affected)
        self.assertEqual(decision.current_action, cls.action)


class LinkingWithOpenSituationsTests(unittest.IsolatedAsyncioTestCase):
    def _open_situation(self, **overrides):
        from app.core.models import Situation
        base = dict(
            id=7,
            chat_id=1001,
            chat_title="Engineering",
            title="Room 305 AC",
            status="open",
            priority="P1",
            summary="Room 305 AC broken, engineer checking",
            last_update_at=datetime.now(timezone.utc).isoformat(),
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        base.update(overrides)
        return Situation(**base)

    async def test_llm_continue_decision_is_trusted_when_the_id_is_known(self) -> None:
        linker = SituationLinker(AppConfig())
        situation = self._open_situation()

        def respond(request):
            body = {"action": "continue", "situation_id": 7, "status": "open", "current_action": "Waiting for part", "dependency": "Spare part", "guest_affected": True, "responsible": "Engineering", "reason": "same AC issue"}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(body)}}]})

        original = httpx.AsyncClient
        with patch("app.priority.situations.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(respond), **kw)):
            decision = await linker.link(_message("Still waiting for the AC part"), _classification(), [situation])
        self.assertEqual(decision.action, "continue")
        self.assertEqual(decision.situation_id, 7)
        self.assertEqual(decision.dependency, "Spare part")

    async def test_hallucinated_situation_id_is_rejected_as_new(self) -> None:
        linker = SituationLinker(AppConfig())
        situation = self._open_situation()

        def respond(request):
            body = {"action": "continue", "situation_id": 999, "status": "open", "reason": "made up"}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(body)}}]})

        original = httpx.AsyncClient
        with patch("app.priority.situations.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(respond), **kw)):
            decision = await linker.link(_message("Unrelated F&B invoice question"), _classification(), [situation])
        self.assertEqual(decision.action, "new")

    async def test_provider_failure_falls_back_to_the_keyword_heuristic(self) -> None:
        linker = SituationLinker(AppConfig())
        situation = self._open_situation()

        def respond(request):
            raise httpx.ConnectError("Ollama unreachable", request=request)

        original = httpx.AsyncClient
        with patch("app.priority.situations.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(respond), **kw)):
            decision = await linker.link(_message("Room 305 AC part still not installed"), _classification(), [situation])
        self.assertEqual(decision.action, "continue")
        self.assertEqual(decision.situation_id, 7)
        self.assertIn("Heuristic", decision.reason)

    def test_heuristic_ignores_a_stale_open_situation(self) -> None:
        linker = SituationLinker(AppConfig())
        stale = self._open_situation(last_update_at=(datetime.now(timezone.utc) - timedelta(hours=200)).isoformat())
        decision = linker._fallback_link(_message("Room 305 AC part still not installed"), _classification(), [stale])
        self.assertEqual(decision.action, "new")

    def test_heuristic_starts_new_for_an_unrelated_message(self) -> None:
        linker = SituationLinker(AppConfig())
        situation = self._open_situation()
        decision = linker._fallback_link(_message("F&B supplier sent the wrong invoice amount"), _classification(summary="F&B invoice mismatch"), [situation])
        self.assertEqual(decision.action, "new")


class SituationStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "messages.db"))
        await self.db.init_db()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_create_then_continue_then_resolve_lifecycle(self) -> None:
        linker = SituationLinker(AppConfig())
        first_msg, first_cls = _message("Room 305 AC broken", message_id=1), _classification(priority="P1")
        decision = await linker.link(first_msg, first_cls, open_situations=[])
        situation_id = await self.db.create_situation(first_msg, decision, first_cls)

        open_situations = await self.db.get_open_situations(chat_id=1001)
        self.assertEqual(len(open_situations), 1)
        self.assertEqual(open_situations[0].message_count, 1)
        self.assertEqual(open_situations[0].source_message_ids, [1])

        second_msg = _message("Room 305 AC still broken, waiting on the spare part", message_id=2)
        second_cls = _classification(priority="P0", message_type="waiting", summary="Room 305 AC still broken, spare part needed")
        continue_decision = linker._fallback_link(second_msg, second_cls, open_situations)
        self.assertEqual(continue_decision.action, "continue")
        await self.db.update_situation(situation_id, second_msg, continue_decision, second_cls)

        updated = await self.db.get_situation(situation_id)
        self.assertEqual(updated.message_count, 2)
        self.assertEqual(updated.source_message_ids, [1, 2])
        # A higher-severity update (P0) raises the tracked priority.
        self.assertEqual(updated.priority, "P0")

        resolved = await self.db.resolve_situation(situation_id)
        self.assertTrue(resolved)
        after_resolve = await self.db.get_open_situations(chat_id=1001)
        self.assertEqual(after_resolve, [])

    async def test_unrelated_messages_create_separate_situations(self) -> None:
        linker = SituationLinker(AppConfig())
        ac_msg, ac_cls = _message("Room 305 AC broken", message_id=1), _classification(summary="Room 305 AC broken")
        ac_decision = await linker.link(ac_msg, ac_cls, open_situations=[])
        await self.db.create_situation(ac_msg, ac_decision, ac_cls)

        open_situations = await self.db.get_open_situations(chat_id=1001)
        invoice_msg = _message("F&B supplier sent the wrong invoice amount", message_id=2)
        invoice_cls = _classification(summary="F&B invoice mismatch")
        invoice_decision = linker._fallback_link(invoice_msg, invoice_cls, open_situations)
        self.assertEqual(invoice_decision.action, "new")
        await self.db.create_situation(invoice_msg, invoice_decision, invoice_cls)

        self.assertEqual(len(await self.db.get_open_situations(chat_id=1001)), 2)

    async def test_list_situations_ranks_by_priority(self) -> None:
        linker = SituationLinker(AppConfig())
        low_msg, low_cls = _message("Housekeeping restock request", message_id=1), _classification(priority="P2", summary="Restock request")
        await self.db.create_situation(low_msg, await linker.link(low_msg, low_cls, []), low_cls)
        critical_msg, critical_cls = _message("Fire alarm triggered", message_id=2), _classification(priority="P0", summary="Fire alarm triggered")
        await self.db.create_situation(critical_msg, await linker.link(critical_msg, critical_cls, []), critical_cls)

        ranked = await self.db.list_situations(limit=10)
        self.assertEqual(ranked[0].priority, "P0")
        self.assertEqual(ranked[1].priority, "P2")


if __name__ == "__main__":
    unittest.main()
