"""Фикстуры тестов keklib-cli: обёртки над чистым генератором библиотек."""

from __future__ import annotations

import warnings
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from keklib_cli.config import inspect_library
from keklib_cli.models import Library

from tests.harness.library_fixture import (
    ARCHIVES_DIR,
    CATALOG_NAME,
    DEFAULT_FIELDS,
    write_library,
)

__all__ = ["DEFAULT_FIELDS", "make_library", "make_raw_catalog"]


def make_library(
    root: Path,
    groups: Mapping[str, Sequence[Mapping[str, str]]],
    *,
    fields: Sequence[str] = DEFAULT_FIELDS,
    members: Mapping[str, Mapping[str, bytes]] | None = None,
) -> Library:
    """Записать синтетическую библиотеку и вернуть проверенный ``Library``.

    Обёртка над ``tests.harness.library_fixture.write_library`` с теми же
    параметрами: корень проверяется ``inspect_library`` после записи.
    """
    write_library(root, groups, fields=fields, members=members)
    return inspect_library(root)


def make_raw_catalog(
    root: Path,
    structures: Sequence[bytes],
    inps: Mapping[str, bytes],
    *,
    entries: Sequence[tuple[str, bytes]] = (),
    encrypted_structure: bool = False,
) -> Library:
    """Записать библиотеку с точными байтами INPX и вернуть ``Library``.

    Для тестов формата и повреждённых каталогов: ``structures`` — байты
    каждого ``structure.info`` (обычно один; пустая последовательность —
    каталог без структуры, несколько — дубликаты), ключи ``inps`` — имена
    записей INPX (к ним добавляется ``.inp``), значения — точные байты INP.
    В отличие от ``make_library`` содержимое не собирается и не проверяется:
    записи могут содержать BOM, CRLF, некорректные байты и пути.

    :param entries: дополнительные записи INPX с точными именами — позволяют
        дубликаты имён (``a.inp`` дважды) и регистрозависимые суффиксы
        (``a.inp``/``a.INP``), невыразимые через отображение ``inps``.
    :param encrypted_structure: пометить каждую запись ``structure.info``
        флагом шифрования (бит 0x1) без реального шифрования — достаточно,
        чтобы zipfile отказался читать запись без пароля.
    """
    root = Path(root)
    archives = root / ARCHIVES_DIR
    archives.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(root / CATALOG_NAME, "w") as catalog:
        # Повторная запись structure.info — намеренное повреждение, поэтому
        # ожидаемое предупреждение zipfile скрывается, а не маскирует вывод.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message="Duplicate name", category=UserWarning
            )
            for structure in structures:
                if encrypted_structure:
                    # writestr перезаписывает флаги локального заголовка, а
                    # центральный каталог пишется при close() из filelist —
                    # поэтому бит шифрования ставится после записи записи.
                    info = zipfile.ZipInfo("structure.info")
                    catalog.writestr(info, structure)
                    info.flag_bits |= 0x1
                else:
                    catalog.writestr("structure.info", structure)
            for inp_name, payload in inps.items():
                catalog.writestr(f"{inp_name}.inp", payload)
            for entry_name, payload in entries:
                catalog.writestr(entry_name, payload)
    return inspect_library(root)
