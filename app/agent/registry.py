from __future__ import annotations

from typing import Any, Iterable

from pydantic import BaseModel, ValidationError

from .errors import ToolValidationError, UnknownToolError
from .schemas import ToolDefinition, ToolResult


class ToolRegistry:
    """Allow-list for agent calls; models never resolve arbitrary functions."""

    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"Tool already registered: {definition.name}")
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(f"Unknown tool: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    async def execute(self, name: str, arguments: dict[str, Any], context: Any) -> ToolResult:
        definition = self.get(name)
        try:
            validated = definition.input_schema.model_validate(arguments)
        except ValidationError as exc:
            raise ToolValidationError(f"Invalid arguments for {name}: {exc.errors()}") from exc
        result = await definition.handler(validated, context)
        if isinstance(result, ToolResult):
            return result
        if isinstance(result, BaseModel):
            result = result.model_dump(mode="json")
        try:
            result = definition.output_schema.model_validate(result).model_dump(mode="json")
        except ValidationError as exc:
            raise ToolValidationError(f"Invalid output from {name}: {exc.errors()}") from exc
        return ToolResult(data=result)


