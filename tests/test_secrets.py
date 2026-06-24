import json

from app.models.bot_config import BotConfig
from app.security.secrets import encrypt_config_payload, mask_secret, masked_telegram_token, load_master_key
from app.services.admin_store import load_admin_bot_configs, save_admin_bot_config


def test_masked_telegram_token_keeps_safe_edges() -> None:
    assert masked_telegram_token("123456:ABCdefXYZ") == "123456:ABC***XYZ"


def test_mask_secret_masks_nested_sensitive_values() -> None:
    payload = {"text": "send 123456:ABCdefXYZ", "hmac_secret": "top-secret"}

    assert mask_secret(payload) == {"text": "send 123456:ABC***XYZ", "hmac_secret": "***"}


def test_encrypt_config_payload_and_model_decrypt(monkeypatch) -> None:
    monkeypatch.setenv("TG_CONNECT_MASTER_KEY", "test-master-key")
    load_master_key.cache_clear()
    payload = {
        "id": "main",
        "name": "main",
        "telegram_bot_token": "123456:ABCdefXYZ",
        "hmac_secret": "top-secret",
    }

    encrypted = encrypt_config_payload(payload)
    assert encrypted["telegram_bot_token"].startswith("enc:v1:")
    assert encrypted["hmac_secret"].startswith("enc:v1:")
    assert "123456:ABCdefXYZ" not in json.dumps(encrypted)

    config = BotConfig.model_validate(encrypted)
    assert config.telegram_bot_token == "123456:ABCdefXYZ"
    assert config.hmac_secret == "top-secret"


def test_admin_store_persists_encrypted_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TG_CONNECT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("ADMIN_STATE_PATH", str(tmp_path / "state.json"))
    load_master_key.cache_clear()

    save_admin_bot_config(BotConfig(id="main", name="main", telegram_bot_token="123456:ABCdefXYZ", hmac_secret="top-secret"))

    raw = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "123456:ABCdefXYZ" not in raw
    assert "top-secret" not in raw
    loaded = load_admin_bot_configs()[0]
    assert loaded.telegram_bot_token == "123456:ABCdefXYZ"
    assert loaded.hmac_secret == "top-secret"
