"""Тесты хранилища индекса SQLite FTS5 (`keklib_cli.index_store`, `keklib_cli.text`)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from keklib_cli.errors import CatalogError, IndexBuildError
from keklib_cli.index_store import (
    RULES_VERSION,
    SCHEMA_VERSION,
    read_meta,
    validate_index,
    write_index,
)
from keklib_cli.models import Book, ImportStats, IndexMeta, Series
from keklib_cli.text import normalize


def _book(**overrides):
    """Книга с типовыми значениями; поля переопределяются по ключевым словам."""
    values = {
        "archive": "f.zip",
        "member": "1.fb2",
        "file_id": "1",
        "author": "Автор Авторов",
        "title": "Название книги",
        "language": "ru",
        "series": (),
        "size": None,
        "libid": "1",
        "metadata": {"AUTHOR": "Автор Авторов", "FILE": "1"},
    }
    values.update(overrides)
    return Book(**values)


def _meta(stats: ImportStats) -> IndexMeta:
    """Метаданные индекса, согласованные с текущими версиями схемы и правил."""
    return IndexMeta(
        root="/library",
        catalog_rel="catalog/lib.inpx",
        sha256="ab" * 32,
        schema_version=SCHEMA_VERSION,
        rules_version=RULES_VERSION,
        built_at="2026-02-11T12:34:56+00:00",
        stats=stats,
    )


class _NoFts5Connection:
    """Соединение, у которого отсутствует модуль FTS5.

    Прокси повторяет интерфейс, используемый хранилищем (``execute``/``close``),
    и отвергает создание виртуальной таблицы FTS5 так же, как SQLite без
    скомпилированного модуля: ``no such module: fts5``.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def execute(self, sql: str, parameters=()):
        if "using fts5" in sql.lower():
            raise sqlite3.OperationalError("no such module: fts5")
        return self._conn.execute(sql, parameters)

    def close(self):
        self._conn.close()


class IndexStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.path = self.base / "index.sqlite"

    def _connect_ro(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        self.addCleanup(conn.close)
        return conn

    def _write_two_books(self) -> None:
        books = [
            _book(member="1.fb2"),
            _book(member="2.fb2", file_id="2", libid="2"),
        ]
        write_index(self.path, books, _meta(ImportStats(rows=2, books=2)))

    def test_roundtrip(self):
        books = [
            _book(
                member="1.fb2",
                author="Перумов, Ник",
                title="Кольцо Тьмы",
                series=(
                    Series(name="Хроники Хьёрварда", number="1"),
                    Series(name="Иное", number=""),
                ),
                size=None,
                metadata={"AUTHOR": "Перумов, Ник", "FILE": "1", "GENRE": "sf"},
            ),
            _book(
                member="2.fb2",
                file_id="2",
                libid="2000",
                author="Булгаков Михаил",
                title="Мастер и Маргарита",
                size=1000,
                metadata={"AUTHOR": "Булгаков Михаил", "FILE": "2"},
            ),
        ]
        meta = _meta(ImportStats(rows=4, excluded=1, duplicates=1, books=2))
        write_index(self.path, books, meta)
        # Журнал DELETE: после закрытия пишущего соединения нет WAL-остатков.
        self.assertFalse(self.path.with_name(self.path.name + "-wal").exists())
        self.assertFalse(self.path.with_name(self.path.name + "-shm").exists())
        validate_index(self.path)
        validate_index(self.path, quick=True)
        self.assertEqual(read_meta(self.path), meta)
        conn = self._connect_ro()
        self.assertEqual(
            conn.execute("PRAGMA journal_mode").fetchone()[0], "delete"
        )
        rows = conn.execute(
            "SELECT archive, member, file_id, author, title, language, "
            "series_json, size, libid, metadata_json, norm_author, norm_title "
            "FROM book ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        first, second = rows
        self.assertEqual(first[0], "f.zip")
        self.assertEqual(first[1], "1.fb2")
        self.assertIsNone(first[7])
        self.assertEqual(json.loads(first[6]), [["Хроники Хьёрварда", "1"], ["Иное", ""]])
        self.assertEqual(
            json.loads(first[9]),
            {"AUTHOR": "Перумов, Ник", "FILE": "1", "GENRE": "sf"},
        )
        self.assertEqual(first[10], normalize("Перумов, Ник"))
        self.assertEqual(first[11], normalize("Кольцо Тьмы"))
        self.assertEqual(second[7], 1000)
        self.assertEqual(json.loads(second[6]), [])
        self.assertEqual(second[11], normalize("Мастер и Маргарита"))

    def test_stats_after_generator(self):
        stats = ImportStats()

        def books():
            # Счётчики растут по мере чтения, в том числе после yield:
            # сериализация должна произойти строго после исчерпания потока.
            stats.rows += 1
            stats.excluded += 1
            stats.rows += 1
            yield _book(member="1.fb2")
            stats.books += 1
            stats.rows += 1
            yield _book(member="2.fb2", file_id="2", libid="2")
            stats.books += 1

        write_index(self.path, books(), _meta(stats))
        self.assertEqual(
            (stats.rows, stats.excluded, stats.duplicates, stats.books), (3, 1, 0, 2)
        )
        stored = read_meta(self.path)
        self.assertEqual(stored.stats, stats)
        self.assertGreater(stored.stats.rows, 0)
        self.assertGreater(stored.stats.books, 0)
        validate_index(self.path)

    def test_normalize(self):
        self.assertEqual(normalize("ЁЖ"), "еж")
        # Совместимые полноширинные символы приводятся NFKC к ASCII.
        self.assertEqual(normalize("ＡＢ２"), "ab2")
        self.assertEqual(normalize("ＥＶＡ"), normalize("EVA"))
        self.assertEqual(normalize(""), "")

    def test_fts_prefix(self):
        books = [
            _book(
                member="1.fb2",
                author="Булгаков Михаил",
                title="Мастер и Маргарита",
            ),
            _book(member="2.fb2", file_id="2", libid="2", title="Иное название"),
        ]
        write_index(self.path, books, _meta(ImportStats(rows=2, books=2)))
        conn = self._connect_ro()
        found = conn.execute(
            "SELECT rowid FROM book_fts WHERE book_fts MATCH ?",
            ('"булг"* AND "мастер"*',),
        ).fetchall()
        self.assertEqual(found, [(1,)])

    def test_empty_index(self):
        write_index(self.path, [], _meta(ImportStats()))
        validate_index(self.path)
        validate_index(self.path, quick=True)
        self.assertEqual(read_meta(self.path).stats, ImportStats())
        conn = self._connect_ro()
        self.assertEqual(conn.execute("SELECT count(*) FROM book").fetchone()[0], 0)

    def test_missing_fts5(self):
        real_connect = sqlite3.connect

        def connect_without_fts5(*args, **kwargs):
            return _NoFts5Connection(real_connect(*args, **kwargs))

        with mock.patch(
            "keklib_cli.index_store.sqlite3.connect",
            side_effect=connect_without_fts5,
        ):
            with self.assertRaises(IndexBuildError) as ctx:
                write_index(self.path, [_book()], _meta(ImportStats(rows=1, books=1)))
        self.assertIn("fts5", str(ctx.exception).lower())
        # Неудавшаяся постройка не оставляет своих файлов.
        self.assertEqual(list(self.base.iterdir()), [])

    def test_corrupt_or_wrong_count(self):
        # Мусорные байты вместо базы.
        self.path.write_bytes(b"definitely not an sqlite database")
        with self.subTest(case="garbage-bytes"):
            with self.assertRaises(IndexBuildError):
                validate_index(self.path)
            with self.assertRaises(IndexBuildError):
                read_meta(self.path)
        # Валидная база, но фактическое число книг не совпадает со счётчиком.
        self.path.unlink()
        self._write_two_books()
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DELETE FROM book WHERE id = 1")
            conn.commit()
        finally:
            conn.close()
        with self.subTest(case="wrong-count"):
            with self.assertRaises(IndexBuildError):
                validate_index(self.path)
            with self.assertRaises(IndexBuildError):
                validate_index(self.path, quick=True)
        # Валидная база с утраченным полем метаданных.
        self.path.unlink()
        self._write_two_books()
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DELETE FROM meta WHERE key = 'sha256'")
            conn.commit()
        finally:
            conn.close()
        with self.subTest(case="missing-meta-key"):
            with self.assertRaises(IndexBuildError):
                validate_index(self.path)

    def test_existing_target_preserved(self):
        self.path.write_bytes(b"foreign payload")
        with self.assertRaises(IndexBuildError):
            write_index(self.path, [_book()], _meta(ImportStats(rows=1, books=1)))
        self.assertEqual(self.path.read_bytes(), b"foreign payload")
        self.assertEqual(list(self.base.iterdir()), [self.path])

    def test_foreign_sidecars_preserved(self):
        # Эксклюзивное создание основного файла не даёт владения чужими
        # соседними -journal/-wal/-shm: постройка отвергается, байты чужих
        # файлов сохраняются, новых файлов не появляется.
        for suffix in ("-wal", "-journal", "-shm"):
            with self.subTest(suffix=suffix):
                base = self.base / suffix.lstrip("-")
                base.mkdir()
                path = base / "index.sqlite"
                sidecar = base / f"index.sqlite{suffix}"
                sidecar.write_bytes(b"foreign sidecar payload")
                with self.assertRaises(IndexBuildError):
                    write_index(
                        path, [_book()], _meta(ImportStats(rows=1, books=1))
                    )
                self.assertEqual(sidecar.read_bytes(), b"foreign sidecar payload")
                self.assertFalse(path.exists())
                self.assertEqual(list(base.iterdir()), [sidecar])

    def test_foreign_dangling_sidecar_symlinks_preserved(self):
        # Битая чужая symlink на sidecar-имя — существующая запись каталога:
        # Path.exists() её не видит (цель отсутствует), но удалять чужую
        # запись нельзя; проверка обязана смотреть на саму запись.
        for suffix in ("-wal", "-journal", "-shm"):
            with self.subTest(suffix=suffix):
                base = self.base / suffix.lstrip("-")
                base.mkdir()
                path = base / "index.sqlite"
                sidecar = base / f"index.sqlite{suffix}"
                try:
                    os.symlink("foreign-missing-target", sidecar)
                except OSError as exc:
                    self.skipTest(f"symlink creation unavailable: {exc}")
                with self.assertRaises(IndexBuildError):
                    write_index(
                        path, [_book()], _meta(ImportStats(rows=1, books=1))
                    )
                self.assertTrue(sidecar.is_symlink())
                self.assertEqual(os.readlink(sidecar), "foreign-missing-target")
                self.assertFalse((base / "foreign-missing-target").exists())
                self.assertFalse(path.exists())
                self.assertEqual(list(base.iterdir()), [sidecar])

    def test_meta_invalid_value_types(self):
        # NULL и BLOB в meta.value — штатное повреждение индекса; оба
        # публичных читателя обязаны отвечать IndexBuildError, а не TypeError.
        cases = {
            "null-schema": "UPDATE meta SET value=NULL WHERE key='schema_version'",
            "blob-schema": "UPDATE meta SET value=X'00ff' WHERE key='schema_version'",
            "null-stats": "UPDATE meta SET value=NULL WHERE key='stats'",
            "blob-stats": "UPDATE meta SET value=X'00ff' WHERE key='stats'",
        }
        for name, mutation in cases.items():
            with self.subTest(case=name):
                self.path.unlink(missing_ok=True)
                self._write_two_books()
                conn = sqlite3.connect(self.path)
                try:
                    conn.execute(mutation)
                    conn.commit()
                finally:
                    conn.close()
                with self.assertRaises(IndexBuildError):
                    read_meta(self.path)
                with self.assertRaises(IndexBuildError):
                    validate_index(self.path)

    def test_validate_rejects_fake_fts_table(self):
        # Обычная таблица с именем book_fts не годится для MATCH: валидация
        # обязана отличать настоящую виртуальную таблицу FTS5 с нужными
        # параметрами.
        self._write_two_books()
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DROP TABLE book_fts")
            conn.execute("CREATE TABLE book_fts(norm_title TEXT, norm_author TEXT)")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(IndexBuildError) as ctx:
            validate_index(self.path)
        self.assertIn("book_fts", str(ctx.exception))

    def test_validate_rejects_missing_unique_pair(self):
        # Схема book без UNIQUE(archive, member) пропускает дубликаты книг и
        # не является схемой хранилища.
        self._write_two_books()
        columns = (
            "id INTEGER PRIMARY KEY, archive TEXT, member TEXT, file_id TEXT, "
            "author TEXT, title TEXT, language TEXT, series_json TEXT, "
            "size INTEGER, libid TEXT, metadata_json TEXT, norm_author TEXT, "
            "norm_title TEXT"
        )
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(f"CREATE TABLE book_copy({columns})")
            conn.execute(
                "INSERT INTO book_copy SELECT id, archive, member, file_id, "
                "author, title, language, series_json, size, libid, "
                "metadata_json, norm_author, norm_title FROM book"
            )
            conn.execute("DROP TABLE book")
            conn.execute("ALTER TABLE book_copy RENAME TO book")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(IndexBuildError) as ctx:
            validate_index(self.path)
        self.assertIn("UNIQUE(archive, member)", str(ctx.exception))

    def test_fts_content_mismatch_fails_build(self):
        # Пишущая проверка обязана ловить расхождение FTS-индекса с
        # content-table, а не только внутреннюю целостность FTS.
        real_connect = sqlite3.connect

        class TamperingConnection:
            def __init__(self, conn: sqlite3.Connection):
                self._conn = conn

            def execute(self, sql, parameters=()):
                if "integrity-check" in sql:
                    self._conn.execute(
                        "UPDATE book SET norm_title = norm_title || 'x'"
                    )
                return self._conn.execute(sql, parameters)

            def executemany(self, sql, rows):
                return self._conn.executemany(sql, rows)

            def close(self):
                self._conn.close()

        with mock.patch(
            "keklib_cli.index_store.sqlite3.connect",
            side_effect=lambda *args, **kwargs: TamperingConnection(
                real_connect(*args, **kwargs)
            ),
        ):
            with self.assertRaises(IndexBuildError) as ctx:
                write_index(self.path, [_book()], _meta(ImportStats(rows=1, books=1)))
        self.assertIn("checksum mismatch", str(ctx.exception))
        self.assertEqual(list(self.base.iterdir()), [])

    def test_generator_error_cleans_up(self):
        # Ошибка источника книг пробрасывается как есть, собственные файлы
        # неудавшейся постройки удаляются.
        def books():
            yield _book()
            raise CatalogError("catalog failed mid-import")

        with self.assertRaises(CatalogError):
            write_index(self.path, books(), _meta(ImportStats(rows=1, books=1)))
        self.assertEqual(list(self.base.iterdir()), [])

    def test_keyboard_interrupt_cleans_up(self):
        # Отмена пользователя сохраняет каталог чистым: без своего файла
        # и без journal-остатков.
        def books():
            yield _book()
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            write_index(self.path, books(), _meta(ImportStats(rows=1, books=1)))
        self.assertEqual(list(self.base.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
