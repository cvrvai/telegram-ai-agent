"""Small, transport-independent access policy for business conversations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Set


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str


@dataclass
class AccessController:
    """Enforce owner/user/group access before any data or AI operation."""

    owner_id: Optional[int] = None
    approved_users: Set[int] = field(default_factory=set)
    approved_groups: Set[int] = field(default_factory=set)
    disabled: bool = False

    def decide(self, user_id: Optional[int], chat_id: Optional[int], chat_type: str) -> AccessDecision:
        if self.disabled:
            return AccessDecision(False, "assistant_disabled")
        if user_id is None:
            return AccessDecision(False, "unknown_sender")
        if self.owner_id is not None and user_id == self.owner_id:
            return AccessDecision(True, "owner")
        if user_id not in self.approved_users:
            return AccessDecision(False, "user_not_approved")
        if chat_type in {"group", "supergroup", "channel"} and chat_id not in self.approved_groups:
            return AccessDecision(False, "group_not_approved")
        return AccessDecision(True, "approved")

    def can_use(self, user_id: Optional[int], chat_id: Optional[int], chat_type: str) -> bool:
        return self.decide(user_id, chat_id, chat_type).allowed

    def approve_user(self, user_id: int) -> None:
        self.approved_users.add(user_id)

    def revoke_user(self, user_id: int) -> None:
        self.approved_users.discard(user_id)

    def enable_group(self, chat_id: int) -> None:
        self.approved_groups.add(chat_id)

    def disable_group(self, chat_id: int) -> None:
        self.approved_groups.discard(chat_id)

