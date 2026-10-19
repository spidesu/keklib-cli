"""Проверка относительных имён файлов источника (INP, ZIP, FB2).

Общий валидатор для импорта INPX (задача 3) и проверок архивов при
извлечении (задача 7): правила одинаковы на всех ОС, чтобы перенос
библиотеки между Linux, Windows и macOS не переносил доверие к путям.

Имя — простой файл без компонентов пути. Запрещены (спецификация §6):
разделители ``/`` и ``\\``; абсолютные, drive-relative и UNC/device-пути
Windows (``C:x``, ``\\\\server\\share``, ``\\\\.\\COM1``); NUL и другие
управляющие символы; компоненты ``.`` и ``..``; пустое имя.
"""

from __future__ import annotations

import re

from keklib_cli.errors import CatalogError

_CONTROL_CHAR = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")
_SPECIAL_COMPONENTS = frozenset({".", ".."})


def validate_source_name(name: str) -> str:
    """Проверить простое имя файла источника и вернуть его без изменений.

    :param name: имя записи INPX, ZIP-архива или FB2-файла из INP.
    :return: то же имя; проверка ничего не изменяет и не нормализует.
    :raises CatalogError: имя пусто, содержит разделитель пути,
        drive-relative/UNC/device-префикс Windows, NUL или другой
        управляющий символ либо является компонентом ``.``/``..``.
    """
    if not name:
        raise CatalogError("source name must not be empty")
    if "/" in name or "\\" in name:
        raise CatalogError(f"source name contains a path separator: {name!r}")
    if _DRIVE_PREFIX.fullmatch(name[:2]):
        raise CatalogError(f"source name is a Windows drive path: {name!r}")
    if name in _SPECIAL_COMPONENTS:
        raise CatalogError(
            f"source name is a path component, not a file name: {name!r}"
        )
    control = _CONTROL_CHAR.search(name)
    if control:
        raise CatalogError(
            f"source name contains control character "
            f"U+{ord(control.group()):04X}: {name!r}"
        )
    return name
