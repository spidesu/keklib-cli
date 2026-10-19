"""Тесты переносимого имени файла и атомарной no-replace публикации (§9).

`safe_filename` применяет правила Windows-переносимости одинаково на всех ОС;
`extract_book` пишет поток во временный файл в каталоге назначения, дочитывает
CRC, закрывает источник/temp и публикует файл платформенным примитивом без
перезаписи. Гонки и коллизии доходят до реального примитива публикации, а
нативная семантика файловой системы (регистр, нормализация Unicode)
проверяется пробой самой ФС, а не предположением об ОС.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import stat
import sys
import tempfile
import threading
import unicodedata
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import keklib_cli.output
from keklib_cli.errors import ExtractionError, TargetExistsError
from keklib_cli.models import Book
from keklib_cli.output import extract_book, safe_filename
from keklib_cli.platforms import publish_no_replace

from tests.fixtures import make_library

ZIP_NAME = "flib.zip"
MEMBER = "66372.fb2"
FB2_BYTES = (
    b"<?xml version='1.0' encoding='utf-8'?>\n"
    b"<FictionBook><body>\xd0\x9a\xd0\xbd\xd0\xb8\xd0\xb3\xd0\xb0"
    b"</body></FictionBook>"
)

_RESERVED_WINDOWS = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
     "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_FORBIDDEN_CHARS = '<>:"|?*/\\'
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_RACERS = 8
_WAIT_SECONDS = 10.0


def _book(
    *,
    archive: str = ZIP_NAME,
    member: str = MEMBER,
    file_id: str = "66372",
    author: str = "Перумов, Ник",
    title: str = "Кольцо Тьмы",
) -> Book:
    """Книга с метаданными для имени; извлечению нужны имена источника."""
    return Book(
        archive=archive,
        member=member,
        file_id=file_id,
        author=author,
        title=title,
        language="ru",
        series=(),
        size=len(FB2_BYTES),
        libid=file_id,
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


def _write_zip(path: Path, entries: list[tuple[zipfile.ZipInfo, bytes]]) -> Path:
    """Записать тестовый ZIP с точными ZipInfo записей."""
    with zipfile.ZipFile(path, "w") as archive:
        for info, payload in entries:
            archive.writestr(info, payload)
    return path


class OutputTests(unittest.TestCase):
    """Переносимое имя (§9.4) и атомарная публикация без перезаписи (§9.5-9.7)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.library = make_library(self.base / "library", {})
        self.archives = self.library.archives

    # Переносимое имя.

    def test_utf8_limit(self):
        """Ограничение 240 байт UTF-8 без разрыва кодовой точки; хвост [ID].fb2."""
        book = _book(title="Д" * 200)
        name = safe_filename(book)
        self.assertLessEqual(len(name.encode("utf-8")), 240)
        self.assertTrue(name.endswith(" [66372].fb2"))
        # Точная граница: префикс «Ник Перумов — » (26 байт) + 101 «Д» (202)
        # + « [66372].fb2» (12) = 240 байт; 102-я «Д» уже не влезает.
        self.assertEqual(name, "Ник Перумов — " + "Д" * 101 + " [66372].fb2")

        # Нечётный бюджет обрезает посреди кодовой точки: 61 − 12 = 49 байт
        # на заголовок, из них 23 на название — 11 кириллических символов.
        short = safe_filename(book, max_bytes=61)
        self.assertLessEqual(len(short.encode("utf-8")), 61)
        self.assertTrue(short.endswith(" [66372].fb2"))
        self.assertEqual(short, "Ник Перумов — " + "Д" * 11 + " [66372].fb2")

        # Слишком длинный идентификатор отклоняется: даже «<ID>.fb2» не влезает.
        with self.assertRaises(ExtractionError):
            safe_filename(_book(file_id="9" * 300, member="9" * 300 + ".fb2"))

    def test_empty_metadata(self):
        """Пустые метаданные — безопасный идентификатор: «66372.fb2»."""
        self.assertEqual(safe_filename(_book(author="", title="")), "66372.fb2")
        self.assertEqual(safe_filename(_book(title="")), "Ник Перумов [66372].fb2")
        self.assertEqual(
            safe_filename(_book(author="")), "Кольцо Тьмы [66372].fb2"
        )

        # AUTHOR «Фамилия,Имя,Отчество:» — читаемое имя; авторы через «; ».
        self.assertEqual(
            safe_filename(_book(author="Гаррисон, Гарри:Скаландис, Ант")),
            "Гарри Гаррисон; Ант Скаландис — Кольцо Тьмы [66372].fb2",
        )
        self.assertTrue(
            safe_filename(_book(author="Перумов,Ник,Сергеевич")).startswith(
                "Ник Сергеевич Перумов"
            )
        )

    def test_windows_names_everywhere(self):
        """Правила Windows применяются и на POSIX: символы, хвосты, устройства."""
        book = _book(
            author='Б<>:"|?*/\\в, Иван.',
            title="Con\tNUL\nCOM1. Титул. ",
        )
        name = safe_filename(book)
        for char in _FORBIDDEN_CHARS:
            self.assertNotIn(char, name, f"запрещённый символ {char!r} в {name!r}")
        self.assertIsNone(_CONTROL.search(name))
        stem = name[: -len(".fb2")]
        self.assertFalse(stem.endswith((" ", ".")), stem)
        base = stem.split(".", 1)[0].rstrip(" .")
        self.assertNotIn(base.upper(), _RESERVED_WINDOWS)

        # Идентификатор, целиком совпадающий с устройством, обезвреживается,
        # включая надстрочные COM¹/LPT² и форму с расширением.
        for device in ("NUL", "COM¹", "LPT²", "com³"):
            with self.subTest(device=device):
                reserved = safe_filename(
                    _book(author="", title="", file_id=device)
                )
                base = reserved.split(".", 1)[0]
                self.assertNotIn(base.upper(), _RESERVED_WINDOWS)
                self.assertNotIn(base.casefold(), _RESERVED_WINDOWS)
                self.assertTrue(reserved.endswith(".fb2"))

    def test_unicode_control_categories(self):
        """Санитизация всех Unicode C*: Cf/Cs/Co/Cn, а не только C0/C1.

        U+202E (Cf), U+200B (Cf), U+E000 (Co), U+0378 (Cn) и суррогаты
        (Cs) не должны попадать в имя и не должны вызывать необработанный
        UnicodeEncodeError при кодировании UTF-8.
        """
        book = _book(
            author="A\u200bБ\u202eВ",
            title="Caf\u202ee \ue000\u0378",
            file_id="66\u0378372",
        )
        name = safe_filename(book)
        for char in ("\u200b", "\u202e", "\ue000", "\u0378"):
            self.assertNotIn(char, name, f"символ U+{ord(char):04X} прошёл в имя")
        self.assertIn("66 372", name)  # Cn в ID заменён пробелом

        # Суррогаты (Cs) в авторе/названии — замена, а не UnicodeEncodeError.
        with_surrogate = safe_filename(_book(author="a\ud800b", title="x\udfffy"))
        self.assertNotIn("\ud800", with_surrogate)
        self.assertNotIn("\udfff", with_surrogate)
        with_surrogate.encode("utf-8")  # не поднимает UnicodeEncodeError

        # ID только из C* — предметный отказ, а не сбой кодирования.
        with self.assertRaises(ExtractionError):
            safe_filename(_book(author="", title="", file_id="\ud800\udfff"))

    # Извлечение и публикация.

    def test_extract_exact_bytes(self):
        """Точные байты книги, создание каталога, отсутствие temp-остатков."""
        zip_path = _write_zip(
            self.archives / ZIP_NAME, [(_zip_info(MEMBER), FB2_BYTES)]
        )
        before = zip_path.read_bytes()

        destination = self.base / "out" / "books"  # вложенный отсутствующий
        result = extract_book(self.library, _book(), destination)
        expected = destination / "Ник Перумов — Кольцо Тьмы [66372].fb2"
        self.assertEqual(result, expected)
        self.assertTrue(result.is_file())
        self.assertEqual(result.read_bytes(), FB2_BYTES)
        self.assertEqual(list(destination.iterdir()), [expected])
        self.assertEqual(zip_path.read_bytes(), before)

    def test_existing_file_symlink_and_race(self):
        """Существующий файл/symlink — отказ без перезаписи; гонка до примитива."""
        _write_zip(self.archives / ZIP_NAME, [(_zip_info(MEMBER), FB2_BYTES)])
        destination = self.base / "out"
        destination.mkdir()

        target = extract_book(self.library, _book(), destination)
        with self.assertRaises(TargetExistsError) as ctx:
            extract_book(self.library, _book(), destination)
        self.assertIn("--output", str(ctx.exception))
        self.assertEqual(target.read_bytes(), FB2_BYTES)
        self.assertEqual(list(destination.iterdir()), [target])

        # Живой symlink на месте цели: отказ, ссылка и её данные сохранены.
        target.unlink()
        elsewhere = self.base / "elsewhere.bin"
        elsewhere.write_bytes(b"OLD")
        os.symlink(elsewhere, target)
        with self.assertRaises(TargetExistsError):
            extract_book(self.library, _book(), destination)
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target), str(elsewhere))
        self.assertEqual(elsewhere.read_bytes(), b"OLD")
        self.assertEqual(list(destination.iterdir()), [target])

        # Битая symlink — тоже существующее имя, а не дыра для записи.
        target.unlink()
        os.symlink(self.base / "no-such-file", target)
        with self.assertRaises(TargetExistsError):
            extract_book(self.library, _book(), destination)
        self.assertTrue(target.is_symlink())
        target.unlink()

        # Реальная гонка до примитива публикации: N потоков, ровно один
        # победитель, содержимое победителя не перезаписано.
        barrier = threading.Barrier(_RACERS)
        lock = threading.Lock()
        attempts: list[tuple[Path, bytes]] = []
        winners: list[bytes] = []
        conflicts: list[TargetExistsError] = []

        def attempt(index: int) -> None:
            fd, temp_name = tempfile.mkstemp(
                dir=destination, prefix=f".race-{index}-"
            )
            os.close(fd)
            temp = Path(temp_name)
            payload = f"racer-{index}".encode("ascii")
            temp.write_bytes(payload)
            with lock:
                attempts.append((temp, payload))
            barrier.wait(timeout=_WAIT_SECONDS)
            try:
                publish_no_replace(temp, target)
            except TargetExistsError as exc:
                with lock:
                    conflicts.append(exc)
            else:
                with lock:
                    winners.append(payload)

        threads = [
            threading.Thread(target=attempt, args=(index,))
            for index in range(_RACERS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_WAIT_SECONDS)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

        self.assertEqual(len(winners), 1, "победителей гонки должно быть ровно один")
        self.assertEqual(len(conflicts), _RACERS - 1)
        self.assertEqual(target.read_bytes(), winners[0])
        for temp, _payload in attempts:  # temp проигравших чистит вызывающий
            if temp.exists():
                temp.unlink()
        self.assertEqual(list(destination.iterdir()), [target])

    def test_case_unicode_collision(self):
        """Коллизия регистра/нормализации решается нативной семантикой ФС."""
        probe = self.base / "probe"
        probe.mkdir()
        marker = probe / "Книга.fb2"
        marker.write_bytes(b"m")
        case_insensitive = (probe / "КНИГА.fb2").exists()

        # Действительно разлагаемая пара: «й» = и + U+0306; строки обязаны
        # различаться до пробы, иначе проверка NFD ничего не доказывает.
        nfc_title = "Книжный"
        nfd_title = unicodedata.normalize("NFD", nfc_title)
        self.assertNotEqual(nfc_title, nfd_title)
        normal_marker = probe / f"{nfc_title}.fb2"
        normal_marker.write_bytes(b"m")
        unicode_insensitive = (probe / f"{nfd_title}.fb2").exists()

        _write_zip(
            self.archives / ZIP_NAME,
            [
                (_zip_info(MEMBER), FB2_BYTES),
                (_zip_info("777.fb2"), FB2_BYTES),
            ],
        )
        destination = self.base / "out"
        destination.mkdir()
        first = extract_book(
            self.library, _book(title="Книга"), destination
        )
        self.assertEqual(first.name, "Ник Перумов — Книга [66372].fb2")

        # Другой регистр того же названия: на регистронезависимой ФС — коллизия.
        try:
            upper = extract_book(
                self.library, _book(title="КНИГА", member="777.fb2"), destination
            )
        except TargetExistsError:
            self.assertTrue(
                case_insensitive,
                "ФС регистрозависима, но публикация отказала по коллизии",
            )
        else:
            self.assertFalse(
                case_insensitive,
                "ФС регистронезависима, но вторая публикация прошла",
            )
            self.assertEqual(upper.read_bytes(), FB2_BYTES)
            self.assertNotEqual(upper, first)
        self.assertEqual(first.read_bytes(), FB2_BYTES)

        # NFD-название против уже извлечённого NFC: нативная семантика та же.
        nfc_book = _book(title=nfc_title, member="777.fb2")
        nfc_file = extract_book(self.library, nfc_book, destination)
        self.assertEqual(nfc_file.name, f"Ник Перумов — {nfc_title} [66372].fb2")
        try:
            second = extract_book(
                self.library, _book(title=nfd_title, member="777.fb2"),
                destination,
            )
        except TargetExistsError:
            self.assertTrue(
                unicode_insensitive,
                "ФС различает NFC/NFD, но публикация отказала по коллизии",
            )
        else:
            self.assertFalse(
                unicode_insensitive,
                "ФС склеивает NFC/NFD, но вторая публикация прошла",
            )
            self.assertEqual(second.read_bytes(), FB2_BYTES)
            self.assertNotEqual(second, nfc_file)
        self.assertEqual(nfc_file.read_bytes(), FB2_BYTES)

    def test_crc_disk_write_cancel(self):
        """CRC, ошибка диска и отмена не оставляют частичного FB2 и своего temp."""
        destination = self.base / "out"
        destination.mkdir()

        # Повреждённый STORED-member: чтение до EOF ловит CRC до публикации.
        payload = b"CRC-EXTRACT-PAYLOAD-0123456789"
        zip_path = _write_zip(
            self.archives / ZIP_NAME,
            [(_zip_info(MEMBER, compress_type=zipfile.ZIP_STORED), payload)],
        )
        flipped = b"X" + payload[1:]
        raw = zip_path.read_bytes()
        self.assertEqual(raw.count(payload), 1)
        zip_path.write_bytes(raw.replace(payload, flipped, 1))

        with self.assertRaises(ExtractionError) as ctx:
            extract_book(self.library, _book(), destination)
        self.assertIn("CRC", str(ctx.exception))
        self.assertEqual(list(destination.iterdir()), [])

        # Ошибка записи на диск (нет места) — отказ без публикации.
        _write_zip(self.archives / ZIP_NAME, [(_zip_info(MEMBER), FB2_BYTES)])
        with mock.patch.object(
            os, "fsync", side_effect=OSError(28, "No space left on device")
        ):
            with self.assertRaises(ExtractionError) as ctx:
                extract_book(self.library, _book(), destination)
        self.assertIn(str(destination), str(ctx.exception))
        self.assertEqual(list(destination.iterdir()), [])

        # Отмена: KeyboardInterrupt не перекрашивается, мусора нет.
        with mock.patch.object(os, "fsync", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                extract_book(self.library, _book(), destination)
        self.assertEqual(list(destination.iterdir()), [])

    def test_unsupported_publish(self):
        """Отсутствие безопасного примитива — отказ, а не небезопасный fallback."""
        _write_zip(self.archives / ZIP_NAME, [(_zip_info(MEMBER), FB2_BYTES)])
        destination = self.base / "out"
        destination.mkdir()
        target = destination / safe_filename(_book())
        target.write_bytes(b"KEEP")

        if sys.platform == "win32":
            link_error = OSError()
            link_error.winerror = 50  # ERROR_NOT_SUPPORTED
            rename_error = OSError()
            rename_error.winerror = 50
            patches = [
                mock.patch.object(os, "link", side_effect=link_error),
                mock.patch.object(os, "rename", side_effect=rename_error),
            ]
        else:
            from keklib_cli.platforms import posix as posix_module

            patches = [
                mock.patch.object(
                    os, "link", side_effect=OSError(1, "link unsupported")
                ),
                mock.patch.object(
                    posix_module,
                    "_rename_noreplace",
                    side_effect=ExtractionError("публикация не поддерживается"),
                ),
            ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with self.assertRaises(ExtractionError) as ctx:
                extract_book(self.library, _book(), destination)
        self.assertNotIsInstance(ctx.exception, TargetExistsError)
        self.assertEqual(target.read_bytes(), b"KEEP", "цель перезаписана fallback")
        self.assertEqual(list(destination.iterdir()), [target])

    def test_long_full_path(self):
        """Диагностика с полным путём при слишком длинном пути назначения."""
        _write_zip(self.archives / ZIP_NAME, [(_zip_info(MEMBER), FB2_BYTES)])
        deep = self.base / ("x" * 300)  # компонент больше NAME_MAX

        with self.assertRaises(ExtractionError) as ctx:
            extract_book(self.library, _book(), deep)
        message = str(ctx.exception)
        self.assertIn(str(deep), message)
        self.assertIn("каталог назначения", message)
        self.assertIn("более короткий каталог", message)

    def test_long_full_path_later_stages(self):
        """Длина пути на этапах temp/публикации при существующем каталоге.

        Каталог назначения существует и помещается в лимит (mkdir проходит);
        ENAMETOOLONG (Linux/POSIX) на mkstemp и winerror 206 (Windows,
        ERROR_FILENAME_EXCED_RANGE) на публикации должны давать адресную
        диагностику с полным путём цели и советом выбрать более короткий
        каталог — общий обработчик «не удалось записать» здесь недостаточен.
        """
        _write_zip(self.archives / ZIP_NAME, [(_zip_info(MEMBER), FB2_BYTES)])
        destination = self.base / "out"
        destination.mkdir()

        with mock.patch.object(
            tempfile,
            "mkstemp",
            side_effect=OSError(errno.ENAMETOOLONG, "File name too long"),
        ):
            with self.assertRaises(ExtractionError) as ctx:
                extract_book(self.library, _book(), destination)
        message = str(ctx.exception)
        self.assertIn("временного файла", message)  # адресный этап
        self.assertIn(str(destination), message)
        self.assertIn("более короткий каталог", message)
        self.assertEqual(list(destination.iterdir()), [])

        win_length = OSError()
        win_length.winerror = 206  # ERROR_FILENAME_EXCED_RANGE
        with mock.patch.object(
            keklib_cli.output, "publish_no_replace", side_effect=win_length
        ):
            with self.assertRaises(ExtractionError) as ctx:
                extract_book(self.library, _book(), destination)
        message = str(ctx.exception)
        self.assertIn("публикации книги", message)  # адресный этап
        self.assertIn(str(destination), message)
        self.assertIn("более короткий каталог", message)
        self.assertEqual(list(destination.iterdir()), [])  # temp убран, цели нет


if __name__ == "__main__":
    unittest.main()
