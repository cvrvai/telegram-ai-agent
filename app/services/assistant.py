"""Business assistant use case: authorize, budget, answer, and persist."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from app.ai.provider import AIProvider
from app.content import DocumentExtractor
from app.security.access import AccessController
from app.storage.business import BusinessRepository
from app.usage.budget import UsageBudget
from app.projects.workflow import validate_priority, validate_work_item_type, normalize_status


class AssistantService:
    def __init__(self, repository: BusinessRepository, access: AccessController, provider: AIProvider, budget: UsageBudget, assistant_id: str = "business", input_price_per_million: float = 0.30, output_price_per_million: float = 2.50, extractor: DocumentExtractor | None = None):
        self.repository = repository
        self.access = access
        self.provider = provider
        self.budget = budget
        self.assistant_id = assistant_id
        self.input_price_per_million = input_price_per_million
        self.output_price_per_million = output_price_per_million
        self.extractor = extractor or DocumentExtractor()

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, (len(text) + 3) // 4)

    async def answer(self, user_id: int, chat_id: int, chat_type: str, query: str, context: Sequence[str] = ()) -> str:
        decision = self.access.decide(user_id, chat_id, chat_type)
        if not decision.allowed:
            raise PermissionError(decision.reason)

        await self.repository.mark_user_seen(user_id)
        conversation_id = await self.repository.create_or_get_conversation(self.assistant_id, user_id, chat_id)
        history = await self.repository.recent_turns(conversation_id, limit=12)
        history_context = [f"{turn['role']}: {turn['content']}" for turn in history]
        prompt_context = list(context) + history_context

        estimated_input = self._estimate_tokens(query + "\n".join(prompt_context))
        estimated_output = 600
        # Rates are configured estimates; reconcile them with provider billing.
        estimate = estimated_input * self.input_price_per_million / 1_000_000 + estimated_output * self.output_price_per_million / 1_000_000
        self.budget.reserve(estimate)
        try:
            response = await self.provider.answer(query, prompt_context)
        except Exception:
            self.budget.settle(0.0, estimate)
            raise

        actual_input = response.input_tokens or estimated_input
        actual_output = response.output_tokens or self._estimate_tokens(response.text)
        cost = (
            actual_input * response.input_price_per_million / 1_000_000
            + actual_output * response.output_price_per_million / 1_000_000
        )
        self.budget.settle(cost, estimate)
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        await self.repository.add_turn(conversation_id, "user", query)
        await self.repository.add_turn(conversation_id, "assistant", response.text, response.model, actual_input, actual_output)
        await self.repository.record_usage(period, self.assistant_id, "chat", actual_input, actual_output, cost, user_id, response.model)
        return response.text

    async def answer_file(self, user_id: int, chat_id: int, chat_type: str, file_path: str, question: str = "Summarize this file and list the important business points.") -> str:
        """Authorize and analyze a supported document/image, then persist it as a turn."""
        decision = self.access.decide(user_id, chat_id, chat_type)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        extracted = self.extractor.extract(file_path)
        conversation_id = await self.repository.create_or_get_conversation(self.assistant_id, user_id, chat_id)
        prompt = f"{question}\nFile type: {extracted.content_type}\nFile name: {Path(file_path).name}"
        file_size_estimate = min(Path(file_path).stat().st_size // 2, 5_000_000)
        estimated_input = max(
            self._estimate_tokens(extracted.text or Path(file_path).read_bytes()[:200_000].decode("utf-8", errors="ignore")),
            file_size_estimate,
        )
        estimated_output = 800
        reserved = estimated_input * self.input_price_per_million / 1_000_000 + estimated_output * self.output_price_per_million / 1_000_000
        self.budget.reserve(reserved)
        provider_file = getattr(self.provider, "answer_file", None)
        if extracted.content_type == "image" and not provider_file:
            self.budget.settle(0.0, reserved)
            raise RuntimeError("The configured AI provider does not support image analysis")
        try:
            if provider_file and extracted.content_type in {"image", "pdf"}:
                response = await provider_file(prompt, file_path, ())
            else:
                response = await self.provider.answer(prompt, (f"FILE CONTENT:\n{extracted.text}",))
        except Exception:
            self.budget.settle(0.0, reserved)
            raise
        actual_input = response.input_tokens or self._estimate_tokens(extracted.text + question)
        actual_output = response.output_tokens or self._estimate_tokens(response.text)
        cost = actual_input * response.input_price_per_million / 1_000_000 + actual_output * response.output_price_per_million / 1_000_000
        self.budget.settle(cost, reserved)
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        await self.repository.add_turn(conversation_id, "user", prompt)
        await self.repository.add_turn(conversation_id, "assistant", response.text, response.model, actual_input, actual_output)
        await self.repository.record_usage(period, self.assistant_id, "document", actual_input, actual_output, cost, user_id, response.model)
        return response.text

    async def search_memory(self, user_id: int, chat_id: int, chat_type: str, query: str, limit: int = 10):
        """Search only the caller's saved assistant conversation memory."""
        decision = self.access.decide(user_id, chat_id, chat_type)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        return await self.repository.search_turns(user_id, self.assistant_id, query, limit)

    async def reset_conversation(self, user_id: int, chat_id: int, chat_type: str) -> int:
        """Delete the current conversation after access validation."""
        decision = self.access.decide(user_id, chat_id, chat_type)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        return await self.repository.clear_conversation(user_id, chat_id, self.assistant_id)

    async def forget_memory(self, user_id: int, chat_id: int, chat_type: str) -> int:
        """Delete all assistant memory owned by the requesting user."""
        decision = self.access.decide(user_id, chat_id, chat_type)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        return await self.repository.clear_user_memory(user_id, self.assistant_id)

    async def enforce_retention(self, days: int) -> int:
        """Apply a retention window to saved assistant turns."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        return await self.repository.purge_turns_older_than(cutoff.isoformat())

    async def create_task(self, user_id: int, chat_id: int, chat_type: str, title: str, details: str = "", due_at: str | None = None) -> Any:
        self._check(user_id, chat_id, chat_type)
        return await self.repository.create_task(user_id, title, details, due_at)

    async def create_project(self, user_id: int, chat_id: int, chat_type: str, name: str, description: str = "", department_id: int | None = None, start_date: str | None = None, target_date: str | None = None) -> Any:
        self._check(user_id, chat_id, chat_type)
        return await self.repository.create_project(user_id, name, description, department_id, start_date, target_date)

    async def create_department(self, user_id: int, chat_id: int, chat_type: str, name: str, description: str = "") -> Any:
        self._check(user_id, chat_id, chat_type)
        return await self.repository.create_department(name, description)

    async def list_departments(self, user_id: int, chat_id: int, chat_type: str):
        self._check(user_id, chat_id, chat_type)
        return await self.repository.list_departments()

    async def list_projects(self, user_id: int, chat_id: int, chat_type: str):
        self._check(user_id, chat_id, chat_type)
        return await self.repository.list_projects(user_id)

    async def get_project(self, user_id: int, chat_id: int, chat_type: str, project_id: Any):
        self._check(user_id, chat_id, chat_type)
        return await self.repository.get_project(user_id, project_id)

    async def find_projects(self, user_id: int, chat_id: int, chat_type: str, hint: str):
        self._check(user_id, chat_id, chat_type)
        return await self.repository.find_projects(user_id, hint)

    async def create_project_task(self, user_id: int, chat_id: int, chat_type: str, project_id: Any, title: str, duration_days: int = 1, assignee_id: int | None = None, priority: str = "P2", due_at: str | None = None, item_type: str = "task", parent_id: Any = None):
        self._check(user_id, chat_id, chat_type)
        return await self.repository.create_task(user_id, title, project_id=project_id, assignee_id=assignee_id, priority=validate_priority(priority), duration_days=duration_days, due_at=due_at, item_type=validate_work_item_type(item_type), parent_id=parent_id, reporter_id=user_id)

    async def update_task(self, user_id: int, chat_id: int, chat_type: str, task_id: Any, **changes: Any) -> bool:
        self._check(user_id, chat_id, chat_type)
        return await self.repository.update_task(user_id, task_id, **changes)

    async def get_work_item(self, user_id: int, chat_id: int, chat_type: str, item_id: Any):
        self._check(user_id, chat_id, chat_type)
        return await self.repository.get_work_item(user_id, item_id)

    async def list_work_items(self, user_id: int, chat_id: int, chat_type: str, status: str = "all", limit: int = 50, offset: int = 0, project_id: Any = None, due_before: str | None = None):
        self._check(user_id, chat_id, chat_type)
        return await self.repository.list_work_items(user_id, status, limit, offset, project_id=project_id, due_before=due_before)

    async def add_dependency(self, user_id: int, chat_id: int, chat_type: str, task_id: Any, predecessor_id: Any) -> None:
        self._check(user_id, chat_id, chat_type)
        await self.repository.add_dependency(task_id, predecessor_id)

    async def add_milestone(self, user_id: int, chat_id: int, chat_type: str, project_id: int, name: str, target_date: str) -> Any:
        self._check(user_id, chat_id, chat_type)
        return await self.repository.add_milestone(project_id, name, target_date)

    async def add_comment(self, user_id: int, chat_id: int, chat_type: str, task_id: int, comment: str) -> Any:
        self._check(user_id, chat_id, chat_type)
        return await self.repository.add_task_comment(task_id, user_id, comment)

    async def project_plan(self, user_id: int, chat_id: int, chat_type: str, project_id: int) -> dict[str, Any]:
        self._check(user_id, chat_id, chat_type)
        from app.projects.cpm import calculate_cpm
        data = await self.repository.project_plan_data(user_id, project_id)
        if not data:
            return {}
        predecessor_map: dict[str, list[str]] = {str(task["id"]): [] for task in data["tasks"]}
        for dep in data["dependencies"]:
            predecessor_map.setdefault(str(dep["task_id"]), []).append(str(dep["predecessor_id"]))
        plan_tasks = [{**task, "predecessors": predecessor_map.get(str(task["id"]), [])} for task in data["tasks"]]
        result = calculate_cpm(plan_tasks)
        completed = sum(1 for task in data["tasks"] if normalize_status(task.get("status")) == "done")
        result["project"] = data["project"]
        result["milestones"] = data["milestones"]
        result["completion_percent"] = round((completed / len(data["tasks"])) * 100, 1) if data["tasks"] else 0.0
        return result

    async def list_tasks(self, user_id: int, chat_id: int, chat_type: str, status: str = "open", limit: int = 20):
        self._check(user_id, chat_id, chat_type)
        return await self.repository.list_tasks(user_id, status, limit)

    async def complete_task(self, user_id: int, chat_id: int, chat_type: str, task_id: Any) -> bool:
        self._check(user_id, chat_id, chat_type)
        return await self.repository.complete_task(user_id, task_id)

    async def create_reminder(self, user_id: int, chat_id: int, chat_type: str, text: str, remind_at: str) -> Any:
        self._check(user_id, chat_id, chat_type)
        return await self.repository.create_reminder(user_id, text, remind_at)

    async def list_reminders(self, user_id: int, chat_id: int, chat_type: str, limit: int = 20):
        self._check(user_id, chat_id, chat_type)
        return await self.repository.list_reminders(user_id, "pending", limit)

    async def draft_action(self, user_id: int, chat_id: int, chat_type: str, action_type: str, target: str, payload: str, ttl_minutes: int = 15) -> Any:
        self._check(user_id, chat_id, chat_type)
        expires = datetime.now(timezone.utc) + timedelta(minutes=max(1, ttl_minutes))
        return await self.repository.create_action(user_id, action_type, target, payload, expires.isoformat())

    def _check(self, user_id: int, chat_id: int, chat_type: str) -> None:
        decision = self.access.decide(user_id, chat_id, chat_type)
        if not decision.allowed:
            raise PermissionError(decision.reason)
