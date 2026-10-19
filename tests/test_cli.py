"""Тесты команд CLI (setup/index/search) и кодов завершения (`keklib_cli.cli`).

Все сценарии выполняются на синтетических библиотеках во временных каталогах
через внедрённые потоки и пути состояния: настоящая библиотека и нативные
пользовательские каталоги не затрагиваются. Занятый индекс проверяется
настоящим дочерним процессом, удерживающим shared-блокировку; готовность
обменивается строкой через pipe.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from keklib_cli import cli
from keklib_cli.models import StatePaths
from tests.fixtures import make_library, make_raw_catalog

_WAIT_SECONDS = 5.0  # защитный предел ожидания дочернего процесса

# Дочерний процесс-читатель: держит shared-блокировку индекса до EOF в stdin.
_SHARED_READER_CHILD = """
import sys
from pathlib import Path

from keklib_cli.platforms import index_lock

with index_lock(Path(sys.argv[1]), exclusive=False):
    print("READY", flush=True)
    sys.stdin.readline()
"""


def _row(file_id: str, author: str, title: str, **extra: str) -> dict[str, str]:
    """Строка INP активной FB2-книги с необязательными полями."""
    record = {
        "AUTHOR": author,
        "TITLE": title,
        "FILE": file_id,
        "DEL": "0",
        "EXT": "fb2",
    }
    record.update(extra)
    return record


class _ScriptedTTY:
    """stdin с запрограммированными ответами и isatty() == True."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)

    def readline(self) -> str:
        return self._answers.pop(0) if self._answers else ""

    def isatty(self) -> bool:
        return True


class _InterruptedTTY:
    """Интерактивный stdin, чтение которого имитирует Ctrl+C."""

    def readline(self) -> str:
        raise KeyboardInterrupt

    def isatty(self) -> bool:
        return True


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        state = self.base / "state"
        self.paths = StatePaths(
            config=state / "config.json",
            cache=state / "cache",
            index=state / "cache" / "index.sqlite3",
        )
        self.home = self.base / "home"
        self.home.mkdir()

    def _run(self, argv, *, answers=(), tty=True, stdin=None):
        stream = (
            stdin
            if stdin is not None
            else (_ScriptedTTY(answers) if tty else io.StringIO())
        )
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv), stdin=stream, stdout=out, stderr=err, paths=self.paths)
        return code, out.getvalue(), err.getvalue()

    def _library(self, name="library", members=None):
        rows = [
            _row("1", "Автор, Раз", "Книга Раз", LANG="ru", SIZE="1000"),
            _row("2", "Автор, Два", "Книга Два", LANG="ru", SIZE="2000"),
        ]
        default = {"a": {"1.fb2": b"<FictionBook id='1'/>", "2.fb2": b"<FictionBook id='2'/>"}}
        return make_library(
            self.base / name,
            {"a": rows},
            members=default if members is None else members,
        )

    def _setup(self, library):
        code, out, err = self._run(["setup", "--library", str(library.root)])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        return err

    @staticmethod
    def _narrow(encoding):
        """Поток с узкой кодировкой поверх BytesIO; возвращает (raw, stream)."""
        raw = io.BytesIO()
        return raw, io.TextIOWrapper(raw, encoding=encoding)

    def test_help_narrow_encoding_no_traceback(self):
        """Справка главного parser и подкоманд в узкой кодировке — код 0.

        Структура справки (переводы строк) сохраняется: санитизация не
        применяется к доверенному тексту, только безопасная замена
        непредставимых символов.
        """
        raw, out = self._narrow("ascii")
        code = cli.main(["--help"], stdout=out, stderr=io.StringIO())
        self.assertEqual(code, 0)
        text = raw.getvalue().decode("ascii")
        self.assertIn("usage:", text)
        self.assertIn("setup", text)
        self.assertGreater(text.count("\n"), 2)
        raw2, out2 = self._narrow("ascii")
        code = cli.main(["search", "--help"], stdout=out2, stderr=io.StringIO())
        self.assertEqual(code, 0)
        text2 = raw2.getvalue().decode("ascii")
        self.assertIn("--output", text2)
        self.assertGreater(text2.count("\n"), 2)

    def test_argparse_errors_sanitized_and_structured(self):
        """Ошибки argparse: без traceback в узкой кодировке, без ESC/bidi
        в эхе аргументов, с собственными переводами строк usage."""
        raw, out = self._narrow("ascii")
        err_raw, err = self._narrow("ascii")
        code = cli.main(["search", "book", "--bad-\U0001f4d6"], stdout=out, stderr=err)
        self.assertEqual(code, 2)
        err_text = err_raw.getvalue().decode("ascii")
        self.assertIn("error:", err_text)
        self.assertIn("\n", err_text)  # usage/error — структурные строки
        # Управляющие символы в argv не проходят в терминал даже в utf-8:
        # эхо неизвестного аргумента подкоманды и неверного имени команды.
        raw2, out2 = self._narrow("utf-8")
        err_raw2, err2 = self._narrow("utf-8")
        code = cli.main(
            ["search", "book", "--invalid\x1b[2J\u202e"],
            stdout=out2,
            stderr=err2,
        )
        self.assertEqual(code, 2)
        err_text2 = err_raw2.getvalue().decode("utf-8")
        self.assertIn("invalid", err_text2)  # аргумент назван в диагностике
        self.assertNotIn("\x1b", err_text2)
        self.assertNotIn("\u202e", err_text2)
        err3 = io.StringIO()
        code = cli.main(["команда\x1b[2J"], stdout=io.StringIO(), stderr=err3)
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err3.getvalue())
        self.assertNotIn("\x1b", err3.getvalue())

    def test_setup_diagnostic_sanitizes_control_path(self):
        """Диагностика предметной ошибки санитизирует путь с ESC/bidi/переводом
        строки; реальный путь не создаётся."""
        bad_root = self.base / "нет-\x1b[2J\u202e\nбиблиотеки"
        code, out, err = self._run(["setup", "--library", str(bad_root)])
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)
        self.assertIn("нет- [2J", err)  # показ: ESC и bidi — пробелы
        self.assertNotIn("\x1b", err)
        self.assertNotIn("\u202e", err)
        # Внедрённый перевод строки нейтрализован: имя пути — одна строка.
        diagnostic_lines = [line for line in err.splitlines() if "нет- [2J" in line]
        self.assertTrue(diagnostic_lines)
        self.assertTrue(any("библиотеки" in line for line in diagnostic_lines))
        self.assertFalse(bad_root.exists())

    def test_extract_flow(self):
        library = self._library()
        self._setup(library)
        catalog_sha = hashlib.sha256(library.catalog.read_bytes()).hexdigest()
        zip_path = library.archives / "a.zip"
        zip_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        output = self.base / "выход"
        code, out, err = self._run(
            ["search", "раз", "--output", str(output)], answers=["1"]
        )
        self.assertEqual(code, 0)
        files = sorted(path for path in output.iterdir() if path.is_file())
        self.assertEqual(len(files), 1)
        book_file = files[0]
        self.assertEqual(book_file.suffix, ".fb2")
        self.assertEqual(book_file.read_bytes(), b"<FictionBook id='1'/>")
        lines = out.splitlines()
        self.assertEqual(lines[-1], str(book_file))
        self.assertTrue(book_file.is_absolute())
        self.assertTrue(any(line.startswith("1.") for line in lines))
        self.assertIn("номер книги, n, p или q", err)
        # Источники только читались: байты INPX и ZIP не изменились.
        self.assertEqual(
            hashlib.sha256(library.catalog.read_bytes()).hexdigest(), catalog_sha
        )
        self.assertEqual(hashlib.sha256(zip_path.read_bytes()).hexdigest(), zip_sha)

    def test_setup_structure_before_save(self):
        library = self._library("good")
        self._setup(library)
        saved = self.paths.config.read_bytes()
        bad = make_raw_catalog(self.base / "bad", [b"AUTHOR;TITLE;\n"], {"a": b""})
        code, out, err = self._run(["setup", "--library", str(bad.root)])
        self.assertEqual(code, 1)
        self.assertEqual(self.paths.config.read_bytes(), saved)
        self.assertIn("structure.info", err)
        self.assertEqual(out, "")

    def test_non_tty_before_index(self):
        self._library()
        code, out, err = self._run(["search", "раз"], tty=False)
        self.assertEqual(code, 2)
        self.assertIn("интерактивн", err)
        self.assertFalse((self.base / "state").exists())
        code, _, err = self._run(["search", "   !!! "], tty=True)
        self.assertEqual(code, 2)
        self.assertIn("нет слов", err)
        self.assertFalse((self.base / "state").exists())

    def test_output_override_and_tilde(self):
        library = self._library()
        self._setup(library)
        with mock.patch.dict(
            os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        ):
            code, out, err = self._run(
                ["search", "раз", "--output", "~/Книги"], answers=["1"]
            )
            self.assertEqual(code, 0)
            custom = self.home / "Книги"
            files = list(custom.glob("*.fb2"))
            self.assertEqual(len(files), 1)
            self.assertEqual(out.splitlines()[-1], str(files[0]))
            # Назначение по умолчанию — Path.home() / 'Books'.
            code, out, err = self._run(["search", "два"], answers=["1"])
            self.assertEqual(code, 0)
            self.assertEqual(len(list((self.home / "Books").glob("*.fb2"))), 1)

    def test_missing_updated_zip(self):
        library = self._library(members={})
        self._setup(library)
        output = self.base / "out"
        code, out, err = self._run(
            ["search", "раз", "--output", str(output)], answers=["1"]
        )
        self.assertEqual(code, 1)
        self.assertIn("a.zip", err)
        self.assertIn("1.fb2", err)
        self.assertEqual(list(output.glob("*.fb2")), [])
        self.assertEqual(list(output.glob(".keklib-*")), [])

    def test_failed_refresh_no_stale_selection(self):
        library = self._library()
        self._setup(library)
        output = self.base / "out"
        code, _, _ = self._run(
            ["search", "раз", "--output", str(output)], answers=["1"]
        )
        self.assertEqual(code, 0)
        index_sha = hashlib.sha256(self.paths.index.read_bytes()).hexdigest()
        fb2_files = sorted(output.glob("*.fb2"))
        self.assertEqual(len(fb2_files), 1)
        # INPX обновился до нечитаемой структуры: пересборка падает до выдачи
        # таблицы — устаревший индекс не используется для выбора.
        make_raw_catalog(library.root, [b"AUTHOR;TITLE;\n"], {"a": b""})
        code, out, err = self._run(
            ["search", "раз", "--output", str(output)], answers=["1"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("structure.info", err)
        self.assertEqual(
            hashlib.sha256(self.paths.index.read_bytes()).hexdigest(), index_sha
        )
        self.assertEqual(sorted(output.glob("*.fb2")), fb2_files)

    def test_corrupt_catalog_zip_surfaces_as_user_error(self):
        """Битые байты ZIP-контейнера INPX — предметный отказ CLI (F3).

        Повреждённый deflate-поток и недопустимое UTF-8-имя центрального
        каталога прежде выходили низкоуровневыми zlib.error /
        UnicodeDecodeError с traceback; ожидается код 1 с адресной
        диагностикой и прежним индексом.
        """
        library = self._library()
        self._setup(library)
        output = self.base / "out"
        code, _, _ = self._run(
            ["search", "раз", "--output", str(output)], answers=["1"]
        )
        self.assertEqual(code, 0)
        index_sha = hashlib.sha256(self.paths.index.read_bytes()).hexdigest()
        fb2_files = sorted(output.glob("*.fb2"))

        for label, mutator in (
            ("битый deflate INP", self._break_inp_deflate),
            ("битое имя центрального каталога", self._break_central_name),
        ):
            with self.subTest(corruption=label):
                mutator(library.root / "librusec_flib.inpx")
                code, out, err = self._run(
                    ["search", "раз", "--output", str(output)], answers=["1"]
                )
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertNotIn("Traceback", err)
                self.assertIn(str(library.root / "librusec_flib.inpx"), err)
                self.assertEqual(
                    hashlib.sha256(self.paths.index.read_bytes()).hexdigest(),
                    index_sha,
                )
                self.assertEqual(sorted(output.glob("*.fb2")), fb2_files)

    @staticmethod
    def _break_inp_deflate(catalog: Path) -> None:
        raw = bytearray(catalog.read_bytes())
        name_at = raw.find(b"a.inp")
        data_at = name_at + len("a.inp")
        raw[data_at] = 0x07  # первый байт сжатых данных — битый deflate
        catalog.write_bytes(raw)

    @staticmethod
    def _break_central_name(catalog: Path) -> None:
        raw = bytearray(catalog.read_bytes())
        central = raw.find(b"PK\x01\x02")
        assert central >= 0
        raw[central + 9] |= 0x08  # general purpose flags: бит 0x800 (UTF-8)
        raw[central + 46] = 0xFF  # первый байт имени — не UTF-8
        catalog.write_bytes(raw)

    def test_busy_reader_diagnostic(self):
        library = self._library()
        self._setup(library)
        code, _, _ = self._run(["index", "--refresh"])
        self.assertEqual(code, 0)
        lock_path = self.paths.index.parent / "index.lock"
        proc = subprocess.Popen(
            [sys.executable, "-c", _SHARED_READER_CHILD, str(lock_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._stop_child, proc)
        self.assertEqual(proc.stdout.readline().strip(), "READY")
        code, out, err = self._run(["index", "--refresh"])
        self.assertEqual(code, 1)
        self.assertIn("заблокирован другим процессом", err)
        self.assertIn(str(lock_path), err)
        self.assertEqual(out, "")

    @staticmethod
    def _stop_child(proc):
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=_WAIT_SECONDS)
        proc.stdout.close()

    def test_codes_and_channels(self):
        library = self._library()
        # 0: справка (в stdout), включая подкоманду search с --output.
        code, out, err = self._run(["--help"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("usage", out)
        code, out, err = self._run(["search", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("--output", out)
        # 2: неверные аргументы — диагностика argparse в stderr.
        for argv in (["bogus"], ["setup"], ["search"], ["index"]):
            with self.subTest(argv=argv):
                code, out, err = self._run(argv)
                self.assertEqual(code, 2)
                self.assertNotEqual(err, "")
        # 1: предметная ошибка — конфигурация не настроена.
        code, out, err = self._run(["search", "раз"])
        self.assertEqual(code, 1)
        self.assertIn("configuration", err.lower())
        self.assertEqual(out, "")
        # Успешный setup: сообщение в stderr, stdout пуст (перенаправляемый канал).
        code, out, err = self._run(["setup", "--library", str(library.root)])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("настроена", err)
        # 0: index --refresh — прогресс в stderr, stdout пуст.
        code, out, err = self._run(["index", "--refresh"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        # 130: Ctrl+C в приглашении выбора.
        code, out, err = self._run(["search", "раз"], stdin=_InterruptedTTY())
        self.assertEqual(code, 130)
        self.assertIn("отменено", err)

    def test_target_conflict_no_overwrite(self):
        library = self._library()
        self._setup(library)
        output = self.base / "out"
        code, _, _ = self._run(
            ["search", "раз", "--output", str(output)], answers=["1"]
        )
        self.assertEqual(code, 0)
        existing = sorted(output.glob("*.fb2"))[0]
        before = existing.read_bytes()
        code, out, err = self._run(
            ["search", "раз", "--output", str(output)], answers=["1"]
        )
        self.assertEqual(code, 1)
        self.assertIn("уже существует", err)
        self.assertIn("--output", err)
        self.assertEqual(existing.read_bytes(), before)
        self.assertEqual(len(list(output.glob("*.fb2"))), 1)
        self.assertEqual(list(output.glob(".keklib-*")), [])


if __name__ == "__main__":
    unittest.main()
