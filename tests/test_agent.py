"""Contract tests for the bounded work-management agent runtime."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.agent.policy import PolicyEngine
from app.agent.runtime import AgentRuntime, ProviderDecisionPlanner
from app.agent.schemas import AgentDecision, RiskLevel
from app.agent.tools import build_registry
from app.ai.provider import StubProvider
from app.security.access import AccessController
from app.services.assistant import AssistantService
from app.storage.business import BusinessRepository
from app.usage.budget import UsageBudget


class SequencePlanner:
    def __init__(self, *decisions: AgentDecision):
        self.decisions = list(decisions)

    async def decide(self, user_text, context, tool_names, tool_results):
        return self.decisions.pop(0) if self.decisions else AgentDecision(kind="final", message="done")


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = BusinessRepository(str(Path(self.tmp.name) / "business.db"))
        await self.repo.init()
        self.service = AssistantService(self.repo, AccessController(owner_id=42), StubProvider(), UsageBudget(20))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def test_registry_allow_list_and_policy(self):
        registry = build_registry()
        self.assertIn("get_my_work", registry.names())
        self.assertNotIn("delete_database", registry.names())
        write = registry.get("change_priority")
        self.assertEqual(PolicyEngine().evaluate(write, permission_allowed=True, explicitness="speculative").outcome, "DENY")
        self.assertEqual(PolicyEngine().evaluate(write, permission_allowed=True, explicitness="command").outcome, "ALLOW")

    def test_provider_decision_accepts_fenced_json(self):
        payload = ProviderDecisionPlanner._decode_decision('```json\n{"kind":"tool_call","tool_name":"search_messages","arguments":{"query":"Saved Messages"},"explicitness":"read"}\n```')
        self.assertEqual(payload["tool_name"], "search_messages")

    async def test_read_tool_then_final_is_bounded(self):
        project_id = await self.service.create_project(42, 1, "private", "Red Craw Sales")
        runtime = AgentRuntime(self.service, self.repo, StubProvider(), registry=build_registry(), max_rounds=2)
        runtime.planner = SequencePlanner(
            AgentDecision(kind="tool_call", tool_name="list_projects", arguments={"limit": 10}, explicitness="read"),
            AgentDecision(kind="final", message="I found the project.")
        )
        answer = await runtime.handle_turn(42, 1, "private", "show my projects")
        self.assertEqual(answer, "I found the project.")
        self.assertEqual((await self.repo.list_projects(42))[0]["id"], project_id)

    async def test_ambiguous_or_unresolved_write_does_not_mutate(self):
        await self.service.create_project(42, 1, "private", "Red Craw")
        task_id = await self.service.create_project_task(42, 1, "private", 1, "Package order", priority="P2")
        runtime = AgentRuntime(self.service, self.repo, StubProvider(), registry=build_registry())
        runtime.planner = SequencePlanner(AgentDecision(kind="tool_call", tool_name="change_priority", arguments={"work_item_id": task_id, "priority": "P0"}, explicitness="speculative"))
        answer = await runtime.handle_turn(42, 1, "private", "maybe make it urgent")
        self.assertIn("explicit", answer)
        self.assertEqual((await self.repo.get_work_item(42, task_id))["priority"], "P2")

    async def test_audit_event_is_recorded_for_tool_call(self):
        runtime = AgentRuntime(self.service, self.repo, StubProvider(), registry=build_registry())
        runtime.planner = SequencePlanner(AgentDecision(kind="tool_call", tool_name="get_my_work", arguments={"limit": 5}, explicitness="read"), AgentDecision(kind="final", message="none"))
        await runtime.handle_turn(42, 1, "private", "what is mine")
        async with __import__("aiosqlite").connect(self.repo.db_path) as db:
            row = await (await db.execute("SELECT tool_name FROM agent_audit_events ORDER BY id DESC LIMIT 1")).fetchone()
        self.assertEqual(row[0], "get_my_work")


if __name__ == "__main__":
    unittest.main()


