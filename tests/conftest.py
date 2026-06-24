import pytest


@pytest.fixture(autouse=True)
def isolated_admin_state(tmp_path, monkeypatch):
    """Не позволяем тестам писать runtime-состояние в рабочий каталог репозитория."""
    monkeypatch.setenv("ADMIN_STATE_PATH", str(tmp_path / "admin_state.json"))
