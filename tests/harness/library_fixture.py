"""Генератор синтетических библиотек для тестов.

Это генератор тестовых данных, а не второй INPX-importer: он только пишет
маленькие синтетические библиотеки во временный каталог и ничего не читает.
Модуль не импортирует приложение `keklib_cli` и пригоден вне checkout.

Структура создаваемой библиотеки повторяет реальную раскладку Lib.rus.ec:

- ``librusec_flib.inpx`` — ZIP-контейнер с ``structure.info`` и отдельными
  INP-файлами; имена INP — простые, в корне контейнера, без компонентов пути
  (§6 спецификации); записи каталога внутри INPX не создаются;
- ``lib.rus.ec/`` — каталог архивов книг на диске (создаётся всегда, даже без
  архивов); соответствующий ZIP ищется по имени INP.

Формат файлов повторяет исходный:

- ``structure.info`` — имена полей через ``;`` с завершающим ``;``;
- INP — UTF-8, поля разделены ``\\x04``, записи — переводом строки;
- значения отсутствующих полей — пустые строки.

Вывод детерминирован: одинаковые аргументы дают одинаковые байты (фиксированная
метка времени ZIP-записей).
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

CATALOG_NAME = "librusec_flib.inpx"
ARCHIVES_DIR = "lib.rus.ec"

#: Порядок полей по умолчанию, как в реальном `structure.info`.
DEFAULT_FIELDS = (
    "AUTHOR",
    "GENRE",
    "TITLE",
    "SERIES",
    "SERNO",
    "FILE",
    "SIZE",
    "LIBID",
    "DEL",
    "EXT",
    "DATE",
    "LANG",
    "LIBRATE",
    "KEYWORDS",
    "YEAR",
    "SOURCELIB",
)

_FIELD_SEPARATOR = "\x04"
_ROW_SEPARATOR = "\n"
_SIMPLE_NAME = re.compile(r"^[^/\\]+$")
# Фиксированная метка времени для детерминированных архивов.
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)


def write_library(
    root: Path,
    groups: Mapping[str, Sequence[Mapping[str, str]]],
    *,
    fields: Sequence[str] = DEFAULT_FIELDS,
    members: Mapping[str, Mapping[str, bytes]] | None = None,
) -> Path:
    """Записать синтетическую библиотеку в ``root`` и вернуть путь корня.

    :param root: корень библиотеки (создаётся при необходимости).
    :param groups: ключи — имена INP (без каталогов и расширения), значения —
        последовательности записей; запись — отображение имя поля → значение.
    :param fields: порядок полей для ``structure.info`` и строк INP.
    :param members: ключи — имена ZIP (без каталогов и расширения), значения —
        отображения имя FB2 → байты; архивы создаются в ``lib.rus.ec/``.
    :raises ValueError: имя INP/ZIP или имя FB2 содержит компоненты пути.
    """
    root = Path(root)
    archives_dir = root / ARCHIVES_DIR
    archives_dir.mkdir(parents=True, exist_ok=True)

    for zip_name, files in (members or {}).items():
        _check_simple_name(zip_name, "ZIP name")
        archive_path = archives_dir / f"{zip_name}.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for member_name, payload in files.items():
                _check_simple_name(member_name, "FB2 name")
                archive.writestr(
                    _zip_info(f"{member_name}"), payload, compress_type=zipfile.ZIP_DEFLATED
                )

    catalog_path = root / CATALOG_NAME
    with zipfile.ZipFile(catalog_path, "w") as catalog:
        catalog.writestr(_zip_info("structure.info"), _structure_info(fields))
        for inp_name, rows in groups.items():
            _check_simple_name(inp_name, "INP name")
            catalog.writestr(
                _zip_info(f"{inp_name}.inp"),
                _inp_bytes(rows, fields),
                compress_type=zipfile.ZIP_DEFLATED,
            )
    return root


def _structure_info(fields: Sequence[str]) -> bytes:
    """Собрать `structure.info`: поля через ``;`` с завершающим ``;``."""
    line = "".join(f"{field};" for field in fields)
    return f"{line}{_ROW_SEPARATOR}".encode("utf-8")


def _inp_bytes(rows: Sequence[Mapping[str, str]], fields: Sequence[str]) -> bytes:
    """Собрать INP: UTF-8, поля ``\\x04``, записи переводом строки."""
    lines = [
        _FIELD_SEPARATOR.join(str(row.get(field, "")) for field in fields)
        for row in rows
    ]
    if not lines:
        return b""
    return _ROW_SEPARATOR.join(lines).encode("utf-8")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_DATE_TIME)
    info.external_attr = 0o600 << 16
    return info


def _check_simple_name(name: str, what: str) -> None:
    if not isinstance(name, str) or not _SIMPLE_NAME.match(name):
        raise ValueError(f"{what} must be a simple file name, got: {name!r}")
