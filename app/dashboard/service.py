"""Read-only dashboard snapshot assembled from authoritative application state."""

from __future__ import annotations

from typing import Any, Dict

from app.storage.business import BusinessRepository
from app.usage.budget import UsageBudget


class DashboardService:
    def __init__(self, repository: BusinessRepository, budget: UsageBudget, schedules: list[str] | None = None, default_user_id: int | None = None):
        self.repository = repository
        self.budget = budget
        self.schedules = schedules or []
        self.default_user_id = default_user_id

    async def snapshot(self, period: str) -> Dict[str, Any]:
        """Return safe display data; caller must authenticate before exposing it."""
        spent = await self.repository.usage_total(period)
        return {
            "period": period,
            "budget_usd": self.budget.monthly_limit_usd,
            "spent_usd": spent,
            "remaining_usd": max(0.0, self.budget.monthly_limit_usd - spent),
            "budget_paused": spent >= self.budget.monthly_limit_usd,
        }

    async def users(self) -> list[dict[str, Any]]:
        return await self.repository.list_users()

    async def assistants(self) -> list[dict[str, Any]]:
        return await self.repository.list_profiles()

    async def groups(self) -> list[dict[str, Any]]:
        return await self.repository.list_groups()

    async def actions(self) -> list[dict[str, Any]]:
        return await self.repository.list_actions()

    def schedule_list(self) -> list[str]:
        return list(self.schedules)

    async def tasks(self, user_id: int | None = None) -> list[dict[str, Any]]:
        if user_id is None:
            user_id = 0
        return await self.repository.list_tasks(user_id) if user_id else []

    async def projects(self, user_id: int) -> list[dict[str, Any]]:
        return await self.repository.list_projects(user_id)

    async def create_project(self, user_id: int, data: dict[str, Any]) -> Any:
        return await self.repository.create_project(user_id, str(data["name"]), str(data.get("description", "")), data.get("department_id"), data.get("start_date"), data.get("target_date"))

    async def create_task(self, user_id: int, data: dict[str, Any]) -> Any:
        return await self.repository.create_task(user_id, str(data["title"]), str(data.get("details", "")), data.get("due_at"), data.get("project_id"), data.get("assignee_id"), str(data.get("priority", "normal")), int(data.get("duration_days", 1)))

    async def update_task(self, user_id: int, task_id: Any, data: dict[str, Any]) -> bool:
        return await self.repository.update_task(user_id, task_id, status=data.get("status"), assignee_id=data.get("assignee_id"), priority=data.get("priority"), due_at=data.get("due_at"))

    async def add_dependency(self, task_id: Any, predecessor_id: Any) -> None:
        await self.repository.add_dependency(task_id, predecessor_id)

    async def project_plan(self, user_id: int, project_id: int | str) -> dict[str, Any]:
        from app.projects.cpm import calculate_cpm
        data = await self.repository.project_plan_data(user_id, project_id)
        if not data:
            return {}
        predecessors: dict[str, list[str]] = {str(task["id"]): [] for task in data["tasks"]}
        for dep in data["dependencies"]:
            predecessors.setdefault(str(dep["task_id"]), []).append(str(dep["predecessor_id"]))
        result = calculate_cpm([{**task, "predecessors": predecessors.get(str(task["id"]), [])} for task in data["tasks"]])
        result["project"] = data["project"]
        result["milestones"] = data["milestones"]
        completed = sum(1 for task in data["tasks"] if task.get("status") == "completed")
        result["completion_percent"] = round(completed / len(data["tasks"]) * 100, 1) if data["tasks"] else 0.0
        return result

    async def departments(self) -> list[dict[str, Any]]:
        return await self.repository.list_departments()

    async def approve_user(self, user_id: int, approved: bool) -> None:
        await self.repository.set_user_approval(user_id, approved)

    async def save_assistant(self, assistant_id: str, name: str, instructions: str = "", enabled: bool = True) -> None:
        await self.repository.upsert_profile(assistant_id, name, instructions, enabled)
