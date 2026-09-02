"""Configuration manager for Telegram AI Priority Bot."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional
import yaml
from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings

# Load .env file automatically
load_dotenv()


class UserProfile(BaseSettings):
    """User profile and prioritization rules."""

    user_name: str = "User"
    high_priority_rules: List[str] = Field(default_factory=lambda: [
        "Deadlines, urgent requests, payment/invoice blockers",
        "Direct mentions or questions requiring user approval/reply",
        "Important project architecture/decision items",
    ])
    medium_priority_rules: List[str] = Field(default_factory=lambda: [
        "Project status updates",
        "Meeting notes and agendas",
        "Useful technical announcements",
    ])
    low_priority_rules: List[str] = Field(default_factory=lambda: [
        "Casual chatting, greetings, memes, banter",
        "Emoji-only messages, stickers",
        "Broadcast spam",
    ])
    vip_senders: List[str] = Field(default_factory=list)
    important_projects: List[str] = Field(default_factory=list)
    ignore_chats: List[str] = Field(default_factory=list)

    @classmethod
    def load_from_yaml(cls, path: str = "user_profile.yaml") -> "UserProfile":
        yaml_path = Path(path)
        if yaml_path.exists():
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                return cls(**data)
        return cls()


class AppConfig(BaseSettings):
    """Application settings."""

    # Telegram Userbot Client
    telegram_api_id: Optional[int] = Field(default=None, alias="TELEGRAM_API_ID")
    telegram_api_hash: Optional[str] = Field(default=None, alias="TELEGRAM_API_HASH")
    telegram_phone: Optional[str] = Field(default=None, alias="TELEGRAM_PHONE")
    telegram_session_name: str = Field(default="telegram_ai_session", alias="TELEGRAM_SESSION_NAME")

    # Telegram Notification Bot
    telegram_bot_token: Optional[str] = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    notification_chat_id: Optional[int] = Field(default=None, alias="NOTIFICATION_CHAT_ID")

    # AI Configuration
    ai_provider: str = Field(default="gemini", alias="AI_PROVIDER")  # "gemini", "openai", "openrouter", or "ollama"
    gemini_api_key: Optional[str] = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-3.5-flash-lite", alias="GEMINI_MODEL")

    @property
    def effective_gemini_model(self) -> str:
        return os.getenv("GEMINI_TEXT_MODEL") or self.gemini_model or "gemini-3.5-flash-lite"

    openai_api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    openai_base_url: Optional[str] = Field(default=None, alias="OPENAI_BASE_URL")

    # Ollama Configuration
    ollama_base_url: str = Field(default="http://localhost:11434/v1", alias="OLLAMA_BASE_URL")
    ollama_model: str = Field(default="llama3.1", alias="OLLAMA_MODEL")
    ollama_api_key: str = Field(default="ollama", alias="OLLAMA_API_KEY")

    # Priority & Alerts
    urgent_score_threshold: int = Field(default=90, alias="URGENT_SCORE_THRESHOLD")
    immediate_alert_priorities: str = Field(default="P0,P1", alias="IMMEDIATE_ALERT_PRIORITIES")

    # Digest Schedules
    digest_interval_hours: int = Field(default=3, alias="DIGEST_INTERVAL_HOURS")
    digest_schedule_times: str = Field(default="08:00,13:00,19:00,22:00", alias="DIGEST_SCHEDULE_TIMES")

    # Database
    database_path: str = Field(default="telegram_bot.db", alias="DATABASE_PATH")

    # User profile rules
    profile: UserProfile = Field(default_factory=UserProfile.load_from_yaml)

    @property
    def alert_priority_list(self) -> List[str]:
        return [p.strip().upper() for p in self.immediate_alert_priorities.split(",") if p.strip()]

    @property
    def schedule_times_list(self) -> List[str]:
        return [t.strip() for t in self.digest_schedule_times.split(",") if t.strip()]

    class Config:
        env_file = ".env"
        extra = "ignore"


# Global settings instance
config = AppConfig()
