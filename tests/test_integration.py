"""Нативные сквозные тесты CLI в настоящем TTY и приёмка библиотеки.

`tests.harness.tty.run_tty` запускает реальный module CLI
(``python -m keklib_cli``) в настоящем псевдотерминале — POSIX PTY, Windows
ConPTY через test-only ``pywinpty`` — и передаёт команды только после
появления приглашения; результат содержит слитый transcript и настоящий
код завершения. Приёмочный инструмент ``tools/verify_library.py``
выполняется настоящим subprocess на синтетических библиотеках с известной
парой приёмки: собственный workdir с маркером владения, измерения и отчёт
JSON. Реальная библиотека пользователя не затрагивается.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import make_library
from tests.harness.tty import run_tty

REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY_TOOL = REPO_ROOT / "tools" / "verify_library.py"

_TTY_TIMEOUT = 60.0
_VERIFY_TIMEOUT = 180.0

#: Известная пара приёмки из реальной библиотеки Lib.rus.ec.
KNOWN_INP = "fb2-060424-074391"
KNOWN_ARCHIVE = "fb2-060424-074391.zip"
KNOWN_FILE = "66372"
KNOWN_MEMBER = "66372.fb2"
KNOWN_TITLE = "Мастер и Маргарита"
KNOWN_AUTHOR = "Булгаков, Михаил Афанасьевич"
KNOWN_PAYLOAD = b"<FictionBook id='66372'/>"


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


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _known_library(root: Path, *, known: bool):
    """Библиотека приёмки: известная пара на месте или подменённая.

    При ``known=False`` книга с теми же автором/названием и файлом
    ``66372.fb2`` лежит в другом архиве — инструмент обязан отказаться,
    а не выбирать похожее издание.
    """
    rows_known = [
        _row(
            KNOWN_FILE,
            KNOWN_AUTHOR,
            KNOWN_TITLE,
            LANG="ru",
            SIZE=str(len(KNOWN_PAYLOAD)),
        )
    ]
    rows_other = [_row("2", "Автор, Раз", "Книга Раз", LANG="ru", SIZE="100")]
    members_known = {KNOWN_MEMBER: KNOWN_PAYLOAD}
    members_other = {"2.fb2": b"<FictionBook id='2'/>"}
    if known:
        groups = {
            KNOWN_INP: rows_known + rows_other,
            "fb2-other-000": [_row("3", "Автор, Три", "Книга Три")],
        }
        members = {
            KNOWN_INP: {**members_known, **members_other},
            "fb2-other-000": {"3.fb2": b"<FictionBook id='3'/>"},
        }
    else:
        groups = {
            KNOWN_INP: rows_other,
            "fb2-other-000": rows_known,
        }
        members = {
            KNOWN_INP: members_other,
            "fb2-other-000": members_known,
        }
    return make_library(root, groups, members=members)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.env = self._child_env(self.base)

    @staticmethod
    def _child_env(base: Path) -> dict[str, str]:
        """Изолированное окружение дочернего CLI на любой ОС.

        Нативные пути состояния redirected на временный домашний каталог
        (XDG на POSIX, APPDATA/LOCALAPPDATA на Windows); пользовательские
        config/cache не затрагиваются.
        """
        home = base / "home"
        for parts in (
            (".config",),
            (".cache",),
            ("AppData", "Roaming"),
            ("AppData", "Local"),
        ):
            (home.joinpath(*parts)).mkdir(parents=True, exist_ok=True)
        tmp = base / "tmp"
        tmp.mkdir(exist_ok=True)
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "APPDATA": str(home / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(home / "AppData" / "Local"),
                "TMPDIR": str(tmp),
                "TMP": str(tmp),
                "TEMP": str(tmp),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        env.pop("PYTHONSTARTUP", None)
        env.pop("PYTHONPATH", None)
        return env

    def _cli(self, argv, commands, *, timeout=_TTY_TIMEOUT):
        """Реальный module CLI в настоящем TTY; команды — после приглашения."""
        return run_tty(
            [sys.executable, "-m", "keklib_cli", *[str(item) for item in argv]],
            commands,
            env=self.env,
            cwd=self.base,
            timeout=timeout,
        )

    def _verify(self, workdir, *, reuse=False, timeout=_VERIFY_TIMEOUT):
        argv = [
            sys.executable,
            str(VERIFY_TOOL),
            "--library",
            str(self.library.root),
            "--workdir",
            str(workdir),
        ]
        if reuse:
            argv.append("--reuse-index")
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            # Дочерний Python запускается с PYTHONIOENCODING=utf-8 (см.
            # _child_env), его вывод — UTF-8 на любой ОС. Без явной
            # кодировки subprocess на Windows декодирует cp1252 и падает
            # UnicodeDecodeError в reader-потоке: stdout/stderr становятся
            # None и все проверки отчёта ломаются вторичными TypeError
            # (первый CI, все Windows-jobs).
            encoding="utf-8",
            timeout=timeout,
            env=self.env,
            cwd=self.base,
        )

    def _library(self, name="library"):
        return _known_library(self.base / name, known=True)

    def test_native_setup_search_update_select(self):
        """Реальный CLI в PTY: настройка, выбор номера, SHA-256 FB2, обновление."""
        library = self._library()
        setup = self._cli(["setup", "--library", str(library.root)], [])
        self.assertEqual(setup.returncode, 0, setup.stdout)
        self.assertIn("библиотека настроена", setup.stdout)

        out1 = self.base / "out1"
        search = self._cli(
            ["search", "булгаков мастер", "--output", str(out1)], ["1"]
        )
        self.assertEqual(search.returncode, 0, search.stdout)
        self.assertIn("Найдено книг:", search.stdout)
        self.assertIn("Мастер и Маргарита", search.stdout)
        extracted = sorted(out1.glob("*.fb2"))
        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0].read_bytes(), KNOWN_PAYLOAD)
        # Итоговый абсолютный путь — отдельной строкой transcript. Сравнение —
        # по канонической идентичности (realpath обеих сторон): на macOS TMPDIR
        # идёт через symlink-предок /var → /private/var, на Windows — short
        # имя RUNNER~1 против длинного runneradmin; строковое равенство
        # печатаемого и resolve()-пути на этих ОСах не обязано выполняться.
        lines = [line for line in search.stdout.splitlines() if line.strip()]
        printed = lines[-1]
        self.assertEqual(
            os.path.realpath(printed),
            os.path.realpath(extracted[0]),
            f"итоговая строка transcript: {printed!r}",
        )

        # До обновления каталога новой книги нет — честный «не найдено».
        absent = self._cli(["search", "новая"], [])
        self.assertEqual(absent.returncode, 0, absent.stdout)
        self.assertIn("книг не найдено", absent.stdout)

        # Новый INPX и ZIP: обновление отражается в поиске без перестройки
        # вручную — сессия сама доводит индекс до актуальности.
        payload_new = b"<FictionBook id='777' new/>"
        make_library(
            library.root,
            {
                KNOWN_INP: [
                    _row(
                        KNOWN_FILE,
                        KNOWN_AUTHOR,
                        KNOWN_TITLE,
                        LANG="ru",
                        SIZE=str(len(KNOWN_PAYLOAD)),
                    ),
                    _row("2", "Автор, Раз", "Книга Раз", LANG="ru", SIZE="100"),
                    _row("777", "Автор, Нов", "Книга Новая", LANG="ru"),
                ],
                "fb2-other-000": [_row("3", "Автор, Три", "Книга Три")],
            },
            members={
                KNOWN_INP: {
                    KNOWN_MEMBER: KNOWN_PAYLOAD,
                    "2.fb2": b"<FictionBook id='2'/>",
                    "777.fb2": payload_new,
                },
                "fb2-other-000": {"3.fb2": b"<FictionBook id='3'/>"},
            },
        )
        out2 = self.base / "out2"
        updated = self._cli(["search", "новая", "--output", str(out2)], ["1"])
        self.assertEqual(updated.returncode, 0, updated.stdout)
        self.assertIn("Книга Новая", updated.stdout)
        extracted_new = sorted(out2.glob("*.fb2"))
        self.assertEqual(len(extracted_new), 1)
        self.assertEqual(extracted_new[0].read_bytes(), payload_new)

    def test_read_only_source_bytes(self):
        """Источники только читаются: SHA-256 и mtime_ns INPX/ZIP неизменны."""
        library = self._library()
        sources = [library.catalog, *sorted(library.archives.glob("*.zip"))]
        before = {
            path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in sources
        }
        setup = self._cli(["setup", "--library", str(library.root)], [])
        self.assertEqual(setup.returncode, 0, setup.stdout)
        search = self._cli(
            ["search", "книга раз", "--output", str(self.base / "out")], ["1"]
        )
        self.assertEqual(search.returncode, 0, search.stdout)
        after = {
            path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in sources
        }
        self.assertEqual(before, after)  # atime сознательно не сравнивается

    def test_library_switch(self):
        """Смена настроенной библиотеки исключает прежние результаты."""
        library_a = self._library("library-a")
        library_b = make_library(
            self.base / "library-b",
            {"fb2-other-111": [_row("5", "Автор, Пять", "Книга Пять")]},
            members={"fb2-other-111": {"5.fb2": b"<FictionBook id='5'/>"}},
        )
        setup_a = self._cli(["setup", "--library", str(library_a.root)], [])
        self.assertEqual(setup_a.returncode, 0, setup_a.stdout)
        search_a = self._cli(
            ["search", "булгаков мастер", "--output", str(self.base / "out")], ["q"]
        )
        self.assertEqual(search_a.returncode, 0, search_a.stdout)
        self.assertIn("Мастер и Маргарита", search_a.stdout)

        setup_b = self._cli(["setup", "--library", str(library_b.root)], [])
        self.assertEqual(setup_b.returncode, 0, setup_b.stdout)
        search_b = self._cli(
            ["search", "булгаков мастер", "--output", str(self.base / "out")], []
        )
        self.assertEqual(search_b.returncode, 0, search_b.stdout)
        self.assertIn("книг не найдено", search_b.stdout)
        self.assertNotIn("Маргарита", search_b.stdout)

    def test_verify_refuses_library_workdir(self):
        """Verify отвергает workdir внутри библиотеки до любой записи."""
        self.library = self._library()
        inside = self.library.root / "verify-workdir"
        catalog_before = hashlib.sha256(self.library.catalog.read_bytes()).hexdigest()
        proc = self._verify(inside)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("workdir", proc.stderr)
        self.assertFalse(inside.exists())
        self.assertFalse(
            list(self.library.archives.glob("*verify*")),
            "в каталоге архивов не должно появляться ничего постороннего",
        )
        self.assertEqual(
            hashlib.sha256(self.library.catalog.read_bytes()).hexdigest(),
            catalog_before,
        )

    def test_verify_report(self):
        """Отчёт приёмки на fixture известной книги: все ключи и точная пара."""
        self.library = self._library()
        workdir = self.base / "verify-work"
        proc = self._verify(workdir)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        report = json.loads((workdir / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["ok"], report["failures"])
        self.assertTrue(report["query_target_met"])
        self.assertTrue(report["source_unchanged"])
        self.assertTrue(report["rebuilt"])
        self.assertEqual(report["selected_archive"], KNOWN_ARCHIVE)
        self.assertEqual(report["selected_member"], KNOWN_MEMBER)
        self.assertEqual(report["selected_title"], KNOWN_TITLE)
        self.assertEqual(report["row_count"], 3)
        self.assertEqual(report["book_count"], 3)
        self.assertEqual(report["output_sha256"], _sha(KNOWN_PAYLOAD))
        self.assertEqual(
            report["catalog_sha256"],
            hashlib.sha256(self.library.catalog.read_bytes()).hexdigest(),
        )
        for key in (
            "hash_seconds",
            "build_seconds",
            "query_seconds",
            "peak_rss_kib",
            "memory_note",
            "index_sha256",
        ):
            self.assertIn(key, report)
        extracted = sorted((workdir / "output").glob(f"*[[]{KNOWN_FILE}].fb2"))
        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0].read_bytes(), KNOWN_PAYLOAD)

    def test_verify_reuse(self):
        """Повтор с --reuse-index: без пересборки, прежние hash базы и FB2."""
        self.library = self._library()
        workdir = self.base / "verify-work"
        first = self._verify(workdir)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        report1 = json.loads((workdir / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(report1["rebuilt"])
        extracted = sorted((workdir / "output").glob("*.fb2"))
        self.assertEqual(len(extracted), 1)
        fb2_stat = extracted[0].stat()
        index_stat = (workdir / "state" / "cache" / "index.sqlite3").stat()

        second = self._verify(workdir, reuse=True)
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        report2 = json.loads((workdir / "report.json").read_text(encoding="utf-8"))
        self.assertFalse(report2["rebuilt"])
        self.assertTrue(report2["output_reused"])
        self.assertEqual(report2["index_sha256"], report1["index_sha256"])
        self.assertEqual(report2["output_sha256"], report1["output_sha256"])
        self.assertEqual(extracted[0].stat().st_mtime_ns, fb2_stat.st_mtime_ns)
        index_after = (workdir / "state" / "cache" / "index.sqlite3").stat()
        self.assertEqual(index_after.st_mtime_ns, index_stat.st_mtime_ns)

        # Чужой workdir без маркера: reuse запрещён, содержимое не трогается.
        foreign = self.base / "foreign"
        foreign.mkdir()
        markerless = self._verify(foreign, reuse=True)
        self.assertNotEqual(markerless.returncode, 0, markerless.stdout)
        self.assertIn("workdir", markerless.stderr)
        self.assertEqual(list(foreign.iterdir()), [])

    def test_verify_missing_known_book(self):
        """Известная пара отсутствует/подменена — явный отказ, не случайное издание."""
        self.library = _known_library(self.base / "library", known=False)
        workdir = self.base / "verify-work"
        proc = self._verify(workdir)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        report = json.loads((workdir / "report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["query_target_met"])
        self.assertFalse(report["ok"])
        self.assertIsNone(report["selected_archive"])
        self.assertIsNone(report["selected_member"])
        self.assertIsNone(report["output_sha256"])
        self.assertTrue(report["failures"])
        output_dir = workdir / "output"
        self.assertFalse(output_dir.exists() or list(output_dir.glob("*.fb2")))

    def test_verify_wrong_title_no_extraction(self):
        """Точная пара с подменённым названием — отказ ДО любого чтения/извлечения."""
        self.library = make_library(
            self.base / "library",
            {
                KNOWN_INP: [
                    _row(
                        KNOWN_FILE,
                        KNOWN_AUTHOR,
                        "Мастер подменён",
                        LANG="ru",
                        SIZE=str(len(KNOWN_PAYLOAD)),
                    )
                ],
                "fb2-other-000": [_row("3", "Автор, Три", "Книга Три")],
            },
            members={
                KNOWN_INP: {KNOWN_MEMBER: KNOWN_PAYLOAD},
                "fb2-other-000": {"3.fb2": b"<FictionBook id='3'/>"},
            },
        )
        workdir = self.base / "verify-work"
        proc = self._verify(workdir)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        report = json.loads((workdir / "report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["query_target_met"])
        self.assertIsNone(report["output_sha256"])
        self.assertTrue(any("назван" in failure for failure in report["failures"]))
        # Ни чтения open_book, ни извлечения: файла нет вообще.
        output_dir = workdir / "output"
        self.assertFalse(output_dir.exists() or list(output_dir.glob("*.fb2")))

    def _owned_workdir(self, name="verify-work"):
        """Один успешный прогон verify — честно владельческий workdir с маркером."""
        self.library = self._library()
        workdir = self.base / name
        proc = self._verify(workdir)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return workdir

    @staticmethod
    def _source_fingerprint(path):
        """SHA-256 + mtime_ns источника (atime сознательно не учитывается)."""
        import hashlib

        return (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )

    def test_verify_rejects_symlinked_state_into_library(self):
        """Symlink state в существующий каталог библиотеки — отказ до записи.

        Цель ссылки существует (как `docs/` в реальной библиотеке): без
        барьера управляемого дерева ensure_index создал бы внутри библиотеки
        `cache/index.sqlite3`. Битая цель не показатель — makedirs падает
        раньше любой записи.
        """
        import shutil

        workdir = self._owned_workdir()
        target = self.library.root / "docs"
        target.mkdir()
        shutil.rmtree(workdir / "state")
        (workdir / "state").symlink_to(target)
        fingerprint = self._source_fingerprint(self.library.catalog)

        proc = self._verify(workdir, reuse=True)

        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("state", proc.stderr)
        self.assertFalse(
            list(target.glob("cache/*")),
            "ни один путь внутри библиотеки не должен создаваться",
        )
        self.assertEqual(self._source_fingerprint(self.library.catalog), fingerprint)

    def test_verify_rejects_symlinked_report_target(self):
        """report.json → чужой файл: отказ, чужие байты и mtime не тронуты."""
        workdir = self._owned_workdir()
        foreign = self.base / "foreign-target.txt"
        sentinel = b"foreign sentinel bytes\n"
        foreign.write_bytes(sentinel)
        (workdir / "report.json").unlink()
        (workdir / "report.json").symlink_to(foreign)
        fingerprint = self._source_fingerprint(foreign)

        proc = self._verify(workdir, reuse=True)

        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("report.json", proc.stderr)
        self.assertEqual(foreign.read_bytes(), sentinel)
        self.assertEqual(self._source_fingerprint(foreign), fingerprint)
        self.assertTrue((workdir / "report.json").is_symlink())

    def test_verify_rejects_report_symlink_into_library(self):
        """report.json → INPX библиотеки: отказ, источник побайтно прежний."""
        import hashlib

        workdir = self._owned_workdir()
        (workdir / "report.json").unlink()
        (workdir / "report.json").symlink_to(self.library.catalog)
        fingerprint = self._source_fingerprint(self.library.catalog)

        proc = self._verify(workdir, reuse=True)

        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(
            hashlib.sha256(self.library.catalog.read_bytes()).hexdigest(),
            fingerprint[0],
        )
        self.assertEqual(self._source_fingerprint(self.library.catalog), fingerprint)

    def test_verify_rejects_dangling_report_symlink(self):
        """Битая symlink report.json: отказ, цель-несуществующий путь не создаётся."""
        workdir = self._owned_workdir()
        leak = self.base / "never-created.json"
        (workdir / "report.json").unlink()
        (workdir / "report.json").symlink_to(leak)

        proc = self._verify(workdir, reuse=True)

        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertFalse(leak.exists(), "запись сквозь битую symlink недопустима")
        self.assertTrue((workdir / "report.json").is_symlink())

    def test_verify_rejects_foreign_marker_content(self):
        """Маркер чужого содержимого — workdir не наш; отказ без изменений."""
        workdir = self._owned_workdir()
        marker = workdir / "keklib-cli-verify.owner"
        marker.write_bytes(b"foreign owner\n")

        proc = self._verify(workdir, reuse=True)

        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("маркер", proc.stderr)
        self.assertEqual(marker.read_bytes(), b"foreign owner\n")

    def test_windows_tty_required(self):
        """На Windows harness обязан быть установлен — отказ, а не skip.

        Проверяется импорт из модуля ``winpty`` (его предоставляет
        дистрибутив ``pywinpty``) и присутствие используемых harness методов
        upstream API — это статическая проверка доступности и формы API,
        нативным поведением она не является (нативный прогон — задача 12).
        """
        if sys.platform != "win32":
            self.skipTest(
                "нативный ConPTY-прогон выполняется на Windows; здесь проверен "
                "только POSIX PTY-путь"
            )
        try:
            from winpty import PtyProcess
        except ImportError:
            self.fail(
                "нативный Windows TTY-harness не установлен: модуль winpty "
                "(дистрибутив pywinpty) отсутствует; установите test-only "
                "зависимость из packaging/requirements-test-windows.lock"
            )
        for method in ("spawn", "read", "write", "terminate", "close", "isalive"):
            self.assertTrue(
                callable(getattr(PtyProcess, method, None)),
                f"upstream API PtyProcess.{method} недоступен",
            )
        self.assertTrue(hasattr(PtyProcess, "exitstatus"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
