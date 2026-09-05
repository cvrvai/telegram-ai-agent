"""Configuration manager for Telegram AI Priority Bot."""

from __future__ import annotations

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
    telegram_bot_session_name: str = Field(default="bot_listener_session", alias="TELEGRAM_BOT_SESSION_NAME")

    # Telegram Notification Bot
    telegram_bot_token: Optional[str] = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    notification_chat_id: Optional[int] = Field(default=None, alias="NOTIFICATION_CHAT_ID")

    # AI Configuration
    ai_provider: str = Field(default="ollama", alias="AI_PROVIDER")

    # Business assistant access and budget. The budget is a guardrail, not a
    # provider subscription or a promise of a fixed message quota.
    owner_user_id: Optional[int] = Field(default=None, alias="OWNER_USER_ID")
    approved_user_ids: str = Field(default="", alias="APPROVED_USER_IDS")
    approved_group_ids: str = Field(default="", alias="APPROVED_GROUP_IDS")
    ai_monthly_budget_usd: float = Field(default=20.0, alias="AI_MONTHLY_BUDGET_USD")
    ai_input_price_per_million: float = Field(default=0.30, alias="AI_INPUT_PRICE_PER_MILLION")
    ai_output_price_per_million: float = Field(default=2.50, alias="AI_OUTPUT_PRICE_PER_MILLION")
    dashboard_token: Optional[str] = Field(default=None, alias="DASHBOARD_TOKEN")
    dashboard_host: str = Field(default="127.0.0.1", alias="DASHBOARD_HOST")
    dashboard_port: int = Field(default=3141, alias="DASHBOARD_PORT")
    dashboard_public_url: Optional[str] = Field(default=None, alias="DASHBOARD_PUBLIC_URL")
    assistant_ids: str = Field(default="business", alias="ASSISTANT_IDS")
    integration_secret: Optional[str] = Field(default=None, alias="INTEGRATION_SECRET")
    email_webhook_url: Optional[str] = Field(default=None, alias="EMAIL_WEBHOOK_URL")
    calendar_webhook_url: Optional[str] = Field(default=None, alias="CALENDAR_WEBHOOK_URL")
    crm_webhook_url: Optional[str] = Field(default=None, alias="CRM_WEBHOOK_URL")
    task_webhook_url: Optional[str] = Field(default=None, alias="TASK_WEBHOOK_URL")

    # Google Calendar + Gmail (owner-only). The OAuth redirect lands on the
    # dashboard server, so this also requires dashboard_public_url/dashboard_token.
    google_client_id: Optional[str] = Field(default=None, alias="GOOGLE_CLIENT_ID")
    google_client_secret: Optional[str] = Field(default=None, alias="GOOGLE_CLIENT_SECRET")
    google_oauth_redirect_uri: Optional[str] = Field(default=None, alias="GOOGLE_OAUTH_REDIRECT_URI")

    # Ollama Configuration
    ollama_base_url: str = Field(default="http://localhost:11434/v1", alias="OLLAMA_BASE_URL")
    ollama_model: str = Field(default="llama3.1", alias="OLLAMA_MODEL")
    ollama_api_key: str = Field(default="ollama", alias="OLLAMA_API_KEY")
    ai_request_timeout_seconds: float = Field(default=120.0, alias="AI_REQUEST_TIMEOUT_SECONDS")

    # Priority & Alerts
    urgent_score_threshold: int = Field(default=90, alias="URGENT_SCORE_THRESHOLD")
    immediate_alert_priorities: str = Field(default="P0,P1", alias="IMMEDIATE_ALERT_PRIORITIES")

    # Digest Schedules
    digest_interval_hours: int = Field(default=3, alias="DIGEST_INTERVAL_HOURS")
    digest_schedule_times: str = Field(default="08:00,13:00,19:00,22:00", alias="DIGEST_SCHEDULE_TIMES")

    # Database
    # Runtime storage is MongoDB-only. DATABASE_PATH is retained solely for
    # the one-time SQLite migration utility and local compatibility tests.
    database_path: str = Field(default="telegram_bot.db", alias="DATABASE_PATH")
    storage_backend: str = Field(default="mongo", alias="STORAGE_BACKEND")
    mongo_uri: Optional[str] = Field(default=None, alias="MONGO_URI")
    mongo_database: str = Field(default="telegram_business", alias="MONGO_DATABASE")

    # User profile rules
    profile: UserProfile = Field(default_factory=UserProfile.load_from_yaml)

    @property
    def alert_priority_list(self) -> List[str]:
        return [p.strip().upper() for p in self.immediate_alert_priorities.split(",") if p.strip()]

    @property
    def schedule_times_list(self) -> List[str]:
        return [t.strip() for t in self.digest_schedule_times.split(",") if t.strip()]

    @staticmethod
    def _parse_ids(value: str) -> List[int]:
        ids: List[int] = []
        for item in value.split(","):
            try:
                if item.strip():
                    ids.append(int(item.strip()))
            except ValueError:
                continue
        return ids

    @property
    def approved_user_id_list(self) -> List[int]:
        return self._parse_ids(self.approved_user_ids)

    @property
    def approved_group_id_list(self) -> List[int]:
        return self._parse_ids(self.approved_group_ids)

    @property
    def assistant_id_list(self) -> List[str]:
        return [item.strip() for item in self.assistant_ids.split(",") if item.strip()]

    class Config:
        env_file = ".env"
        extra = "ignore"


# Global settings instance
config = AppConfig()
