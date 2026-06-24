from __future__ import annotations

from typing import Any

from app.security.secrets import mask_secret


def mask_sensitive(value: Any) -> Any:
    """Маскирует токены и секреты перед записью в логи, audit-события или ответы API."""
    return mask_secret(value)
