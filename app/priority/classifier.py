"""AI Priority Classifier using Ollama's OpenAI-compatible API."""

from __future__ import annotations

import json
import logging
from typing import Optional
from config import AppConfig, config
from app.core.models import IncomingMessage, PriorityClassification

logger = logging.getLogger("classifier")


class AIClassifier:
    """Classifies Telegram messages into structured priority scores using LLMs."""

    def __init__(self, cfg: Optional[AppConfig] = None):
        self.cfg = cfg or config
        self._ollama_client = None
        self._init_clients()

    def _init_clients(self) -> None:
        try:
            from openai import OpenAI
            self._ollama_client = OpenAI(
                api_key=self.cfg.ollama_api_key or "ollama",
                base_url=self.cfg.ollama_base_url,
            )
        except Exception as e:
            logger.warning(f"Could not initialize Ollama client: {e}")

    def _build_system_prompt(self) -> str:
        """Constructs the system prompt injected with the user's priority profile."""
        profile = self.cfg.profile

        high_rules = "\n".join(f"- {r}" for r in profile.high_priority_rules)
        med_rules = "\n".join(f"- {r}" for r in profile.medium_priority_rules)
        low_rules = "\n".join(f"- {r}" for r in profile.low_priority_rules)
        vips = ", ".join(profile.vip_senders) if profile.vip_senders else "None"
        business = profile.business
        units = ", ".join(business.units) if business.units else "Not specified"
        projects = ", ".join(profile.important_projects) if profile.important_projects else "None"

        return f"""You are an elite AI Executive Assistant and Notification Gatekeeper for {profile.user_name}.
You support {business.name}: {business.description}
Teams/departments/areas: {units}
Your job is to analyze incoming Telegram messages and classify their priority, score, actionable items, and summary with surgical precision.

=== USER PRIORITY RULES ===
HIGH PRIORITY (P0/P1 - Score 70-100):
{high_rules}

MEDIUM PRIORITY (P2 - Score 40-69):
{med_rules}

LOW PRIORITY (P3 - Score 0-39):
{low_rules}

KEY PROJECTS: {projects}
VIP SENDER LIST: {vips}

=== PRIORITY DEFINITIONS (independent from message type) ===
* P0 (Score 90-100) - CRITICAL:
  - Immediate action/reply required NOW.
  - Critical emergencies, server downtime, financial/supplier blockers, urgent payment confirmation.
  - Direct request from a VIP sender or decision-maker with an immediate deadline.

* P1 (Score 70-89) - HIGH:
  - Important tasks, deadline changes, decisions, reviews, and approvals.
  - Direct questions addressed to the user requiring response within hours.

* P2 (Score 40-69) - NORMAL:
  - Planned work, useful updates, decisions, and routine questions.

* P3 (Score 0-39) - LOW:
  - Low-impact work or chatter. Chatter is not a task unless explicitly converted.

=== MESSAGE / WORK TYPE (independent from priority) ===
Choose exactly one: task, update, question, decision, waiting, blocker, chatter.
An update may be P0 and a task may be P3. Do not infer type from priority.

=== CONTEXT EVALUATION INSTRUCTION ===
Always evaluate context!
- The SAME words mean different things in different chats: a time change in a work/operations
  group is high priority; the same sentence in a social group is noise. Judge by the chat and
  the priority rules above, never by keywords alone.
- Extract clear, actionable instructions in the `action` field if `needs_action` is true.
- If a deadline or time is mentioned, extract it into the `deadline` field.
- Provide a crisp 1-sentence `summary` focusing on what the user needs to know.
- Set `critical_impact` true ONLY when: {business.impact_definition}
"""

    def _build_user_message(self, msg: IncomingMessage) -> str:
        """Formats the incoming message and surrounding context for the LLM."""
        reply_context = f"\n- Replying to previous message: \"{msg.reply_to_text}\"" if msg.reply_to_text else ""
        link_context = f"\n- Link: {msg.message_link}" if msg.message_link else ""

        return f"""Evaluate this Telegram message:
- Chat Title: {msg.chat_title}
- Chat Type: {msg.chat_type}
- Sender: {msg.sender_name} (@{msg.sender_username or 'none'})
- Date: {msg.date.strftime('%Y-%m-%d %H:%M:%S')}
- Media Type: {msg.media_type or 'text'}{reply_context}{link_context}

MESSAGE CONTENT:
\"\"\"{msg.text}\"\"\"
"""

    async def classify(self, msg: IncomingMessage) -> PriorityClassification:
        """Classifies an incoming message using the configured AI provider."""
        if self._ollama_client:
            return await self._classify_ollama(msg)
        return self._fallback_classification(msg, reason="Ollama is unavailable")

    async def _classify_ollama(self, msg: IncomingMessage) -> PriorityClassification:
        """Classify using Ollama / OpenAI-compatible endpoint using direct HTTP requests."""
        import httpx

        system_prompt = (
            self._build_system_prompt()
            + "\nCRITICAL: Return ONLY a valid JSON object matching this schema:\n"
            + "{\n"
            + '  "priority": "P0" | "P1" | "P2" | "P3",\n'
            + '  "score": integer (0 to 100),\n'
            + '  "reason": "string",\n'
            + '  "needs_action": boolean,\n'
            + '  "action": "string" or null,\n'
            + '  "deadline": "string" or null,\n'
            + '  "category": "string",\n'
             + '  "summary": "string",\n'
             + '  "message_type": "task" | "update" | "question" | "decision" | "waiting" | "blocker" | "chatter",\n'
             + '  "critical_impact": boolean,\n'
             + '  "ai_confidence": number (0.0 to 1.0)\n'
            + "}"
        )
        user_prompt = self._build_user_message(msg)

        base_url = (self.cfg.ollama_base_url or "http://localhost:11434/v1").rstrip("/")
        url = f"{base_url}/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.ollama_api_key or 'ollama'}",
        }

        payload = {
            "model": self.cfg.ollama_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }

        try:
            async with httpx.AsyncClient(timeout=self.cfg.ai_request_timeout_seconds) as client:
                res = await client.post(url, json=payload, headers=headers)
                res.raise_for_status()
                data = res.json()
                raw_content = data["choices"][0]["message"]["content"]
                parsed_dict = self._extract_json(raw_content)
                return PriorityClassification(**parsed_dict)
        except Exception as e:
            logger.error(f"Ollama/LLM HTTP call failed: {e}")
            return self._fallback_classification(msg, reason=f"Ollama error: {e}")


    @staticmethod
    def _extract_json(raw: str) -> dict:
        """Safely parses JSON from LLM output, stripping markdown formatting if present."""
        clean = raw.strip()
        if clean.startswith("```json"):
            clean = clean[7:]
        elif clean.startswith("```"):
            clean = clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        clean = clean.strip()
        return json.loads(clean)

    def _fallback_classification(self, msg: IncomingMessage, reason: Optional[str] = None) -> PriorityClassification:
        """Heuristic fallback classification when AI API is unavailable."""
        text = (msg.text or "").lower()
        chat = (msg.chat_title or "").lower()

        is_urgent = any(w in text for w in ["deadline", "urgent", "asap", "emergency", "payment", "due today", "cancel"])
        is_blocker = any(w in text for w in ["blocking", "blocked", "outage", "production down", "service disconnection"])
        is_question = "?" in text or any(w in text for w in ["can you", "could you", "please confirm"])
        is_task = any(w in text for w in ["please", "need to", "fix", "send", "prepare", "review", "approve", "finish"])
        # No offline keyword table can know this business; the only domain
        # signal available without the model is what the profile declared.
        projects = [name.lower() for name in self.cfg.profile.important_projects if name]
        is_key_project = any(name in text or name in chat for name in projects)

        if is_urgent or is_blocker or (is_key_project and "approve" in text):
            return PriorityClassification(
                priority="P0" if is_blocker or "emergency" in text or "production down" in text else "P1",
                score=85,
                reason=reason or "Keyword heuristic: Urgent deadline/approval detected.",
                needs_action=True,
                action="Review message and respond",
                deadline=None,
                category="project" if is_key_project else "general",
                summary=f"[{msg.chat_title}] {msg.text[:100]}",
                message_type="blocker" if is_blocker else ("question" if is_question and not is_task else "task"),
                ai_confidence=0.35,
            )
        elif is_key_project:
            return PriorityClassification(
                priority="P2",
                score=60,
                reason=reason or "Keyword heuristic: key project update.",
                needs_action=False,
                action=None,
                deadline=None,
                category="project",
                summary=f"[{msg.chat_title}] {msg.text[:100]}",
                message_type="update",
                ai_confidence=0.30,
            )
        else:
            return PriorityClassification(
                priority="P3",
                score=30,
                reason=reason or "Default fallback classification.",
                needs_action=False,
                action=None,
                deadline=None,
                category="general",
                summary=f"[{msg.chat_title}] {msg.text[:80]}",
                message_type="task" if is_task and not is_question else ("question" if is_question else "chatter"),
                ai_confidence=0.20,
            )

    async def answer_user_query(self, query: str, context_messages: list) -> str:
        """Answers the user's conversational questions about their messages using LLM."""
        profile = self.cfg.profile
        msgs_summary = []
        for m in context_messages:
            act = f" [Action: {m.action}]" if m.action else ""
            msgs_summary.append(
                f"- [{m.priority}|Score:{m.score}] From '{m.sender_name}' in '{m.chat_title}': \"{m.text}\"{act}"
            )

        context_str = "\n".join(msgs_summary) if msgs_summary else "No recent messages recorded yet."

        system_prompt = f"""You are the personal AI Assistant for {profile.user_name}.
Below is a list of recent messages captured from the user's Telegram chats:

=== RECENT MESSAGES CONTEXT ===
{context_str}

=== INSTRUCTIONS ===
Answer the user's question accurately, concisely, and helpfully based on the context above.
If the information is not in the context, clearly say so.
Format with clean bullet points and emojis if helpful."""

        # Use Ollama's OpenAI-compatible endpoint.
        import httpx
        base_url = (self.cfg.ollama_base_url or "http://localhost:11434/v1").rstrip("/")
        url = f"{base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.ollama_api_key or 'ollama'}",
        }
        payload = {
            "model": self.cfg.ollama_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            "temperature": 0.2,
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(url, json=payload, headers=headers)
                res.raise_for_status()
                data = res.json()
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Error answering user query via LLM: {e}")
            return f"⚠️ Could not query AI model: {e}\n\nHere are recent messages:\n" + "\n".join(msgs_summary[:5])


