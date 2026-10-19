"""Переносимое имя файла книги и атомарная no-replace публикация (§9).

``safe_filename`` строит имя ``<автор> — <название> [<ID>].fb2`` по правилам,
одинаковым на всех ОС (переносимость извлечённых имён): запрет разделителей
пути и символов Windows, символов Unicode-категорий C* (управляющие, форматные,
суррогаты, private use, неназначенные), зарезервированных имён
устройств и недопустимых хвостов; ограничение длины в байтах UTF-8 без
разрыва кодовой точки с сохранением идентификатора и расширения.

``extract_book`` потоково читает точный файл книги через ``archive.open_book``
во временный файл каталога назначения, дочитывает CRC-32 до EOF, делает
flush/fsync и закрывает дескрипторы; публикация — платформенный
``publish_no_replace`` уже после закрытия temp и источника. Существующая
цель не перезаписывается и не удаляется ни при каком восстановлении; при
ошибке, нехватке места или отмене не остаётся ни опубликованного частичного
FB2, ни своего временного файла.
"""

from __future__ import annotations

import errno
import os
import re
import tempfile
import unicodedata
from pathlib import Path

from keklib_cli.archive import open_book
from keklib_cli.errors import ExtractionError, TargetExistsError
from keklib_cli.models import Book, Library
from keklib_cli.platforms import publish_no_replace

__all__ = ["DEFAULT_DESTINATION_NAME", "safe_filename", "extract_book"]

#: Расширение извлекаемой книги.
DEFAULT_EXTENSION = ".fb2"

#: Имя подкаталога домашнего каталога, которое CLI использует по умолчанию
#: (§9: назначение по умолчанию — ``~/Books``; сам каталог задаёт CLI).
DEFAULT_DESTINATION_NAME = "Books"

#: Разделитель автора и названия (§9.4).
_AUTHOR_TITLE_SEPARATOR = " — "

#: Читаемое разделение авторов в имени файла.
_AUTHOR_SEPARATOR = "; "

#: Запрещённые символы: разделители пути и символы Windows — заменяются
#: пробелом (§9.4). Отдельно от них заменяются все символы Unicode-категорий
#: C* (см. ``_is_unsafe_char``).
_FORBIDDEN_CHARS = frozenset('/\\<>:"|?*')

#: Схлопывание пробелов после замены.
_WHITESPACE = re.compile(r"\s+")

#: Зарезервированные имена устройств Windows (без учёта регистра; имя файла
#: до первой точки, поэтому «CON.fb2» так же недопустимо, как «CON»).
#: Windows резервирует и надстрочные формы COM¹/COM²/COM³, LPT¹/LPT²/LPT³.
_RESERVED_WINDOWS = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
     "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

#: Недопустимые хвосты имени на Windows.
_INVALID_TAIL = " ."

#: Windows: превышение ограничения длины пути/имени (ERROR_FILENAME_EXCED_RANGE).
_WINERROR_FILENAME_EXCED_RANGE = 206

#: Размер блока потокового копирования.
_CHUNK_BYTES = 1024 * 1024

#: Префикс/суффикс собственного временного файла в каталоге назначения.
_TEMP_PREFIX = ".keklib-"
_TEMP_SUFFIX = ".part"


def safe_filename(book: Book, *, max_bytes: int = 240) -> str:
    """Переносимое имя файла для книги: ``<автор> — <название> [<ID>].fb2``.

    Правила одинаковы на всех ОС (§9.4): AUTHOR в форме
    ``Фамилия,Имя,Отчество`` (несколько авторов через ``:``) преобразуется в
    читаемые имена ``Имя Отчество Фамилия``, разделённые ``; ``; запрещённые
    символы и Unicode C* заменяются пробелами, пробелы схлопываются,
    недопустимые хвосты и зарезервированные имена устройств Windows
    исключаются; идентификатор проходит те же проверки. Длина всего имени
    ограничивается ``max_bytes`` байтами UTF-8 без разрыва кодовой точки с
    сохранением хвоста `` [<ID>].fb2``. При пустых метаданных имя — безопасный
    идентификатор: ``<ID>.fb2``.

    :param book: выбранная книга; используются ``author``, ``title``, ``file_id``.
    :param max_bytes: предел длины имени в байтах UTF-8.
    :return: имя файла без каталога, безопасное на Linux, Windows и macOS.
    :raises ExtractionError: идентификатор не образует безопасного имени или
        слишком длинный: даже ``<ID>.fb2`` не помещается в ``max_bytes``.
    """
    file_id = _sanitize_id(book.file_id)
    fallback = f"{file_id}{DEFAULT_EXTENSION}"
    if len(fallback.encode("utf-8")) > max_bytes:
        raise ExtractionError(
            f"идентификатор книги {file_id!r} слишком длинный: даже имя "
            f"{fallback!r} превышает предел {max_bytes} байт UTF-8"
        )
    author = _readable_author(book.author)
    title = _sanitize_text(book.title)
    parts = [part for part in (author, title) if part]
    if not parts:
        name = fallback
    else:
        head = _AUTHOR_TITLE_SEPARATOR.join(parts)
        tail = f" [{file_id}]{DEFAULT_EXTENSION}"
        budget = max_bytes - len(tail.encode("utf-8"))
        head = _truncate_utf8(head, budget)
        head = _strip_separator_tail(head)
        name = f"{head}{tail}" if head else fallback
    if _is_reserved_windows(name):
        name = f"_{name}"
    if len(name.encode("utf-8")) > max_bytes:
        raise ExtractionError(
            f"не удалось уложить безопасное имя файла в предел {max_bytes} "
            f"байт UTF-8 (идентификатор {file_id!r})"
        )
    return name


def extract_book(library: Library, book: Book, destination: Path) -> Path:
    """Извлечь книгу в ``destination`` без перезаписи существующих файлов.

    Каталог назначения создаётся при отсутствии. Точный файл книги читается
    потоком из её ZIP (``archive.open_book``: повторные проверки имён,
    containment и записи) в собственный временный файл того же каталога;
    чтение до EOF завершает проверку CRC-32, затем flush/fsync и закрытие
    temp и источника — только после этого платформенная публикация
    ``publish_no_replace``. Существующее имя цели (включая symlink и гонку с
    другим процессом) оставляет цель неизменной. При любой ошибке, нехватке
    места или отмене удаляется только собственный временный файл; частичный
    FB2 не публикуется, цель не удаляется.

    :param library: проверенная библиотека (задача 1).
    :param book: выбранная книга.
    :param destination: каталог назначения (CLI подставляет ``~/Books``).
    :return: путь к опубликованному файлу.
    :raises TargetExistsError: файл уже существует; книга не перезаписана.
    :raises ExtractionError: каталог недоступен, источник не читается
        (включая CRC), диск недоступен для записи или файловая система не
        поддерживает безопасную публикацию.
    """
    destination = Path(destination)
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _mkdir_error(destination, exc) from exc
    filename = safe_filename(book)
    target = destination / filename
    stage = "открытия источника"
    temp: Path | None = None
    try:
        with open_book(library, book) as stream:
            stage = "создания временного файла"
            descriptor, temp_name = tempfile.mkstemp(
                dir=destination, prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX
            )
            temp = Path(temp_name)
            stage = "записи книги"
            with os.fdopen(descriptor, "wb") as sink:
                while True:
                    chunk = stream.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    sink.write(chunk)
                sink.flush()
                os.fsync(sink.fileno())
        # temp и источник закрыты; публикация — единственная точка,
        # где появляется имя цели.
        stage = "публикации книги"
        publish_no_replace(temp, target)
    except TargetExistsError as exc:
        raise TargetExistsError(
            f"{target}: файл уже существует, книга не перезаписана; "
            f"для другого каталога повторите поиск с --output"
        ) from exc
    except OSError as exc:
        raise _write_error(destination, target, stage, exc) from exc
    finally:
        if temp is not None:
            # Только свой temp и только при незавершённой публикации;
            # успешная публикация уже забрала его (link→unlink или rename).
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    return target


def _is_unsafe_char(char: str) -> bool:
    """Символ подлежит замене: запрещённый разделитель/Windows-символ или
    любая Unicode-категория C* — Cc (управляющие C0/C1), Cf (форматные,
    включая U+202E), Cs (суррогаты — иначе ``encode('utf-8')`` падает),
    Co (private use), Cn (неназначенные)."""
    return char in _FORBIDDEN_CHARS or unicodedata.category(char)[0] == "C"


def _sanitize_text(value: str) -> str:
    """Заменить запрещённые символы и Unicode C* пробелами, схлопнуть, убрать хвосты."""
    replaced = "".join(" " if _is_unsafe_char(char) else char for char in value)
    collapsed = _WHITESPACE.sub(" ", replaced).strip()
    return collapsed.strip(_INVALID_TAIL)


def _readable_author(author: str) -> str:
    """Преобразовать AUTHOR в читаемые имена авторов.

    Форма INP — ``Фамилия,Имя,Отчество`` (запятые внутри, ``:`` между
    авторами): каждый автор превращается в ``Имя Отчество Фамилия``, авторы
    соединяются ``; ``. Части проходят ту же санитизацию, что и название.
    """
    names: list[str] = []
    for raw_author in author.split(":"):
        parts = [part for part in map(_sanitize_text, raw_author.split(",")) if part]
        if len(parts) >= 2:
            names.append(f"{' '.join(parts[1:])} {parts[0]}")
        elif parts:
            names.append(parts[0])
    return _AUTHOR_SEPARATOR.join(names)


def _sanitize_id(file_id: str) -> str:
    """Проверить/обезопасить идентификатор: те же правила, что и для текста.

    :raises ExtractionError: идентификатор не образует непустого безопасного
        имени (состоит из запрещённых символов).
    """
    ident = _sanitize_text(file_id)
    if not ident:
        raise ExtractionError(
            f"идентификатор книги {file_id!r} не образует безопасного имени файла"
        )
    return ident


def _truncate_utf8(text: str, limit: int) -> str:
    """Обрезать до ``limit`` байт UTF-8 без разрыва кодовой точки."""
    if limit <= 0:
        return ""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    # Незавершённая кодовая точка на границе отбрасывается декодером.
    return data[:limit].decode("utf-8", errors="ignore")


def _strip_separator_tail(head: str) -> str:
    """Убрать хвостовые пробелы/точки и осиротевший разделитель « — »."""
    stripped = head.rstrip(_INVALID_TAIL)
    if stripped.endswith(_AUTHOR_TITLE_SEPARATOR.strip()):
        stripped = stripped[: -len(_AUTHOR_TITLE_SEPARATOR.strip())].rstrip(
            _INVALID_TAIL
        )
    return stripped


def _is_reserved_windows(name: str) -> bool:
    """Имя до первой точки совпадает с устройством Windows (без регистра)."""
    base = name.split(".", 1)[0].rstrip(_INVALID_TAIL)
    return base.upper() in _RESERVED_WINDOWS


def _is_length_error(exc: OSError) -> bool:
    """Превышение ограничения длины пути: ENAMETOOLONG или winerror 206."""
    return exc.errno == errno.ENAMETOOLONG or (
        getattr(exc, "winerror", None) == _WINERROR_FILENAME_EXCED_RANGE
    )


def _mkdir_error(destination: Path, exc: OSError) -> ExtractionError:
    """Диагностика отказа создания каталога назначения.

    Ошибка длины пути — адресный совет выбрать более короткий каталог (§9);
    системная политика длинных путей Windows приложением не меняется.
    """
    if _is_length_error(exc):
        return ExtractionError(
            f"превышено ограничение длины пути ОС: не удалось создать каталог "
            f"назначения {destination}: {exc}; выберите более короткий каталог "
            f"назначения"
        )
    return ExtractionError(
        f"не удалось создать каталог назначения {destination}: {exc}; "
        f"проверьте права доступа и, если путь слишком длинный, выберите "
        f"более короткий каталог назначения"
    )


def _write_error(
    destination: Path, target: Path, stage: str, exc: OSError
) -> ExtractionError:
    """Диагностика OSError записи/публикации с этапом и полным путём цели.

    Ошибка длины пути возможна не только при mkdir: каталог может
    помещаться в лимит, а полный путь цели (temp, имя цели публикации) —
    нет; тогда даётся адресный совет выбрать более короткий каталог.
    """
    if _is_length_error(exc):
        return ExtractionError(
            f"превышено ограничение длины пути ОС на этапе {stage}: полный "
            f"путь {target}, каталог назначения {destination}: {exc}; "
            f"выберите более короткий каталог назначения"
        )
    return ExtractionError(
        f"сбой на этапе {stage}: не удалось записать книгу в каталог "
        f"назначения {destination} ({target.name}): {exc}"
    )
