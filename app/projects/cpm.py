"""Critical Path Method (CPM) calculation for project plans.

Durations are expressed in working-day units supplied by the caller. The
algorithm uses finish-to-start dependencies, detects cycles, and returns early
and late dates, float, critical tasks, and the project duration.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Iterable


def calculate_cpm(tasks: Iterable[dict[str, Any]], start_day: int = 0) -> dict[str, Any]:
    rows = {str(task["id"]): dict(task) for task in tasks}
    predecessor_map: dict[str, list[str]] = {
        key: [str(value) for value in row.get("predecessors", []) if str(value) in rows]
        for key, row in rows.items()
    }
    successors: dict[str, list[str]] = defaultdict(list)
    indegree = {key: len(values) for key, values in predecessor_map.items()}
    for task_id, predecessors in predecessor_map.items():
        for predecessor in predecessors:
            successors[predecessor].append(task_id)

    order: list[str] = []
    queue = deque(key for key, degree in indegree.items() if degree == 0)
    while queue:
        current = queue.popleft()
        order.append(current)
        for successor in successors[current]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                queue.append(successor)
    if len(order) != len(rows):
        raise ValueError("Task dependencies contain a cycle.")

    early_start: dict[str, int] = {}
    early_finish: dict[str, int] = {}
    for task_id in order:
        duration = max(0, int(rows[task_id].get("duration_days") or 0))
        early_start[task_id] = max((early_finish[pred] for pred in predecessor_map[task_id]), default=start_day)
        early_finish[task_id] = early_start[task_id] + duration

    project_finish = max(early_finish.values(), default=start_day)
    late_finish: dict[str, int] = {}
    late_start: dict[str, int] = {}
    for task_id in reversed(order):
        duration = max(0, int(rows[task_id].get("duration_days") or 0))
        late_finish[task_id] = min((late_start[successor] for successor in successors[task_id]), default=project_finish)
        late_start[task_id] = late_finish[task_id] - duration

    planned: list[dict[str, Any]] = []
    critical: list[str] = []
    for task_id in order:
        total_float = late_start[task_id] - early_start[task_id]
        if total_float == 0:
            critical.append(task_id)
        planned.append({
            **rows[task_id],
            "early_start": early_start[task_id],
            "early_finish": early_finish[task_id],
            "late_start": late_start[task_id],
            "late_finish": late_finish[task_id],
            "total_float": total_float,
            "critical": total_float == 0,
        })
    return {"duration_days": project_finish - start_day, "project_finish_day": project_finish, "critical_task_ids": critical, "tasks": planned}
