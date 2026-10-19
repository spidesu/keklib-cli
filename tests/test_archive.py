"""Тесты чтения точного файла книги из ZIP (`keklib_cli.archive`)."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from keklib_cli.archive import open_book
from keklib_cli.errors import ExtractionError
from keklib_cli.models import Book

from tests.fixtures import make_library

ZIP_NAME = "flib.zip"
MEMBER = "204000.fb2"
FB2_BYTES = (
    b"<?xml version='1.0' encoding='utf-8'?>\n"
    b"<FictionBook><body>\xd0\x9a\xd0\xbd\xd0\xb8\xd0\xb3\xd0\xb0"
    b"</body></FictionBook>"
)


def _book(archive: str = ZIP_NAME, member: str = MEMBER) -> Book:
    """Книга с минимальными метаданными; извлечению нужны только имена."""
    return Book(
        archive=archive,
        member=member,
        file_id="204000",
        author="Перумов, Ник",
        title="Кольцо Тьмы",
        language="ru",
        series=(),
        size=len(FB2_BYTES),
        libid="204000",
        metadata={},
    )


def _zip_info(
    name: str, *, compress_type: int = zipfile.ZIP_DEFLATED
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3  # UNIX: старшие биты external_attr — режим файла
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.compress_type = compress_type
    return info


def _write_zip(
    path: Path,
    entries: list[tuple[zipfile.ZipInfo, bytes]],
    *,
    duplicate_names: bool = False,
    compress_type: int | None = None,
) -> Path:
    """Записать тестовый ZIP с точными ZipInfo записей."""
    with warnings.catch_warnings():
        if duplicate_names:
            warnings.filterwarnings(
                "ignore", message="Duplicate name", category=UserWarning
            )
        with zipfile.ZipFile(path, "w") as archive:
            for info, payload in entries:
                if compress_type is None:
                    archive.writestr(info, payload)
                else:
                    archive.writestr(info, payload, compress_type=compress_type)
    return path


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.library = make_library(self.base / "library", {})
        self.archives = self.library.archives

    def _spy_zipfile_close(self, events: list[str]):
        """Подменить zipfile.ZipFile классом, записывающим свои close.

        Каждый экземпляр удерживается списком: деструктор zipfile (в
        CPython 3.14 ``__del__`` вызывает ``close()`` повторно) не может
        добавить ложное событие, поэтому каждое записанное закрытие —
        явный ``close`` менеджера ``open_book``, а не сборка мусора.
        """
        instances: list[zipfile.ZipFile] = []

        class _SpyZipFile(zipfile.ZipFile):
            def __init__(self, *args: object, **kwargs: object) -> None:
                instances.append(self)  # type: ignore[arg-type]
                super().__init__(*args, **kwargs)  # type: ignore[arg-type]

            def close(self) -> None:
                events.append("zipfile closed")
                super().close()

        return mock.patch.object(zipfile, "ZipFile", _SpyZipFile)

    def test_exact_bytes(self):
        zip_path = _write_zip(
            self.archives / ZIP_NAME, [(_zip_info(MEMBER), FB2_BYTES)]
        )
        before = zip_path.read_bytes()

        events: list[str] = []
        with self._spy_zipfile_close(events):
            with open_book(self.library, _book()) as stream:
                self.assertEqual(stream.read(), FB2_BYTES)
            self.assertTrue(stream.closed)
            with self.assertRaises(ValueError):
                stream.read()
            with open_book(self.library, _book()) as chunked:
                pieces = []
                while True:
                    piece = chunked.read(7)
                    if not piece:
                        break
                    pieces.append(piece)
            self.assertEqual(b"".join(pieces), FB2_BYTES)

        self.assertEqual(events, ["zipfile closed", "zipfile closed"])
        self.assertEqual(zip_path.read_bytes(), before)

    def test_missing_new_zip_or_member(self):
        # Архив ещё не скопирован в библиотеку (§9: книга видна до ZIP).
        with self.assertRaises(ExtractionError) as ctx:
            with open_book(self.library, _book()):
                pass
        message = str(ctx.exception)
        self.assertIn(ZIP_NAME, message)
        self.assertIn(MEMBER, message)

        # Архив есть, выбранного member нет; соседний member не читается.
        _write_zip(
            self.archives / ZIP_NAME, [(_zip_info("other.fb2"), b"neighbor")]
        )
        with self.assertRaises(ExtractionError) as ctx:
            with open_book(self.library, _book()):
                pass
        message = str(ctx.exception)
        self.assertIn(ZIP_NAME, message)
        self.assertIn(MEMBER, message)

    def test_duplicate_member(self):
        zip_path = _write_zip(
            self.archives / ZIP_NAME,
            [(_zip_info(MEMBER), FB2_BYTES), (_zip_info(MEMBER), b"second")],
            duplicate_names=True,
        )
        before = zip_path.read_bytes()

        with self.assertRaises(ExtractionError) as ctx:
            with open_book(self.library, _book()):
                pass
        message = str(ctx.exception)
        self.assertIn(ZIP_NAME, message)
        self.assertIn(MEMBER, message)
        self.assertIn("совпадающих записей: 2", message)

        self.assertEqual(zip_path.read_bytes(), before)

    def test_crc_corruption(self):
        # ZIP_STORED с изменённым байтом данных и прежним CRC в заголовках.
        payload = b"CRC-FB2-PAYLOAD-0123456789"
        zip_path = _write_zip(
            self.archives / ZIP_NAME,
            [(_zip_info(MEMBER, compress_type=zipfile.ZIP_STORED), payload)],
        )
        flipped = b"X" + payload[1:]
        raw = zip_path.read_bytes()
        self.assertEqual(raw.count(payload), 1)  # payload ровно один раз
        zip_path.write_bytes(raw.replace(payload, flipped, 1))
        before = zip_path.read_bytes()

        events: list[str] = []
        stream_box: dict[str, object] = {}
        with self._spy_zipfile_close(events):
            with self.assertRaises(ExtractionError) as ctx:
                with open_book(self.library, _book()) as stream:
                    stream_box["stream"] = stream
                    stream.read()  # чтение до EOF завершает проверку CRC
        self.assertIn("CRC", str(ctx.exception))
        self.assertTrue(getattr(stream_box["stream"], "closed"))
        self.assertEqual(events, ["zipfile closed"])
        self.assertEqual(zip_path.read_bytes(), before)

    def test_encrypted_or_unsupported(self):
        # Зашифрованная запись: бит шифрования только в центральном каталоге.
        encrypted = self.archives / ZIP_NAME
        with zipfile.ZipFile(encrypted, "w") as archive:
            info = _zip_info(MEMBER)
            archive.writestr(info, FB2_BYTES)
            info.flag_bits |= 0x1
        with self.assertRaises(ExtractionError) as ctx:
            with open_book(self.library, _book()):
                pass
        self.assertIn("зашифрован", str(ctx.exception))

        # Неподдерживаемое сжатие: настоящий bzip2-member, который сам
        # zipfile читает — отказ является политикой keklib-cli.
        bzip2 = _write_zip(
            self.archives / "bzz.zip",
            [(_zip_info(MEMBER), FB2_BYTES)],
            compress_type=zipfile.ZIP_BZIP2,
        )
        with zipfile.ZipFile(bzip2) as archive:
            self.assertEqual(archive.read(MEMBER), FB2_BYTES)
        with self.assertRaises(ExtractionError) as ctx:
            with open_book(self.library, _book(archive="bzz.zip")):
                pass
        self.assertIn("не поддерживается", str(ctx.exception))

    def test_cross_platform_escape(self):
        outside = self.base / "outside"
        outside.mkdir()
        evil = _write_zip(outside / "evil.zip", [(_zip_info(MEMBER), b"stolen")])
        evil_before = evil.read_bytes()
        os.symlink(evil, self.archives / "link.zip")

        # Реальный выход symlink наружу: отказ по разрешённому пути,
        # а не строковое сравнение префиксов каталогов.
        with self.assertRaises(ExtractionError) as ctx:
            with open_book(self.library, _book(archive="link.zip")):
                pass
        self.assertIn("выходит за пределы", str(ctx.exception))
        self.assertEqual(evil.read_bytes(), evil_before)

        # Имена-побеги отвергаются до файловой системы на любой ОС.
        for book in (
            _book(archive="../outside/evil.zip"),
            _book(archive="..\\outside\\evil.zip"),
            _book(archive="C:evil.zip"),
            _book(archive="\\\\server\\share\\evil.zip"),
            _book(member="..\\204000.fb2"),
        ):
            with self.subTest(archive=book.archive, member=book.member):
                with self.assertRaises(ExtractionError):
                    with open_book(self.library, book):
                        pass

        # Честное containment, а не запрет symlink как такового: ссылка на
        # другой архив внутри lib.rus.ec разрешается и читается.
        _write_zip(
            self.archives / "real.zip", [(_zip_info(MEMBER), FB2_BYTES)]
        )
        os.symlink("real.zip", self.archives / "inside.zip")
        with open_book(self.library, _book(archive="inside.zip")) as stream:
            self.assertEqual(stream.read(), FB2_BYTES)

    def test_directory_or_symlink_entry(self):
        # UNIX-symlink запись с именем выбранного member — не обычный файл.
        link_info = zipfile.ZipInfo(MEMBER, date_time=(1980, 1, 1, 0, 0, 0))
        link_info.create_system = 3
        link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
        _write_zip(self.archives / ZIP_NAME, [(link_info, b"../outside/target")])
        with self.assertRaises(ExtractionError) as ctx:
            with open_book(self.library, _book()):
                pass
        self.assertIn("не является обычным файлом", str(ctx.exception))

        # Запись-каталог не совпадает с точным именем и не выбирается.
        _write_zip(
            self.archives / "dir.zip", [(_zip_info(f"{MEMBER}/"), b"")]
        )
        with self.assertRaises(ExtractionError) as ctx:
            with open_book(self.library, _book(archive="dir.zip")):
                pass
        self.assertIn(MEMBER, str(ctx.exception))

        # Member с разделителем каталога отвергается до открытия ZIP.
        with self.assertRaises(ExtractionError):
            with open_book(self.library, _book(member=f"sub/{MEMBER}")):
                pass

    def test_unix_nonregular_entry_types(self):
        # Явные UNIX-типы записи без завершающего '/': каталог, FIFO,
        # socket — не обычный файл, реальный create_system=3.
        for mode in (
            stat.S_IFDIR | 0o755,
            stat.S_IFIFO | 0o600,
            stat.S_IFSOCK | 0o600,
        ):
            with self.subTest(mode=stat.filemode(mode)):
                info = zipfile.ZipInfo(MEMBER, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = mode << 16
                _write_zip(self.archives / ZIP_NAME, [(info, FB2_BYTES)])
                with self.assertRaises(ExtractionError) as ctx:
                    with open_book(self.library, _book()):
                        pass
                message = str(ctx.exception)
                self.assertIn("не является обычным файлом", message)
                self.assertIn(stat.filemode(mode), message)

        # Совместимость: записи без UNIX-типизации (нулевые биты типа —
        # например, ZIP с Windows) и регулярные записи читаются.
        for attr in (0x20, 0o600 << 16, (stat.S_IFREG | 0o644) << 16):
            with self.subTest(external_attr=oct(attr)):
                info = zipfile.ZipInfo(MEMBER, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = attr
                _write_zip(self.archives / ZIP_NAME, [(info, FB2_BYTES)])
                with open_book(self.library, _book()) as stream:
                    self.assertEqual(stream.read(), FB2_BYTES)

    def test_malformed_utf8_central_directory_name(self):
        # Повреждённый ZIP: в центральном каталоге установлен бит UTF-8
        # (0x800), но первый байт имени записи — недопустимый UTF-8.
        zip_path = _write_zip(
            self.archives / ZIP_NAME, [(_zip_info(MEMBER), FB2_BYTES)]
        )
        raw = bytearray(zip_path.read_bytes())
        central = raw.find(b"PK\x01\x02")
        self.assertGreaterEqual(central, 0)
        # central directory: длина имени — смещение +28, имя — +46.
        name_len = raw[central + 28] | (raw[central + 29] << 8)
        self.assertEqual(
            bytes(raw[central + 46 : central + 46 + name_len]),
            MEMBER.encode("ascii"),
        )
        raw[central + 9] |= 0x08  # general purpose flags: бит 0x800
        raw[central + 46] = 0xFF  # первый байт имени — не UTF-8
        zip_path.write_bytes(raw)

        with self.assertRaises(ExtractionError) as ctx:
            with open_book(self.library, _book()):
                pass
        message = str(ctx.exception)
        self.assertIn(ZIP_NAME, message)
        self.assertIn(MEMBER, message)

    def test_zip_replaced_after_search(self):
        zip_path = _write_zip(
            self.archives / ZIP_NAME, [(_zip_info(MEMBER), FB2_BYTES)]
        )
        book = _book()

        # Между поиском и извлечением архив заменён: выбранного member нет.
        _write_zip(zip_path, [(_zip_info("other.fb2"), b"other")])
        with self.assertRaises(ExtractionError) as ctx:
            with open_book(self.library, book):
                pass
        self.assertIn(MEMBER, str(ctx.exception))

        # Архив заменён вообще не-ZIP файлом.
        zip_path.write_bytes(b"this is not a zip archive")
        with self.assertRaises(ExtractionError):
            with open_book(self.library, book):
                pass

    def test_caller_error_not_relabelled(self):
        _write_zip(self.archives / ZIP_NAME, [(_zip_info(MEMBER), FB2_BYTES)])
        events: list[str] = []
        stream_box: dict[str, object] = {}
        with self._spy_zipfile_close(events):
            with self.assertRaises(OSError) as ctx:
                with open_book(self.library, _book()) as stream:
                    stream_box["stream"] = stream
                    raise OSError("caller boom")
            self.assertNotIsInstance(ctx.exception, ExtractionError)
            self.assertEqual(str(ctx.exception), "caller boom")
            self.assertTrue(getattr(stream_box["stream"], "closed"))

            with self.assertRaises(KeyboardInterrupt):
                with open_book(self.library, _book()):
                    raise KeyboardInterrupt("interrupt")

            # Граница сохраняется и для UnicodeDecodeError вызывающего кода.
            with self.assertRaises(UnicodeDecodeError):
                with open_book(self.library, _book()):
                    raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad caller")

        self.assertEqual(events, ["zipfile closed"] * 3)


if __name__ == "__main__":
    unittest.main()
