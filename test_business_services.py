"""Focused tests for the new business-assistant boundaries."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.assistants import AssistantProfile, AssistantRegistry
from app.ai.provider import StubProvider
from app.content import DocumentExtractor
from app.dashboard import DashboardService
from app.security.access import AccessController
from app.services.assistant import AssistantService
from app.storage.business import BusinessRepository
from app.usage.budget import BudgetExceeded, UsageBudget
from app.projects.cpm import calculate_cpm


class BusinessAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "business.db")
        self.repository = BusinessRepository(self.db_path)
        await self.repository.init()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_authorized_chat_is_persisted_and_metered(self) -> None:
        service = AssistantService(
            self.repository,
            AccessController(owner_id=42),
            StubProvider("Approved answer", input_tokens=100, output_tokens=20),
            UsageBudget(monthly_limit_usd=20),
        )

        answer = await service.answer(42, 1001, "private", "What is the next meeting?")

        self.assertEqual(answer, "Approved answer")
        conversation_id = await self.repository.create_or_get_conversation("business", 42, 1001)
        turns = await self.repository.recent_turns(conversation_id)
        self.assertEqual([turn["role"] for turn in turns], ["user", "assistant"])
        self.assertGreater(await self.repository.usage_total(service.budget.period), 0)

    async def test_unapproved_user_cannot_create_a_conversation(self) -> None:
        service = AssistantService(
            self.repository,
            AccessController(owner_id=42),
            StubProvider(),
            UsageBudget(),
        )

        with self.assertRaises(PermissionError):
            await service.answer(99, 1001, "private", "Show me the business history")

    async def test_budget_stops_requests_before_provider_call(self) -> None:
        service = AssistantService(
            self.repository,
            AccessController(owner_id=42),
            StubProvider(),
            UsageBudget(monthly_limit_usd=0.000001),
        )

        with self.assertRaises(BudgetExceeded):
            await service.answer(42, 1001, "private", "A request that is above the allowance")

    async def test_pairing_can_be_approved_once(self) -> None:
        code = await self.repository.create_pairing(99, "New teammate")
        self.assertEqual(await self.repository.resolve_pairing(code, True), 99)
        self.assertIsNone(await self.repository.resolve_pairing(code, True))

    async def test_dashboard_reports_persisted_usage(self) -> None:
        await self.repository.record_usage("2026-09", "business", "chat", 100, 20, 1.25, 42, "stub")
        snapshot = await DashboardService(self.repository, UsageBudget(20)).snapshot("2026-09")
        self.assertEqual(snapshot["spent_usd"], 1.25)
        self.assertEqual(snapshot["remaining_usd"], 18.75)

    def test_registry_routes_only_enabled_profiles(self) -> None:
        registry = AssistantRegistry([
            AssistantProfile("sales", "Sales", allowed_users=frozenset({42}), allowed_groups=frozenset({1001})),
        ])
        self.assertIsNotNone(registry.route(42, 1001, "sales"))
        self.assertIsNone(registry.route(99, 1001, "sales"))

    def test_text_extractor_bounds_content(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "brief.txt"
            path.write_text("important " * 100, encoding="utf-8")
            extracted = DocumentExtractor(max_chars=30).extract(str(path))
            self.assertEqual(extracted.content_type, "text")
            self.assertEqual(len(extracted.text), 30)
            self.assertTrue(extracted.truncated)

    async def test_authorized_document_is_answered_and_metered(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "brief.txt"
            path.write_text("Revenue is up 12 percent.", encoding="utf-8")
            service = AssistantService(
                self.repository,
                AccessController(owner_id=42),
                StubProvider("The file says revenue is up 12 percent."),
                UsageBudget(monthly_limit_usd=20),
            )
            answer = await service.answer_file(42, 1001, "private", str(path))
            self.assertIn("revenue", answer)
            self.assertGreater(await self.repository.usage_total(service.budget.period), 0)

    async def test_memory_search_is_scoped_to_the_requesting_user(self) -> None:
        service = AssistantService(
            self.repository,
            AccessController(owner_id=42),
            StubProvider("Saved answer"),
            UsageBudget(monthly_limit_usd=20),
        )
        await service.answer(42, 1001, "private", "The launch is on Friday")
        await service.answer(42, 1001, "private", "What should I prepare?")
        matches = await service.search_memory(42, 1001, "private", "Friday")
        self.assertEqual(len(matches), 1)
        self.assertIn("Friday", matches[0]["content"])

    async def test_reset_and_forget_remove_saved_memory(self) -> None:
        service = AssistantService(
            self.repository,
            AccessController(owner_id=42),
            StubProvider("Saved answer"),
            UsageBudget(monthly_limit_usd=20),
        )
        await service.answer(42, 1001, "private", "Keep this conversation")
        self.assertEqual(await service.reset_conversation(42, 1001, "private"), 1)
        await service.answer(42, 1001, "private", "Delete this too")
        self.assertEqual(await service.forget_memory(42, 1001, "private"), 1)

    async def test_tasks_reminders_and_approval_actions_are_persistent(self) -> None:
        service = AssistantService(self.repository, AccessController(owner_id=42), StubProvider(), UsageBudget())
        task_id = await service.create_task(42, 1001, "private", "Send proposal")
        self.assertEqual(len(await service.list_tasks(42, 1001, "private")), 1)
        self.assertTrue(await service.complete_task(42, 1001, "private", task_id))
        reminder_id = await service.create_reminder(42, 1001, "private", "Call client", "2099-01-01T00:00:00+00:00")
        self.assertTrue(reminder_id)
        action_id = await service.draft_action(42, 1001, "private", "send_message", "99", "Approved text")
        action = await self.repository.resolve_action(action_id, 42, "approved")
        self.assertEqual(action["payload"], "Approved text")

    async def test_project_plan_and_cpm_identify_critical_path(self) -> None:
        project_id = await self.repository.create_project(42, "Client rollout")
        first = await self.repository.create_task(42, "Design", project_id=project_id, duration_days=3)
        second = await self.repository.create_task(42, "Build", project_id=project_id, duration_days=5)
        third = await self.repository.create_task(42, "Training", project_id=project_id, duration_days=2)
        await self.repository.add_dependency(second, first)
        await self.repository.add_dependency(third, first)
        await self.repository.add_dependency(third, second)
        plan_data = await self.repository.project_plan_data(42, project_id)
        predecessors = {first: [], second: [first], third: [second]}
        result = calculate_cpm([{**task, "predecessors": predecessors.get(task["id"], [])} for task in plan_data["tasks"]])
        self.assertEqual(result["duration_days"], 10)
        self.assertEqual(result["critical_task_ids"], [str(first), str(second), str(third)])


if __name__ == "__main__":
    unittest.main()
