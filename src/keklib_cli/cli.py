"""Точка входа keklib-cli: команды setup/index/search (§4 спецификации).

``main`` получает потоки и пути состояния явно и возвращает код завершения:
0 — успех, ``q``/EOF без выбора или пустая выдача; 1 — предметная
(``UserError``) или IO-ошибка; 2 — неверные аргументы, пустой запрос или
неинтерактивный stdin; 130 — Ctrl+C. Общего ``except Exception`` нет:
программные ошибки завершаются traceback, а не скрываются диагностикой.

Вывод делится на доверенные тексты (справка/usage argparse, строки таблицы)
и динамические (пути, диагностика, прогресс): доверенные записываются через
безопасную кодировку с сохранением структуры, динамические дополнительно
санитизируются от Unicode C* (ESC/bidi/переводы строк) — во всех
пользовательских каналах, включая argparse. Проверка запроса и
интерактивности выполняется до каких-либо обращений к состоянию и индексу.
Таблица результатов и итоговый путь печатаются в ``stdout`` (его разрешено
перенаправлять); прогресс, приглашения и диагностика — в ``stderr``. Пути
библиотеки и назначения раскрывают ``~`` приложением, а не только оболочкой.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn, TextIO

from keklib_cli.config import inspect_library, load_library, save_library
from keklib_cli.errors import UserError
from keklib_cli.indexer import ensure_index
from keklib_cli.inpx import read_structure
from keklib_cli.models import StatePaths
from keklib_cli.output import DEFAULT_DESTINATION_NAME, extract_book
from keklib_cli.platforms import state_paths
from keklib_cli.search import open_search
from keklib_cli.terminal import (
    # Приватный _encodable переиспользуется намеренно: единая замена
    # непредставимых символов не должна расходиться между argparse и UI.
    _encodable,
    choose_book,
    display_text,
    write_sanitized_line,
)
from keklib_cli.text import query_tokens

PROG = "keklib-cli"

#: Успех, ``q``/EOF без выбора, пустая выдача.
EXIT_OK = 0
#: Предметная (``UserError``) или IO-ошибка.
EXIT_ERROR = 1
#: Неверные аргументы, пустой запрос, неинтерактивный stdin (как в argparse).
EXIT_USAGE = 2
#: Ctrl+C во время операции.
EXIT_INTERRUPTED = 130


class _SafeWriter:
    """Поток argparse: безопасная кодировка без санитизации доверенных текстов.

    Справка и usage — собственные многострочные тексты: их переводы строк
    структурны и сохраняются; санитизация не применяется. Непредставимые в
    кодировке потока символы заменяются до записи, поэтому узкая кодировка
    не даёт traceback. Каждая запись сбрасывается: буферизованный/
    перенаправленный поток показывает usage/ошибку без внешнего flush.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        payload = _encodable(text, self._stream)
        self._stream.write(payload)
        self._stream.flush()
        return len(payload)

    def flush(self) -> None:
        self._stream.flush()

    @property
    def encoding(self) -> str | None:
        return getattr(self._stream, "encoding", None)


class _CommandParser(argparse.ArgumentParser):
    """Parser, чьи сообщения об ошибках санитизируют данные пользователя.

    Сообщение ошибки argparse содержит значения argv (invalid choice,
    unrecognized arguments) — динамический текст: ESC/bidi/переводы строк
    заменяются пробелами. usage печатается отдельно и, как и справка,
    доверен. Класс наследуется всеми подкомандами автоматически
    (``parser_class`` по умолчанию — тип родителя).
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {display_text(message)}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = _CommandParser(
        prog=PROG,
        description="Поиск в локальной библиотеке Lib.rus.ec и безопасное "
        "извлечение выбранных книг в FB2.",
    )
    commands = parser.add_subparsers(
        title="commands", dest="command", metavar="command", required=True
    )
    setup = commands.add_parser(
        "setup", help="проверить корень библиотеки и сохранить настройку"
    )
    setup.add_argument(
        "--library", required=True, help="путь к корню библиотеки (допускает ~)"
    )
    index = commands.add_parser(
        "index", help="привести поисковый индекс в актуальное состояние"
    )
    index.add_argument(
        "--refresh",
        action="store_true",
        required=True,
        help="принудительно перестроить индекс даже без изменений INPX",
    )
    search = commands.add_parser(
        "search", help="найти книги и извлечь выбранную по номеру"
    )
    search.add_argument(
        "query", help="поисковый запрос: все слова обязательны, префиксный поиск"
    )
    search.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help="каталог назначения (по умолчанию ~/Books; допускает ~)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    paths: StatePaths | None = None,
) -> int:
    """Разобрать аргументы, выполнить команду и вернуть код завершения.

    :param argv: аргументы командной строки; ``None`` означает ``sys.argv``.
    :param stdin: поток выбора номера; должен быть интерактивным для search.
    :param stdout: таблица результатов и итоговый путь; перенаправляем.
    :param stderr: прогресс, приглашения и диагностика.
    :param paths: пути состояния; ``None`` — нативные ``state_paths()``
        (внедряется тестами, чтобы не затрагивать пользовательские каталоги).
    """
    in_stream = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    parser = _build_parser()
    try:
        with contextlib.redirect_stdout(
            _SafeWriter(out)
        ), contextlib.redirect_stderr(_SafeWriter(err)):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse: 0 (справка) или 2 (неверные аргументы); текст уже в
        # потоках через _SafeWriter/_CommandParser.
        if exc.code is None:
            return EXIT_OK
        if isinstance(exc.code, int):
            return exc.code
        write_sanitized_line(err, str(exc.code))
        return EXIT_ERROR
    try:
        if args.command == "setup":
            return _setup(args, err=err, paths=paths)
        if args.command == "index":
            return _index(args, err=err, paths=paths)
        return _search(args, stdin=in_stream, out=out, err=err, paths=paths)
    except KeyboardInterrupt:
        write_sanitized_line(err, "отменено пользователем (Ctrl+C)")
        return EXIT_INTERRUPTED
    except UserError as exc:
        write_sanitized_line(err, f"keklib-cli: ошибка: {exc}")
        return EXIT_ERROR
    except OSError as exc:
        write_sanitized_line(err, f"keklib-cli: ошибка ввода-вывода: {exc}")
        return EXIT_ERROR


def _setup(args: argparse.Namespace, *, err: TextIO, paths: StatePaths | None) -> int:
    """setup --library: проверка корня и структуры INPX до сохранения.

    ``inspect_library`` и ``read_structure`` полностью выполняются до
    ``save_library``: при плохом INPX прежняя настройка не перезаписывается.
    """
    resolved = paths if paths is not None else state_paths()
    library = inspect_library(Path(args.library))
    read_structure(library.catalog)
    save_library(resolved, library)
    write_sanitized_line(err, f"библиотека настроена: {library.root}")
    write_sanitized_line(err, f"конфигурация: {resolved.config}")
    return EXIT_OK


def _index(args: argparse.Namespace, *, err: TextIO, paths: StatePaths | None) -> int:
    """index --refresh: принудительная пересборка индекса с прогрессом в stderr."""
    resolved = paths if paths is not None else state_paths()
    library = load_library(resolved)
    result = ensure_index(
        library,
        resolved,
        force=args.refresh,
        progress=lambda message: write_sanitized_line(err, message),
    )
    state = "перестроен" if result.rebuilt else "актуален без пересборки"
    write_sanitized_line(err, f"индекс {state}: {result.path}")
    return EXIT_OK


def _search(
    args: argparse.Namespace,
    *,
    stdin: TextIO,
    out: TextIO,
    err: TextIO,
    paths: StatePaths | None,
) -> int:
    """search QUERY [--output PATH]: поиск, выбор по номеру, извлечение.

    Пустой запрос и неинтерактивный stdin отклоняются с кодом 2 до каких-либо
    обращений к состоянию и индексу. Извлечение выполняется внутри поисковой
    сессии (общая блокировка держится до закрытия — «каталог не обновляется
    посреди выбора»); итоговый абсолютный путь книги печатается в stdout.
    """
    query = args.query
    if not query_tokens(query):
        write_sanitized_line(err, f"в запросе нет слов для поиска: {query!r}")
        return EXIT_USAGE
    if not stdin.isatty():
        write_sanitized_line(
            err,
            "для выбора книги нужен интерактивный терминал (stdin не TTY); "
            "машинный интерфейс в первой версии не поддерживается",
        )
        return EXIT_USAGE
    resolved = paths if paths is not None else state_paths()
    library = load_library(resolved)
    if args.output is not None:
        destination = Path(args.output).expanduser()
    else:
        destination = Path.home() / DEFAULT_DESTINATION_NAME
    with open_search(
        library, resolved, progress=lambda message: write_sanitized_line(err, message)
    ) as session:
        book = choose_book(session, query, stdin=stdin, stdout=out, stderr=err)
        if book is None:
            write_sanitized_line(err, "книга не выбрана; извлечения не было")
            return EXIT_OK
        target = extract_book(library, book, destination)
    published = target if target.is_absolute() else Path.cwd() / target
    write_sanitized_line(out, str(published))
    return EXIT_OK
