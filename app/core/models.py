"""Data models and schemas for Telegram AI Priority Filter."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field


PriorityLevel = Literal["P0", "P1", "P2", "P3"]
MessageType = Literal["task", "update", "question", "decision", "waiting", "blocker", "chatter", "unknown"]


class PriorityClassification(BaseModel):
    """Structured output returned by the AI classifier.

    Priority and message type are deliberately independent dimensions. A
    critical update and a low-priority task are both valid classifications.
    """

    priority: PriorityLevel = Field(
        description="P0 (Urgent/Emergency/Blocker), P1 (Important/Actionable), P2 (Useful update), P3 (Noise/Chatter)"
    )
    score: int = Field(
        ge=0,
        le=100,
        description="Priority score from 0 (pure noise) to 100 (critical emergency/must act immediately)",
    )
    reason: str = Field(
        description="Brief concise justification for why this priority score was assigned."
    )
    needs_action: bool = Field(
        description="True if the user must take an action or reply, False otherwise."
    )
    action: Optional[str] = Field(
        default=None,
        description="The specific action item the user should take (e.g., 'Approve new wireframe', 'Reply to professor', 'Check invoice'). None if no action needed.",
    )
    deadline: Optional[str] = Field(
        default=None,
        description="Extracted deadline or due date/time if mentioned (e.g. 'Friday 5 PM', 'Today EOD'), otherwise null.",
    )
    category: str = Field(
        description="Category classification, e.g., 'university', 'project', 'work', 'finance', 'meeting', 'personal', 'noise'."
    )
    summary: str = Field(
        description="A crisp, 1-sentence summary of the message in the perspective of what matters to the user."
    )
    message_type: MessageType = Field(
        default="unknown",
        description="Independent work/message type: task, update, question, decision, waiting, blocker, chatter, or unknown.",
    )
    ai_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Classifier confidence from 0.0 to 1.0.",
    )


class IncomingMessage(BaseModel):
    """Normalized incoming Telegram message representation."""

    message_id: int
    chat_id: int
    chat_title: str
    chat_type: str  # "private", "group", "supergroup", "channel"
    sender_id: Optional[int] = None
    sender_name: str
    sender_username: Optional[str] = None
    text: str
    date: datetime = Field(default_factory=datetime.utcnow)
    is_reply: bool = False
    reply_to_msg_id: Optional[int] = None
    reply_to_text: Optional[str] = None
    media_type: Optional[str] = None  # "text", "photo", "document", "voice", "sticker", etc.
    message_link: Optional[str] = None


class MessageRecord(BaseModel):
    """Database record representation of a processed message."""

    id: Optional[int] = None
    message_id: int
    chat_id: int
    chat_title: str
    chat_type: str
    sender_id: Optional[int] = None
    sender_name: str
    sender_username: Optional[str] = None
    text: str
    date: str
    media_type: Optional[str] = None
    message_link: Optional[str] = None

    # Classification fields
    priority: PriorityLevel
    score: int
    reason: str
    needs_action: bool
    action: Optional[str] = None
    deadline: Optional[str] = None
    category: str
    summary: str
    message_type: MessageType = "unknown"
    ai_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    # Pipeline tracking
    is_prefiltered: bool = False
    alert_sent: bool = False
    digest_sent: bool = False
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class DigestStats(BaseModel):
    """Statistics for a generated digest."""

    total_messages: int = 0
    p0_count: int = 0
    p1_count: int = 0
    p2_count: int = 0
    p3_count: int = 0
    action_items_count: int = 0


