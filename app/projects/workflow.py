"""Shared project work-item types, statuses, and transition rules."""

from __future__ import annotations

from typing import FrozenSet


# Message types can become work items; epic/subtask provide the lightweight
# hierarchy needed by projects without introducing another task system.
WORK_ITEM_TYPES = frozenset({"task", "update", "question", "decision", "waiting", "blocker", "chatter", "epic", "subtask"})
PRIORITIES = ("P0", "P1", "P2", "P3")
WORK_ITEM_STATUSES = ("backlog", "todo", "in_progress", "review", "waiting", "blocked", "done")

# A deliberately small workflow for Telegram. The UI and services both use
# this table so handlers cannot silently invent a different transition model.
ALLOWED_TRANSITIONS: dict[str, FrozenSet[str]] = {
    "backlog": frozenset({"todo"}),
    "todo": frozenset({"in_progress", "waiting", "blocked"}),
    "in_progress": frozenset({"review", "waiting", "blocked", "todo"}),
    "review": frozenset({"done", "in_progress"}),
    "waiting": frozenset({"todo", "in_progress", "blocked"}),
    "blocked": frozenset({"todo", "in_progress", "waiting"}),
    "done": frozenset({"todo", "in_progress"}),
}


def normalize_status(status: str | None) -> str:
    value = (status or "todo").strip().lower()
    # Preserve old records while presenting one normalized domain vocabulary.
    if value == "open":
        return "todo"
    if value in {"to_do", "to do"}:
        return "todo"
    if value in {"in-progress", "in progress"}:
        return "in_progress"
    if value in {"complete", "completed"}:
        return "done"
    return value if value in WORK_ITEM_STATUSES else "todo"


def can_transition(current: str | None, target: str | None) -> bool:
    source = normalize_status(current)
    destination = normalize_status(target)
    return source == destination or destination in ALLOWED_TRANSITIONS.get(source, frozenset())


def validate_work_item_type(value: str | None) -> str:
    candidate = (value or "task").strip().lower()
    if candidate not in WORK_ITEM_TYPES:
        raise ValueError(f"Unsupported work item type: {candidate}")
    return candidate


def validate_priority(value: str | None) -> str:
    candidate = (value or "P2").strip().upper()
    if candidate in {"NORMAL", "MEDIUM"}:
        candidate = "P2"
    elif candidate in {"HIGH", "IMPORTANT"}:
        candidate = "P1"
    elif candidate in {"URGENT", "CRITICAL"}:
        candidate = "P0"
    elif candidate in {"LOW"}:
        candidate = "P3"
    if candidate not in PRIORITIES:
        raise ValueError(f"Unsupported priority: {candidate}")
    return candidate
