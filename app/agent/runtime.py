from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from app.ai.provider import AIProvider

from .context import AgentContext, ContextRetriever
from .errors import AgentError
from .policy import PolicyEngine
from .registry import ToolRegistry
from .schemas import AgentDecision, RiskLevel


class DecisionPlanner(Protocol):
    async def decide(self, user_text: str, context: AgentContext, tool_names: tuple[str, ...], tool_results: list[dict[str, Any]]) -> AgentDecision: ...


class ProviderDecisionPlanner:
    """Adapts the existing provider answer interface to AgentDecision."""

    def __init__(self, provider: AIProvider, answer_callable: Callable[[str, list[str]], Any] | None = None) -> None:
        self.provider = provider
        self.answer_callable = answer_callable

    async def decide(self, user_text: str, context: AgentContext, tool_names: tuple[str, ...], tool_results: list[dict[str, Any]]) -> AgentDecision:
        instructions = (
            "You are a work-management agent. Return ONLY valid JSON matching this contract: "
            '{"kind":"final|tool_call|clarification","message":string|null,"tool_name":string|null,'
            '"arguments":object,"question":string|null,"candidate_options":[],"explicitness":"read|suggest|command|speculative"}. '
            "Use only registered tools. Never invent internal IDs; use public keys or hints. "
            "For greetings and casual conversation, reply naturally and briefly; do not suggest tasks, projects, or workspace actions unless the user asks. "
            "For Telegram source requests, use search_messages with a non-empty hint; search_memory is only for saved assistant conversation memory. "
            "If the requested source is outside the authorized source chat IDs, ask the owner for permission instead of trying another tool. "
            "Use clarification for ambiguity and speculative for maybe/could suggestions. "
            f"Registered tools: {', '.join(tool_names)}.\n"
            f"Context:\n{chr(10).join(context.prompt_lines())}\n"
            f"Previous tool results:\n{json.dumps(tool_results[-4:], default=str)}\n"
            f"User request: {user_text}"
        )
        response = await (self.answer_callable(instructions, context.prompt_lines()) if self.answer_callable else self.provider.answer(instructions, context.prompt_lines()))
        try:
            payload = self._decode_decision(response.text)
            return AgentDecision.model_validate(payload)
        except Exception:
            # Providers sometimes wrap valid JSON in Markdown fences. If the
            # response is not a valid decision, return a clean user message
            # instead of leaking an internal tool payload into Telegram.
            text = response.text.strip()
            if text.startswith("```"):
                text = text.strip("`").removeprefix("json").strip()
            return AgentDecision(kind="final", message=text or "I could not determine a response.", explicitness="read")

    @staticmethod
    def _decode_decision(text: str) -> dict[str, Any]:
        candidate = (text or "").strip()
        if candidate.startswith("```"):
            candidate = candidate[3:]
            if candidate.lstrip().startswith("json"):
                candidate = candidate.lstrip()[4:]
            candidate = candidate.rsplit("```", 1)[0].strip()
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            start, end = candidate.find("{"), candidate.rfind("}")
            if start < 0 or end <= start:
                raise
            value = json.loads(candidate[start:end + 1])
        if not isinstance(value, dict):
            raise ValueError("Agent decision must be a JSON object")
        return value


class AgentRuntime:
    def __init__(self, service: Any, repository: Any, provider: AIProvider, message_database: Any = None, registry: ToolRegistry | None = None, policy: PolicyEngine | None = None, max_rounds: int = 4, timeout_seconds: float = 45.0, allowed_chat_ids_provider: Callable[[], set[int]] | None = None) -> None:
        self.service = service
        self.repository = repository
        self.registry = registry or ToolRegistry()
        self.policy = policy or PolicyEngine()
        self.retriever = ContextRetriever(service, repository, message_database, allowed_chat_ids_provider)
        self.planner = ProviderDecisionPlanner(provider, self._call_provider)
        self.max_rounds = max(1, max_rounds)
        self.timeout_seconds = timeout_seconds
        self.sessions: dict[tuple[int, int], dict[str, Any]] = {}

    async def handle_turn(self, actor_id: int, chat_id: int, chat_type: str, user_text: str) -> str:
        decision = self.service.access.decide(actor_id, chat_id, chat_type)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        session = self.sessions.setdefault((actor_id, chat_id), {})
        context = await self.retriever.retrieve(actor_id, chat_id, chat_type, user_text, session)
        tool_results: list[dict[str, Any]] = []
        seen_calls: set[str] = set()
        try:
            for _ in range(self.max_rounds):
                planned = await asyncio.wait_for(self.planner.decide(user_text, context, self.registry.names(), tool_results), self.timeout_seconds)
                if planned.kind == "final":
                    message = planned.message or "I could not determine a response."
                    await self._remember_turn(actor_id, chat_id, user_text, message)
                    return message
                if planned.kind == "clarification":
                    question = planned.question or "Could you clarify which item you mean?"
                    session["pending_clarification"] = {"question": question, "candidates": planned.candidate_options}
                    await self._remember_turn(actor_id, chat_id, user_text, question)
                    return question
                call_key = json.dumps([planned.tool_name, planned.arguments], sort_keys=True, default=str)
                if call_key in seen_calls:
                    return "I stopped because the same action was requested repeatedly."
                seen_calls.add(call_key)
                tool = self.registry.get(planned.tool_name or "")
                permission_allowed = self.service.access.decide(actor_id, chat_id, chat_type).allowed
                resolved = self._arguments_resolved(planned.arguments, context)
                policy = self.policy.evaluate(tool, permission_allowed=permission_allowed, explicitness=planned.explicitness, resolved=resolved)
                await self._audit(actor_id, tool.name, planned.arguments, policy.outcome, None)
                if policy.outcome == "DENY":
                    return policy.reason
                if policy.outcome == "REQUIRE_APPROVAL":
                    return "This action requires approval before it can be performed."
                result = await self.registry.execute(tool.name, planned.arguments, context)
                payload = result.model_dump(mode="json")
                await self._audit(actor_id, tool.name, planned.arguments, policy.outcome, payload.get("ok"))
                tool_results.append({"tool": tool.name, "result": payload, "policy": policy.outcome})
                if isinstance(payload.get("data"), dict):
                    item = payload["data"].get("work_item")
                    if isinstance(item, dict) and item.get("id"):
                        session["active_work_item_id"] = item["id"]
                    project = payload["data"].get("project")
                    if isinstance(project, dict) and project.get("id"):
                        session["active_project_id"] = project["id"]
            return "I could not complete that request within the safe tool limit."
        except (AgentError, ValueError, KeyError) as exc:
            return f"I could not complete that request safely: {exc}"

    async def _remember_turn(self, actor_id: int, chat_id: int, user_text: str, response: str) -> None:
        conversation_id = await self.repository.create_or_get_conversation("business", actor_id, chat_id)
        await self.repository.add_turn(conversation_id, "user", user_text)
        await self.repository.add_turn(conversation_id, "assistant", response)

    @staticmethod
    def _arguments_resolved(arguments: dict[str, Any], context: AgentContext) -> bool:
        if any(value is None for value in arguments.values()):
            return False
        pronouns = {"it", "that", "this task", "that task", "this issue", "that issue"}
        if any(isinstance(value, str) and value.strip().casefold() in pronouns for value in arguments.values()):
            return context.active_work_item is not None
        return True

    async def _call_provider(self, prompt: str, context: list[str]) -> Any:
        """Call the configured provider through the existing usage budget."""
        budget = getattr(self.service, "budget", None)
        if budget is None:
            return await self.planner.provider.answer(prompt, context)
        estimate_input = max(1, (len(prompt) + sum(len(item) for item in context) + 3) // 4)
        estimate_output = 600
        input_rate = float(getattr(self.service, "input_price_per_million", 0.30))
        output_rate = float(getattr(self.service, "output_price_per_million", 2.50))
        estimate = estimate_input * input_rate / 1_000_000 + estimate_output * output_rate / 1_000_000
        budget.reserve(estimate)
        try:
            response = await self.planner.provider.answer(prompt, context)
        except Exception:
            budget.settle(0.0, estimate)
            raise
        actual_input = response.input_tokens or estimate_input
        actual_output = response.output_tokens or max(1, (len(response.text) + 3) // 4)
        cost = actual_input * response.input_price_per_million / 1_000_000 + actual_output * response.output_price_per_million / 1_000_000
        budget.settle(cost, estimate)
        recorder = getattr(self.repository, "record_usage", None)
        if recorder is not None:
            await recorder(datetime.now(timezone.utc).strftime("%Y-%m"), "business", "agent", actual_input, actual_output, cost, None, response.model)
        return response

    async def _audit(self, actor_id: int, tool_name: str, arguments: dict[str, Any], policy_result: str, execution_result: Any) -> None:
        recorder = getattr(self.repository, "record_agent_audit", None)
        if recorder is None:
            return
        safe_args = {key: value for key, value in arguments.items() if key not in {"token", "password", "secret"}}
        try:
            await recorder(actor_id=actor_id, tool_name=tool_name, arguments_summary=safe_args, policy_result=policy_result, execution_result=execution_result)
        except Exception:
            # Auditing must never make an otherwise safe read or write fail.
            return


