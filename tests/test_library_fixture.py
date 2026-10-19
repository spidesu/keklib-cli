"""Регрессионные тесты генератора синтетических библиотек.

Спецификация §6 требует простых имён INP без компонентов пути; записи INP
лежат в корне INPX-контейнера. `lib.rus.ec/` — каталог архивов книг на диске.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.harness.library_fixture import write_library

ROW = {"AUTHOR": "Автор", "TITLE": "Книга", "FILE": "204000", "EXT": "fb2"}


class LibraryFixtureTests(unittest.TestCase):
    def test_inpx_members_are_simple_root_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_library(
                Path(tmp) / "library",
                {"fb2-001": [ROW], "other": []},
                members={"fb2-001": {"204000.fb2": b"<FictionBook/>"}},
            )
            with zipfile.ZipFile(root / "librusec_flib.inpx") as catalog:
                names = catalog.namelist()
            self.assertEqual(names, ["structure.info", "fb2-001.inp", "other.inp"])
            for name in names:
                self.assertNotIn("/", name)
            archives = root / "lib.rus.ec"
            self.assertTrue(archives.is_dir())
            self.assertEqual(
                sorted(path.name for path in archives.iterdir()), ["fb2-001.zip"]
            )

    def test_archives_dir_always_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_library(Path(tmp) / "library", {})
            self.assertTrue((root / "lib.rus.ec").is_dir())
            with zipfile.ZipFile(root / "librusec_flib.inpx") as catalog:
                self.assertEqual(catalog.namelist(), ["structure.info"])


if __name__ == "__main__":
    unittest.main()
