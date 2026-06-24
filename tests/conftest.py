import pytest

from app.queue.delivery import delivery_queue


@pytest.fixture(autouse=True)
def isolated_runtime_state(tmp_path, monkeypatch):
    """Не позволяем тестам писать runtime-состояние в рабочий каталог репозитория."""
    monkeypatch.setenv("ADMIN_STATE_PATH", str(tmp_path / "admin_state.json"))
    monkeypatch.setenv("DELIVERY_QUEUE_PATH", str(tmp_path / "delivery_queue.json"))
    delivery_queue.clear()
