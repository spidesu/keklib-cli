"""Поисковая сессия под общей блокировкой индекса (§8 спецификации).

``open_search`` — единственная точка входа: он доводит индекс до актуального
состояния (``ensure_index``), берёт общую блокировку чтения, повторно
проверяет метаданные/хеш каталога/версии уже под ней и только затем открывает
read-only соединение. Сессия живёт до выхода из контекстного менеджера:
совместимые читатели работают одновременно, а пересборка (исключительная
блокировка) невозможна, поэтому страницы и номера книг относятся к одному
снимку индекса — «каталог не обновляется посреди выбора». SQLite-соединение
закрывается до освобождения блокировки, включая ошибки и отмену, что
существенно для Windows.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from keklib_cli.errors import SearchError
from keklib_cli.indexer import (
    INDEX_LOCK_NAME,
    _current_meta,
    _reject_foreign_sidecars,
    ensure_index,
)
from keklib_cli.models import Book, Library, SearchPage, Series, StatePaths
from keklib_cli.platforms import index_lock
from keklib_cli.text import build_match, normalize, query_tokens

# Приватные помощники индексатора (_current_meta, _reject_foreign_sidecars)
# переиспользуются намеренно: повторная проверка свежести и отказ по чужим
# sidecar-записям обязаны совпадать с семантикой индексатора дословно; вторая
# копия этой логики в поиске опаснее видимого подчёркивания.

__all__ = ["PAGE_SIZE", "SearchSession", "open_search"]

#: Книг на странице выдачи (§8).
PAGE_SIZE = 20
#: Попыток «ensure → общая блокировка → повторная проверка» до понятного отказа.
_OPEN_ATTEMPTS = 2

_COUNT_SQL = "SELECT count(*) FROM book_fts WHERE book_fts MATCH ?"
_SELECT_SQL = (
    "SELECT book.archive, book.member, book.file_id, book.author, book.title,"
    " book.language, book.series_json, book.size, book.libid,"
    " book.metadata_json FROM book"
    " JOIN book_fts ON book_fts.rowid = book.id"
    " WHERE book_fts MATCH ?"
    " ORDER BY (book.norm_title = ?) DESC, bm25(book_fts),"
    " book.author, book.title, book.archive, book.member"
    " LIMIT ? OFFSET ?"
)


class SearchSession:
    """Поиск по одному снимку индекса; создаётся только :func:`open_search`.

    Соединение открывается в режиме только для чтения и живёт, пока жива
    сессия; все вызовы :meth:`search` читают один и тот же опубликованный
    индекс, поэтому нумерация книг и страниц стабильна внутри сессии.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def search(self, query: str, *, page: int = 0) -> SearchPage:
        """Вернуть страницу результатов ``query``; нумерация страниц с нуля.

        Все слова запроса обязательны и ищутся префиксами слов в авторе или
        названии. Точное совпадение нормализованного названия с
        нормализованным запросом ранжируется выше релевантности FTS5, далее
        автор, название, архив и имя файла для устойчивого порядка. ``COUNT``
        и строки страницы выполняются в одной read-only транзакции, поэтому
        ``total`` и ``books`` согласованы между собой.

        :raises SearchError: запрос не содержит слов, номер страницы
            отрицателен, индекс не читается или содержит нечитаемые данные.
        """
        tokens = query_tokens(query)
        if not tokens:
            raise SearchError(f"в запросе нет слов для поиска: {query!r}")
        if page < 0:
            raise SearchError(f"номер страницы не может быть отрицательным: {page}")
        match = build_match(tokens)
        exact = normalize(query)
        try:
            total, rows = self._read(match, exact, page)
        except sqlite3.Error as exc:
            raise SearchError(f"поиск по индексу не выполнен: {exc}") from exc
        try:
            books = tuple(_row_to_book(row) for row in rows)
        except (TypeError, ValueError) as exc:
            raise SearchError(f"индекс содержит нечитаемую книгу: {exc}") from exc
        return SearchPage(books=books, page=page, total=total)

    def _read(self, match: str, exact: str, page: int) -> tuple[int, list[tuple]]:
        """``COUNT`` и страница в одной транзакции — единый снимок чтения.

        ``COMMIT`` выполняется внутри защищённой области: при его отказе
        выполняется best-effort ``ROLLBACK`` (первопричина важнее ошибки
        отката read-only транзакции), поэтому повторный поиск той же
        сессии не падает на «transaction within a transaction».
        """
        self._connection.execute("BEGIN")
        try:
            total = self._connection.execute(_COUNT_SQL, (match,)).fetchone()[0]
            rows = self._connection.execute(
                _SELECT_SQL, (match, exact, PAGE_SIZE, page * PAGE_SIZE)
            ).fetchall()
            self._connection.execute("COMMIT")
        except BaseException:
            try:
                self._connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # первопричина важнее отката read-only транзакции
            raise
        return total, rows


@contextmanager
def open_search(
    library: Library,
    paths: StatePaths,
    *,
    progress: Callable[[str], None] | None = None,
) -> Iterator[SearchSession]:
    """Открыть поисковую сессию по актуальному индексу (§8).

    ``ensure_index`` доводит индекс до актуальности и освобождает свои
    блокировки до возврата, затем берётся общая блокировка чтения и
    метаданные/хеш/версии проверяются заново уже под ней: смена источника
    между ensure и открытием не пропускается. При обнаружении смены общая
    блокировка освобождается до повторного ensure (никакого повышения
    блокировки); после двух безуспешных попыток —
    ``SearchError``. Чужие sidecar-записи рядом с индексом отвергаются до
    открытия, как в индексаторе. Read-only соединение открывается только
    после всех проверок и закрывается до освобождения блокировки, включая
    ошибки и отмену в теле ``with``.

    :param library: проверенная библиотека (задача 1).
    :param paths: нативные пути состояния; индекс — ``paths.index``.
    :param progress: необязательный приёмник сообщений стадий ``ensure_index``.
    :raises IndexBusyError: пересборка потребовала exclusive-блокировку,
        занятую другим процессом.
    :raises CatalogError: живой INPX не читается или каталог повреждён.
    :raises IndexBuildError: постройка/валидация/публикация не удалась, в том
        числе рядом с индексом есть чужие sidecar-записи.
    :raises SearchError: источник изменяется дольше двух попыток либо готовый
        индекс не открылся для чтения.
    """
    lock_path = paths.index.parent / INDEX_LOCK_NAME
    for _attempt in range(_OPEN_ATTEMPTS):
        ensure_index(library, paths, progress=progress)
        with index_lock(lock_path, exclusive=False):
            _reject_foreign_sidecars(paths.index)
            if _current_meta(library, paths) is None:
                # Источник изменился уже после ensure: освободить shared до
                # повторной пересборки, повышение блокировки запрещено.
                continue
            try:
                connection = _connect(paths.index)
            except sqlite3.Error as exc:
                raise SearchError(
                    f"не удалось открыть индекс для чтения {paths.index}: {exc}"
                ) from exc
            try:
                yield SearchSession(connection)
            finally:
                # Строго до освобождения shared — на всех ОС, включая Windows.
                connection.close()
            return
    raise SearchError(
        f"каталог библиотеки изменяется, поисковая сессия не открыта за "
        f"{_OPEN_ATTEMPTS} попытки; повторите поиск после завершения "
        f"обновления библиотеки: {library.catalog}"
    )


def _connect(path: Path) -> sqlite3.Connection:
    """Открыть индекс только для чтения; транзакциями управляет сессия.

    URI собирается ``Path.as_uri()`` (корректные ``file:``-URI на всех ОС);
    ``isolation_level=None`` — авточтранзакции отключены, ``BEGIN``/``COMMIT``
    выполняет :meth:`SearchSession._read`.
    """
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None)


def _row_to_book(row: tuple) -> Book:
    """Восстановить точную ``Book`` из строки индекса.

    Серии и метаданные читаются из JSON-столбцов; структура проверяется,
    потому что ``validate_index`` гарантирует схему и счётчики, но не
    разбирает JSON каждой строки.

    :raises ValueError: столбец или JSON не соответствует ожидаемой форме.
    """
    (
        archive,
        member,
        file_id,
        author,
        title,
        language,
        series_json,
        size,
        libid,
        metadata_json,
    ) = row
    for name, value in (
        ("archive", archive),
        ("member", member),
        ("file_id", file_id),
        ("author", author),
        ("title", title),
        ("language", language),
        ("libid", libid),
    ):
        if not isinstance(value, str):
            raise ValueError(f"столбец {name} не является текстом: {value!r}")
    series_payload = json.loads(series_json)
    if not isinstance(series_payload, list):
        raise ValueError(f"series_json не список: {series_payload!r}")
    series: list[Series] = []
    for pair in series_payload:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(item, str) for item in pair)
        ):
            raise ValueError(f"элемент series_json не пара [имя, номер]: {pair!r}")
        series.append(Series(name=pair[0], number=pair[1]))
    metadata_payload = json.loads(metadata_json)
    if not isinstance(metadata_payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in metadata_payload.items()
    ):
        raise ValueError(
            f"metadata_json не объект «текст → текст»: {metadata_payload!r}"
        )
    if size is not None and (isinstance(size, bool) or not isinstance(size, int)):
        raise ValueError(f"size не целое число и не NULL: {size!r}")
    return Book(
        archive=archive,
        member=member,
        file_id=file_id,
        author=author,
        title=title,
        language=language,
        series=tuple(series),
        size=size,
        libid=libid,
        metadata=metadata_payload,
    )
