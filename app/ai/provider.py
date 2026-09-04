"""A narrow provider interface used by chat, documents, and summaries."""

from __future__ import annotations

import httpx
from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class AIResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    input_price_per_million: float = 0.30
    output_price_per_million: float = 2.50


class AIProvider(Protocol):
    async def answer(self, prompt: str, context: Sequence[str] = ()) -> AIResponse: ...


class OpenAICompatibleProvider:
    """Adapter for Ollama's OpenAI-compatible API."""

    def __init__(self, base_url: str, api_key: str, model: str, input_price_per_million: float = 0.0, output_price_per_million: float = 0.0, timeout_seconds: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.input_price_per_million = input_price_per_million
        self.output_price_per_million = output_price_per_million
        self.timeout_seconds = timeout_seconds

    async def answer(self, prompt: str, context: Sequence[str] = ()) -> AIResponse:
        context_block = "\n".join(context)
        messages = [{"role": "system", "content": "You are a concise business assistant. Use only the supplied context."}]
        if context_block:
            messages.append({"role": "system", "content": f"AUTHORIZED CONTEXT:\n{context_block}"})
        messages.append({"role": "user", "content": prompt})
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "messages": messages, "temperature": 0.2},
            )
            response.raise_for_status()
            payload = response.json()
        usage = payload.get("usage") or {}
        text = payload.get("choices", [{}])[0].get("message", {}).get("content") or "I could not generate a response."
        return AIResponse(
            text=text,
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            model=self.model,
            input_price_per_million=self.input_price_per_million,
            output_price_per_million=self.output_price_per_million,
        )


class StubProvider:
    """Deterministic provider for tests and local UI development."""

    def __init__(self, text: str = "Test response", input_tokens: int = 10, output_tokens: int = 5):
        self.response = AIResponse(text, input_tokens, output_tokens, "stub")

    async def answer(self, prompt: str, context: Sequence[str] = ()) -> AIResponse:
        return self.response
