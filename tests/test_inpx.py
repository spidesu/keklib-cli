"""Тесты потокового импорта INPX и объединения серий (`keklib_cli.inpx`)."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from keklib_cli.config import inspect_library
from keklib_cli.errors import CatalogError
from keklib_cli.inpx import iter_books, read_structure
from keklib_cli.models import ImportStats, Series
from keklib_cli.source_names import validate_source_name
from tests.fixtures import DEFAULT_FIELDS, make_library, make_raw_catalog

ROW = {
    "AUTHOR": "Перумов, Ник",
    "GENRE": "фэнтези",
    "TITLE": "Кольцо Тьмы",
    "SERIES": "Хроники Хьёрварда",
    "SERNO": "1",
    "FILE": "204000",
    "SIZE": "1000",
    "LIBID": "204000",
    "DEL": "0",
    "EXT": "fb2",
    "DATE": "2026-01-01",
    "LANG": "ru",
    "LIBRATE": "5",
    "KEYWORDS": "",
    "YEAR": "2005",
    "SOURCELIB": "",
}

UTF8_BOM = b"\xef\xbb\xbf"


def structure_bytes(fields=DEFAULT_FIELDS) -> bytes:
    """Байты корректного `structure.info` для заданного порядка полей."""
    return (";".join(fields) + ";\n").encode("utf-8")


def row_bytes(row, fields=DEFAULT_FIELDS) -> bytes:
    """Байты одной строки INP без завершающего перевода строки."""
    return "\x04".join(row.get(field, "") for field in fields).encode("utf-8")


class InpxTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def collect(self, library):
        stats = ImportStats()
        books = list(iter_books(library.catalog, stats))
        return books, stats

    @staticmethod
    def _break_first_deflated_byte(catalog: Path, entry: str) -> None:
        """Испортить первый байт сжатых данных ``entry`` (валидный, но
        битый deflate-поток: заголовки остаются корректными)."""
        raw = bytearray(catalog.read_bytes())
        local = raw.find(entry.encode("ascii"))
        header_at = raw.rfind(b"PK\x03\x04", 0, local)
        assert header_at >= 0
        data_at = local + len(entry)
        raw[data_at] = 0x07
        catalog.write_bytes(raw)

    @staticmethod
    def _break_central_filename(catalog: Path) -> None:
        """UTF-8 flag + недопустимый первый байт имени в центральном каталоге."""
        raw = bytearray(catalog.read_bytes())
        central = raw.find(b"PK\x01\x02")
        assert central >= 0
        raw[central + 9] |= 0x08  # general purpose flags: бит 0x800 (UTF-8)
        raw[central + 46] = 0xFF  # первый байт имени — не UTF-8
        catalog.write_bytes(raw)

    def test_order_bom_crlf(self):
        fields = (
            "TITLE",
            "GENRE",
            "AUTHOR",
            "FILE",
            "SIZE",
            "LIBID",
            "DEL",
            "EXT",
            "DATE",
            "LANG",
        )
        values = (
            "Кольцо Тьмы",
            "фэнтези",
            "Перумов, Ник",
            "204000",
            "1000",
            "204000",
            "0",
            "fb2",
            "2026-01-01",
            "ru",
        )
        structure = UTF8_BOM + (";".join(fields) + ";\r\n").encode("utf-8")
        inp = UTF8_BOM + ("\x04".join(values) + "\r\n").encode("utf-8")
        library = make_raw_catalog(self.base / "library", [structure], {"fb2-001": inp})

        books, stats = self.collect(library)

        self.assertEqual(len(books), 1)
        book = books[0]
        self.assertEqual(book.title, "Кольцо Тьмы")
        self.assertEqual(book.author, "Перумов, Ник")
        self.assertEqual(book.archive, "fb2-001.zip")
        self.assertEqual(book.member, "204000.fb2")
        self.assertEqual(book.size, 1000)
        self.assertEqual(book.language, "ru")
        self.assertEqual(stats.books, 1)
        stats.check()
        self.assertEqual(read_structure(library.catalog), fields)

    def test_filter(self):
        rows = [
            {**ROW, "DEL": "0", "EXT": "fb2"},
            {**ROW, "DEL": "0", "EXT": "epub"},
            {**ROW, "DEL": "1", "EXT": "fb2"},
            {**ROW, "DEL": "2", "EXT": "fb2"},
            {**ROW, "DEL": "", "EXT": "fb2"},
        ]
        library = make_library(self.base / "library", {"fb2-001": rows})

        books, stats = self.collect(library)

        self.assertEqual(stats.rows, 5)
        self.assertEqual(stats.books, 1)
        self.assertEqual(stats.excluded, 4)
        self.assertEqual(stats.duplicates, 0)
        stats.check()
        self.assertEqual([book.member for book in books], ["204000.fb2"])

    def test_merge_series(self):
        rows = [
            {**ROW, "SERIES": "Хроники Хьёрварда", "SERNO": "1"},
            {**ROW, "SERIES": "Звёздный меч", "SERNO": "2"},
            {**ROW, "SERIES": "Хроники Хьёрварда", "SERNO": "1"},
        ]
        library = make_library(self.base / "library", {"fb2-001": rows})

        books, stats = self.collect(library)

        self.assertEqual(len(books), 1)
        self.assertEqual(
            books[0].series,
            (
                Series(name="Хроники Хьёрварда", number="1"),
                Series(name="Звёздный меч", number="2"),
            ),
        )
        self.assertEqual(stats.rows, 3)
        self.assertEqual(stats.books, 1)
        self.assertEqual(stats.duplicates, 2)
        self.assertEqual(stats.excluded, 0)
        stats.check()

    def test_other_member_is_edition(self):
        rows = [
            {**ROW, "FILE": "204000"},
            {**ROW, "FILE": "204001", "LIBID": "204001"},
        ]
        library = make_library(self.base / "library", {"fb2-001": rows})

        books, stats = self.collect(library)

        self.assertEqual(len(books), 2)
        self.assertEqual([book.member for book in books], ["204000.fb2", "204001.fb2"])
        self.assertTrue(all(book.title == ROW["TITLE"] for book in books))
        self.assertEqual(stats.books, 2)
        self.assertEqual(stats.duplicates, 0)
        stats.check()

    def test_conflict_reports_both_lines(self):
        rows = [
            {**ROW, "TITLE": "Мастер и Маргарита"},
            {**ROW, "TITLE": "Собачье сердце"},
        ]
        library = make_library(self.base / "library", {"fb2-001": rows})

        with self.assertRaises(CatalogError) as ctx:
            self.collect(library)

        message = str(ctx.exception)
        self.assertIn("fb2-001.inp", message)
        self.assertIn("Мастер и Маргарита", message)
        self.assertIn("Собачье сердце", message)
        self.assertIn("line 1", message)
        self.assertIn("line 2", message)

    def test_duplicate_structure(self):
        structure = structure_bytes()
        row = row_bytes(ROW)
        cases = {
            "missing-structure": [],
            "duplicate-structure": [structure, structure],
            "duplicate-field": [
                b"AUTHOR;GENRE;TITLE;AUTHOR;SERNO;FILE;SIZE;LIBID;DEL;EXT;\n"
            ],
            "missing-required": [
                b"AUTHOR;GENRE;TITLE;SERIES;SERNO;FILE;SIZE;LIBID;EXT;\n"
            ],
        }
        for name, structures in cases.items():
            with self.subTest(case=name):
                library = make_raw_catalog(self.base / name, structures, {"fb2-001": row})
                with self.assertRaises(CatalogError):
                    read_structure(library.catalog)
                with self.assertRaises(CatalogError):
                    list(iter_books(library.catalog, ImportStats()))

    def test_bad_encoding_or_size(self):
        structure = structure_bytes()
        cases = {
            "invalid-utf8": b"\xd0\xd0\xd0\n",
            "negative-size": row_bytes({**ROW, "SIZE": "-100"}) + b"\n",
            "float-size": row_bytes({**ROW, "SIZE": "1.5"}) + b"\n",
            "text-size": row_bytes({**ROW, "SIZE": "N/A"}) + b"\n",
        }
        for name, inp in cases.items():
            with self.subTest(case=name):
                library = make_raw_catalog(self.base / name, [structure], {"fb2-001": inp})
                with self.assertRaises(CatalogError) as ctx:
                    list(iter_books(library.catalog, ImportStats()))
                message = str(ctx.exception)
                self.assertIn("fb2-001.inp", message)
                self.assertIn("line 1", message)

    def test_size_sqlite_range_boundaries(self):
        """SIZE согласован со столбцом SQLite INTEGER: 0..2**63-1 (F4).

        Верхняя граница проходит импорт как точное целое; всё, что выше,
        отвергается адресным CatalogError с пояснением поддерживаемого
        диапазона — не OverflowError из sqlite3 и не молчаливое усечение.
        """
        structure = structure_bytes()
        max_size = "9223372036854775807"  # 2**63 - 1
        library = make_raw_catalog(
            self.base / "ok",
            [structure],
            {"fb2-001": row_bytes({**ROW, "SIZE": max_size}) + b"\n"},
        )
        books, stats = self.collect(library)
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0].size, 2**63 - 1)
        self.assertEqual(stats.books, 1)

        oversized = "9223372036854775808"  # 2**63
        library = make_raw_catalog(
            self.base / "over",
            [structure],
            {"fb2-001": row_bytes({**ROW, "SIZE": oversized}) + b"\n"},
        )
        with self.assertRaises(CatalogError) as ctx:
            self.collect(library)
        message = str(ctx.exception)
        self.assertIn("fb2-001.inp", message)
        self.assertIn("line 1", message)
        self.assertIn(oversized, message)
        self.assertIn("0", message)
        self.assertIn(max_size, message)

    def test_corrupt_deflated_structure_is_catalog_error(self):
        """Битый deflate-поток structure.info — CatalogError, не zlib.error (F3)."""
        structure = structure_bytes()
        library = make_raw_catalog(self.base / "lib", [structure], {})
        self._break_first_deflated_byte(library.catalog, "structure.info")
        for action in (read_structure, lambda catalog: list(iter_books(catalog, ImportStats()))):
            with self.assertRaises(CatalogError) as ctx:
                action(library.catalog)
            message = str(ctx.exception)
            self.assertIn("structure.info", message)
            self.assertIn(str(library.catalog), message)

    def test_corrupt_deflated_inp_is_catalog_error(self):
        """Битый deflate-поток INP — CatalogError с именем записи, не zlib.error."""
        structure = structure_bytes()
        library = make_raw_catalog(
            self.base / "lib", [structure],
            {"fb2-001": row_bytes(ROW) + b"\n" + b"y" * 200},
        )
        self._break_first_deflated_byte(library.catalog, "fb2-001.inp")
        with self.assertRaises(CatalogError) as ctx:
            self.collect(library)
        message = str(ctx.exception)
        self.assertIn("fb2-001.inp", message)

    def test_malformed_central_filename_is_catalog_error(self):
        """Битое UTF-8-имя центрального каталога — CatalogError на открытии."""
        structure = structure_bytes()
        library = make_raw_catalog(
            self.base / "lib", [structure], {"fb2-001": row_bytes(ROW) + b"\n"}
        )
        self._break_central_filename(library.catalog)
        for action in (read_structure, lambda catalog: list(iter_books(catalog, ImportStats()))):
            with self.assertRaises(CatalogError) as ctx:
                action(library.catalog)
            self.assertIn(str(library.catalog), str(ctx.exception))

    def test_inp_truncated_data_is_catalog_error(self):
        """Физически оборванный файл INPX — CatalogError, не EOFError/BadZipFile."""
        structure = structure_bytes()
        library = make_raw_catalog(
            self.base / "lib", [structure], {"fb2-001": row_bytes(ROW) + b"\n"}
        )
        raw = bytearray(library.catalog.read_bytes())
        central = raw.find(b"PK\x01\x02")
        # Оборвать файл посреди сжатых данных INP, сохранив весь
        # центральный каталог и EOCD: повреждение проявляется при
        # открытии контейнера — отказ адресует путь каталога.
        local_inp = raw.find(b"fb2-001.inp")
        cut = local_inp + len("fb2-001.inp") + 2  # лишь 2 байта сжатых данных
        head = bytes(raw[:cut])
        library.catalog.write_bytes(head + bytes(raw[central:]))
        with self.assertRaises(CatalogError) as ctx:
            self.collect(library)
        self.assertIn(str(library.catalog), str(ctx.exception))

    def test_inp_compression_methods(self):
        """Неподдерживаемый метод сжатия INP — CatalogError; методы, которые
        фактически читает zipfile (bzip2), импортируются без новой политики."""
        structure = structure_bytes()
        payload = row_bytes(ROW) + b"\n"
        root = self.base / "lib"
        (root / "lib.rus.ec").mkdir(parents=True)
        catalog = root / "librusec_flib.inpx"

        def write_catalog(inp_compress: int, *, method_99: bool = False):
            import zipfile as _zipfile
            with _zipfile.ZipFile(catalog, "w") as container:
                container.writestr("structure.info", structure)
                info = _zipfile.ZipInfo("fb2-001.inp", (1980, 1, 1, 0, 0, 0))
                container.writestr(info, payload, compress_type=inp_compress)
            if method_99:
                # zipfile не пишет чужие методы: записать DEFLATED и
                # пропатчить номер метода (локальный +8, центральный +10)
                # на 99 — open() отдаёт NotImplementedError.
                raw = bytearray(catalog.read_bytes())
                local = raw.find(b"fb2-001.inp")
                header = raw.rfind(b"PK\x03\x04", 0, local)
                raw[header + 8] = 99
                raw[header + 9] = 0
                central_rec = raw.find(b"fb2-001.inp", local + 1)
                central = raw.rfind(b"PK\x01\x02", 0, central_rec)
                raw[central + 10] = 99
                raw[central + 11] = 0
                catalog.write_bytes(raw)

        write_catalog(zipfile.ZIP_BZIP2)
        library = inspect_library(root)
        books, _ = self.collect(library)
        self.assertEqual([book.file_id for book in books], ["204000"])

        write_catalog(zipfile.ZIP_DEFLATED, method_99=True)
        library = inspect_library(root)
        with self.assertRaises(CatalogError) as ctx:
            self.collect(library)
        self.assertIn("fb2-001.inp", str(ctx.exception))

    def test_truncated_required_fields(self):
        structure = structure_bytes()
        short = (
            "\x04".join([ROW["AUTHOR"], ROW["GENRE"], ROW["TITLE"]]).encode("utf-8")
            + b"\n"
        )
        extra = row_bytes(ROW) + b"\x04extra"
        cases = {"short-row": short, "extra-field": extra}
        for name, inp in cases.items():
            with self.subTest(case=name):
                library = make_raw_catalog(self.base / name, [structure], {"fb2-001": inp})
                with self.assertRaises(CatalogError) as ctx:
                    list(iter_books(library.catalog, ImportStats()))
                message = str(ctx.exception)
                self.assertIn("fb2-001.inp", message)
                self.assertIn("line 1", message)

    def test_real_catalog_trailing_field_separator(self):
        """Реальная конвенция Lib.rus.ec: строка INP заканчивается `\\x04`.

        Строки настоящего каталога несут завершающий разделитель поля перед
        переводом строки (как завершающий `;` в `structure.info`): полей 16,
        значений 17, последнее пустое. Такая строка — корректная запись, а не
        повреждение; непустой лишний хвост и два разделителя подряд в конце
        остаются ошибкой.
        """
        structure = structure_bytes()
        real_line = row_bytes(ROW) + b"\x04\r\n"
        library = make_raw_catalog(
            self.base / "real", [structure], {"fb2-000007-788888_lost": real_line}
        )

        books, stats = self.collect(library)

        self.assertEqual(len(books), 1)
        book = books[0]
        self.assertEqual(book.archive, "fb2-000007-788888_lost.zip")
        self.assertEqual(book.member, "204000.fb2")
        self.assertEqual(book.title, ROW["TITLE"])
        self.assertEqual(book.metadata["SOURCELIB"], "")
        self.assertEqual(stats.rows, 1)
        stats.check()
        # Повтор той же строки с завершающим разделителем — прежняя книга.
        twice = real_line + real_line
        library_twice = make_raw_catalog(
            self.base / "real-twice", [structure], {"fb2-000007-788888_lost": twice}
        )
        books_twice, stats_twice = self.collect(library_twice)
        self.assertEqual(len(books_twice), 1)
        self.assertEqual(stats_twice.duplicates, 1)
        # Хвост с данными и двойной завершающий разделитель — повреждение.
        for name, payload in {
            "nonempty-tail": row_bytes(ROW) + b"\x04data\r\n",
            "double-tail": row_bytes(ROW) + b"\x04\x04\r\n",
        }.items():
            with self.subTest(case=name):
                broken = make_raw_catalog(
                    self.base / name, [structure], {"fb2-001": payload}
                )
                with self.assertRaises(CatalogError) as ctx:
                    list(iter_books(broken.catalog, ImportStats()))
                self.assertIn("fb2-001.inp", str(ctx.exception))

    def test_unknown_and_optional_fields(self):
        fields = (*DEFAULT_FIELDS, "CUSTOM")
        row = {
            **ROW,
            "AUTHOR": "Гаррисон, Гарри:Скаландис, Ант",
            "SIZE": "",
            "LANG": "en",
            "CUSTOM": "заметка редакции",
        }
        library = make_library(self.base / "library", {"fb2-001": [row]}, fields=fields)

        books, stats = self.collect(library)

        self.assertEqual(len(books), 1)
        book = books[0]
        self.assertIsNone(book.size)
        self.assertEqual(book.author, "Гаррисон, Гарри:Скаландис, Ант")
        self.assertEqual(book.metadata["AUTHOR"], book.author)
        self.assertEqual(book.metadata["CUSTOM"], "заметка редакции")
        self.assertEqual(book.metadata["LIBID"], "204000")
        self.assertNotIn("SERIES", book.metadata)
        self.assertNotIn("SERNO", book.metadata)
        self.assertEqual(book.language, "en")
        self.assertEqual(book.series, (Series(name=ROW["SERIES"], number="1"),))
        stats.check()

    def test_empty_catalog(self):
        libraries = {
            "no-inp": make_library(self.base / "no-inp", {}),
            "empty-inp": make_library(self.base / "empty-inp", {"fb2-001": []}),
        }
        for name, library in libraries.items():
            with self.subTest(case=name):
                stats = ImportStats()
                self.assertEqual(list(iter_books(library.catalog, stats)), [])
                self.assertEqual(
                    (stats.rows, stats.excluded, stats.duplicates, stats.books),
                    (0, 0, 0, 0),
                )
                stats.check()

    def test_duplicate_inp_entries(self):
        structure = structure_bytes()
        first = row_bytes({**ROW, "TITLE": "первая строка"})
        second = row_bytes({**ROW, "TITLE": "вторая строка"})
        cases = {
            "same-name": [("a.inp", first), ("a.inp", second)],
            "suffix-case": [("a.inp", first), ("a.INP", second)],
        }
        for name, entries in cases.items():
            with self.subTest(case=name):
                library = make_raw_catalog(
                    self.base / name, [structure], {}, entries=entries
                )
                with self.assertRaises(CatalogError) as ctx:
                    list(iter_books(library.catalog, ImportStats()))
                message = str(ctx.exception)
                self.assertIn("'a.inp'", message)
                if name == "suffix-case":
                    self.assertIn("'a.INP'", message)
                    self.assertIn("'a.zip'", message)
                else:
                    self.assertIn("duplicate INP entry", message)

    def test_blank_record_is_malformed(self):
        structure = structure_bytes()
        row = row_bytes(ROW)
        cases = {
            "blank-line": (row + b"\n" + b"\n", 2),
            "bom-only-line": (UTF8_BOM + b"\n" + row + b"\n", 1),
            "crlf-only-line": (row + b"\r\n" + b"\r\n", 2),
        }
        for name, (inp, lineno) in cases.items():
            with self.subTest(case=name):
                library = make_raw_catalog(
                    self.base / name, [structure], {"fb2-001": inp}
                )
                with self.assertRaises(CatalogError) as ctx:
                    list(iter_books(library.catalog, ImportStats()))
                message = str(ctx.exception)
                self.assertIn("fb2-001.inp", message)
                self.assertIn(f"line {lineno}", message)
                self.assertIn("record is empty", message)
        with self.subTest(case="empty-inp-is-valid"):
            library = make_raw_catalog(
                self.base / "empty", [structure], {"fb2-001": b""}
            )
            stats = ImportStats()
            self.assertEqual(list(iter_books(library.catalog, stats)), [])
            stats.check()

    def test_long_digit_size(self):
        structure = structure_bytes()
        inp = row_bytes({**ROW, "SIZE": "9" * 5000}) + b"\n"
        library = make_raw_catalog(self.base / "library", [structure], {"fb2-001": inp})

        with self.assertRaises(CatalogError) as ctx:
            list(iter_books(library.catalog, ImportStats()))

        message = str(ctx.exception)
        self.assertIn("fb2-001.inp", message)
        self.assertIn("line 1", message)
        self.assertIn("SIZE", message)
        self.assertIn("5000 digits", message)
        # Само значение не дублируется в диагностике.
        self.assertLess(len(message), 300)

    def test_encrypted_structure(self):
        library = make_raw_catalog(
            self.base / "library",
            [structure_bytes()],
            {"fb2-001": row_bytes(ROW) + b"\n"},
            encrypted_structure=True,
        )
        with self.subTest(case="read-structure"):
            with self.assertRaises(CatalogError) as ctx:
                read_structure(library.catalog)
            message = str(ctx.exception)
            self.assertIn("structure.info", message)
            self.assertIn("encrypted", message)
        with self.subTest(case="iter-books"):
            with self.assertRaises(CatalogError) as ctx:
                list(iter_books(library.catalog, ImportStats()))
            self.assertIn("structure.info", str(ctx.exception))

    def test_cross_platform_source_names(self):
        bad_names = [
            "../x",
            "..\\x",
            "C:x",
            "C:\\x",
            "c:",
            "\\\\server\\share",
            "\\\\?\\C:\\x",
            "\\\\.\\COM1",
            "a\x00b",
            "tab\there",
            "line\nbreak",
            "sub/x.inp",
            "sub\\x.inp",
            ".",
            "..",
            "",
        ]
        for name in bad_names:
            with self.subTest(name=name):
                with self.assertRaises(CatalogError):
                    validate_source_name(name)
        for name in ("fb2-001.inp", "fb2-001.zip", "204000.fb2", "Кольцо Тьмы.fb2"):
            with self.subTest(name=name):
                self.assertEqual(validate_source_name(name), name)
        for index, file_value in enumerate(("../evil", "..\\evil", "C:evil")):
            with self.subTest(file=file_value):
                library = make_library(
                    self.base / f"book-{index}",
                    {"fb2-001": [{**ROW, "FILE": file_value}]},
                )
                with self.assertRaises(CatalogError) as ctx:
                    list(iter_books(library.catalog, ImportStats()))
                self.assertIn("fb2-001.inp", str(ctx.exception))
        with self.subTest(case="inp-entry-with-path"):
            library = make_raw_catalog(
                self.base / "nested", [structure_bytes()], {"sub/x": row_bytes(ROW)}
            )
            with self.assertRaises(CatalogError):
                list(iter_books(library.catalog, ImportStats()))


if __name__ == "__main__":
    unittest.main()
