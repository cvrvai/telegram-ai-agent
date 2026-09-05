"""Fuses related messages into a persistent, evolving Situation instead of
scoring each message in isolation -- the case study's own "most important
capability" (five messages about one AC problem become one tracked issue
with a status, not five unrelated alerts).

Only messages already classified P0-P2 with a situation-worthy message_type
reach this module; chatter and P3 noise never create or touch a situation.
Linking costs one extra LLM call, and only when there is something to
disambiguate against: a chat with no open situation gets a free "new"
decision with no model call at all.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from config import AppConfig, config
from app.core.models import IncomingMessage, PriorityClassification, Situation, SituationDecision
from app.priority.classifier import AIClassifier

logger = logging.getLogger("situations")

SITUATION_WORTHY_TYPES = {"task", "blocker", "waiting", "decision", "update"}
# An "open" situation this stale is treated as unrelated history, not a live
# match -- a new message about the same room three weeks later is a new issue.
REOPEN_WINDOW_HOURS = 72
_GUEST_WORDS = ("guest", "guests", "customer", "customers", "visitor", "visitors")
_STOPWORDS = {
    "this", "that", "with", "from", "have", "need", "will", "please", "about",
    "there", "when", "what", "should", "could", "would", "still", "already",
    "again", "today", "tomorrow", "message", "thanks",
}


def _mentions_guest(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in _GUEST_WORDS)


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9]{4,}", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS}


class SituationLinker:
    """Decides whether a classified message continues, resolves, or starts a Situation."""

    def __init__(self, cfg: Optional[AppConfig] = None):
        self.cfg = cfg or config

    @staticmethod
    def eligible(cls: PriorityClassification) -> bool:
        return cls.priority in {"P0", "P1", "P2"} and cls.message_type in SITUATION_WORTHY_TYPES

    async def link(self, msg: IncomingMessage, cls: PriorityClassification, open_situations: list[Situation]) -> SituationDecision:
        if not open_situations:
            return self._seed_new(msg, cls)
        try:
            decision = await self._link_ollama(msg, cls, open_situations)
        except Exception as exc:
            logger.warning("Situation linking via LLM failed, using heuristic fallback: %s", exc)
            return self._fallback_link(msg, cls, open_situations)
        valid_ids = {situation.id for situation in open_situations}
        if decision.action in {"continue", "resolve"} and decision.situation_id not in valid_ids:
            # Never let a hallucinated ID silently update the wrong record.
            logger.warning("Situation linker returned an unknown situation_id %r; treating as new.", decision.situation_id)
            return self._seed_new(msg, cls)
        return decision

    def _seed_new(self, msg: IncomingMessage, cls: PriorityClassification) -> SituationDecision:
        return SituationDecision(
            action="new",
            title=cls.summary[:120] or msg.text[:120],
            status="open",
            current_action=cls.action,
            dependency=None,
            guest_affected=_mentions_guest(msg.text),
            responsible=cls.category or msg.chat_title,
            reason="No open situation exists yet for this chat.",
        )

    async def _link_ollama(self, msg: IncomingMessage, cls: PriorityClassification, open_situations: list[Situation]) -> SituationDecision:
        options = "\n".join(
            f"- id={situation.id} | \"{situation.title}\" | status={situation.status} | latest: {situation.summary}"
            for situation in open_situations
        )
        system_prompt = (
            "You track ongoing operational situations for a hotel management assistant. "
            "Decide whether a new message continues one of the listed OPEN situations, "
            "resolves one, or is unrelated and starts a new situation. Only match a message "
            "to a situation if it is genuinely the same underlying issue -- do not merge "
            "unrelated topics just because they are in the same chat.\n"
            "Return ONLY a JSON object matching this schema:\n"
            '{"action":"new"|"continue"|"resolve","situation_id":integer or null,'
            '"title":"string or null","status":"open"|"monitoring"|"resolved",'
            '"current_action":"string or null","dependency":"string or null",'
            '"guest_affected":boolean,"responsible":"string or null","reason":"string"}'
        )
        user_prompt = (
            f"OPEN SITUATIONS in this chat:\n{options}\n\n"
            f"NEW MESSAGE (priority {cls.priority}, type {cls.message_type}):\n"
            f"Summary: {cls.summary}\nAction: {cls.action or 'none'}\nText: {msg.text[:500]}"
        )
        base_url = (self.cfg.ollama_base_url or "http://localhost:11434/v1").rstrip("/")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.cfg.ollama_api_key or 'ollama'}"}
        payload = {
            "model": self.cfg.ollama_model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }
        async with httpx.AsyncClient(timeout=self.cfg.ai_request_timeout_seconds) as client:
            response = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
            return SituationDecision(**AIClassifier._extract_json(raw))

    def _fallback_link(self, msg: IncomingMessage, cls: PriorityClassification, open_situations: list[Situation]) -> SituationDecision:
        now = datetime.now(timezone.utc)
        needle = _keywords(msg.text) | _keywords(cls.summary)
        best: Optional[Situation] = None
        best_overlap = 0
        for situation in open_situations:
            try:
                updated = datetime.fromisoformat(situation.last_update_at)
            except ValueError:
                continue
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if (now - updated).total_seconds() > REOPEN_WINDOW_HOURS * 3600:
                continue
            overlap = len((needle & _keywords(situation.title)) | (needle & _keywords(situation.summary)))
            if overlap > best_overlap:
                best, best_overlap = situation, overlap
        if best is not None and best_overlap >= 2:
            return SituationDecision(
                action="continue",
                situation_id=best.id,
                status=best.status,
                current_action=cls.action or best.current_action,
                dependency=best.dependency,
                guest_affected=best.guest_affected or _mentions_guest(msg.text),
                responsible=best.responsible,
                reason=f"Heuristic keyword overlap ({best_overlap}) with an open situation.",
            )
        seeded = self._seed_new(msg, cls)
        seeded.reason = "Heuristic fallback: no sufficiently similar open situation found."
        return seeded
