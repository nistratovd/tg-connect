from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from app.security.secrets import decrypt_secret, mask_secret, masked_telegram_token

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BotConfig(BaseModel):
    """Конфигурация подключенного Telegram-бота."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    telegram_bot_token: str = Field(..., min_length=1)
    bitrix_webhook_url: AnyHttpUrl | None = None
    telegram_update_mode: Literal["webhook", "long_polling"] = "webhook"
    enabled: bool = True
    secret: str | None = None
    allowed_ips: list[str] = Field(default_factory=list)
    rate_limit: int | None = Field(default=None, ge=1)
    hmac_secret: str | None = None
    bitrix_auth_token: str | None = Field(default=None, min_length=1)
    timestamp_tolerance_seconds: int = Field(default=300, ge=1)
    max_request_body_bytes: int = Field(default=1024 * 1024, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def decrypt_encrypted_secrets(cls, value: Any) -> Any:
        if isinstance(value, dict):
            data = dict(value)
            for field in ("telegram_bot_token", "hmac_secret", "secret", "bitrix_auth_token"):
                data[field] = decrypt_secret(data.get(field))
            return data
        return value

    @field_validator("allowed_ips", mode="before")
    @classmethod
    def normalize_allowed_ips(cls, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def require_hmac_signature(self) -> bool:
        """Включает HMAC-проверку, если задан отдельный hmac_secret или legacy secret."""
        return bool(self.hmac_secret or self.secret)

    @property
    def effective_hmac_secret(self) -> str | None:
        """Возвращает секрет для подписи с поддержкой существующего поля secret."""
        return self.hmac_secret or self.secret

    @property
    def masked_telegram_bot_token(self) -> str:
        """Безопасная маска Telegram-токена для административного интерфейса."""
        return masked_telegram_token(self.telegram_bot_token)

    @property
    def masked_bitrix_auth_token(self) -> str:
        """Безопасная маска токена, передаваемого в Битрикс."""
        if not self.bitrix_auth_token:
            return ""
        masked = mask_secret(self.bitrix_auth_token)
        return masked if masked != self.bitrix_auth_token else "***"
