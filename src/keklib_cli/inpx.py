"""Потоковое чтение INPX-каталога и объединение серий (спецификация §6).

``read_structure`` возвращает порядок полей из единственного
``structure.info``. ``iter_books`` читает INP-записи потока: в памяти
находится только агрегация текущего INP (объединение повторов и серий),
весь каталог не загружается. Содержимое ZIP-архивов книг не открывается,
исходный INPX только читается.
"""

from __future__ import annotations

import re
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from keklib_cli.errors import CatalogError
from keklib_cli.models import Book, ImportStats, Series
from keklib_cli.source_names import validate_source_name

STRUCTURE_NAME = "structure.info"
INP_SUFFIX = ".inp"
ZIP_SUFFIX = ".zip"
STRUCTURE_SEPARATOR = ";"
FIELD_SEPARATOR = "\x04"
#: Обязательные поля структуры; необязательные отображаемые — LANG, SERIES,
#: SERNO, SIZE, LIBID; неизвестные поля сохраняются в ``Book.metadata``.
REQUIRED_FIELDS = frozenset({"AUTHOR", "TITLE", "FILE", "EXT", "DEL"})
_SERIES_FIELDS = frozenset({"SERIES", "SERNO"})
_ACTIVE_DEL = "0"
_ACTIVE_EXT = "fb2"
_NON_NEGATIVE_INT = re.compile(r"[0-9]+")
_UTF8_BOM = b"\xef\xbb\xbf"
#: SIZE хранится в столбце SQLite INTEGER (signed 64-bit): импортёр
#: принимает точно поддерживаемый диапазон 0..2**63-1 и адресно отказывает
#: за его пределами, не полагаясь на OverflowError из sqlite3-биндинга.
_MAX_SQLITE_INTEGER = 2**63 - 1

#: Ошибки собственных операций чтения ZIP-контейнера каталога, которые
#: переводятся в CatalogError: повреждённый контент (deflate — zlib.error,
#: усечение — BadZipFile/EOFError), недопустимое UTF-8-имя записи
#: (UnicodeDecodeError из конструктора/чтения), шифрование (RuntimeError)
#: и неподдерживаемый метод сжатия (NotImplementedError). Исключения
#: потребителя генератора этими обработчиками не перекрашиваются.
_ZIP_READ_ERRORS = (
    OSError,
    zipfile.BadZipFile,
    UnicodeDecodeError,
    zlib.error,
    EOFError,
    NotImplementedError,
    RuntimeError,
    ValueError,
)


def read_structure(catalog: Path) -> tuple[str, ...]:
    """Прочитать порядок полей из ``structure.info`` INPX-каталога.

    :param catalog: путь к INPX-файлу (например, ``librusec_flib.inpx``).
    :return: кортеж имён полей в порядке структуры.
    :raises CatalogError: каталог не читается; ``structure.info`` отсутствует,
        записан несколько раз, не декодируется как UTF-8, не является одной
        строкой, содержит пустые или повторяющиеся имена полей или не
        содержит обязательные поля ``REQUIRED_FIELDS``.
    """
    catalog = Path(catalog)
    try:
        with zipfile.ZipFile(catalog) as container:
            structures = [
                info for info in container.infolist() if info.filename == STRUCTURE_NAME
            ]
            if not structures:
                raise CatalogError(f"missing structure.info in INPX catalog: {catalog}")
            if len(structures) > 1:
                raise CatalogError(
                    f"duplicate structure.info entries in INPX catalog: {catalog}"
                )
            payload = container.read(structures[0])
    except CatalogError:
        raise
    except _ZIP_READ_ERRORS as exc:
        # Повреждённый ZIP-контейнер — данные пользователя, а не программная
        # ошибка: битый deflate (zlib.error), усечение (BadZipFile/EOFError),
        # недопустимое UTF-8-имя записи (UnicodeDecodeError), шифрование и
        # неподдерживаемое сжатие получают предметный отказ с путём каталога.
        raise CatalogError(
            f"cannot read INPX catalog {catalog}: {exc.__class__.__name__}: {exc}"
        ) from exc
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CatalogError(f"structure.info is not valid UTF-8: {catalog}") from exc
    # Снимаются только CR/LF; финальное пустое поле (завершающий `;`) допустимо.
    text = text.rstrip("\r\n")
    if "\r" in text or "\n" in text:
        raise CatalogError(f"structure.info must be a single line: {catalog}")
    if text.endswith(STRUCTURE_SEPARATOR):
        text = text[: -len(STRUCTURE_SEPARATOR)]
    fields = text.split(STRUCTURE_SEPARATOR)
    seen: set[str] = set()
    for field in fields:
        if not field:
            raise CatalogError(f"empty field name in structure.info: {catalog}")
        if field in seen:
            raise CatalogError(
                f"duplicate field name {field!r} in structure.info: {catalog}"
            )
        seen.add(field)
    missing = sorted(REQUIRED_FIELDS - seen)
    if missing:
        raise CatalogError(
            f"structure.info is missing required fields {missing}: {catalog}"
        )
    return tuple(fields)


def iter_books(catalog: Path, stats: ImportStats) -> Iterator[Book]:
    """Читать активные FB2-книги из INPX-каталога потоком.

    Строки обрабатываются по одному INP; повторы одной идентичности
    объединяются в пределах своего INP, серии дедуплицируются парами
    «серия, номер» в порядке появления. Книги выдаются после завершения
    текущего INP, поэтому память ограничена агрегацией одного INP.
    Переданный ``stats`` обновляется на месте по мере чтения.

    :param catalog: путь к INPX-файлу.
    :param stats: счётчики импорта; каждая исходная строка увеличивает
        ровно один из счётчиков ``excluded``/``duplicates``/``books``.
    :return: итератор книг; повреждённые, конфликтные или нечитаемые
        записи прерывают его ``CatalogError`` с именем INP и номером строки.
    """
    catalog = Path(catalog)
    fields = read_structure(catalog)
    try:
        container = zipfile.ZipFile(catalog)
    except _ZIP_READ_ERRORS as exc:
        # Конструктор читает центральный каталог: его повреждения — те же
        # предметные ошибки данных, что и при чтении structure.info.
        raise CatalogError(
            f"cannot read INPX catalog {catalog}: {exc.__class__.__name__}: {exc}"
        ) from exc
    try:
        # Preflight до чтения первого INP: обход конкретных ZipInfo и
        # однозначность отображения INP → archive. Повторные записи и
        # коллизии суффикса (a.inp/a.INP) отвергаются с диагностикой
        # обоих имён — молчаливая потеря строк и неоднозначные книги
        # недопустимы, а агрегация остаётся ограниченной одним INP.
        inp_entries: dict[str, tuple[str, zipfile.ZipInfo]] = {}
        for info in container.infolist():
            entry = info.filename
            if entry == STRUCTURE_NAME or not entry.lower().endswith(INP_SUFFIX):
                continue
            try:
                validate_source_name(entry)
            except CatalogError as exc:
                raise CatalogError(f"{catalog}: {exc}") from exc
            archive_name = entry[: -len(INP_SUFFIX)] + ZIP_SUFFIX
            previous = inp_entries.get(archive_name)
            if previous is not None:
                previous_name = previous[0]
                if previous_name == entry:
                    raise CatalogError(
                        f"duplicate INP entry {entry!r} "
                        f"in INPX catalog {catalog}"
                    )
                raise CatalogError(
                    f"INP entries {previous_name!r} and {entry!r} both map "
                    f"to archive {archive_name!r} in INPX catalog {catalog}"
                )
            inp_entries[archive_name] = (entry, info)
        for entry, info in inp_entries.values():
            try:
                # Открытие читает локальный заголовок: шифрование
                # (RuntimeError) и неподдерживаемый метод сжатия
                # (NotImplementedError) выявляются здесь, до строк INP.
                stream = container.open(info)
            except _ZIP_READ_ERRORS as exc:
                raise CatalogError(
                    f"cannot open {entry} in INPX catalog {catalog}: "
                    f"{exc.__class__.__name__}: {exc}"
                ) from exc
            try:
                # Само чтение строк переводится в _merge_inp: вокруг yield
                # нет перехвата — исключения потребителя генератора
                # (включая OSError и отмену) проходят без перекраски.
                yield from _merge_inp(stream, entry, fields, stats)
            finally:
                stream.close()
    finally:
        container.close()


@dataclass(slots=True)
class _Record:
    """Агрегация одной книги внутри текущего INP."""

    book: Book
    series: list[Series]
    line: str
    lineno: int


def _merge_inp(
    stream: BinaryIO,
    inp_name: str,
    fields: tuple[str, ...],
    stats: ImportStats,
) -> Iterator[Book]:
    """Обработать строки одного INP и выдать объединённые книги."""
    archive_name = inp_name[: -len(INP_SUFFIX)] + ZIP_SUFFIX
    expected = len(fields)
    records: dict[tuple[str, str], _Record] = {}
    lineno = 0
    while True:
        try:
            raw = stream.readline()
        except _ZIP_READ_ERRORS as exc:
            # Повреждённые сжатые данные самого INP (zlib.error, усечение,
            # CRC) — предметный отказ с именем записи; ошибки потребителя
            # сюда попасть не могут: это чтение собственного потока.
            raise CatalogError(
                f"{inp_name}: cannot read INP data from INPX catalog: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc
        if not raw:
            break
        lineno += 1
        if lineno == 1 and raw.startswith(_UTF8_BOM):
            raw = raw[len(_UTF8_BOM) :]
        try:
            # Снимаются только CR/LF; другие символы не отбрасываются.
            text = raw.rstrip(b"\r\n").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CatalogError(
                f"{inp_name} line {lineno}: row is not valid UTF-8: {exc}"
            ) from exc
        if not text:
            # Пустая физическая строка (в том числе только BOM или CRLF) —
            # повреждённая запись, а не повод для тихого пропуска; пустой INP
            # отличается отсутствием строк вообще.
            raise CatalogError(f"{inp_name} line {lineno}: record is empty")
        values = text.split(FIELD_SEPARATOR)
        if len(values) == expected + 1 and values[-1] == "":
            # Реальная конвенция Lib.rus.ec: строка INP заканчивается
            # разделителем поля перед переводом строки — как завершающий
            # `;` в structure.info (см. read_structure). Пустой хвостовой
            # значению не соответствует поле структуры, оно отбрасывается;
            # непустой хвост и лишние разделители — повреждение записи.
            values = values[:-1]
        if len(values) != expected:
            raise CatalogError(
                f"{inp_name} line {lineno}: expected {expected} fields, got {len(values)}"
            )
        row = dict(zip(fields, values))
        stats.rows += 1
        if row["DEL"] != _ACTIVE_DEL or row["EXT"].lower() != _ACTIVE_EXT:
            stats.excluded += 1
            continue
        file_value = row["FILE"]
        if not file_value:
            raise CatalogError(f"{inp_name} line {lineno}: active record has empty FILE")
        member = f"{file_value}.{row['EXT']}"
        try:
            validate_source_name(member)
        except CatalogError as exc:
            raise CatalogError(
                f"{inp_name} line {lineno}: unsafe member name {member!r}: {exc}"
            ) from exc
        size = _parse_size(row.get("SIZE", ""), inp_name, lineno)
        metadata = {
            name: value for name, value in row.items() if name not in _SERIES_FIELDS
        }
        identity = (archive_name, member)
        record = records.get(identity)
        if record is None:
            book = Book(
                archive=archive_name,
                member=member,
                file_id=file_value,
                author=row["AUTHOR"],
                title=row["TITLE"],
                language=row.get("LANG", ""),
                series=(),
                size=size,
                libid=row.get("LIBID", ""),
                metadata=metadata,
            )
            record = _Record(book=book, series=[], line=text, lineno=lineno)
            records[identity] = record
            stats.books += 1
        elif record.book.metadata != metadata:
            # Различие core-полей (всё, кроме SERIES/SERNO, включая
            # неизвестные) — конфликт данных; приводятся обе исходные строки.
            raise CatalogError(
                f"{inp_name}: conflicting records for {archive_name}:{member}: "
                f"line {record.lineno}: {record.line!r} "
                f"vs line {lineno}: {text!r}"
            )
        else:
            stats.duplicates += 1
        series_name = row.get("SERIES", "")
        if series_name:
            pair = Series(name=series_name, number=row.get("SERNO", ""))
            if pair not in record.series:
                record.series.append(pair)
    for record in records.values():
        record.book.series = tuple(record.series)
        yield record.book


def _parse_size(value: str, inp_name: str, lineno: int) -> int | None:
    """Разобрать SIZE активной записи: пусто — ``None``, иначе целое >= 0.

    Диапазон ограничен столбцом хранения SQLite INTEGER: ``0..2**63-1``.
    Большее значение — повреждённая запись каталога, а не молчаливое
    усечение/float; отказ адресный, с поддерживаемым диапазоном.
    """
    if not value:
        return None
    if not _NON_NEGATIVE_INT.fullmatch(value):
        raise CatalogError(
            f"{inp_name} line {lineno}: SIZE must be a non-negative integer, "
            f"got {value!r}"
        )
    try:
        # Защитный лимит CPython на преобразование строк не отключается:
        # отказ оформления длины оборачивается предметной ошибкой.
        size = int(value)
    except ValueError as exc:
        raise CatalogError(
            f"{inp_name} line {lineno}: SIZE is too large to convert "
            f"({len(value)} digits)"
        ) from exc
    if size > _MAX_SQLITE_INTEGER:
        raise CatalogError(
            f"{inp_name} line {lineno}: SIZE {value} exceeds the supported "
            f"range 0..{_MAX_SQLITE_INTEGER} (SQLite INTEGER): "
            "the catalog record is corrupt"
        )
    return size
