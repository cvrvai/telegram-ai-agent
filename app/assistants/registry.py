"""In-memory registry used by Telegram and the future dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional


@dataclass(frozen=True)
class AssistantProfile:
    assistant_id: str
    name: str
    instructions: str = ""
    allowed_users: frozenset[int] = field(default_factory=frozenset)
    allowed_groups: frozenset[int] = field(default_factory=frozenset)
    enabled: bool = True


class AssistantRegistry:
    def __init__(self, profiles: Optional[Iterable[AssistantProfile]] = None):
        self._profiles: Dict[str, AssistantProfile] = {}
        for profile in profiles or ():
            self.register(profile)

    def register(self, profile: AssistantProfile) -> None:
        if not profile.assistant_id or not profile.name:
            raise ValueError("assistant_id and name are required")
        self._profiles[profile.assistant_id] = profile

    def get(self, assistant_id: str = "business") -> AssistantProfile:
        try:
            return self._profiles[assistant_id]
        except KeyError as exc:
            raise KeyError(f"Unknown assistant: {assistant_id}") from exc

    def list(self) -> list[AssistantProfile]:
        return list(self._profiles.values())

    def route(self, user_id: int, chat_id: int, assistant_id: str = "business") -> Optional[AssistantProfile]:
        profile = self.get(assistant_id)
        if not profile.enabled:
            return None
        if profile.allowed_users and user_id not in profile.allowed_users:
            return None
        if profile.allowed_groups and chat_id not in profile.allowed_groups:
            return None
        return profile

