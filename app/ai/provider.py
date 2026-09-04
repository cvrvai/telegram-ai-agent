"""A narrow provider interface used by chat, documents, and summaries."""

from __future__ import annotations

import asyncio
import httpx
import mimetypes
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence


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


class GeminiProvider:
    """Gemini implementation; no key is printed or persisted by this class."""

    def __init__(self, api_key: Optional[str], model: str = "gemini-3.6-flash"):
        self.model = model
        self._client: Any = None
        if api_key:
            try:
                from google import genai

                self._client = genai.Client(api_key=api_key)
            except Exception:
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    async def answer(self, prompt: str, context: Sequence[str] = ()) -> AIResponse:
        if not self._client:
            raise RuntimeError("Gemini is not configured")
        context_block = "\n".join(context)
        full_prompt = f"{context_block}\n\nUSER REQUEST:\n{prompt}" if context_block else prompt

        def call() -> Any:
            return self._client.models.generate_content(model=self.model, contents=full_prompt)

        response = await asyncio.to_thread(call)
        usage = getattr(response, "usage_metadata", None)
        return AIResponse(
            text=getattr(response, "text", None) or "I could not generate a response.",
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
            model=self.model,
            input_price_per_million=0.30,
            output_price_per_million=2.50,
        )

    async def answer_file(self, prompt: str, file_path: str, context: Sequence[str] = ()) -> AIResponse:
        """Send a bounded local file to Gemini's multimodal endpoint."""
        if not self._client:
            raise RuntimeError("Gemini is not configured")
        path = Path(file_path)
        data = path.read_bytes()
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        context_block = "\n".join(context)

        def call() -> Any:
            from google.genai import types

            parts = [types.Part.from_bytes(data=data, mime_type=mime_type), prompt]
            if context_block:
                parts.insert(0, f"AUTHORIZED CONTEXT:\n{context_block}")
            return self._client.models.generate_content(model=self.model, contents=parts)

        response = await asyncio.to_thread(call)
        usage = getattr(response, "usage_metadata", None)
        return AIResponse(
            text=getattr(response, "text", None) or "I could not analyze that file.",
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
            model=self.model,
            input_price_per_million=0.30,
            output_price_per_million=2.50,
        )


class OpenAICompatibleProvider:
    """Adapter for Ollama, OpenRouter, or another compatible endpoint."""

    def __init__(self, base_url: str, api_key: str, model: str, input_price_per_million: float = 0.0, output_price_per_million: float = 0.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.input_price_per_million = input_price_per_million
        self.output_price_per_million = output_price_per_million

    async def answer(self, prompt: str, context: Sequence[str] = ()) -> AIResponse:
        context_block = "\n".join(context)
        messages = [{"role": "system", "content": "You are a concise business assistant. Use only the supplied context."}]
        if context_block:
            messages.append({"role": "system", "content": f"AUTHORIZED CONTEXT:\n{context_block}"})
        messages.append({"role": "user", "content": prompt})
        async with httpx.AsyncClient(timeout=45.0) as client:
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
