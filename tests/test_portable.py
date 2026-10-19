"""Контракт платформенно-нативных temp-каталогов тестов.

Реальный CI run 39271547601 (BASE 4ba52eb9): Windows-hosted runners не
задают ``TMPDIR``, а жёстко заданный путь ``/tmp`` там не существует —
36 ERROR в новых inventory/packaging тестах. Контракт: ``TMPDIR``
уважается, если задан; иначе нативный default ``tempfile`` самой ОС.
Никакого ``mkdir C:\\tmp``, маскировки окружения всего CI или OS-skip.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent

#: Анти-паттерн волны native-tooling тестов (собран конкатенацией, чтобы
#: структурная регрессия не находила саму себя в собственном исходнике).
TMPDIR_TMP_FALLBACK = "os.environ.get(" + '"TMPDIR", "/tmp")'


class PortableTempTests(unittest.TestCase):
    def test_respects_tmpdir_when_set(self):
        from tests.portable import portable_mkdtemp

        base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: os.rmdir(base))
        with mock.patch.dict(os.environ, {"TMPDIR": str(base)}):
            made = portable_mkdtemp()
        self.assertEqual(
            made.parent, base, "TMPDIR задан — каталог создаётся внутри него"
        )
        made.rmdir()

    def test_native_default_without_tmpdir(self):
        """TMPDIR отсутствует — нативный default ОС, а не hardcoded /tmp."""
        from tests.portable import portable_mkdtemp

        env = {k: v for k, v in os.environ.items() if k != "TMPDIR"}
        with mock.patch.dict(
            os.environ, env, clear=True
        ), mock.patch.object(tempfile, "tempdir", None):
            expected_root = Path(tempfile.gettempdir())
            made = portable_mkdtemp()
            self.assertEqual(
                made.parent,
                expected_root,
                "без TMPDIR используется нативный default tempfile этой ОС",
            )
        made.rmdir()

    def test_helper_has_no_hardcoded_tmp(self):
        source = (TESTS_DIR / "portable.py").read_text(encoding="utf-8")
        self.assertNotIn(
            '"/tmp"', source, "портативный helper не содержит hardcoded /tmp"
        )
        self.assertNotIn("'/tmp'", source)

    def test_no_tmpdir_slash_tmp_fallback_in_tests(self):
        """Структурная регрессия: анти-паттерн не возвращается в тесты."""
        offenders = []
        for path in sorted(TESTS_DIR.rglob("*.py")):
            if TMPDIR_TMP_FALLBACK in path.read_text(encoding="utf-8"):
                offenders.append(path.relative_to(TESTS_DIR).as_posix())
        self.assertEqual(
            offenders, [], f"hardcoded /tmp fallback вернулся: {offenders}"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
