"""Вычисление нативных путей состояния по §5 спецификации.

Функция чистая: не создаёт каталоги и не читает диск. Платформа выбирается
явным `system` (для тестов и симуляции) либо реальной ОС; отображение
переменных окружения берётся из `env` либо из `os.environ`. Пути соединяются
штатным оператором `/` платформы, без ручных разделителей.
"""

from __future__ import annotations

import os
import platform
from collections.abc import Mapping
from pathlib import Path

from ..models import StatePaths

_APP_DIR = "keklib-cli"
_CONFIG_NAME = "config.json"
_INDEX_NAME = "index.sqlite3"


def state_paths(
    *,
    system: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> StatePaths:
    """Пути config/cache/index для нативных пользовательских каталогов ОС.

    ``system`` — имя ОС как ``platform.system()`` (``Linux``, ``Windows``,
    ``Darwin``); ``None`` означает реальную ОС. Прочие POSIX-системы трактуются
    по строке Linux (XDG). ``env`` подменяет ``os.environ`` для XDG и
    APPDATA/LOCALAPPDATA, ``home`` — базу ``Path.home()``.
    """
    resolved_system = platform.system() if system is None else system
    env_map = os.environ if env is None else env
    base = Path.home() if home is None else Path(home)

    if resolved_system == "Windows":
        config_root = _env_or(env_map, "APPDATA", base / "AppData" / "Roaming")
        cache_root = _env_or(env_map, "LOCALAPPDATA", base / "AppData" / "Local")
        app_cache = cache_root / _APP_DIR / "Cache"
        return StatePaths(
            config=config_root / _APP_DIR / _CONFIG_NAME,
            cache=app_cache,
            index=app_cache / _INDEX_NAME,
        )

    if resolved_system == "Darwin":
        app_cache = base / "Library" / "Caches" / _APP_DIR
        return StatePaths(
            config=base / "Library" / "Application Support" / _APP_DIR / _CONFIG_NAME,
            cache=app_cache,
            index=app_cache / _INDEX_NAME,
        )

    # Linux и прочие POSIX: XDG с умолчаниями ~/.config и ~/.cache.
    config_root = _xdg_root(env_map.get("XDG_CONFIG_HOME"), base, ".config")
    cache_root = _xdg_root(env_map.get("XDG_CACHE_HOME"), base, ".cache")
    app_cache = cache_root / _APP_DIR
    return StatePaths(
        config=config_root / _APP_DIR / _CONFIG_NAME,
        cache=app_cache,
        index=app_cache / _INDEX_NAME,
    )


def _xdg_root(value: str | None, home: Path, default: str) -> Path:
    """Корень XDG: пустые и относительные значения переменной игнорируются."""
    if value:
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate
    return home / default


def _env_or(env: Mapping[str, str], key: str, default: Path) -> Path:
    """Значение переменной окружения или умолчание; пустое значение — как отсутствующее."""
    value = env.get(key)
    return Path(value) if value else default
