from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import subprocess
from functools import lru_cache
from typing import Any

ENCRYPTED_PREFIX = "enc:v1:"
_MASK = "***"
SENSITIVE_KEY_RE = re.compile(r"(token|secret|signature|password|authorization|webhook)", re.IGNORECASE)
TELEGRAM_TOKEN_RE = re.compile(r"\b(?P<prefix>\d{3,}:[A-Za-z0-9_-]{3})(?P<middle>[A-Za-z0-9_-]*)(?P<suffix>[A-Za-z0-9_-]{3})\b")


class SecretError(RuntimeError):
    """Ошибка работы с зашифрованными секретами."""


def mask_secret(value: Any) -> Any:
    """Маскирует секреты для логов, audit-событий и API-ответов."""
    if isinstance(value, dict):
        return {
            key: _MASK if SENSITIVE_KEY_RE.search(str(key)) else mask_secret(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask_secret(item) for item in value]
    if isinstance(value, tuple):
        return tuple(mask_secret(item) for item in value)
    if isinstance(value, str):
        return TELEGRAM_TOKEN_RE.sub(lambda m: f"{m.group('prefix')}***{m.group('suffix')}", value)
    return value


def masked_telegram_token(token: str | None) -> str:
    """Возвращает безопасное представление Telegram-токена вида 123456:ABC***XYZ."""
    if not token:
        return ""
    masked = mask_secret(token)
    return masked if masked != token else _MASK


def is_masked_secret(value: str | None) -> bool:
    return bool(value) and _MASK in value


@lru_cache(maxsize=1)
def load_master_key() -> bytes:
    """Загружает master key из env, файла или внешнего secret storage command."""
    raw_key = os.getenv("TG_CONNECT_MASTER_KEY")
    key_file = os.getenv("TG_CONNECT_MASTER_KEY_FILE")
    key_cmd = os.getenv("TG_CONNECT_MASTER_KEY_CMD")

    if raw_key is None and key_file:
        raw_key = open(key_file, encoding="utf-8").read().strip()
    if raw_key is None and key_cmd:
        raw_key = subprocess.check_output(key_cmd, shell=True, text=True, timeout=10).strip()  # noqa: S602 - команда задается администратором окружения.
    if raw_key is None:
        raise SecretError(
            "Master key is not configured: set TG_CONNECT_MASTER_KEY, "
            "TG_CONNECT_MASTER_KEY_FILE or TG_CONNECT_MASTER_KEY_CMD"
        )
    return hashlib.sha256(raw_key.strip().encode()).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks: list[bytes] = []
    counter = 0
    while sum(len(block) for block in blocks) < length:
        blocks.append(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return b"".join(blocks)[:length]


def encrypt_secret(secret: str | None) -> str | None:
    if secret is None or secret == "" or is_encrypted_secret(secret):
        return secret
    key = load_master_key()
    nonce = secrets.token_bytes(16)
    plaintext = secret.encode()
    stream = _keystream(key, nonce, len(plaintext))
    ciphertext = bytes(left ^ right for left, right in zip(plaintext, stream, strict=True))
    tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    payload = base64.urlsafe_b64encode(nonce + tag + ciphertext).decode()
    return ENCRYPTED_PREFIX + payload


def decrypt_secret(secret: str | None) -> str | None:
    if secret is None or secret == "" or not is_encrypted_secret(secret):
        return secret
    try:
        raw = base64.urlsafe_b64decode(secret.removeprefix(ENCRYPTED_PREFIX).encode())
        nonce, tag, ciphertext = raw[:16], raw[16:48], raw[48:]
    except (ValueError, TypeError) as exc:
        raise SecretError("Encrypted secret payload is malformed") from exc
    key = load_master_key()
    expected_tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise SecretError("Unable to decrypt secret with configured master key")
    stream = _keystream(key, nonce, len(ciphertext))
    plaintext = bytes(left ^ right for left, right in zip(ciphertext, stream, strict=True))
    return plaintext.decode()


def is_encrypted_secret(secret: str | None) -> bool:
    return bool(secret and secret.startswith(ENCRYPTED_PREFIX))


def encrypt_config_payload(payload: dict[str, Any]) -> dict[str, Any]:
    encrypted = dict(payload)
    for field in ("telegram_bot_token", "hmac_secret", "secret"):
        encrypted[field] = encrypt_secret(encrypted.get(field))
    return encrypted
