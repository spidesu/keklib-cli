"""Тесты контрактов конфигурации keklib-cli (`keklib_cli.config`)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from keklib_cli.config import inspect_library, load_library, save_library
from keklib_cli.errors import CatalogError, ConfigError
from keklib_cli.models import Library, StatePaths

from tests.fixtures import make_library

ROW = {
    "AUTHOR": "Перумов, Ник",
    "TITLE": "Кольцо Тьмы",
    "SERIES": "Хроники Хьёрварда",
    "SERNO": "1",
    "FILE": "204000",
    "SIZE": "1000",
    "LIBID": "204000",
    "DEL": "0",
    "EXT": "fb2",
    "LANG": "ru",
}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        state = self.base / "state"
        self.paths = StatePaths(
            config=state / "config.json",
            cache=state / "cache",
            index=state / "index",
        )

    def test_roundtrip(self):
        library = make_library(
            self.base / "library",
            {"flib": [ROW]},
            members={"flib": {"204000.fb2": b"<FictionBook/>"}},
        )
        save_library(self.paths, library)
        self.assertTrue(self.paths.config.is_file())
        self.assertEqual(load_library(self.paths), library)

    def test_bad_json_or_version_preserved(self):
        root = make_library(
            self.base / "library",
            {"flib": [ROW]},
            members={"flib": {"204000.fb2": b"<FictionBook/>"}},
        ).root
        cases = {
            "not-json": b"\xff\xfe not a json",
            "wrong-version": json.dumps(
                {"version": 2, "library_root": str(root)}
            ).encode("utf-8"),
            "no-version": json.dumps({"library_root": str(root)}).encode("utf-8"),
            "bool-version": json.dumps(
                {"version": True, "library_root": str(root)}
            ).encode("utf-8"),
            "float-version": json.dumps(
                {"version": 1.0, "library_root": str(root)}
            ).encode("utf-8"),
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                self.paths.config.parent.mkdir(parents=True, exist_ok=True)
                self.paths.config.write_bytes(payload)
                with self.assertRaises(ConfigError):
                    load_library(self.paths)
                self.assertEqual(self.paths.config.read_bytes(), payload)

    def test_invalid_root_not_saved(self):
        empty_root = self.base / "empty"
        empty_root.mkdir()
        incomplete = make_library(self.base / "no-zips", {"flib": [ROW]})
        incomplete.archives.rmdir()
        libraries = {
            "no-inpx": Library(
                root=empty_root,
                catalog=empty_root / "librusec_flib.inpx",
                archives=empty_root / "lib.rus.ec",
            ),
            "no-archives-dir": incomplete,
        }
        for name, library in libraries.items():
            with self.subTest(case=name):
                with self.assertRaises(CatalogError):
                    save_library(self.paths, library)
                self.assertFalse(self.paths.config.exists())

    def test_cleanup_failure_does_not_mask_write_error(self):
        library = make_library(
            self.base / "library",
            {"flib": [ROW]},
            members={"flib": {"204000.fb2": b"<FictionBook/>"}},
        )
        replace_error = OSError("replace failed")
        with (
            mock.patch(
                "keklib_cli.config.os.replace", side_effect=replace_error
            ),
            mock.patch(
                "keklib_cli.config.Path.unlink",
                side_effect=OSError("cleanup failed"),
            ),
        ):
            with self.assertRaises(ConfigError) as ctx:
                save_library(self.paths, library)
        self.assertIn("cannot write configuration file", str(ctx.exception))
        self.assertIs(ctx.exception.__cause__, replace_error)
        self.assertNotIn("cleanup failed", str(ctx.exception))

    def test_spaces_and_cyrillic(self):
        library = make_library(
            self.base / "Библиотека книг",
            {"flib": [ROW]},
            members={"flib": {"204000.fb2": b"<FictionBook/>"}},
        )
        save_library(self.paths, library)
        stored = json.loads(self.paths.config.read_text(encoding="utf-8"))
        self.assertEqual(stored["library_root"], str(library.root))
        self.assertEqual(load_library(self.paths), library)


class InspectLibraryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def test_absolute_paths(self):
        library = make_library(self.base / "library", {"flib": [ROW]})
        for path in (library.root, library.catalog, library.archives):
            self.assertTrue(path.is_absolute())
        self.assertEqual(
            library,
            inspect_library(self.base / "library" / "." / ".." / "library"),
        )


if __name__ == "__main__":
    unittest.main()
