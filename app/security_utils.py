from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY_RE = re.compile(r"(token|secret|signature|password|authorization|webhook)", re.IGNORECASE)
TOKEN_RE = re.compile(r"\b\d{3,}:[A-Za-z0-9_-]{8,}\b")


def mask_sensitive(value: Any) -> Any:
    """Маскирует токены и секреты перед записью в логи или audit-события."""
    if isinstance(value, dict):
        return {
            key: "***" if SENSITIVE_KEY_RE.search(str(key)) else mask_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(mask_sensitive(item) for item in value)
    if isinstance(value, str):
        return TOKEN_RE.sub("***", value)
    return value
