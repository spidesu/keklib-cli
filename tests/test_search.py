"""Тесты поисковой сессии под общей блокировкой (§8 спецификации).

Конкуренция с писателем проверяется настоящими дочерними процессами
(``sys.executable``): готовность и освобождение блокировки — обмен строками
через pipe с защитными таймаутами, без sleep как условия корректности.
Порядок «SQLite-соединение закрыто до освобождения блокировки» проверяется
записью событий из подменённых ``_connect`` и ``index_lock`` модуля поиска.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from keklib_cli import search as search_module
from keklib_cli import text as text_module
from keklib_cli.errors import SearchError
from keklib_cli.models import Book, Series, StatePaths
from keklib_cli.search import PAGE_SIZE, open_search
from keklib_cli.text import build_match, normalize, query_tokens
from tests.fixtures import make_library
from tests.harness.library_fixture import DEFAULT_FIELDS, write_library

_WAIT_SECONDS = 5.0  # защитный предел ожидания события дочернего процесса

# Дочерний процесс: настоящий ensure_index (plain/force). BUSY — IndexBusyError
# (индекс занят читателем), REBUILT — пересобрал, OK — актуален без пересборки.
_ENSURE_WRITER_CHILD = """
import sys
from pathlib import Path

from keklib_cli.config import inspect_library
from keklib_cli.errors import IndexBusyError
from keklib_cli.indexer import ensure_index
from keklib_cli.models import StatePaths

root, config, cache, index, mode = sys.argv[1:6]
library = inspect_library(Path(root))
paths = StatePaths(config=Path(config), cache=Path(cache), index=Path(index))
try:
    result = ensure_index(library, paths, force=(mode == "force"))
except IndexBusyError:
    print("BUSY")
else:
    print("REBUILT" if result.rebuilt else "OK")
"""

# Попытка exclusive без ожидания после закрытия сессии: ACQUIRED — блокировка
# свободна (утечки нет), BUSY — зависла; блокировка освобождается по EOF в stdin.
_ATTEMPT_CHILD = """
import sys
from pathlib import Path

from keklib_cli.errors import IndexBusyError
from keklib_cli.platforms import index_lock

lock_path = Path(sys.argv[1])
try:
    with index_lock(lock_path, exclusive=True):
        sys.stdout.write("ACQUIRED\\n")
        sys.stdout.flush()
        sys.stdin.readline()
except IndexBusyError:
    sys.stdout.write("BUSY\\n")
    sys.stdout.flush()
    sys.exit(0)
sys.stdout.write("RELEASED\\n")
sys.stdout.flush()
"""


def _row(file_id: str, author: str, title: str, **extra: str) -> dict[str, str]:
    """Строка INP активной FB2-книги с необязательными дополнительными полями."""
    record = {
        "AUTHOR": author,
        "TITLE": title,
        "FILE": file_id,
        "DEL": "0",
        "EXT": "fb2",
    }
    record.update(extra)
    return record


def _metadata(row: dict[str, str]) -> dict[str, str]:
    """Метаданные Books: все поля structure.info кроме SERIES/SERNO."""
    return {
        field: str(row.get(field, ""))
        for field in DEFAULT_FIELDS
        if field not in ("SERIES", "SERNO")
    }


def _expected_book(row: dict[str, str], archive: str = "a.zip") -> Book:
    """Точная Book, которую должен получить импортёр из одной строки INP.

    Для объединённых серий передавайте ``series`` явно — здесь только
    одиночная серия, как в одной строке.
    """
    series_name = row.get("SERIES", "")
    return Book(
        archive=archive,
        member=f"{row['FILE']}.{row['EXT']}",
        file_id=row["FILE"],
        author=row["AUTHOR"],
        title=row["TITLE"],
        language=row.get("LANG", ""),
        series=(Series(name=series_name, number=row.get("SERNO", "")),)
        if series_name
        else (),
        size=int(row["SIZE"]) if row.get("SIZE") else None,
        libid=row.get("LIBID", ""),
        metadata=_metadata(row),
    )


_BASIC_GROUPS = {
    "a": [
        _row("1", "Автор Один", "Книга один"),
        _row("2", "Автор Два", "Книга два"),
    ],
}


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "library"
        self.cache = self.base / "cache"
        self.paths = StatePaths(
            config=self.base / "config" / "config.json",
            cache=self.cache,
            index=self.cache / "index.sqlite3",
        )
        self.lock_path = self.cache / "index.lock"

    # Вспомогательные методы: дочерние процессы с ограниченными ожиданиями.

    def _start_child(self, script: str, *args: str) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [sys.executable, "-c", script, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._reap, proc)
        return proc

    def _reap(self, proc: subprocess.Popen[str]) -> None:
        """Гарантированный teardown: EOF в stdin, ожидание, kill при зависании."""
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            proc.wait(timeout=_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=_WAIT_SECONDS)
        finally:
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except (BrokenPipeError, OSError, ValueError):
                        pass

    def _expect_line(
        self,
        proc: subprocess.Popen[str],
        expected: str,
        timeout: float = _WAIT_SECONDS,
    ) -> None:
        """Ограниченное по времени ожидание строки stdout (pipe, без sleep)."""
        assert proc.stdout is not None
        stdout = proc.stdout
        received: list[str] = []

        def _read() -> None:
            received.append(stdout.readline())

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            proc.kill()
            proc.wait(timeout=_WAIT_SECONDS)
            self.fail(
                f"таймаут {timeout} с ожидания строки {expected!r} "
                "от дочернего процесса"
            )
        self.assertEqual(
            (received[0] if received else "").strip(),
            expected,
            "неожиданный ответ дочернего процесса",
        )

    def _release(self, proc: subprocess.Popen[str]) -> None:
        """EOF в stdin → держатель освобождает блокировку и выходит с кодом 0."""
        assert proc.stdin is not None
        proc.stdin.close()
        self._expect_line(proc, "RELEASED")
        self.assertEqual(proc.wait(timeout=_WAIT_SECONDS), 0)

    def _run_writer(self, mode: str) -> str:
        """Настоящий ensure_index в дочернем процессе; ответ по stdout."""
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _ENSURE_WRITER_CHILD,
                str(self.root),
                str(self.paths.config),
                str(self.paths.cache),
                str(self.paths.index),
                mode,
            ],
            capture_output=True,
            text=True,
            timeout=_WAIT_SECONDS * 4,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    # Тесты брифа.

    def test_required_prefix_tokens(self) -> None:
        """Все слова запроса обязательны; каждое — префикс слова; точная Book."""
        groups = {
            "a": [
                _row("1", "Булгаков Михаил", "Мастер и Маргарита"),
                _row("2", "Автор Пивной", "Мастерство пива"),
            ]
        }
        library = make_library(self.root, groups)
        with open_search(library, self.paths) as session:
            both = session.search("мастер")
            self.assertEqual(both.total, 2)
            page = session.search("булг мастер")
            self.assertEqual(page.total, 1)
            self.assertEqual(
                page.books, (_expected_book(groups["a"][0], "a.zip"),)
            )
            missing = session.search("булг отсутствует")
            self.assertEqual((missing.total, missing.books), (0, ()))

    def test_yo_case_nfkc(self) -> None:
        """Ё/ё/регистр и полноширинные символы сводятся к одной форме."""
        groups = {
            "a": [
                _row("1", "Ёжиков Ёж", "Ёж и Ёлка"),
                _row("2", "Автор", "ＡＢ２"),
            ]
        }
        library = make_library(self.root, groups)
        with open_search(library, self.paths) as session:
            for query in ("ёж", "ЁЖ", "еж"):
                with self.subTest(query=query):
                    found = session.search(query)
                    self.assertEqual(found.total, 1)
                    self.assertEqual(found.books[0].title, "Ёж и Ёлка")
            for query in ("ab2", "AB2", "ＡＢ２"):
                with self.subTest(query=query):
                    found = session.search(query)
                    self.assertEqual(found.total, 1)
                    self.assertEqual(found.books[0].title, "ＡＢ２")

    def test_punctuation_and_operators(self) -> None:
        """Пунктуация/``_`` — разделители; кавычки и операторы не исполняются."""
        self.assertEqual(build_match(("or",)), '"or"*')
        self.assertEqual(
            build_match(("мастер", "еж")), '"мастер"* AND "еж"*'
        )
        self.assertEqual(query_tokens("ё-жик_плюс!!!"), ("е", "жик", "плюс"))
        self.assertEqual(
            query_tokens('"мастер" OR "ёж"'), ("мастер", "or", "еж")
        )
        groups = {
            "a": [
                _row("1", "Булгаков Михаил", "Мастер и Маргарита"),
                _row("2", "Автор", "Ёж"),
            ]
        }
        library = make_library(self.root, groups)
        with open_search(library, self.paths) as session:
            for bad in ("!!!", "   ", '""'):
                with self.subTest(query=bad):
                    with self.assertRaises(SearchError):
                        session.search(bad)
            # Наивная подстановка raw-запроса исполнила бы OR и нашла бы обе
            # книги; корректная токенизация требует все три слова сразу.
            quoted = session.search('"мастер" OR "ёж"')
            self.assertEqual((quoted.total, quoted.books), (0, ()))
            self.assertEqual(session.search("мастер").total, 1)
            self.assertEqual(session.search("ёж").total, 1)

    def test_combining_marks_full_title_and_prefixes(self) -> None:
        """Полное видимое название с combining-метками находит само себя.

        Регрессия финального обзора (F2): ``unicode61 remove_diacritics 0``
        держит диакритики ВНУТРИ токена индекса (ма́стер, i̇stanbul после
        casefold, q́abc), а ``query_tokens`` раньше резала запрос на каждой
        метке — вставка точного названия из выдачи не находила книгу.
        Devanagari-метки (Mc) — разделители для ОБОИХ сторон: прежде
        работавший случай не должен сломаться.
        """
        groups = {
            "a": [
                _row("1", "Автор Метка", "ма́стер"),
                _row("2", "Автор Метка", "İstanbul"),
                _row("3", "Автор Метка", "q́abc"),
                _row("4", "Автор Метка", "किताब"),
                _row("5", "Автор Метка", "Мастер"),
            ]
        }
        library = make_library(self.root, groups)
        with open_search(library, self.paths) as session:
            for query, member in (
                ("ма́стер", "1.fb2"),  # полное название = побуквенный запрос
                ("ма́с", "1.fb2"),     # префикс с меткой внутри
                ("ма́сте", "1.fb2"),
                ("İstanbul", "2.fb2"),  # casefold вводит U+0307
                ("i̇st", "2.fb2"),      # префикс i + U+0307 + st
                ("q́abc", "3.fb2"),
                ("किताब", "4.fb2"),   # Mc-метки: согласованное разбиение
            ):
                with self.subTest(query=query):
                    found = session.search(query)
                    self.assertEqual(found.total, 1, query)
                    self.assertEqual(found.books[0].member, member)
            # Отображение не изменено: в выдаче исходная форма с меткой.
            self.assertEqual(session.search("ма́стер").books[0].title, "ма́стер")
            # AND двух слов с метками: оба обязательны, второе слово —
            # с меткой в середине.
            both = session.search("ма́стер q́abc")
            self.assertEqual(both.total, 0)
            # Ранг: точное совпадение названия с меткой выше релевантности.
            ranked = session.search("ма́стер")
            self.assertEqual(ranked.books[0].title, "ма́стер")
            self.assertEqual(ranked.books[0].member, "1.fb2")

    def test_combining_marks_token_lexicon(self) -> None:
        """Токены запроса зеркалят фактическую лексику FTS на метках.

        Прежние контракты сохранены: пунктуация/``_`` — разделители,
        пользовательский синтаксис FTS не исполняется, NFKC/регистр/ё
        по-прежнему нормализуются.
        """
        self.assertEqual(query_tokens("ма́стер"), ("ма́стер",))
        self.assertEqual(query_tokens("ма́стер-консоль"), ("ма́стер", "консоль"))
        # Метка до начала слова отбрасывается, как это делает токенизатор.
        self.assertEqual(query_tokens("́мастер"), ("мастер",))
        self.assertEqual(query_tokens("i̇stanbul"), ("i̇stanbul",))
        self.assertEqual(query_tokens("q́abc q́az"), ("q́abc", "q́az"))
        self.assertEqual(query_tokens("किताब"), ("क", "त", "ब"))
        # Экранирование MATCH не изменилось.
        self.assertEqual(build_match(("ма́стер",)), '"ма́стер"*')
        # Демонстрация раскладки нормализации: casefold(İ) = i + U+0307.
        self.assertEqual(normalize("İstanbul"), "i̇stanbul")

    def test_failed_commit_rolls_back_and_recovers(self) -> None:
        """Отказ COMMIT откатывает транзакцию и не ломает сессию (F5).

        Ранее COMMIT стоял вне rollback-защиты: после однократного отказа
        транзакция оставалась открытой, и любой следующий поиск той же
        сессии падал на «cannot start a transaction within a transaction».
        """
        library = make_library(self.root, _BASIC_GROUPS)
        with open_search(library, self.paths) as session:
            real = session._connection
            events: list[str] = []

            class _FlakyCommit:
                """Прокси: первый COMMIT падает, остальное прозрачно."""

                def __init__(self) -> None:
                    self.injected = False

                def execute(self, sql: str, *args: object):
                    if sql == "COMMIT" and not self.injected:
                        self.injected = True
                        raise sqlite3.OperationalError("injected commit failure")
                    if sql == "ROLLBACK":
                        events.append("rollback")
                    return real.execute(sql, *args)

                def __getattr__(self, name: str):
                    return getattr(real, name)

            proxy = _FlakyCommit()
            session._connection = proxy  # type: ignore[assignment]
            try:
                with self.assertRaises(SearchError) as caught:
                    session.search("книга")
                self.assertIn("injected commit failure", str(caught.exception))
                self.assertTrue(proxy.injected)
                # Транзакция закрыта best-effort ROLLBACK — первопричина
                # сохранена, сессия пригодна для следующего поиска.
                self.assertEqual(events, ["rollback"])
                self.assertFalse(real.in_transaction)
                retry = session.search("книга")
                self.assertEqual(retry.total, 2)
                self.assertFalse(real.in_transaction)
            finally:
                session._connection = real  # type: ignore[assignment]

    def test_exact_title_first(self) -> None:
        """Точное совпадение нормализованного названия выше остальных."""
        groups = {
            "a": [
                _row("1", "Автор Б", "Мастер"),
                _row("2", "Автор А", "Мастер"),
                _row("3", "Автор В", "Мастерство"),
            ]
        }
        library = make_library(self.root, groups)
        with open_search(library, self.paths) as session:
            page = session.search("мастер")
            self.assertEqual(page.total, 3)
            self.assertEqual(
                [book.title for book in page.books],
                ["Мастер", "Мастер", "Мастерство"],
            )
            self.assertEqual(
                [book.author for book in page.books],
                ["Автор А", "Автор Б", "Автор В"],
            )

    def test_pages(self) -> None:
        """41 результат — страницы 20/20/1 в одинаковом порядке; page<0 отказ."""
        rows = [
            _row(f"{i:03d}", "Автор Строй", f"Книга {i:03d}")
            for i in range(1, 42)
        ]
        library = make_library(self.root, {"a": rows})
        with open_search(library, self.paths) as session:
            page0 = session.search("книга")
            page1 = session.search("книга", page=1)
            page2 = session.search("книга", page=2)
            self.assertEqual((page0.page, page0.total), (0, 41))
            self.assertEqual(len(page0.books), PAGE_SIZE)
            self.assertEqual((page1.page, page1.total, len(page1.books)), (1, 41, PAGE_SIZE))
            self.assertEqual((page2.page, page2.total, len(page2.books)), (2, 41, 1))
            titles = [book.title for book in page0.books + page1.books + page2.books]
            self.assertEqual(titles, [f"Книга {i:03d}" for i in range(1, 42)])
            # Одинаковый порядок при повторном запросе той же страницы.
            self.assertEqual(session.search("книга").books, page0.books)
            with self.assertRaises(SearchError):
                session.search("книга", page=-1)

    def test_series_merged_and_separate_editions(self) -> None:
        """Повторные серии одной Book не дублируют результат; другой member —
        отдельное издание; серии и метаданные восстанавливаются точно."""
        groups = {
            "a": [
                _row(
                    "1",
                    "Автор Серии",
                    "Книга серийная",
                    SERIES="Серия одна",
                    SERNO="1",
                ),
                _row(
                    "1",
                    "Автор Серии",
                    "Книга серийная",
                    SERIES="Серия два",
                    SERNO="2",
                ),
                _row("2", "Автор Серии", "Книга серийная"),
            ]
        }
        library = make_library(self.root, groups)
        with open_search(library, self.paths) as session:
            page = session.search("серий")
            self.assertEqual(page.total, 2)
            merged, other = page.books
            self.assertEqual(merged.member, "1.fb2")
            self.assertEqual(
                merged,
                Book(
                    archive="a.zip",
                    member="1.fb2",
                    file_id="1",
                    author="Автор Серии",
                    title="Книга серийная",
                    language="",
                    series=(
                        Series(name="Серия одна", number="1"),
                        Series(name="Серия два", number="2"),
                    ),
                    size=None,
                    libid="",
                    metadata=_metadata(groups["a"][0]),
                ),
            )
            self.assertEqual(other.member, "2.fb2")
            self.assertEqual(other.series, ())
            self.assertEqual(
                other.metadata, _metadata(groups["a"][2])
            )

    def test_session_blocks_refresh(self) -> None:
        """Пока сессия открыта, писатель получает IndexBusyError; сессия
        продолжает работать со своим снимком; после close обновление успешно."""
        library = make_library(self.root, _BASIC_GROUPS)
        v2 = {
            "a": [
                _BASIC_GROUPS["a"][0],
                _BASIC_GROUPS["a"][1],
                _row("3", "Новый Автор", "Новая книга"),
            ]
        }
        with open_search(library, self.paths) as session:
            self.assertEqual(session.search("книга").total, 2)
            # Настоящий процесс-писатель: пересборка невозможна, пока жив
            # читатель (force требует exclusive).
            self.assertEqual(self._run_writer("force"), "BUSY")
            # Каталог изменился: plain-писатель видит неактуальность, но
            # exclusive по-прежнему занят читателем.
            write_library(self.root, v2)
            self.assertEqual(self._run_writer("plain"), "BUSY")
            # Сессия работает со своим снимком: старые книги на месте, новой нет.
            self.assertEqual(session.search("книга").total, 2)
            self.assertEqual(session.search("новая").total, 0)
        # Сессия закрыта: писатель успешно обновляет индекс.
        self.assertEqual(self._run_writer("plain"), "REBUILT")
        with open_search(library, self.paths) as session:
            self.assertEqual(session.search("книга").total, 3)
            self.assertEqual(session.search("новая").total, 1)

    def test_two_sessions(self) -> None:
        """Совместимые shared-сессии работают одновременно и дают одинаковые
        результаты; после закрытия обеих писатель не блокирован."""
        library = make_library(self.root, _BASIC_GROUPS)
        with open_search(library, self.paths) as first:
            with open_search(library, self.paths) as second:
                from_first = first.search("книга")
                from_second = second.search("книга")
                self.assertEqual(from_first.total, 2)
                self.assertEqual(from_first, from_second)
        self.assertEqual(self._run_writer("force"), "REBUILT")

    def test_changed_between_ensure_and_open(self) -> None:
        """Метаданные проверяются заново под shared до открытия: смена
        источника после ensure ведёт к повтору, а не к устаревшей выдаче;
        непрерывная смена — понятная ошибка не позже двух попыток."""
        v1 = {"a": [_row("1", "Автор Первый", "Книга одна")]}
        v2 = {"a": [_row("2", "Автор Второй", "Книга две")]}
        v3 = {"a": [_row("3", "Автор Третий", "Книга три")]}
        library = make_library(self.root, v1)
        real_ensure = search_module.ensure_index
        calls: list[str] = []

        def mutates_once(library_arg, paths_arg, **kwargs):
            result = real_ensure(library_arg, paths_arg, **kwargs)
            calls.append(result.meta.sha256)
            if len(calls) == 1:
                # Источник меняется после успешного ensure, до проверки сессии.
                write_library(self.root, v2)
            return result

        with mock.patch.object(search_module, "ensure_index", mutates_once):
            with open_search(library, self.paths) as session:
                self.assertEqual(len(calls), 2)
                self.assertEqual(session.search("две").total, 1)
                self.assertEqual(session.search("одна").total, 0)
        self.assertEqual(len(calls), 2)

        calls.clear()

        def always_mutates(library_arg, paths_arg, **kwargs):
            result = real_ensure(library_arg, paths_arg, **kwargs)
            calls.append(result.meta.sha256)
            # После каждого ensure источник снова отличается от опубликованного.
            write_library(self.root, v3 if len(calls) % 2 == 1 else v2)
            return result

        with mock.patch.object(search_module, "ensure_index", always_mutates):
            with self.assertRaises(SearchError) as caught:
                with open_search(library, self.paths):
                    self.fail("сессия не должна открыться при непрерывной смене")
        self.assertEqual(len(calls), 2)
        self.assertIn("изменяется", str(caught.exception))

    def test_close_before_unlock(self) -> None:
        """SQLite-соединение закрывается до освобождения блокировки; после
        выхода реальный процесс берёт exclusive без ожидания."""
        library = make_library(self.root, _BASIC_GROUPS)
        events: list[str] = []
        real_connect = search_module._connect
        real_lock = search_module.index_lock

        def connect_recording(path):
            connection = real_connect(path)

            class _Recording:
                """Прокси: фиксирует close настоящего соединения."""

                def close(self) -> None:
                    connection.close()
                    events.append("closed")

                def __getattr__(self, name):
                    return getattr(connection, name)

            return _Recording()

        @contextmanager
        def lock_recording(path, *, exclusive):
            with real_lock(path, exclusive=exclusive):
                yield
            events.append("unlocked")

        with mock.patch.object(
            search_module, "_connect", connect_recording
        ), mock.patch.object(search_module, "index_lock", lock_recording):
            with open_search(library, self.paths) as session:
                self.assertEqual(session.search("книга").total, 2)
        self.assertEqual(events, ["closed", "unlocked"])

        attempt = self._start_child(_ATTEMPT_CHILD, str(self.lock_path))
        self._expect_line(attempt, "ACQUIRED")
        self._release(attempt)


# ---------------------------------------------------------------------------
# Живая сверка лексики запроса с фактическим токенизатором FTS5 (финальный F2)
# ---------------------------------------------------------------------------


class CanonicalNoncharacterTests(unittest.TestCase):
    """Канонизация U+FFFE/U+FFFF и полного surrogate-диапазона (волна CI).

    Живой токенизатор ``unicode61`` менял статус U+FFFE/U+FFFF между
    версиями SQLite: 3.45.1 (сборки CI) завершает ими слово, 3.54.0 —
    оставляет внутри токена. Поэтому эти кодовые точки (и весь диапазон
    суррогатов 0xD800..0xDFFF, непредставимый в строгом UTF-8 каталога)
    вырезаются ``normalize`` симметрично при индексировании и поиске:
    FTS их вообще не видит, лексика запроса и индекса совпадает на любом
    runtime. Таблица разделителей при этом держит их как разделители
    (защита от появления в обход ``normalize``).
    """

    def test_normalize_strips_exactly_canonical_set(self) -> None:
        canonical = set(range(0xD800, 0xE000)) | {0xFFFE, 0xFFFF}
        kept = (
            "мастер и маргарита",
            "a\uFFFDb",  # REPLACEMENT CHARACTER — обычный символ, не noncharacter-пара
            "ё́жик",
            "AB \u2060 Cf",  # форматные символы НЕ входят в канонический набор
        )
        for sample in kept:
            with self.subTest(sample=repr(sample)):
                stripped = [ord(c) for c in normalize(sample) if ord(c) in canonical]
                self.assertEqual(stripped, [], f"канонизация съела лишнее: {sample!r}")
        removed = (
            "a\uFFFEb\uFFFFc мастер",
            "\U0001F4D6 книги\uFFFF",
            "кни\uFFFEги",
        )
        for sample in removed:
            with self.subTest(sample=repr(sample)):
                self.assertNotIn("\ufffe", normalize(sample))
                self.assertNotIn("\uffff", normalize(sample))
        # Отдельные суррогаты (могут появиться только из строк Python,
        # строгий UTF-8 каталог их не даёт) вырезаются до SQLite — биндинг
        # больше не падает на «surrogates not allowed».
        self.assertEqual(normalize("ма" + chr(0xD800) + "стер"), "мастер")

    def test_canonical_codepoints_stay_declared_separators(self) -> None:
        for code in (0xFFFE, 0xFFFF, 0xD800, 0xDABC, 0xDFFF):
            self.assertIn(
                chr(code), text_module._FTS_SEPARATORS, hex(code)
            )

    def test_fts_never_sees_canonical_codepoints(self) -> None:
        """Индекс и запрос согласованы на любом SQLite: спорные точки вырезаны.

        Зонд повторяет сценарий расхождения: «a<CP>b» с кодовой точкой,
        чей статус разделителя зависит от версии SQLite. После
        канонизации FTS получает уже «ab», и фактические токены равны
        ``query_tokens`` независимо от поведения живого токенизатора.
        """
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE VIRTUAL TABLE probe USING fts5(x,"
            " tokenize='unicode61 remove_diacritics 0')"
        )
        connection.execute(
            "CREATE VIRTUAL TABLE vocab USING fts5vocab('probe', 'row')"
        )
        for sample in (
            "a\uFFFEb мастер",
            "a\uFFFFb мастер",
            "маст\uFFFEер и\uFFFE маргарита",
            "a\uFFFDb",  # REPLACEMENT CHARACTER не канонизируется
        ):
            with self.subTest(sample=repr(sample)):
                normalized = normalize(sample)
                connection.execute("DELETE FROM probe")
                connection.execute("INSERT INTO probe VALUES (?)", (normalized,))
                fts_terms = {
                    row[0]
                    for row in connection.execute("SELECT term FROM vocab")
                }
                self.assertEqual(set(query_tokens(sample)), fts_terms)


class TokenizerAlignmentTests(unittest.TestCase):
    """Таблицы ``text`` обязаны совпадать с фактическим unicode61 SQLite.

    Полный перебор кодового пространства против живого SQLite через
    ``fts5vocab``: разделители токенов, продолжающие-но-не-начинающие
    метки и прямая зеркальность ``query_tokens`` фактическому разбиению
    на реальных строках (в том числе после ``normalize``). Любой дрейф
    таблиц FTS в будущих SQLite — громкий отказ теста, а не молчаливое
    расхождение запроса с индексом.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.connection = sqlite3.connect(":memory:")
        cls.connection.execute(
            "CREATE VIRTUAL TABLE probe USING fts5(x,"
            " tokenize='unicode61 remove_diacritics 0')"
        )
        cls.connection.execute(
            "CREATE VIRTUAL TABLE vocab USING fts5vocab('probe', 'row')"
        )
        cls.connection.execute(
            "CREATE VIRTUAL TABLE vocab_seq USING fts5vocab('probe', 'instance')"
        )
        cls.codepoints = [
            cp
            for cp in range(0x110000)
            if cp != 0x20 and not 0xD800 <= cp <= 0xDFFF
        ]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()

    def _terms(self, pattern) -> set[str]:
        """Фактические токены одного зонда: ``pattern(cp)`` на каждое cp."""
        self.connection.execute("DELETE FROM probe")
        self.connection.execute(
            "INSERT INTO probe VALUES (?)",
            (" ".join(pattern(cp) for cp in self.codepoints),),
        )
        return {row[0] for row in self.connection.execute("SELECT term FROM vocab")}

    def test_separator_table_matches_live_tokenizer(self) -> None:
        # Канонизированные кодовые точки (U+FFFE/U+FFFF и суррогаты)
        # исключены из сверки: их статус в живом токенизаторе менялся
        # между версиями SQLite (3.45 — разделители, 3.54 — символы
        # токена), поэтому контракт держит их не сверкой с живым SQLite,
        # а симметричным вырезанием в normalize (см. CanonicalNoncharacterTests)
        # и объявлением разделителями в самой таблице.
        canonical = set(range(0xD800, 0xE000)) | {0xFFFE, 0xFFFF}
        probed = [cp for cp in self.codepoints if cp not in canonical]
        terms = self._terms(lambda cp: f"a{chr(cp)}b")
        live = {
            cp
            for cp in probed
            if not chr(cp).isalnum() and f"a{chr(cp)}b" not in terms
        }
        stored = {
            cp
            for cp in probed
            if chr(cp) in text_module._FTS_SEPARATORS
        }
        # 0x20 непроверяем этим зондом (исключён из полного перебора), но
        # входит в таблицу как разделитель по построению; суррогаты
        # непредставимы в UTF-8 SQLite и тоже сверяются только контрактом.
        self.assertEqual(live, stored, "таблица разделителей разошлась")
        self.assertIn(" ", text_module._FTS_SEPARATORS)
        for code in (0xD800, 0xDFFF, 0xFFFE, 0xFFFF):
            self.assertIn(chr(code), text_module._FTS_SEPARATORS)

    def test_continue_only_marks_match_live_tokenizer(self) -> None:
        continuation = self._terms(lambda cp: f"a{chr(cp)}b")
        starts = self._terms(lambda cp: f"{chr(cp)}b")
        live_continue_only = {
            cp
            for cp in self.codepoints
            if not chr(cp).isalnum()
            and f"a{chr(cp)}b" in continuation
            and f"{chr(cp)}b" not in starts
        }
        stored = {ord(char) for char in text_module._FTS_CONTINUE_ONLY}
        self.assertEqual(live_continue_only, stored, "таблица меток разошлась")

    def test_query_tokens_mirror_live_fts_on_real_strings(self) -> None:
        samples = (
            "ма́стер",
            "Мастер и Маргарита",
            "i̇stanbul",
            "İstanbul",
            "q́abc",
            "किताब",
            "héр деко́мпоз",
            "Ёжиков Ёж",
            "ＡＢ２ текст",
            'quoted "or" AND (stuff)',
            "ё-жик_плюс!!!",
            "автор;жанр 100500",
            "́start end́",
            "á́́b",
            "חַיֶּבֶת",
            " señor  maté ",
            # Канонизированные noncharacters: в FTS не попадают вообще,
            # лексика запроса совпадает с индексом на любой версии SQLite.
            "a\uFFFEb мастер",
            "кни\uFFFFги маст\uFFFEера",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                normalized = normalize(sample)
                self.connection.execute("DELETE FROM probe")
                self.connection.execute(
                    "INSERT INTO probe VALUES (?)", (normalized,)
                )
                fts_terms = [
                    row[0]
                    for row in self.connection.execute(
                        "SELECT term FROM vocab_seq ORDER BY doc, offset"
                    )
                ]
                self.assertEqual(
                    list(query_tokens(sample)), fts_terms,
                    "лексика запроса разошлась с фактическим FTS",
                )


if __name__ == "__main__":
    unittest.main()
