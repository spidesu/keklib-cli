"""Хранилище поискового индекса SQLite FTS5 (спецификация §7).

Модуль только пишет индекс в новый выходной путь и читает готовый индекс:
публикация (атомарная замена) и блокировки — задача индексатора. База
создаётся эксклюзивно по отсутствующему пути, журнал — DELETE (не WAL),
поэтому после закрытия пишущего соединения не остаётся ``-wal``/``-shm``.
Чтение открывается только для чтения через URI ``mode=ro``, построенного
``Path.as_uri()`` (корректные ``file:``-URI на всех ОС, включая Windows).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

from keklib_cli.errors import IndexBuildError
from keklib_cli.models import Book, ImportStats, IndexMeta
from keklib_cli.text import normalize

SCHEMA_VERSION = 1
#: Версия 3 (исправительная волна CI, 2026-10-19): канонизация спорных
#: кодовых точек — ``normalize`` вырезает U+FFFE/U+FFFF (статус в
#: токенизаторе unicode61 менялся между версиями SQLite) и весь диапазон
#: суррогатов симметрично при индексировании и поиске; norm-колонки и
#: лексика запроса меняются, прежние индексы перестраиваются автоматически
#: при первом же запуске. Версия 2 (финальная волна F2): лексика запроса
#: стала точным зеркалом фактического токенизатора FTS5 ``unicode61``
#: (combining-метки и незакреплённые кодпоинты остаются внутри токена).
RULES_VERSION = 3

#: Ключи таблицы ``meta``; отсутствующий ключ — повреждение индекса.
_META_KEYS = (
    "schema_version",
    "rules_version",
    "root",
    "catalog_rel",
    "sha256",
    "built_at",
    "stats",
)
_STATS_FIELDS = ("rows", "excluded", "duplicates", "books")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_NON_NEGATIVE_INT = re.compile(r"[0-9]+")
_JSON_KWARGS = {"ensure_ascii": False, "separators": (",", ":"), "sort_keys": True}

#: Ожидаемые столбцы ``book`` в порядке объявления; защита от чужой схемы.
_BOOK_COLUMNS = (
    "id",
    "archive",
    "member",
    "file_id",
    "author",
    "title",
    "language",
    "series_json",
    "size",
    "libid",
    "metadata_json",
    "norm_author",
    "norm_title",
)
#: Существенные свойства столбцов ``book``: имя → (объявленный тип, pk).
_BOOK_DECLARED = (
    ("id", "INTEGER", 1),
    ("archive", "TEXT", 0),
    ("member", "TEXT", 0),
    ("file_id", "TEXT", 0),
    ("author", "TEXT", 0),
    ("title", "TEXT", 0),
    ("language", "TEXT", 0),
    ("series_json", "TEXT", 0),
    ("size", "INTEGER", 0),
    ("libid", "TEXT", 0),
    ("metadata_json", "TEXT", 0),
    ("norm_author", "TEXT", 0),
    ("norm_title", "TEXT", 0),
)
_FTS_COLUMNS = ("norm_title", "norm_author")
#: Параметры FTS5, без которых MATCH по внешнему контенту невозможен.
_FTS_OPTIONS = (
    ("content", "book"),
    ("content_rowid", "id"),
    ("tokenize", "unicode61 remove_diacritics 0"),
)
#: Суффиксы соседних файлов SQLite; существование любого из них до постройки
#: означает чужие данные, которыми писатель не владеет.
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")

_CREATE_META = "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)"
_CREATE_BOOK = (
    "CREATE TABLE book(id INTEGER PRIMARY KEY, archive TEXT, member TEXT, "
    "file_id TEXT, author TEXT, title TEXT, language TEXT, series_json TEXT, "
    "size INTEGER, libid TEXT, metadata_json TEXT, norm_author TEXT, "
    "norm_title TEXT, UNIQUE(archive, member))"
)
_CREATE_FTS = (
    "CREATE VIRTUAL TABLE book_fts USING fts5(norm_title, norm_author, "
    "content='book', content_rowid='id', "
    "tokenize='unicode61 remove_diacritics 0')"
)
_INSERT_BOOK = (
    "INSERT INTO book(archive, member, file_id, author, title, language, "
    "series_json, size, libid, metadata_json, norm_author, norm_title) "
    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def write_index(path: Path, books: Iterable[Book], meta: IndexMeta) -> None:
    """Построить индекс в ``path``; путь должен не существовать.

    ``books`` исчерпываются полностью; ``meta.stats`` — тот же объект,
    который обновляет источник книг, и сериализуется после исчерпания
    потока, поэтому записываются финальные счётчики. Вся запись идёт одной
    транзакцией; FTS заполняется ``rebuild`` и проверяется
    ``integrity-check`` с ``rank=1`` (согласованность с внешней
    content-table) на пишущем соединении до фиксации. При любой ошибке
    или отмене собственный недостроенный файл удаляется, чужие файлы не
    затрагиваются: постройка отвергается, если рядом с целевым путём уже
    есть чужие ``-journal``/``-wal``/``-shm`` (проверяется сама запись
    каталога, без разыменования symlink).

    :raises IndexBuildError: путь или соседний sidecar-файл существует,
        недоступен каталог, SQLite
        недоступен или повреждён (в том числе без модуля FTS5) либо данные
        нарушают ограничения схемы.
    """
    path = Path(path)
    if path.exists():
        raise IndexBuildError(
            f"index target already exists, refusing to overwrite: {path}"
        )
    if not path.parent.is_dir():
        raise IndexBuildError(
            f"index directory does not exist: {path.parent}"
        )
    # Владение sidecar-файлами не наследуется от эксклюзивного создания
    # основного пути: существующие соседи — чужие данные, постройка рядом с
    # ними не выполняется. Проверяется сама запись каталога (lexists),
    # без разыменования symlink: битая чужая ссылка тоже чужая запись, даже
    # если её цель не существует. Только когда соседних записей нет,
    # появившиеся в процессе журналы гарантированно созданы собственным
    # соединением.
    sidecars = [
        Path(f"{path}{suffix}")
        for suffix in _SIDECAR_SUFFIXES
        if os.path.lexists(f"{path}{suffix}")
    ]
    if sidecars:
        raise IndexBuildError(
            f"refusing to build next to existing sidecar files "
            f"{[sidecar.name for sidecar in sidecars]}: {path}"
        )
    try:
        # Эксклюзивное занятие отсутствующего пути: даже гонка двух
        # писателей оставит ровно одного владельца файла.
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise IndexBuildError(
            f"index target already exists, refusing to overwrite: {path}"
        ) from exc
    except OSError as exc:
        raise IndexBuildError(f"cannot create index file {path}: {exc}") from exc
    os.close(descriptor)
    try:
        _build(path, books, meta)
    except BaseException as exc:
        # Отмена (KeyboardInterrupt) и ошибки одинаково убирают только
        # собственные файлы; публикацией занимается индексатор.
        _discard(path)
        if isinstance(exc, (sqlite3.Error, OSError)):
            raise IndexBuildError(f"failed to build index {path}: {exc}") from exc
        raise


def read_meta(path: Path) -> IndexMeta:
    """Прочитать метаданные готового индекса из ``path``.

    :raises IndexBuildError: файл не открывается, таблица ``meta``
        отсутствует, повреждена или содержит неполные/неверные значения.
    """
    path = Path(path)
    try:
        conn = _connect_ro(path)
        try:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise IndexBuildError(
            f"cannot read index metadata from {path}: {exc}"
        ) from exc
    return _parse_meta(dict(rows), path)


def validate_index(path: Path, *, quick: bool = False) -> None:
    """Проверить целостность и согласованность индекса в ``path``.

    Открывает базу только для чтения, выполняет ``quick_check`` либо
    ``integrity_check``, сверяет существенные свойства схемы ``book``
    (столбцы, типы, ``INTEGER PRIMARY KEY``, ``UNIQUE(archive, member)``)
    и подлинность ``book_fts`` (виртуальная таблица FTS5 с требуемыми
    столбцами и параметрами external-content), метаданные (включая версии
    схемы и правил) и совпадение числа книг со счётчиком.

    :raises IndexBuildError: файл отсутствует, не является базой SQLite,
        не проходит проверку целостности, имеет чужую или непригодную для
        поиска схему, неполные метаданные или несогласованный счётчик книг.
    """
    path = Path(path)
    if not path.is_file():
        raise IndexBuildError(f"index file does not exist: {path}")
    try:
        conn = _connect_ro(path)
        try:
            pragma = "PRAGMA quick_check" if quick else "PRAGMA integrity_check"
            problems = [
                row[0] for row in conn.execute(pragma).fetchall() if row[0] != "ok"
            ]
            if problems:
                raise IndexBuildError(
                    f"index integrity check failed for {path}: "
                    + "; ".join(problems)
                )
            _validate_book_schema(conn, path)
            _validate_fts_schema(conn, path)
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
            stored = _parse_meta(dict(rows), path)
            if stored.schema_version != SCHEMA_VERSION:
                raise IndexBuildError(
                    f"unsupported index schema version {stored.schema_version} "
                    f"(expected {SCHEMA_VERSION}): {path}"
                )
            if stored.rules_version != RULES_VERSION:
                raise IndexBuildError(
                    f"unsupported index rules version {stored.rules_version} "
                    f"(expected {RULES_VERSION}): {path}"
                )
            count = conn.execute("SELECT count(*) FROM book").fetchone()[0]
            if count != stored.stats.books:
                raise IndexBuildError(
                    f"index book count {count} does not match metadata "
                    f"{stored.stats.books}: {path}"
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise IndexBuildError(f"cannot validate index {path}: {exc}") from exc


def _validate_book_schema(conn: sqlite3.Connection, path: Path) -> None:
    """Проверить существенные свойства схемы ``book``, а не только имена.

    Сверяются порядок и имена столбцов, объявленные типы и ``INTEGER
    PRIMARY KEY`` у ``id`` (rowid-алиас, на который ссылается FTS), а также
    уникальное ограничение пары ``(archive, member)`` — без него база
    пропускает дубликаты книг и не является схемой хранилища.
    """
    info = conn.execute("PRAGMA table_info(book)").fetchall()
    columns = tuple(row[1] for row in info)
    if columns != _BOOK_COLUMNS:
        raise IndexBuildError(
            f"index table book has unexpected columns {columns}: {path}"
        )
    declared = {row[1]: (row[2], row[5]) for row in info}
    for name, expected_type, expected_pk in _BOOK_DECLARED:
        actual = declared.get(name)
        if actual != (expected_type, expected_pk):
            raise IndexBuildError(
                f"index table book column {name!r} must be declared "
                f"{expected_type} with pk={expected_pk}, got {actual}: {path}"
            )
    for index in conn.execute("PRAGMA index_list(book)").fetchall():
        unique, name, origin, partial = index[2], index[1], index[3], index[4]
        if not unique or partial or origin != "u":
            continue
        pair = tuple(
            row[2]
            for row in conn.execute(f'PRAGMA index_info("{name}")').fetchall()
        )
        if pair == ("archive", "member"):
            return
    raise IndexBuildError(
        f"index table book lacks UNIQUE(archive, member): {path}"
    )


def _validate_fts_schema(conn: sqlite3.Connection, path: Path) -> None:
    """Проверить, что ``book_fts`` — настоящая виртуальная таблица FTS5.

    ``sqlite_master`` хранит исходный DDL виртуальной таблицы: обычная
    таблица с тем же именем (после DROP + CREATE) не проходит проверку.
    Сверяются столбцы и параметры external-content; без них MATCH по
    ``book`` невозможен.
    """
    row = conn.execute(
        "SELECT type, sql FROM sqlite_master WHERE name = 'book_fts'"
    ).fetchone()
    if row is None:
        raise IndexBuildError(f"index is missing FTS table book_fts: {path}")
    object_type, create_sql = row
    if object_type != "table" or not isinstance(create_sql, str):
        raise IndexBuildError(f"index book_fts is not a virtual table: {path}")
    normalized = " ".join(create_sql.split()).lower()
    if not normalized.startswith("create virtual table") or (
        "using fts5" not in normalized
    ):
        raise IndexBuildError(
            f"index book_fts is not an FTS5 virtual table: {path}"
        )
    for option, expected in _FTS_OPTIONS:
        if not re.search(rf"{option}\s*=\s*'{re.escape(expected)}'", normalized):
            raise IndexBuildError(
                f"index book_fts option {option} is not '{expected}': {path}"
            )
    columns = tuple(
        row[1] for row in conn.execute("PRAGMA table_info(book_fts)").fetchall()
    )
    if columns != _FTS_COLUMNS:
        raise IndexBuildError(
            f"index table book_fts has unexpected columns {columns}: {path}"
        )


def _build(path: Path, books: Iterable[Book], meta: IndexMeta) -> None:
    """Создать схему и записать все данные в одном соединении."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(_CREATE_META)
        conn.execute(_CREATE_BOOK)
        conn.execute(_CREATE_FTS)
        conn.execute("BEGIN IMMEDIATE")
        for book in books:
            conn.execute(_INSERT_BOOK, _book_row(book))
        # Счётчики окончательны только после исчерпания потока книг.
        meta.stats.check()
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES(?, ?)", _meta_rows(meta)
        )
        conn.execute("INSERT INTO book_fts(book_fts) VALUES('rebuild')")
        # rank=1: проверяется не только внутренняя целостность FTS, но и
        # согласованность индекса с внешней content-table ``book``.
        conn.execute(
            "INSERT INTO book_fts(book_fts, rank) VALUES('integrity-check', 1)"
        )
        conn.execute("COMMIT")
    finally:
        # При ошибке до COMMIT незавершённая транзакция откатывается.
        conn.close()


def _book_row(book: Book) -> tuple:
    """Значения одной книги для ``_INSERT_BOOK``; JSON — компактный UTF-8."""
    return (
        book.archive,
        book.member,
        book.file_id,
        book.author,
        book.title,
        book.language,
        json.dumps([(s.name, s.number) for s in book.series], **_JSON_KWARGS),
        book.size,
        book.libid,
        json.dumps(book.metadata, **_JSON_KWARGS),
        normalize(book.author),
        normalize(book.title),
    )


def _meta_rows(meta: IndexMeta) -> list[tuple[str, str]]:
    """Пары «ключ, значение» таблицы ``meta``; версии — от этого модуля."""
    stats = {
        field: getattr(meta.stats, field) for field in _STATS_FIELDS
    }
    return [
        ("schema_version", str(SCHEMA_VERSION)),
        ("rules_version", str(RULES_VERSION)),
        ("root", meta.root),
        ("catalog_rel", meta.catalog_rel),
        ("sha256", meta.sha256),
        ("built_at", meta.built_at),
        ("stats", json.dumps(stats, **_JSON_KWARGS)),
    ]


def _connect_ro(path: Path) -> sqlite3.Connection:
    """Открыть индекс только для чтения через URI, построенный ``as_uri``."""
    if not path.is_absolute():
        raise IndexBuildError(f"index path must be absolute: {path}")
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def _parse_meta(values: Mapping[str, object], path: Path) -> IndexMeta:
    """Восстановить ``IndexMeta`` из строк таблицы ``meta`` со проверками."""
    missing = [key for key in _META_KEYS if key not in values]
    if missing:
        raise IndexBuildError(f"index metadata is missing keys {missing}: {path}")
    # SQLite динамически типизирован: NULL или BLOB в meta.value — штатное
    # повреждение, а не ошибка вызова; проверка типов идёт до любого разбора.
    non_text = [
        key
        for key in _META_KEYS
        if not isinstance(values[key], str)
    ]
    if non_text:
        raise IndexBuildError(
            f"index metadata keys {non_text} have non-text values: {path}"
        )
    schema_version = _parse_index_int(values["schema_version"], "schema_version", path)
    rules_version = _parse_index_int(values["rules_version"], "rules_version", path)
    for key in ("root", "catalog_rel", "sha256", "built_at"):
        if not values[key]:
            raise IndexBuildError(f"index metadata key {key!r} is empty: {path}")
    if not _SHA256_HEX.fullmatch(values["sha256"]):
        raise IndexBuildError(
            f"index metadata 'sha256' is not a lowercase SHA-256 hex digest: {path}"
        )
    try:
        datetime.fromisoformat(values["built_at"])
    except ValueError as exc:
        raise IndexBuildError(
            f"index metadata 'built_at' is not an ISO 8601 timestamp: {path}"
        ) from exc
    return IndexMeta(
        root=values["root"],
        catalog_rel=values["catalog_rel"],
        sha256=values["sha256"],
        schema_version=schema_version,
        rules_version=rules_version,
        built_at=values["built_at"],
        stats=_parse_stats(values["stats"], path),
    )


def _parse_stats(payload: str, path: Path) -> ImportStats:
    """Разобрать JSON счётчиков и проверить их инвариант."""
    try:
        stats = json.loads(payload)
    except ValueError as exc:
        raise IndexBuildError(f"index stats is not valid JSON: {path}") from exc
    if not isinstance(stats, dict):
        raise IndexBuildError(f"index stats is not a JSON object: {path}")
    fields: dict[str, int] = {}
    for field in _STATS_FIELDS:
        value = stats.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise IndexBuildError(
                f"index stats field {field!r} is not a non-negative integer: {path}"
            )
        fields[field] = value
    parsed = ImportStats(**fields)
    try:
        parsed.check()
    except AssertionError as exc:
        raise IndexBuildError(f"index stats invariant is violated: {path}") from exc
    return parsed


def _parse_index_int(value: str, key: str, path: Path) -> int:
    """Разобрать целочисленное метаполе, записанное как текст."""
    if not _NON_NEGATIVE_INT.fullmatch(value):
        raise IndexBuildError(
            f"index metadata {key!r} is not a non-negative integer: {path}"
        )
    return int(value)


def _discard(path: Path) -> None:
    """Удалить файлы неудавшейся постройки.

    Вызывается только владельцем: путь создан через ``O_EXCL``, а
    существовавшие чужие sidecar-файлы отвергнуты до постройки, поэтому
    перечисленные соседи могли появиться только от собственного соединения.
    """
    for suffix in ("",) + _SIDECAR_SUFFIXES:
        try:
            Path(f"{path}{suffix}").unlink(missing_ok=True)
        except OSError:
            # Чистка best-effort: первопричина ошибки важнее остатков.
            pass
