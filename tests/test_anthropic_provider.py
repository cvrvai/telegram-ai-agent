"""The Anthropic path has to produce byte-identical decisions to the Ollama one:
the runtime parses AgentDecision JSON and does not know which provider ran.
These lock the translation in both directions without calling the API."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.ai.anthropic_provider import (
    MODEL_PRICING,
    AnthropicProvider,
    _tools_for_anthropic,
)


def block(**fields):
    return SimpleNamespace(**fields)


def message(content, *, stop_reason="end_turn", stop_details=None, input_tokens=100, output_tokens=20):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class ToolTranslationTests(unittest.TestCase):
    def test_openai_shaped_spec_becomes_an_anthropic_tool(self) -> None:
        spec = [{"type": "function", "function": {
            "name": "list_situations",
            "description": "List open situations",
            "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        }}]
        self.assertEqual(_tools_for_anthropic(spec), [{
            "name": "list_situations",
            "description": "List open situations",
            "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        }])

    def test_missing_parameters_still_yields_a_valid_schema(self) -> None:
        # Anthropic rejects a tool with no input_schema, so never emit one.
        out = _tools_for_anthropic([{"type": "function", "function": {"name": "ping"}}])
        self.assertEqual(out[0]["input_schema"], {"type": "object", "properties": {}})
        self.assertEqual(out[0]["description"], "")

    def test_no_tools_is_an_empty_list_not_none(self) -> None:
        self.assertEqual(_tools_for_anthropic(None), [])


class PricingTests(unittest.TestCase):
    def test_known_model_uses_list_pricing(self) -> None:
        p = AnthropicProvider("k", "claude-opus-5")
        self.assertEqual((p.input_price_per_million, p.output_price_per_million), MODEL_PRICING["claude-opus-5"])

    def test_unknown_model_without_explicit_pricing_is_refused(self) -> None:
        # Billing an unpriced model at zero would silently disable the budget.
        with self.assertRaises(ValueError):
            AnthropicProvider("k", "claude-experimental-9")

    def test_unknown_model_accepts_operator_supplied_pricing(self) -> None:
        p = AnthropicProvider("k", "claude-experimental-9", input_price_per_million=1.5, output_price_per_million=7.5)
        self.assertEqual((p.input_price_per_million, p.output_price_per_million), (1.5, 7.5))


class DecodeTests(unittest.TestCase):
    def test_tool_use_becomes_the_runtime_tool_call_contract(self) -> None:
        decoded = AnthropicProvider._decode(
            message([block(type="tool_use", id="t1", name="get_management_brief", input={"period": "today"})]),
            expect_tools=True,
        )
        self.assertEqual(json.loads(decoded), {
            "kind": "tool_call",
            "tool_name": "get_management_brief",
            "arguments": {"period": "today"},
            "explicitness": "read",
        })

    def test_thinking_blocks_are_not_shown_to_the_user(self) -> None:
        decoded = AnthropicProvider._decode(
            message([block(type="thinking", thinking="deliberating"), block(type="text", text="All clear.")]),
            expect_tools=False,
        )
        self.assertEqual(decoded, "All clear.")

    def test_text_without_a_tool_call_becomes_a_final_decision(self) -> None:
        decoded = AnthropicProvider._decode(message([block(type="text", text="Nothing urgent.")]), expect_tools=True)
        self.assertEqual(json.loads(decoded), {"kind": "final", "message": "Nothing urgent."})

    def test_refusal_is_reported_not_treated_as_an_empty_answer(self) -> None:
        decoded = AnthropicProvider._decode(
            message([], stop_reason="refusal", stop_details=SimpleNamespace(category="cyber", explanation="")),
            expect_tools=True,
        )
        payload = json.loads(decoded)
        self.assertEqual(payload["kind"], "final")
        self.assertIn("cyber", payload["message"])

    def test_empty_content_never_returns_an_empty_string(self) -> None:
        self.assertTrue(AnthropicProvider._decode(message([]), expect_tools=False))


class RequestShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_decide_sends_system_top_level_and_pairs_tool_results(self) -> None:
        provider = AnthropicProvider("k", "claude-opus-5")
        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return message([block(type="text", text="done")])

        with patch.object(provider._client.messages, "create", AsyncMock(side_effect=fake_create)):
            await provider.decide(
                "what is unresolved?",
                ["some context"],
                [{"type": "function", "function": {"name": "list_situations", "parameters": {"type": "object"}}}],
                [{"tool": "list_situations", "arguments": {"limit": 5}, "result": {"ok": True, "data": []}}],
                history=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
            )

        # System prompt is an argument, never a message -- Anthropic has no system role here.
        self.assertIsInstance(captured["system"], str)
        self.assertIn("OUTPUT STYLE", captured["system"])
        self.assertNotIn("system", {m["role"] for m in captured["messages"]})

        # A tool_use block must be answered by a tool_result with the same id.
        uses = [b for m in captured["messages"] if isinstance(m["content"], list)
                for b in m["content"] if b.get("type") == "tool_use"]
        results = [b for m in captured["messages"] if isinstance(m["content"], list)
                   for b in m["content"] if b.get("type") == "tool_result"]
        self.assertEqual(len(uses), 1)
        self.assertEqual(uses[0]["id"], results[0]["tool_use_id"])
        self.assertEqual(uses[0]["name"], "list_situations")

        self.assertEqual(captured["tool_choice"], {"type": "auto", "disable_parallel_tool_use": True})
        self.assertEqual(captured["tools"][0]["name"], "list_situations")

    async def test_answer_omits_tools_entirely(self) -> None:
        provider = AnthropicProvider("k", "claude-opus-5")
        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return message([block(type="text", text="hello")])

        with patch.object(provider._client.messages, "create", AsyncMock(side_effect=fake_create)):
            result = await provider.answer("hi", ["ctx"])

        self.assertNotIn("tools", captured)
        self.assertNotIn("tool_choice", captured)
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.input_tokens, 100)
        self.assertEqual(result.output_tokens, 20)
        self.assertEqual(result.input_price_per_million, MODEL_PRICING["claude-opus-5"][0])

    async def test_effort_is_sent_only_when_configured(self) -> None:
        captured = {}

        async def fake_create(**kwargs):
            captured.clear()
            captured.update(kwargs)
            return message([block(type="text", text="ok")])

        plain = AnthropicProvider("k", "claude-opus-5")
        with patch.object(plain._client.messages, "create", AsyncMock(side_effect=fake_create)):
            await plain.answer("hi")
        self.assertNotIn("output_config", captured)

        tuned = AnthropicProvider("k", "claude-opus-5", effort="low")
        with patch.object(tuned._client.messages, "create", AsyncMock(side_effect=fake_create)):
            await tuned.answer("hi")
        self.assertEqual(captured["output_config"], {"effort": "low"})


class SharedPromptTests(unittest.TestCase):
    def test_both_providers_use_the_same_agent_instructions(self) -> None:
        # If these ever diverge the assistant behaves differently depending on
        # which model is configured, which is exactly what must not happen.
        from app.ai.provider import agent_system_prompt
        for fragment in ("OUTPUT STYLE", "Never show a numeric chat id", "current date and time is"):
            self.assertIn(fragment, agent_system_prompt())


if __name__ == "__main__":
    unittest.main()
