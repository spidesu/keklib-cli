"""Типизированные записи, передаваемые между компонентами keklib-cli.

Компоненты не обмениваются сырыми строками INP: чтение каталога превращает
записи в ``Book``, индексирование возвращает ``IndexMeta``/``IndexResult``,
поиск — ``SearchPage``. Пути в ``Library`` и ``StatePaths`` — абсолютные.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Library:
    """Проверенная библиотека: корень, INPX-каталог и каталог архивов."""

    root: Path
    catalog: Path
    archives: Path


@dataclass(frozen=True, slots=True)
class StatePaths:
    """Пути состояния в нативных пользовательских каталогах."""

    config: Path
    cache: Path
    index: Path


@dataclass(frozen=True, slots=True)
class Series:
    """Серия и номер книги в ней (номер — строка из INP)."""

    name: str
    number: str


@dataclass(slots=True)
class Book:
    """Книга одной строки INP.

    ``archive`` — простое имя ZIP относительно ``Library.archives``;
    ``member`` — имя FB2 внутри ZIP; ``series`` — серии книги;
    ``size`` — размер из INP или ``None``; ``libid`` — идентичность записи;
    ``metadata`` — исходные поля INP без ``SERIES``/``SERNO``.
    """

    archive: str
    member: str
    file_id: str
    author: str
    title: str
    language: str
    series: tuple[Series, ...]
    size: int | None
    libid: str
    metadata: dict[str, str]


@dataclass(slots=True)
class ImportStats:
    """Счётчики одного импорта; инвариант: rows == excluded + duplicates + books."""

    rows: int = 0
    excluded: int = 0
    duplicates: int = 0
    books: int = 0

    def check(self) -> None:
        """Проверить инвариант счётчиков; raises AssertionError при нарушении."""
        total = self.excluded + self.duplicates + self.books
        if self.rows != total:
            raise AssertionError(
                f"import stats invariant violated: rows={self.rows} != "
                f"excluded+duplicates+books={total}"
            )


@dataclass(slots=True)
class IndexMeta:
    """Метаданные построенного индекса.

    ``root`` — абсолютный путь корня библиотеки (строкой, для хранения);
    ``catalog_rel`` — относительный путь INPX; ``sha256`` — отпечаток каталога;
    ``built_at`` — момент постройки (ISO 8601, UTC).
    """

    root: str
    catalog_rel: str
    sha256: str
    schema_version: int
    rules_version: int
    built_at: str
    stats: ImportStats


@dataclass(slots=True)
class IndexResult:
    """Результат операции с индексом и его метаданные."""

    path: Path
    rebuilt: bool
    meta: IndexMeta


@dataclass(slots=True)
class SearchPage:
    """Страница результатов поиска; нумерация страниц начинается с нуля."""

    books: tuple[Book, ...]
    page: int
    total: int
