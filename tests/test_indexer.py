"""Тесты индексатора: свежесть каталога и атомарная пересборка (§7 спецификации).

Конкуренция проверяется настоящими дочерними процессами (``sys.executable``):
готовность и освобождение блокировки — обмен строками через pipe, без sleep
как условия корректности; каждое ожидание ограничено защитным таймаутом с
terminate/kill при превышении. Поведение «старый индекс сохраняется»
проверяется сравнением SHA-256 базы до/после сбоя, отмены и нехватки места.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from keklib_cli.errors import CatalogError, IndexBuildError, IndexBusyError
from keklib_cli.indexer import BUILD_MARKER, ensure_index, sha256_file
from keklib_cli.index_store import read_meta, validate_index
from keklib_cli.models import StatePaths
from tests.fixtures import make_library, make_raw_catalog
from tests.harness.library_fixture import write_library

_WAIT_SECONDS = 5.0  # защитный предел ожидания события дочернего процесса

# Дочерний процесс-держатель: берёт блокировку, печатает READY, ждёт EOF в
# stdin (закрытие родителем), затем печатает RELEASED и выходит с кодом 0.
_HOLDER_CHILD = """
import sys
from pathlib import Path

from keklib_cli.platforms import index_lock

mode, lock_path = sys.argv[1], Path(sys.argv[2])
with index_lock(lock_path, exclusive=(mode == "exclusive")):
    sys.stdout.write("READY\\n")
    sys.stdout.flush()
    sys.stdin.readline()
sys.stdout.write("RELEASED\\n")
sys.stdout.flush()
"""

# Попытка exclusive без ожидания: ACQUIRED (удалось — блокировка свободна),
# BUSY (IndexBusyError) либо RELEASED после EOF в stdin.
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

#: Суффиксы соседних записей SQLite у назначения публикации.
_ALL_SIDECARS = ("-journal", "-wal", "-shm")
#: Магические байты заголовка rollback-журнала SQLite — признак настоящего
#: (не синтетического) журнала.
_JOURNAL_MAGIC = b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"

# Дочерний процесс: настоящий SQLite-writer, завершаемый аварийно
# (os._exit) посреди незавершённой транзакции — рядом с базой остаётся
# реальный горячий rollback-журнал.
_HOT_JOURNAL_CHILD = """
import os
import sqlite3
import sys

db = sys.argv[1]
conn = sqlite3.connect(db)
conn.isolation_level = None
conn.execute("PRAGMA cache_size = 1")
conn.execute("BEGIN IMMEDIATE")
conn.execute("CREATE TABLE aux(payload BLOB)")
conn.execute("INSERT INTO aux(payload) VALUES (?)", (bytes(range(256)) * 1024,))
conn.execute("COMMIT")
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE aux SET payload = ?", (b"z" * 256 * 1024,))
conn.execute("DELETE FROM book")
os._exit(0)  # без COMMIT: -journal остаётся горячим
"""

#: Минимальная синтетическая библиотека: один INP, две активные FB2-книги.
_BASIC_GROUPS = {
    "a": [
        {
            "AUTHOR": "Автор Один",
            "TITLE": "Книга один",
            "FILE": "1",
            "DEL": "0",
            "EXT": "fb2",
        },
        {
            "AUTHOR": "Автор Два",
            "TITLE": "Книга два",
            "FILE": "2",
            "DEL": "0",
            "EXT": "fb2",
        },
    ],
}


class IndexerTests(unittest.TestCase):
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

    def _assert_lock_free(self) -> None:
        """После возврата ensure_index блокировка не удерживается.

        Реальный дочерний процесс немедленно берёт exclusive: ACQUIRED —
        блокировка свободна, BUSY — зависла (утечка).
        """
        attempt = self._start_child(_ATTEMPT_CHILD, str(self.lock_path))
        self._expect_line(attempt, "ACQUIRED")
        self._release(attempt)

    # Вспомогательные методы: база и каталог кэша.

    def _connect_ro(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"{self.paths.index.as_uri()}?mode=ro", uri=True)
        self.addCleanup(conn.close)
        return conn

    def _rows(self, sql: str) -> list[tuple]:
        return self._connect_ro().execute(sql).fetchall()

    def _build_dirs(self) -> list[str]:
        if not self.cache.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.cache.iterdir()
            if entry.name.startswith("build-")
        )

    # Тесты брифа.

    def test_first_then_unchanged(self) -> None:
        """Первый запуск строит, второй без изменений не пересобирает."""
        library = make_library(self.root, _BASIC_GROUPS)
        messages: list[str] = []
        first = ensure_index(library, self.paths, progress=messages.append)
        self.assertTrue(first.rebuilt)
        self.assertEqual(first.path, self.paths.index)
        self.assertTrue(self.paths.index.is_file())
        self.assertEqual(first.meta.stats.rows, 2)
        self.assertEqual(first.meta.stats.books, 2)
        validate_index(self.paths.index)

        digest = sha256_file(self.paths.index)
        stat_before = os.stat(self.paths.index)
        second = ensure_index(library, self.paths, progress=messages.append)
        self.assertFalse(second.rebuilt)
        self.assertEqual(second.path, self.paths.index)
        self.assertEqual(second.meta, first.meta)
        # Прежние байты и mtime: файл вообще не переоткрывался на запись.
        self.assertEqual(sha256_file(self.paths.index), digest)
        self.assertEqual(
            os.stat(self.paths.index).st_mtime_ns, stat_before.st_mtime_ns
        )
        # После возврата блокировка не удерживается (реальный процесс).
        self._assert_lock_free()
        self.assertTrue(messages, "progress-сообщения не передавались")

    def test_same_size_same_mtime_new_hash(self) -> None:
        """Изменение содержимого того же размера с восстановленным mtime
        распознаётся по SHA-256: дата и размер не считаются достаточными."""
        structure = b"AUTHOR;TITLE;FILE;DEL;EXT;\n"

        def row(author: str) -> bytes:
            return f"{author}\x04Книга\x041\x040\x04fb2".encode("utf-8")

        library = make_raw_catalog(self.root, [structure], {"a": row("Авт А")})
        first = ensure_index(library, self.paths)
        self.assertTrue(first.rebuilt)

        catalog = library.catalog
        stat_before = catalog.stat()
        make_raw_catalog(self.root, [structure], {"a": row("Авт Б")})
        # Тот же размер (записи ZIP_STORED, отличие ровно один символ той же
        # длины) и восстановленный mtime — изменился только хеш.
        self.assertEqual(catalog.stat().st_size, stat_before.st_size)
        os.utime(catalog, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))

        second = ensure_index(library, self.paths)
        self.assertTrue(second.rebuilt)
        self.assertNotEqual(second.meta.sha256, first.meta.sha256)
        self.assertEqual(second.meta.stats.books, 1)

    def test_force_schema_rules_root(self) -> None:
        """Все причины обновления: force, версия схемы, версия правил, корень."""
        library = make_library(self.root, _BASIC_GROUPS)
        base = ensure_index(library, self.paths)
        self.assertTrue(base.rebuilt)

        forced = ensure_index(library, self.paths, force=True)
        self.assertTrue(forced.rebuilt)
        # Каталог тот же: перестройка именно из-за force.
        self.assertEqual(forced.meta.sha256, base.meta.sha256)
        validate_index(self.paths.index)

        for key, reason in (
            ("schema_version", "версия схемы"),
            ("rules_version", "версия правил"),
        ):
            with self.subTest(reason=reason):
                conn = sqlite3.connect(self.paths.index)
                try:
                    conn.execute("UPDATE meta SET value = '999' WHERE key = ?", (key,))
                    conn.commit()
                finally:
                    conn.close()
                result = ensure_index(library, self.paths)
                self.assertTrue(result.rebuilt)
                validate_index(self.paths.index)
                self.assertEqual(result.meta.stats.books, 2)

        with self.subTest(reason="смена корня библиотеки"):
            other = make_library(self.base / "library-copy", _BASIC_GROUPS)
            result = ensure_index(other, self.paths)
            self.assertTrue(result.rebuilt)
            self.assertEqual(result.meta.root, str(other.root))
            # Байты каталога идентичны: пересборка именно из-за корня.
            self.assertEqual(result.meta.sha256, base.meta.sha256)
            validate_index(self.paths.index)

    def test_add_edit_delete_del_and_inp(self) -> None:
        """Полная пересборка отражает добавление, правку, исчезновение
        записей, переходы DEL 0→1→0 одной идентичности и удаление целого INP."""
        v1 = {
            "a": [
                {
                    "AUTHOR": "Автор Один",
                    "TITLE": "Книга один",
                    "FILE": "1",
                    "DEL": "0",
                    "EXT": "fb2",
                },
                {
                    "AUTHOR": "Автор Два",
                    "TITLE": "Книга два",
                    "FILE": "2",
                    "DEL": "0",
                    "EXT": "fb2",
                },
                {
                    "AUTHOR": "Автор Девять",
                    "TITLE": "Книга девять",
                    "FILE": "9",
                    "DEL": "0",
                    "EXT": "fb2",
                },
            ],
            "b": [
                {
                    "AUTHOR": "Автор Три",
                    "TITLE": "Книга три",
                    "FILE": "3",
                    "DEL": "0",
                    "EXT": "fb2",
                },
                {
                    "AUTHOR": "Автор Четыре",
                    "TITLE": "Книга четыре",
                    "FILE": "4",
                    "DEL": "1",
                    "EXT": "fb2",
                },
            ],
        }
        library = make_library(self.root, v1)
        first = ensure_index(library, self.paths)
        self.assertTrue(first.rebuilt)
        self.assertEqual(first.meta.stats.rows, 5)
        self.assertEqual(first.meta.stats.excluded, 1)
        self.assertEqual(first.meta.stats.books, 4)
        self.assertEqual(
            self._rows("SELECT member FROM book ORDER BY member"),
            [("1.fb2",), ("2.fb2",), ("3.fb2",), ("9.fb2",)],
        )

        # Правка книги 1, исчезновение книги 2 целиком, добавление книги 5,
        # DEL 0→1 той же идентичности 9.fb2 (книга пропадает из индекса, но
        # строка остаётся в счётчиках как исключённая); INP b удалён целиком:
        # книга 3 исчезает, исключённая книга 4 больше не считается.
        v2 = {
            "a": [
                {
                    "AUTHOR": "Автор Первый",
                    "TITLE": "Книга один (изд. 2)",
                    "FILE": "1",
                    "DEL": "0",
                    "EXT": "fb2",
                },
                {
                    "AUTHOR": "Автор Пять",
                    "TITLE": "Книга пять",
                    "FILE": "5",
                    "DEL": "0",
                    "EXT": "fb2",
                },
                {
                    "AUTHOR": "Автор Девять",
                    "TITLE": "Книга девять",
                    "FILE": "9",
                    "DEL": "1",
                    "EXT": "fb2",
                },
            ],
        }
        write_library(self.root, v2)
        second = ensure_index(library, self.paths)
        self.assertTrue(second.rebuilt)
        self.assertEqual(second.meta.stats.rows, 3)
        self.assertEqual(second.meta.stats.excluded, 1)
        self.assertEqual(second.meta.stats.books, 2)
        self.assertEqual(
            self._rows(
                "SELECT archive, member, file_id, author, title FROM book"
                " ORDER BY id"
            ),
            [
                ("a.zip", "1.fb2", "1", "Автор Первый", "Книга один (изд. 2)"),
                ("a.zip", "5.fb2", "5", "Автор Пять", "Книга пять"),
            ],
        )
        self.assertNotEqual(second.meta.sha256, first.meta.sha256)

        # DEL 1→0 той же идентичности: книга возвращается, хеш снова новый.
        v3 = {
            "a": [
                v2["a"][0],
                v2["a"][1],
                {
                    "AUTHOR": "Автор Девять",
                    "TITLE": "Книга девять",
                    "FILE": "9",
                    "DEL": "0",
                    "EXT": "fb2",
                },
            ],
        }
        write_library(self.root, v3)
        third = ensure_index(library, self.paths)
        self.assertTrue(third.rebuilt)
        self.assertEqual(third.meta.stats.rows, 3)
        self.assertEqual(third.meta.stats.excluded, 0)
        self.assertEqual(third.meta.stats.books, 3)
        self.assertEqual(
            self._rows(
                "SELECT archive, member, file_id, author, title FROM book"
                " WHERE member = '9.fb2'"
            ),
            [("a.zip", "9.fb2", "9", "Автор Девять", "Книга девять")],
        )
        self.assertNotEqual(third.meta.sha256, second.meta.sha256)

    def test_failure_cancel_disk_full_preserve_old(self) -> None:
        """Сбой каталога, отмена и нехватка места сохраняют старую базу
        (сравнение SHA-256 до/после) и не оставляют build-каталогов."""
        library = make_library(self.root, _BASIC_GROUPS)
        old = ensure_index(library, self.paths)
        self.assertTrue(old.rebuilt)
        old_digest = sha256_file(self.paths.index)

        with self.subTest(failure="повреждённый INP"):
            make_raw_catalog(
                self.root,
                [b"AUTHOR;TITLE;FILE;DEL;EXT;\n"],
                {"a": b"short \x04 row"},  # полей меньше, чем в структуре
            )
            with self.assertRaises(CatalogError):
                ensure_index(library, self.paths)
            self.assertEqual(sha256_file(self.paths.index), old_digest)
            self.assertEqual(self._build_dirs(), [])

        with self.subTest(failure="SIZE вне диапазона SQLite INTEGER (F4)"):
            make_raw_catalog(
                self.root,
                [b"AUTHOR;TITLE;FILE;SIZE;DEL;EXT;\n"],
                {
                    "a": (
                        b"Author\x04Title\x041\x04"
                        b"9223372036854775808\x040\x04fb2\n"
                    )
                },
            )
            with self.assertRaises(CatalogError) as oversize:
                ensure_index(library, self.paths)
            self.assertIn("SIZE", str(oversize.exception))
            self.assertEqual(sha256_file(self.paths.index), old_digest)
            self.assertEqual(self._build_dirs(), [])

        v2 = {
            "a": [
                {
                    "AUTHOR": "Другой Автор",
                    "TITLE": "Другая книга",
                    "FILE": "9",
                    "DEL": "0",
                    "EXT": "fb2",
                }
            ]
        }

        with self.subTest(failure="отмена KeyboardInterrupt"):
            write_library(self.root, v2)

            def cancelled(snapshot, stats):
                raise KeyboardInterrupt
                yield  # pragma: no cover — генератор для контракта iter_books

            with mock.patch("keklib_cli.indexer.iter_books", cancelled):
                with self.assertRaises(KeyboardInterrupt):
                    ensure_index(library, self.paths)
            self.assertEqual(sha256_file(self.paths.index), old_digest)
            self.assertEqual(self._build_dirs(), [])

        with self.subTest(failure="нет места"):

            def disk_full(path, books, meta):
                # Частичный файл, как при реальном ENOSPC внутри write_index
                # (само хранилище заворачивает OSError в IndexBuildError).
                path.write_bytes(b"partial")
                raise IndexBuildError(
                    f"failed to build index {path}: "
                    "[Errno 28] No space left on device"
                )

            with mock.patch("keklib_cli.indexer.write_index", disk_full):
                with self.assertRaises(IndexBuildError):
                    ensure_index(library, self.paths)
            self.assertEqual(sha256_file(self.paths.index), old_digest)
            self.assertEqual(self._build_dirs(), [])

        # Старый индекс остаётся пригодным, без устаревшего фолбэка.
        validate_index(self.paths.index)
        self.assertEqual(read_meta(self.paths.index), old.meta)

    def test_changed_live_source(self) -> None:
        """Изменение INPX между снимком и финальным хешем: публикации нет."""
        library = make_library(self.root, _BASIC_GROUPS)
        old = ensure_index(library, self.paths)
        self.assertTrue(old.rebuilt)
        old_digest = sha256_file(self.paths.index)

        v2 = {
            "a": [
                {
                    "AUTHOR": "Третий Автор",
                    "TITLE": "Третья книга",
                    "FILE": "7",
                    "DEL": "0",
                    "EXT": "fb2",
                }
            ]
        }
        v3 = {
            "a": [
                {
                    "AUTHOR": "Четвёртый Автор",
                    "TITLE": "Четвёртая книга",
                    "FILE": "8",
                    "DEL": "0",
                    "EXT": "fb2",
                }
            ]
        }
        write_library(self.root, v2)  # причина пересборки

        from keklib_cli.inpx import iter_books as real_iter_books

        def mutating(snapshot, stats):
            yield from real_iter_books(snapshot, stats)
            # Снимок прочитан полностью; источник меняется до финального хеша.
            write_library(self.root, v3)

        with mock.patch("keklib_cli.indexer.iter_books", mutating):
            with self.assertRaises(IndexBuildError) as caught:
                ensure_index(library, self.paths)
        self.assertIn("changed during indexing", str(caught.exception))
        # Публикации нет: прежние байты и метаданные, своих build-каталогов
        # не осталось.
        self.assertEqual(sha256_file(self.paths.index), old_digest)
        self.assertEqual(read_meta(self.paths.index), old.meta)
        self.assertEqual(self._build_dirs(), [])

    def test_busy_writer(self) -> None:
        """Занятый exclusive — IndexBusyError; совместимый читатель не мешает
        проверке свежести, но блокирует принудительную пересборку."""
        library = make_library(self.root, _BASIC_GROUPS)
        self.assertTrue(ensure_index(library, self.paths).rebuilt)
        digest = sha256_file(self.paths.index)

        with self.subTest(holder="exclusive"):
            writer = self._start_child(_HOLDER_CHILD, "exclusive", str(self.lock_path))
            self._expect_line(writer, "READY")
            with self.assertRaises(IndexBusyError):
                ensure_index(library, self.paths)
            self.assertEqual(sha256_file(self.paths.index), digest)
            self._release(writer)

        with self.subTest(holder="shared + force"):
            reader = self._start_child(_HOLDER_CHILD, "shared", str(self.lock_path))
            self._expect_line(reader, "READY")
            with self.assertRaises(IndexBusyError):
                ensure_index(library, self.paths, force=True)
            self._release(reader)

        with self.subTest(holder="shared + fresh"):
            reader = self._start_child(_HOLDER_CHILD, "shared", str(self.lock_path))
            self._expect_line(reader, "READY")
            self.assertFalse(ensure_index(library, self.paths).rebuilt)
            self._release(reader)

    def test_corrupt_index_rebuilt(self) -> None:
        """Повреждённая база обнаруживается quick-проверкой и пересобирается."""
        library = make_library(self.root, _BASIC_GROUPS)
        first = ensure_index(library, self.paths)
        self.assertTrue(first.rebuilt)

        with open(self.paths.index, "r+b") as stream:
            stream.seek(0)
            stream.write(b"corrupted index!")
        with self.assertRaises(IndexBuildError):
            validate_index(self.paths.index)

        second = ensure_index(library, self.paths)
        self.assertTrue(second.rebuilt)
        validate_index(self.paths.index)
        self.assertEqual(second.meta.sha256, first.meta.sha256)
        self.assertEqual(second.meta.stats.books, 2)
        self.assertEqual(self._build_dirs(), [])

    def _assert_sidecar_refusal(self, library, *, force: bool) -> None:
        """Отказ по чужой sidecar-записи ДО любого открытия назначения.

        ``validate_index``/``read_meta`` подменяются функцией, падающей при
        вызове: если реализация откроет базу раньше проверки sidecar-записей,
        тест упадёт AssertionError, а не пройдёт молча.
        """

        def forbidden(*args, **kwargs):
            raise AssertionError(
                "назначение открыто в SQLite несмотря на чужие sidecar-записи"
            )

        with mock.patch(
            "keklib_cli.indexer.validate_index", forbidden
        ), mock.patch("keklib_cli.indexer.read_meta", forbidden):
            with self.assertRaises(IndexBuildError) as caught:
                ensure_index(library, self.paths, force=force)
        message = str(caught.exception)
        self.assertIn("sidecar", message)
        self.assertTrue(
            any(f"{self.paths.index.name}{suffix}" in message for suffix in _ALL_SIDECARS),
            f"диагностика не называет sidecar-запись: {message}",
        )

    def test_destination_hot_journal_rejected(self) -> None:
        """Настоящий горячий rollback-журнал назначения (аварийный дочерний
        SQLite-writer): отказ с адресной диагностикой, главный файл и журнал
        не тронуты, публикации нет."""
        library = make_library(self.root, _BASIC_GROUPS)
        self.assertTrue(ensure_index(library, self.paths).rebuilt)

        crashed = subprocess.run(
            [sys.executable, "-c", _HOT_JOURNAL_CHILD, str(self.paths.index)],
            capture_output=True,
            text=True,
            timeout=_WAIT_SECONDS * 6,
        )
        self.assertEqual(crashed.returncode, 0, crashed.stderr)
        journal = Path(f"{self.paths.index}-journal")
        self.assertTrue(journal.is_file(), "горячий rollback-журнал не создан")
        journal_bytes = journal.read_bytes()
        # Это настоящий журнал SQLite, а не произвольные байты.
        self.assertEqual(journal_bytes[:8], _JOURNAL_MAGIC)
        main_digest = sha256_file(self.paths.index)

        for mode in ("plain", "force"):
            with self.subTest(mode=mode):
                with self.assertRaises(IndexBuildError) as caught:
                    ensure_index(library, self.paths, force=(mode == "force"))
                # Диагностика адресная и не является sqlite-ошибкой readonly:
                # отказ произошёл до открытия базы.
                self.assertIn("index.sqlite3-journal", str(caught.exception))
                self.assertIn("sidecar", str(caught.exception))

        # Главный файл и журнал побайтно прежние, своих build-каталогов нет.
        self.assertEqual(sha256_file(self.paths.index), main_digest)
        self.assertEqual(journal.read_bytes(), journal_bytes)
        self.assertEqual(self._build_dirs(), [])
        # Опасность реальна: база с горячим журналом непригодна для
        # ro-читателя (rollback требуется уже при открытии).
        with self.assertRaises(IndexBuildError):
            validate_index(self.paths.index)
        self.assertTrue(journal.is_file())

    def test_destination_sidecars_rejected_before_open(self) -> None:
        """Любая чужая sidecar-запись назначения — файл или битая symlink —
        отвергается до открытия SQLite и до постройки; записи сохраняются."""
        library = make_library(self.root, _BASIC_GROUPS)
        self.assertTrue(ensure_index(library, self.paths).rebuilt)
        main_digest = sha256_file(self.paths.index)

        for suffix in _ALL_SIDECARS:
            sidecar = Path(f"{self.paths.index}{suffix}")
            with self.subTest(kind="file", suffix=suffix):
                sidecar.write_bytes(b"foreign data")
                for force in (False, True):
                    self._assert_sidecar_refusal(library, force=force)
                self.assertEqual(sidecar.read_bytes(), b"foreign data")
                self.assertEqual(sha256_file(self.paths.index), main_digest)
                self.assertEqual(self._build_dirs(), [])
                sidecar.unlink()

            with self.subTest(kind="dangling symlink", suffix=suffix):
                sidecar.unlink(missing_ok=True)
                try:
                    os.symlink("foreign-missing-target", sidecar)
                except OSError as exc:
                    self.skipTest(f"симлинки недоступны в этой среде: {exc}")
                for force in (False, True):
                    self._assert_sidecar_refusal(library, force=force)
                # Запись каталога не тронута: ссылка и её отсутствующая цель
                # на месте.
                self.assertTrue(sidecar.is_symlink())
                self.assertEqual(os.readlink(sidecar), "foreign-missing-target")
                self.assertEqual(sha256_file(self.paths.index), main_digest)
                sidecar.unlink()

        with self.subTest(kind="без главного файла"):
            journal = Path(f"{self.paths.index}-journal")
            self.paths.index.unlink()
            journal.write_bytes(b"foreign debris")
            self._assert_sidecar_refusal(library, force=False)
            # Ничего не построено и не опубликовано.
            self.assertFalse(self.paths.index.exists())
            self.assertEqual(journal.read_bytes(), b"foreign debris")

    def test_sidecar_appearing_before_publication_rejected(self) -> None:
        """Sidecar-запись, появившаяся у назначения уже во время постройки,
        останавливает публикацию: повторная проверка перед replace."""
        library = make_library(self.root, _BASIC_GROUPS)
        self.assertTrue(ensure_index(library, self.paths).rebuilt)
        old_digest = sha256_file(self.paths.index)
        write_library(
            self.root,
            {
                "a": [
                    {
                        "AUTHOR": "Новый Автор",
                        "TITLE": "Новая книга",
                        "FILE": "3",
                        "DEL": "0",
                        "EXT": "fb2",
                    }
                ]
            },
        )
        journal = Path(f"{self.paths.index}-journal")
        planted = b"foreign writer crashed during build"

        def progress(message: str) -> None:
            if message.startswith("validating index"):
                # Чужой writer завершается аварийно после первых проверок
                # индексатора, до публикации.
                journal.write_bytes(planted)

        with self.assertRaises(IndexBuildError) as caught:
            ensure_index(library, self.paths, progress=progress)
        self.assertIn("index.sqlite3-journal", str(caught.exception))
        self.assertEqual(journal.read_bytes(), planted)
        self.assertEqual(sha256_file(self.paths.index), old_digest)
        self.assertEqual(self._build_dirs(), [])

    def test_cleanup_not_ours(self) -> None:
        """Автоочистка удаляет только собственные помеченные build-каталоги;
        чужие записи и цели symlink не затрагиваются."""
        library = make_library(self.root, _BASIC_GROUPS)
        self.cache.mkdir(parents=True, exist_ok=True)

        foreign = self.cache / "build-foreign"
        foreign.mkdir()
        (foreign / "not-ours.txt").write_bytes(b"keep")
        stale_own = self.cache / "build-stale-own"
        stale_own.mkdir()
        (stale_own / "index.sqlite3").write_bytes(b"leftover")
        (stale_own / BUILD_MARKER).write_text('{"purpose": "keklib-cli"}\n')
        loose = self.cache / "notes.txt"
        loose.write_bytes(b"keep")

        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_bytes(b"keep")
        link = self.cache / "build-link"
        try:
            os.symlink(outside, link)
        except OSError as exc:
            self.skipTest(f"симлинки недоступны в этой среде: {exc}")

        result = ensure_index(library, self.paths)
        self.assertTrue(result.rebuilt)

        # Чужой build-* без маркера сохранён вместе с содержимым.
        self.assertEqual((foreign / "not-ours.txt").read_bytes(), b"keep")
        # Собственный остаток после аварии удалён, посторонний файл цел.
        self.assertFalse(stale_own.exists())
        self.assertEqual(loose.read_bytes(), b"keep")
        # Symlink не пройден и не удалён: ссылка и её цель на месте.
        self.assertTrue(link.is_symlink())
        self.assertEqual(sentinel.read_bytes(), b"keep")
        # После успешной публикации собственных build-каталогов не осталось.
        self.assertEqual(
            {entry.name for entry in self.cache.iterdir()},
            {"build-foreign", "notes.txt", "build-link", "index.lock",
             "index.sqlite3"},
        )

    def test_replace_failure_preserves_old(self) -> None:
        """Ошибка атомарной замены: понятная ошибка, старый индекс сохранён
        (никакого удаления до replace), своих build-каталогов нет."""
        library = make_library(self.root, _BASIC_GROUPS)
        first = ensure_index(library, self.paths)
        self.assertTrue(first.rebuilt)
        old_digest = sha256_file(self.paths.index)

        write_library(
            self.root,
            {
                "a": [
                    {
                        "AUTHOR": "Новый Автор",
                        "TITLE": "Новая книга",
                        "FILE": "3",
                        "DEL": "0",
                        "EXT": "fb2",
                    }
                ]
            },
        )
        with mock.patch(
            "keklib_cli.indexer.os.replace",
            side_effect=OSError(errno.EACCES, "replace denied"),
        ) as replace_mock:
            with self.assertRaises(IndexBuildError) as caught:
                ensure_index(library, self.paths)
        self.assertIn("cannot publish", str(caught.exception))
        replace_mock.assert_called_once()
        self.assertEqual(sha256_file(self.paths.index), old_digest)
        validate_index(self.paths.index)
        self.assertEqual(read_meta(self.paths.index).sha256, first.meta.sha256)
        self.assertEqual(self._build_dirs(), [])

    def test_recheck_under_exclusive_skips_rebuild(self) -> None:
        """Повторная проверка под exclusive: если индекс стал актуальным,
        пересборка не выполняется (§7, шаг 2)."""
        library = make_library(self.root, _BASIC_GROUPS)
        first = ensure_index(library, self.paths)
        self.assertTrue(first.rebuilt)

        # Под shared живой хеш «другой» → пересборка; под exclusive хеш
        # реальный → актуален. Имитация публикации другим процессом за время
        # между освобождением shared и взятием exclusive.
        with mock.patch(
            "keklib_cli.indexer.sha256_file",
            side_effect=["00" * 32, first.meta.sha256],
        ) as digest_mock:
            result = ensure_index(library, self.paths)
        self.assertFalse(result.rebuilt)
        self.assertEqual(digest_mock.call_count, 2)
        self.assertEqual(result.meta, first.meta)
        self.assertEqual(self._build_dirs(), [])


if __name__ == "__main__":
    unittest.main()
