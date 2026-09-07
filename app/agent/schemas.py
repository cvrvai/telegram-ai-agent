from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class RiskLevel(str, Enum):
    READ = "read"
    SUGGEST = "suggest"
    SAFE_WRITE = "safe_write"
    APPROVAL_REQUIRED = "approval_required"


class AgentDecision(BaseModel):
    """Provider-neutral, validated model output. Hidden reasoning is excluded."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["final", "tool_call", "clarification"]
    message: Optional[str] = None
    tool_name: Optional[str] = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    question: Optional[str] = None
    candidate_options: list[str] = Field(default_factory=list)
    explicitness: Literal["read", "suggest", "command", "speculative"] = "read"


class ToolDefinition(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    risk: RiskLevel
    required_permission: str
    side_effect: bool
    approval_required: bool
    handler: Callable[..., Any]


class ToolResult(BaseModel):
    ok: bool = True
    data: Any = None
    error: Optional[str] = None
    rationale: Optional[str] = None


