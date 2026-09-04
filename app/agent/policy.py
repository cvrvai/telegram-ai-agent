from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schemas import RiskLevel, ToolDefinition


@dataclass(frozen=True)
class PolicyDecision:
    outcome: str
    reason: str


class PolicyEngine:
    def evaluate(self, tool: ToolDefinition, *, permission_allowed: bool, explicitness: str, resolved: bool = True) -> PolicyDecision:
        if not permission_allowed:
            return PolicyDecision("DENY", "You do not have permission for this action.")
        if tool.risk == RiskLevel.APPROVAL_REQUIRED or tool.approval_required:
            return PolicyDecision("REQUIRE_APPROVAL", "This action requires approval.")
        if tool.risk == RiskLevel.SAFE_WRITE and (explicitness != "command" or not resolved):
            return PolicyDecision("DENY", "I need an explicit, unambiguous command before changing work.")
        return PolicyDecision("ALLOW", "Allowed by workspace policy.")


