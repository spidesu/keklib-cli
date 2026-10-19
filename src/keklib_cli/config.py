"""Конфигурация keklib-cli: проверка библиотеки и атомарное сохранение корня.

Формат — JSON ``{"version": 1, "library_root": "..."}`` по пути
``StatePaths.config``. Запись атомарна: временный файл рядом с конфигурацией,
закрытие и ``os.replace``. Проверка содержимого ``structure.info`` выполняется
при импорте (задача 9), здесь проверяются только пути.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from keklib_cli.errors import CatalogError, ConfigError
from keklib_cli.models import Library, StatePaths

CONFIG_VERSION = 1
CATALOG_NAME = "librusec_flib.inpx"
ARCHIVES_DIR_NAME = "lib.rus.ec"


def inspect_library(root: Path) -> Library:
    """Проверить корень библиотеки и вернуть ``Library`` с абсолютными путями.

    Проверяются существование корня, наличие INPX-файла ``librusec_flib.inpx``
    и каталога архивов ``lib.rus.ec/``. Содержимое INPX не читается.

    :raises CatalogError: корень, INPX или каталог архивов отсутствуют.
    """
    root = Path(root).expanduser()
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise CatalogError(f"library root does not exist: {root}") from exc
    if not root.is_dir():
        raise CatalogError(f"library root is not a directory: {root}")
    catalog = root / CATALOG_NAME
    archives = root / ARCHIVES_DIR_NAME
    if not catalog.is_file():
        raise CatalogError(f"missing INPX catalog: {catalog}")
    if not archives.is_dir():
        raise CatalogError(f"missing archives directory: {archives}")
    return Library(root=root, catalog=catalog, archives=archives)


def load_library(paths: StatePaths) -> Library:
    """Прочитать конфигурацию и вернуть заново проверенный ``Library``.

    :raises ConfigError: конфигурация не читается, не является JSON-объектом,
        имеет другую версию или не содержит ``library_root``.
    :raises CatalogError: сохранённая библиотека больше не проходит проверку.
    """
    try:
        raw = paths.config.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file: {paths.config}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"configuration is not valid JSON: {paths.config}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"configuration is not a JSON object: {paths.config}")
    version = data.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != CONFIG_VERSION
    ):
        raise ConfigError(
            f"unsupported configuration version {version!r}, "
            f"expected integer {CONFIG_VERSION}"
        )
    library_root = data.get("library_root")
    if not isinstance(library_root, str) or not library_root:
        raise ConfigError(f"missing library_root in configuration: {paths.config}")
    return inspect_library(Path(library_root))


def save_library(paths: StatePaths, library: Library) -> None:
    """Атомарно сохранить проверенный корень библиотеки в конфигурацию.

    Библиотека проверяется до какой-либо записи: при ошибке конфигурация не
    создаётся и не меняется. Запись выполняется во временный файл рядом с
    конфигурацией, затем ``os.replace`` публикует его поверх старой.

    :raises CatalogError: библиотека не проходит ``inspect_library``.
    :raises ConfigError: конфигурацию не удалось записать.
    """
    verified = inspect_library(library.root)
    payload = (
        json.dumps(
            {"version": CONFIG_VERSION, "library_root": str(verified.root)},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    config = paths.config
    try:
        config.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            dir=config.parent, prefix=f".{config.name}.", suffix=".tmp"
        )
    except OSError as exc:
        raise ConfigError(f"cannot prepare configuration file: {config}") from exc
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, config)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            # Best-effort очистка: исходная ошибка записи важнее и должна
            # дойти до пользователя как ConfigError, а не подмениться сбоем
            # удаления временного файла.
            pass
        raise ConfigError(f"cannot write configuration file: {config}") from exc
