from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BotConfig(BaseModel):
    """Конфигурация подключенного Telegram-бота."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    telegram_bot_token: str = Field(..., min_length=1)
    bitrix_webhook_url: AnyHttpUrl | None = None
    enabled: bool = True
    secret: str | None = None
    allowed_ips: list[str] = Field(default_factory=list)
    rate_limit: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("allowed_ips", mode="before")
    @classmethod
    def normalize_allowed_ips(cls, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value
