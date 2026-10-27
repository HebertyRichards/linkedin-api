from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config.settings import Settings, get_settings

_SETTINGS_ENV = [name.upper() for name in Settings.model_fields]


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No test sees the machine's environment or a .env file: each test sets the values it needs."""
    for name in _SETTINGS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
