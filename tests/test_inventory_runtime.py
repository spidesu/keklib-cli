"""Тесты диагностической runtime-инвентаризации pending-targets (этап 1
native-platform-artifacts).

``tools/inventory_runtime.py`` — диагностический инструмент: он переиспользует
сборочные примитивы ``tools/build_portable.py`` (проверка нативности target,
preflight toolchain, запуск PyInstaller, фактические метаданные build-info,
самопроверка executable, рекурсивный инвентарь библиотек) и пишет один
JSON-отчёт с пометкой ``diagnostic_only``. Архив не создаётся, выпускные
гейты не вызываются и не обходятся, нулевые SQLite-пины остаются нулевыми.

Моки здесь покрывают ТОЛЬКО контракты связки (тяжёлые стадии подменяются
фикстурами); нативной приёмкой Windows/macOS эти тесты не являются и
``native_build_verified`` не устанавливают. Структура workflow
``platform-inventory.yml`` проверяется текстуально, как
``WorkflowStructureTests`` в ``tests/test_release.py``.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.portable import portable_mkdtemp  # noqa: E402

from tools import build_portable as bp  # noqa: E402
from tools import inventory_runtime as ir  # noqa: E402

#: Ровно три pending-target из targets.json со своими стандартными runner.
PENDING_RUNNERS = {
    "windows-x86_64": "windows-2022",
    "macos-x86_64": "macos-15-intel",
    "macos-arm64": "macos-14",
}

VALID_SHA = "0123456789abcdef0123456789abcdef01234567"

WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "platform-inventory.yml"

#: Идентификаторы, которые диагностический инструмент обязан переиспользовать.
REQUIRED_REUSE = {
    "assert_native_target",
    "verify_toolchain_preflight",
    "_run_pyinstaller",
    "build_info",
    "_run_selfcheck",
    "inventory_bundle_libraries",
    # Контроль окружения сборки — общий helper/adapter из build_portable,
    # а не собственная логика inventory-инструмента.
    "pyinstaller_environment_facts",
}

#: Выпускные операции и гейты, которые диагностический инструмент Calling
#: запрещён: их отсутствие в AST модуля — контракт «не выпускать и не
#: обходить гейты», а не обещание теста.
FORBIDDEN_RELEASE_OPS = {
    "archive_bundle",
    "verify_release_metadata",
    "verify_license_closure",
    "verify_glibc_floor",
    "_validate_bundle",
    "repair_checksum",
}


def _fake_pe_bytes() -> bytes:
    """Минимальный MZ/PE-заголовок: достаточно для распознавания формата."""
    header = bytearray(0x100)
    header[0:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x80)
    header[0x80:0x84] = b"PE\x00\x00"
    return bytes(header)


def _fake_bundle(dist: Path, executable: str) -> None:
    """Фикстурный onedir-комплект вместо реального PyInstaller-прогона."""
    bundle = dist / "keklib-cli"
    internal = bundle / "_internal"
    internal.mkdir(parents=True)
    (bundle / executable).write_bytes(_fake_pe_bytes())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("encodings.py", b"")
    (internal / "base_library.zip").write_bytes(buffer.getvalue())
    (internal / "sqlite3.dll").write_bytes(_fake_pe_bytes())
    (internal / "_sha2.pyd").write_bytes(_fake_pe_bytes())
    # Extensionless Mach-O: framework-runtime macOS вида Python.framework.
    framework = internal / "Python.framework" / "Versions" / "3.14"
    framework.mkdir(parents=True)
    (framework / "Python").write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)


def _write_collect_toc(work: Path, entries: list[tuple[str, str, str]]) -> None:
    """Синтетический COLLECT-00.toc в формате PyInstaller (python-literal)."""
    work.mkdir(parents=True, exist_ok=True)
    (work / "COLLECT-00.toc").write_text(repr((entries,)), encoding="utf-8")


@contextlib.contextmanager
def patched_stages(fake_toc: list[tuple[str, str, str]] | None = None):
    """Патч тяжёлых стадий на контракты; фикстура комплекта создаётся сама.

    TOC пишется во вложенный workdir ``<work>/keklib-cli`` — фактический
    layout закреплённого PyInstaller (``workpath = join(workpath,
    specnm)``), с реальными формами dest COLLECT: EXECUTABLE в корне
    комплекта, остальные назначения относительно ``_internal/`` без
    самого префикса.
    """
    executable = "keklib-cli.exe"

    def fake_pyinstaller(distpath: Path, workpath: Path) -> None:
        _fake_bundle(Path(distpath), executable)
        if fake_toc is not None:
            _write_collect_toc(Path(workpath) / "keklib-cli", fake_toc)

    with contextlib.ExitStack() as stack:
        for patch in (
            mock.patch.object(bp, "assert_native_target", mock.Mock()),
            mock.patch.object(bp, "verify_toolchain_preflight", mock.Mock()),
            mock.patch.object(
                bp, "_run_pyinstaller", mock.Mock(side_effect=fake_pyinstaller)
            ),
            mock.patch.object(
                bp,
                "build_info",
                mock.Mock(
                    return_value={
                        "version": "0.1.0",
                        "target": "windows-x86_64",
                        "python": "3.14.8",
                        "sqlite": "3.9.3-synthetic",
                        "sqlite_fts5": True,
                        "pyinstaller": "6.22.3",
                        "schema_version": 2,
                        "rules_version": 2,
                        "minimum_os": "Windows Server 2022 (сборочная ОС)",
                        "minimum_glibc": None,
                    }
                ),
            ),
            mock.patch.object(bp, "_run_selfcheck", mock.Mock()),
        ):
            stack.enter_context(patch)
        yield


class PendingTargetsTests(unittest.TestCase):
    def test_pending_targets_exactly_three(self):
        self.assertEqual(
            ir.PENDING_TARGETS,
            {"windows-x86_64", "macos-x86_64", "macos-arm64"},
        )

    def test_pending_specs_match_targets_json(self):
        targets = bp.load_targets()
        for name, runner in PENDING_RUNNERS.items():
            self.assertIn(name, targets)
            self.assertEqual(targets[name]["runner"], runner)
        self.assertNotIn("linux-x86_64", ir.PENDING_TARGETS)

    def test_refuses_non_pending_target_before_native_check(self):
        """Linux уже принят (не pending): отказ до любых тяжёлых стадий."""
        out = portable_mkdtemp()
        self.addCleanup(lambda: os.rmdir(out))
        native = mock.Mock()
        with mock.patch.object(bp, "assert_native_target", native):
            with self.assertRaises(bp.PackagingError) as ctx:
                ir.inventory_runtime(
                    "linux-x86_64", source_sha=VALID_SHA, output=out
                )
        self.assertIn("pending", str(ctx.exception))
        native.assert_not_called()

    def test_refuses_unknown_target(self):
        out = portable_mkdtemp()
        self.addCleanup(lambda: os.rmdir(out))
        with self.assertRaises(bp.PackagingError):
            ir.inventory_runtime("plan9-arm", source_sha=VALID_SHA, output=out)


class SourceShaTests(unittest.TestCase):
    def test_explicit_sha_accepted_and_normalized(self):
        self.assertEqual(
            ir.resolve_source_sha(VALID_SHA.upper()), VALID_SHA
        )

    def test_github_sha_env_used_when_no_argument(self):
        env = {"GITHUB_SHA": VALID_SHA}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("GITHUB_SHA", None)
            os.environ["GITHUB_SHA"] = VALID_SHA
            try:
                self.assertEqual(ir.resolve_source_sha(None), VALID_SHA)
            finally:
                pass

    def test_explicit_argument_overrides_env(self):
        other = "f" * 40
        with mock.patch.dict(os.environ, {"GITHUB_SHA": other}, clear=False):
            self.assertEqual(ir.resolve_source_sha(VALID_SHA), VALID_SHA)

    def test_missing_sha_is_error(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_SHA", None)
            with self.assertRaises(bp.PackagingError):
                ir.resolve_source_sha(None)

    def test_invalid_sha_rejected(self):
        for bad in ("", "zzz", "a" * 39, "a" * 64, "0123456789abcdef" * 3):
            with self.assertRaises(bp.PackagingError):
                ir.resolve_source_sha(bad)

    def test_sha_required_before_native_and_preflight(self):
        out = portable_mkdtemp()
        self.addCleanup(lambda: os.rmdir(out))
        native = mock.Mock()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_SHA", None)
            with mock.patch.object(bp, "assert_native_target", native):
                with self.assertRaises(bp.PackagingError):
                    ir.inventory_runtime(
                        "windows-x86_64", source_sha=None, output=out
                    )
        native.assert_not_called()


class OrderAndCleanupTests(unittest.TestCase):
    def test_native_gate_precedes_preflight(self):
        out = portable_mkdtemp()
        self.addCleanup(lambda: os.rmdir(out))
        preflight = mock.Mock()
        with mock.patch.object(
            bp,
            "assert_native_target",
            mock.Mock(side_effect=bp.PackagingError("cross-forced")),
        ), mock.patch.object(bp, "verify_toolchain_preflight", preflight):
            with self.assertRaises(bp.PackagingError):
                ir.inventory_runtime(
                    "windows-x86_64", source_sha=VALID_SHA, output=out
                )
        preflight.assert_not_called()

    def test_staging_cleanup_only_own(self):
        build_dir = PROJECT_ROOT / "build"
        build_dir.mkdir(parents=True, exist_ok=True)
        sentinel = build_dir / "not-inventory-sentinel"
        sentinel.mkdir(exist_ok=True)
        before = {p.name for p in build_dir.glob("inventory-*")}
        out = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(out))
        with patched_stages():
            report = ir.inventory_runtime(
                "windows-x86_64", source_sha=VALID_SHA, output=out
            )
        after = {p.name for p in build_dir.glob("inventory-*")}
        self.assertEqual(after, before, "staging обязан удаляться после прогона")
        self.assertTrue(sentinel.is_dir(), "чужие каталоги build/ не трогаются")
        self.assertEqual(
            report.parent, out, "отчёт публикуется вне staging, в output"
        )
        self.assertNotIn("build", report.parts[-3:], "отчёт не в build/")


class ReportContractTests(unittest.TestCase):
    """Полный контракт отчёта на замоканных стадиях (не native acceptance)."""

    TARGET = "windows-x86_64"

    def setUp(self):
        self.out = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(self.out))
        toolchain = PROJECT_ROOT / "packaging" / "toolchain.json"
        self.toolchain_before = toolchain.read_bytes()
        targets = PROJECT_ROOT / "packaging" / "targets.json"
        self.targets_before = targets.read_bytes()
        self.toc = [
            # Реальные формы dest COLLECT: EXECUTABLE в корне комплекта,
            # остальные — относительно _internal/ без префикса. Запись
            # collide/_sha2.pyd провоцирует коллизию basename с
            # _internal/_sha2.pyd: неоднозначный fallback запрещён.
            ("base_library.zip", "/opt/hostedtoolcache/base_library.zip", "BINARY"),
            ("sqlite3.dll", "/opt/hostedtoolcache/sqlite3.dll", "BINARY"),
            ("keklib-cli.exe", "/opt/hostedtoolcache/keklib-cli.exe", "EXECUTABLE"),
            ("collide/_sha2.pyd", "/opt/other/_sha2.pyd", "BINARY"),
        ]

    def test_report_contract(self):
        with patched_stages(self.toc):
            report_path = ir.inventory_runtime(
                self.TARGET, source_sha=VALID_SHA, output=self.out
            )
        self.assertEqual(
            report_path.name, f"inventory-report-{self.TARGET}.json"
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))

        # Обязательные ключи и честные пометки диагностики.
        self.assertEqual(report["schema_version"], 2)
        self.assertIs(report["diagnostic_only"], True)
        self.assertEqual(report["tool"]["name"], "inventory_runtime")
        self.assertEqual(report["source_sha"], VALID_SHA)
        self.assertEqual(report["target"], self.TARGET)
        self.assertEqual(report["target_spec"]["runner"], "windows-2022")
        self.assertEqual(report["target_spec"]["executable"], "keklib-cli.exe")

        # Факты хоста — фактические, не выдуманные.
        import platform as _platform

        self.assertEqual(report["host"]["os"], _platform.system())
        self.assertEqual(
            report["host"]["arch_raw"], _platform.machine()
        )
        self.assertIn(report["host"]["macos_version"], (None, _platform.mac_ver()[0]))

        # Факты runtime: живой интерпретатор, на котором работает инструмент.
        self.assertEqual(report["runtime"]["python"]["version"], _platform.python_version())
        self.assertEqual(
            report["runtime"]["sqlite"]["version"], sqlite3.sqlite_version
        )
        self.assertIs(report["runtime"]["sqlite"]["fts5"], True)
        self.assertIsInstance(report["runtime"]["sqlite"]["compile_options"], list)
        for key in ("openssl", "zlib", "libffi", "decimal", "lzma"):
            self.assertIn(key, report["runtime"])
            self.assertIn("available", report["runtime"][key])
        # F2: версии статических зависимостей, которые runtime-отчёты не
        # измеряли, зондятся честно: decimal.__libmpdec_version__ (строка
        # или null) и узкая ctypes-проба lzma_version_string образа самого
        # модуля _lzma (строка или null — «не экспортирован», без догадок).
        decimal_facts = report["runtime"]["decimal"]
        self.assertIn("libmpdec_version", decimal_facts)
        self.assertTrue(
            decimal_facts["libmpdec_version"] is None
            or isinstance(decimal_facts["libmpdec_version"], str)
        )
        lzma_facts = report["runtime"]["lzma"]
        self.assertIn("liblzma_version", lzma_facts)
        self.assertIn("probe", lzma_facts)
        self.assertTrue(
            lzma_facts["liblzma_version"] is None
            or isinstance(lzma_facts["liblzma_version"], str)
        )
        # Стандартный модуль lzma не раскрывает версию liblzma константой —
        # единственный источник факта узкая ctypes-проба (или честный null).
        self.assertNotIn("module_version", lzma_facts)

        # Preflight прошёл; SQLite-пин pending-target зафиксирован в
        # toolchain.json фактом инвентаризации (3.50.4 для всех трёх
        # native-target) и попадает в отчёт как есть — null более не
        # ожидается, но и не выдумывается: значение приходит из пина.
        self.assertIs(report["preflight"]["passed"], True)
        toolchain = json.loads(
            (PROJECT_ROOT / "packaging" / "toolchain.json").read_text(encoding="utf-8")
        )
        expected_sqlite = toolchain["targets"][self.TARGET]["sqlite"]
        self.assertEqual(report["preflight"]["pins"]["sqlite"], expected_sqlite)
        self.assertEqual(report["preflight"]["pins"]["python"], "3.14.8")

        # Фактические метаданные build-info пробрасываются как есть.
        self.assertEqual(report["build_info"]["sqlite"], "3.9.3-synthetic")
        self.assertIs(report["selfcheck"]["passed"], True)

        # Контроль окружения сборки: фактический PATH child-процесса
        # PyInstaller и PATH родителя — доказательство исключения чужих
        # каталогов (Java_Temurin) на нативном повторе. Факты собираются
        # общим helper'ом build_portable, а не самим инструментом.
        build_env = report["build_environment"]
        self.assertEqual(build_env["platform"], sys.platform)
        self.assertEqual(
            build_env["policy"],
            "windows-restricted-path"
            if sys.platform == "win32"
            else "inherit-parent",
        )
        self.assertEqual(build_env["python_executable"], sys.executable)
        self.assertEqual(build_env["parent_path"], os.environ.get("PATH", ""))
        if sys.platform == "win32":
            from pathlib import PureWindowsPath

            root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT")
            exe_parent = PureWindowsPath(sys.executable).parent
            self.assertEqual(
                build_env["subprocess_path_entries"],
                [
                    str(exe_parent),
                    str(exe_parent / "Scripts"),
                    str(PureWindowsPath(root) / "System32"),
                    str(PureWindowsPath(root)),
                ],
            )
            self.assertEqual(build_env["system_root"], root)
        else:
            # Не-Windows: PATH child-процесса не отличается от родительского.
            self.assertEqual(
                build_env["subprocess_path"], os.environ.get("PATH", "")
            )
            self.assertEqual(build_env["subprocess_path_entries"], [])
            self.assertIsNone(build_env["system_root"])

        # Полный инвентарь: относительные POSIX-пути, размер и SHA-256.
        files = {entry["path"]: entry for entry in report["bundle"]["files"]}
        self.assertEqual(
            set(files),
            {
                "keklib-cli.exe",
                "_internal/base_library.zip",
                "_internal/sqlite3.dll",
                "_internal/_sha2.pyd",
                "_internal/Python.framework/Versions/3.14/Python",
            },
        )
        for entry in files.values():
            self.assertIn(entry["type"], ("file", "symlink"))
            self.assertIsInstance(entry["size"], int)
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("encodings.py", b"")
        self.assertEqual(
            files["_internal/base_library.zip"]["sha256"],
            hashlib.sha256(buffer.getvalue()).hexdigest(),
        )

        # Библиотечный инвентарь (переиспользованный) дополнен нативными
        # бинарями полного обхода по magic: extensionless Mach-O framework
        # runtime получает записи deps/provenance наравне с остальными.
        libraries = {item["path"]: item for item in report["libraries"]}
        self.assertEqual(
            set(libraries),
            {
                "keklib-cli.exe",
                "_internal/base_library.zip",
                "_internal/sqlite3.dll",
                "_internal/_sha2.pyd",
                "_internal/Python.framework/Versions/3.14/Python",
            },
        )
        framework = libraries["_internal/Python.framework/Versions/3.14/Python"]
        # Инвентарь сборки сам распознаёт extensionless Mach-O по magic:
        # framework-runtime — часть контракта лицензионного инвентаря, а не
        # только диагностическое дополнение полного обхода.
        self.assertEqual(framework["recognized_by"], "library_inventory")
        self.assertIsInstance(framework["deps"], dict)
        self.assertIn("available", framework["deps"])
        for path in (
            "keklib-cli.exe",
            "_internal/base_library.zip",
            "_internal/sqlite3.dll",
            "_internal/_sha2.pyd",
        ):
            self.assertEqual(
                libraries[path]["recognized_by"],
                "library_inventory",
                f"{path} закрывается переиспользованным библиотечным инвентарём",
            )

        # Provenance по правилам COLLECT: точное имя в корне, префикс
        # _internal/ отбрасывается у остальных; неоднозначный basename
        # (collide/_sha2.pyd) не подставляет чужой источник.
        self.assertEqual(
            libraries["_internal/sqlite3.dll"]["toc_source"],
            {
                "dest": "sqlite3.dll",
                "src": "/opt/hostedtoolcache/sqlite3.dll",
                "typecode": "BINARY",
            },
        )
        self.assertEqual(
            libraries["_internal/base_library.zip"]["toc_source"]["dest"],
            "base_library.zip",
        )
        self.assertEqual(
            libraries["keklib-cli.exe"]["toc_source"]["dest"],
            "keklib-cli.exe",
        )
        self.assertIsNone(libraries["_internal/_sha2.pyd"]["toc_source"])
        for item in libraries.values():
            self.assertIn("deps", item)

        # Пробы нативных зависимостей — всегда словарь с available, без исключений.
        deps = libraries["_internal/sqlite3.dll"]["deps"]
        self.assertIsInstance(deps, dict)
        self.assertIn("available", deps)

        # Лицензии runtime: известных мест, с путём/хешем/текстом.
        licenses = report["runtime_licenses"]
        self.assertIsInstance(licenses, list)
        for entry in licenses:
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
            self.assertIsInstance(entry["text"], str)
            self.assertIsInstance(entry["path"], str)

        # Пины не изменяются самим инструментом.
        self.assertEqual(
            (PROJECT_ROOT / "packaging" / "toolchain.json").read_bytes(),
            self.toolchain_before,
        )
        self.assertEqual(
            (PROJECT_ROOT / "packaging" / "targets.json").read_bytes(),
            self.targets_before,
        )

    def test_symlink_entries_recorded_without_hash(self):
        try:
            staging = portable_mkdtemp()
        except OSError as exc:  # pragma: no cover
            self.skipTest(f"временный каталог недоступен: {exc}")
        self.addCleanup(lambda: _rmtree(staging))
        link_source = staging / "target.dll"
        link_source.write_bytes(_fake_pe_bytes())
        link = staging / "alias.dll"
        try:
            link.symlink_to(link_source)
        except (OSError, NotImplementedError):
            self.skipTest("symlink на этой ОС/окружении недоступен")
        entries = {e["path"]: e for e in ir.full_bundle_inventory(staging)}
        self.assertEqual(entries["alias.dll"]["type"], "symlink")
        self.assertIsNone(entries["alias.dll"]["sha256"])
        self.assertEqual(entries["target.dll"]["type"], "file")
        self.assertRegex(entries["target.dll"]["sha256"], r"^[0-9a-f]{64}$")


class ReportAtomicityTests(unittest.TestCase):
    def test_refuses_overwrite_existing_report(self):
        out = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(out))
        target = "inventory-report-windows-x86_64.json"
        existing = out / target
        payload = b"previous report must survive"
        existing.write_bytes(payload)
        with patched_stages():
            with self.assertRaises(bp.PackagingError) as ctx:
                ir.inventory_runtime(
                    "windows-x86_64", source_sha=VALID_SHA, output=out
                )
        self.assertIn("перезапис", str(ctx.exception))
        self.assertEqual(existing.read_bytes(), payload)
        leftovers = [p for p in out.iterdir() if p.name != target]
        self.assertEqual(leftovers, [], "временные файлы убираются при отказе")

    def test_failed_publish_leaves_no_report(self):
        out = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(out))
        with patched_stages(), mock.patch.object(
            bp,
            "_publish_no_replace",
            mock.Mock(side_effect=OSError("publish failed")),
        ):
            with self.assertRaises(bp.PackagingError):
                ir.inventory_runtime(
                    "windows-x86_64", source_sha=VALID_SHA, output=out
                )
        self.assertEqual(list(out.iterdir()), [], "частичный отчёт не публикуется")


class SourceContractTests(unittest.TestCase):
    """AST-контракты: переиспользование примитивов и запрет выпуска."""

    SOURCE = Path(ir.__file__).read_text(encoding="utf-8")

    @classmethod
    def _names(cls) -> set[str]:
        tree = ast.parse(cls.SOURCE)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        return names

    def test_reuses_build_portable_primitives(self):
        names = self._names()
        missing = REQUIRED_REUSE - names
        self.assertFalse(
            missing, f"инструмент обязан переиспользовать примитивы: {missing}"
        )

    def test_no_release_operations_or_gate_bypass(self):
        names = self._names()
        used = FORBIDDEN_RELEASE_OPS & names
        self.assertFalse(
            used,
            "диагностический инструмент не вызывает выпускные операции/гейты: "
            f"{used}",
        )


class DepsProbeTests(unittest.TestCase):
    def test_format_detection(self):
        """Распознавание форматов: все Mach-O magic, включая FAT64, ELF — нет.

        FAT64 (universal Mach-O с 64-битными fat_arch) — отдельный формат,
        а не уже покрытый thin Mach-O 64/FAT32: обе ориентации обязаны
        проходить is_macho, иначе полный обход не заметит такой бинарь
        (F1 final review).
        """
        for magic in (
            b"\xcf\xfa\xed\xfe",  # MH_MAGIC_64 LE
            b"\xfe\xed\xfa\xcf",  # MH_MAGIC_64 BE
            b"\xce\xfa\xed\xfe",  # MH_MAGIC LE
            b"\xca\xfe\xba\xbe",  # FAT BE
            b"\xbe\xba\xfe\xca",  # FAT LE
            b"\xca\xfe\xba\xbf",  # FAT64 BE
            b"\xbf\xba\xfe\xca",  # FAT64 LE
        ):
            self.assertTrue(ir.is_macho(magic + b"\x00" * 8), magic.hex())
        self.assertFalse(ir.is_macho(b"\x7fELF" + b"\x00" * 8))
        self.assertFalse(ir.is_macho(b"MZ" + b"\x00" * 8))
        self.assertTrue(ir.is_pe(_fake_pe_bytes()))
        mz_only = bytearray(b"\x00" * 0x100)
        mz_only[0:2] = b"MZ"
        self.assertFalse(
            ir.is_pe(bytes(mz_only)), "MZ без PE-сигнатуры — не PE"
        )

    def test_native_walk_sees_extensionless_fat64(self):
        """Полный обход видит extensionless FAT64-бинарь обеих ориентаций.

        Диагностическая страховка инвентаря сборки обязана покрывать все
        варианты Mach-O/FAT: файл вроде ``_internal/vendor/runtime`` с
        FAT64-magic не может выпадать из full walk (F1 final review).
        """
        bundle = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(bundle))
        files = []
        expected = []
        for magic in (b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"):
            rel = f"_internal/vendor/runtime-{magic.hex()}"
            path = bundle.joinpath(*rel.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(magic + b"payload")
            files.append({"path": rel, "type": "file"})
            expected.append(rel)
        files.append({"path": "_internal/vendor/notes.txt", "type": "file"})
        (bundle / "_internal" / "vendor" / "notes.txt").write_bytes(b"text")
        walked = ir.native_walk_paths(bundle, files)
        for rel in expected:
            self.assertIn(rel, walked)
        self.assertNotIn("_internal/vendor/notes.txt", walked)

    def test_macho_probe_never_raises(self):
        staging = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(staging))
        fake = staging / "lib.cpython-314-darwin.dylib"
        fake.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)
        result = ir.macho_deps(fake)
        self.assertIsInstance(result, dict)
        self.assertIn("available", result)
        self.assertIsInstance(result["available"], bool)
        if result["available"]:
            # otool существует и запущен: rc == 0 → output, иначе →
            # записанный returncode + error (валидность входа — не признак
            # наличия инструмента).
            if result.get("returncode"):
                self.assertIn("error", result)
            else:
                self.assertIn("output", result)
        else:
            self.assertIn("error", result)

    def test_macho_probe_missing_tool_contract(self):
        """Контракт «инструмент отсутствует»: available False + причина.

        Граница независима от окружения: mock ``shutil.which`` → None —
        единственный случай, когда проба Mach-O недоступна.
        """
        staging = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(staging))
        fake = staging / "lib.cpython-314-darwin.dylib"
        fake.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)
        with mock.patch("shutil.which", return_value=None):
            result = ir.macho_deps(fake)
        self.assertFalse(result["available"])
        self.assertIn("error", result)
        self.assertNotIn("returncode", result)

    def test_macho_probe_present_tool_nonzero_is_available(self):
        """Контракт «инструмент существует, вход невалиден»: available True.

        ``available`` — факт наличия и запуска otool, не корректность
        входного файла: ненулевой код (например, на не-Mach-O .py)
        записывается как returncode + error, проба не падает. Реальный Mac
        CI (run 39271547601) опроверг ожидание available False, выведенное
        из Linux-окружения без otool.
        """
        staging = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(staging))
        fake = staging / "not_macho.py"
        fake.write_bytes(b"print('hi')\n")
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="object is not a Mach-O"
        )
        with mock.patch(
            "shutil.which", return_value="/usr/bin/otool"
        ), mock.patch.object(
            ir.subprocess, "run", return_value=completed
        ) as run:
            result = ir.macho_deps(fake)
        self.assertTrue(result["available"])
        self.assertEqual(result["returncode"], 1)
        self.assertIn("error", result)
        run.assert_called_once()

    def test_pe_probe_never_raises(self):
        staging = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(staging))
        fake = staging / "sqlite3.dll"
        fake.write_bytes(_fake_pe_bytes())
        result = ir.pe_deps(fake)
        self.assertIsInstance(result, dict)
        self.assertIn("available", result)
        if not result["available"]:
            self.assertIn("error", result)


class CollectTocTests(unittest.TestCase):
    def test_parse_and_match(self):
        staging = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(staging))
        _write_collect_toc(
            staging,
            [
                ("_internal/sqlite3.dll", "/cache/sqlite3.dll", "BINARY"),
                ("keklib-cli", "/cache/keklib-cli", "EXECUTABLE"),
            ],
        )
        mapping = ir.collect_toc_sources(staging)
        self.assertEqual(
            mapping["_internal/sqlite3.dll"]["src"], "/cache/sqlite3.dll"
        )
        self.assertEqual(
            mapping["_internal/sqlite3.dll"]["typecode"], "BINARY"
        )

    def test_toc_searched_in_spec_nested_workdir(self):
        """R1: закреплённый PyInstaller кладёт TOC в <work>/<specnm>.

        ``build_main`` добавляет имя spec к workpath
        (``workpath = join(workpath, specnm)``); поиск только в корне
        workpath возвращал пустой словарь и терял provenance целиком.
        """
        staging = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(staging))
        _write_collect_toc(
            staging / "keklib-cli",
            [("_internal/x.so", "/cache/x.so", "BINARY")],
        )
        mapping = ir.collect_toc_sources(staging)
        self.assertEqual(mapping["_internal/x.so"]["src"], "/cache/x.so")

    def test_contents_prefix_and_exact_root_matching(self):
        """R1: dest EXECUTABLE — точное имя в корне, прочие — без _internal/.

        Правила COLLECT: исполняемый файл попадает в корень комплекта,
        остальные назначения относительно contents-каталога ``_internal/``
        (самого префикса в dest нет) — сопоставление обязано учитывать
        обе формы.
        """
        staging = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(staging))
        _write_collect_toc(
            staging,
            [
                ("sqlite3.dll", "/cache/sqlite3.dll", "BINARY"),
                ("lib-dynload/_sqlite3.so", "/cache/_sqlite3.so", "EXTENSION"),
                ("keklib-cli", "/cache/keklib-cli", "EXECUTABLE"),
            ],
        )
        mapping = ir.collect_toc_sources(staging)
        dll = ir._provenance_for("_internal/sqlite3.dll", mapping)
        self.assertIsNotNone(dll)
        assert dll is not None
        self.assertEqual(dll["dest"], "sqlite3.dll")
        ext = ir._provenance_for(
            "_internal/lib-dynload/_sqlite3.so", mapping
        )
        self.assertIsNotNone(ext)
        assert ext is not None
        self.assertEqual(ext["src"], "/cache/_sqlite3.so")
        exe = ir._provenance_for("keklib-cli", mapping)
        self.assertIsNotNone(exe)
        assert exe is not None
        self.assertEqual(exe["typecode"], "EXECUTABLE")

    def test_no_ambiguous_basename_fallback(self):
        """R1: одинаковый basename в другом каталоге — не источник.

        fallback по basename подставил бы чужой src при коллизии имён;
        неподтверждённый provenance обязан быть null, а не угаданным.
        """
        staging = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(staging))
        _write_collect_toc(
            staging,
            [("collide/_sha2.pyd", "/other/_sha2.pyd", "BINARY")],
        )
        mapping = ir.collect_toc_sources(staging)
        self.assertIsNone(ir._provenance_for("_internal/_sha2.pyd", mapping))

    def test_malformed_toc_ignored(self):
        staging = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(staging))
        (staging / "COLLECT-00.toc").write_text("not python {", encoding="utf-8")
        self.assertEqual(ir.collect_toc_sources(staging), {})
        self.assertEqual(ir.collect_toc_sources(staging / "missing"), {})


class RuntimeLicensesTests(unittest.TestCase):
    def test_known_locations_only_exact_names(self):
        root = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(root))
        (root / "LICENSE.txt").write_text("psf license text", encoding="utf-8")
        (root / "unrelated.txt").write_text("noise", encoding="utf-8")
        nested = root / "include"
        nested.mkdir()
        (nested / "LICENSE").write_text("nested must not be collected", encoding="utf-8")
        licenses = ir.collect_runtime_licenses(roots=[root])
        self.assertEqual(len(licenses), 1)
        entry = licenses[0]
        self.assertEqual(entry["text"], "psf license text")
        self.assertEqual(
            entry["sha256"],
            hashlib.sha256(b"psf license text").hexdigest(),
        )
        self.assertEqual(entry["size"], len(b"psf license text"))

    def test_missing_roots_give_empty_list(self):
        self.assertEqual(
            ir.collect_runtime_licenses(roots=[Path("/nonexistent-runtime-root")]),
            [],
        )


class CliTests(unittest.TestCase):
    def test_missing_arguments_exit_two(self):
        with self.assertRaises(SystemExit) as ctx:
            ir.main(["--target", "windows-x86_64"])
        self.assertEqual(ctx.exception.code, 2)

    def test_error_returns_one(self):
        out = portable_mkdtemp()
        self.addCleanup(lambda: os.rmdir(out))
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_SHA", None)
            with redirect_stderr(stderr):
                code = ir.main(
                    [
                        "--target",
                        "windows-x86_64",
                        "--output",
                        str(out),
                    ]
                )
        self.assertEqual(code, 1)
        self.assertIn("source", stderr.getvalue().lower())

    def test_success_prints_resolved_report_path(self):
        out = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(out))
        stdout = io.StringIO()
        with patched_stages(), redirect_stdout(stdout):
            code = ir.main(
                [
                    "--target",
                    "windows-x86_64",
                    "--output",
                    str(out),
                    "--source-sha",
                    VALID_SHA,
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn(
            f"inventory-report-windows-x86_64.json", stdout.getvalue()
        )


class PlatformInventoryWorkflowTests(unittest.TestCase):
    """Структура .github/workflows/platform-inventory.yml (текстуально)."""

    TEXT = WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.exists() else ""

    def test_workflow_exists_and_dispatch_only(self):
        self.assertTrue(WORKFLOW.is_file(), "platform-inventory.yml отсутствует")
        self.assertIn("workflow_dispatch:", self.TEXT)
        for forbidden in ("push:", "pull_request:", "schedule:"):
            self.assertNotIn(f"\n  {forbidden}", self.TEXT)

    def test_permissions_read_only(self):
        self.assertRegex(self.TEXT, r"permissions:\s*\n\s*contents:\s*read")

    def test_matrix_exactly_three_pending_targets(self):
        pairs = re.findall(
            r"-\s*name:\s*(\S+)\s*\n\s*runner:\s*(\S+)", self.TEXT
        )
        self.assertEqual(dict(pairs), PENDING_RUNNERS)
        self.assertEqual(set(ir.PENDING_TARGETS), set(PENDING_RUNNERS))

    def test_only_matrix_runners_used(self):
        for match in re.finditer(r"runs-on:\s*(.+)", self.TEXT):
            self.assertIn(
                "${{ matrix.runner }}",
                match.group(1),
                "метки runner берутся только из matrix pending-target",
            )
        for runner in PENDING_RUNNERS.values():
            self.assertNotIn(f"runs-on: {runner}", self.TEXT)

    def test_pinned_python_and_order(self):
        self.assertIn('python-version: "3.14.8"', self.TEXT)
        lock = self.TEXT.index("requirements-build.lock")
        install = self.TEXT.index("python -m pip install .")
        tool = self.TEXT.index("tools/inventory_runtime.py")
        self.assertLess(lock, install, "lock ставится до установки проекта")
        self.assertLess(install, tool, "проект ставится до инвентаризации")

    def test_report_json_only_upload(self):
        self.assertEqual(
            self.TEXT.count("uses: actions/upload-artifact"),
            1,
            "загружается только один артефакт — отчёт",
        )
        self.assertIn(
            "path: inventory-dist/inventory-report-${{ matrix.name }}.json",
            self.TEXT,
        )
        self.assertNotIn("path: dist", self.TEXT)
        for forbidden in ("tools/build_portable.py", "release_gate", "smoke_portable"):
            self.assertNotIn(forbidden, self.TEXT)

    def test_no_continue_on_error_and_timeout(self):
        # Комментарии-заголовки не являются конфигурацией: проверяются
        # только значимые строки workflow.
        config_lines = [
            line
            for line in self.TEXT.splitlines()
            if not line.lstrip().startswith("#")
        ]
        config = "\n".join(config_lines)
        self.assertNotIn("continue-on-error", config)
        match = re.search(r"timeout-minutes:\s*(\d+)", config)
        self.assertIsNotNone(match, "job обязан иметь разумный timeout")
        value = int(match.group(1))
        self.assertGreaterEqual(value, 10)
        self.assertLessEqual(value, 120)

    def test_actions_pinned_by_commit_sha(self):
        for line in self.TEXT.splitlines():
            stripped = line.strip()
            if "uses:" not in stripped or stripped.startswith("#"):
                continue
            self.assertRegex(
                stripped,
                r"uses: [^@\s]+@[0-9a-f]{40}( # .*)?$",
                msg=f"не закреплён action: {stripped}",
            )

    def test_native_python_invocation_uniform(self):
        self.assertNotIn("python3 ", self.TEXT)
        self.assertNotIn("py -3", self.TEXT)


class PeVersionInfoTests(unittest.TestCase):
    """R2: фактическая форма pefile API — FileInfo=[[StringFileInfo]], bytes.

    Реалистичный контракт (проверен review на pinned API): строки
    VERSION-ресурсов лежат во вложенных списках структур, ключи/значения
    ``StringTable.entries`` — bytes. Подмена модуля — контрактный мок,
    не нативная проверка Windows.
    """

    def _fake_pefile_module(self):
        import types

        class _ImportEntry:
            def __init__(self, dll: bytes) -> None:
                self.dll = dll

        class _StringTable:
            def __init__(self, entries: dict) -> None:
                self.entries = entries

        class _StringFileInfo:
            def __init__(self, tables: list) -> None:
                self.StringTable = tables

        class _PE:
            DIRECTORY_ENTRY_IMPORT = [_ImportEntry(b"SYSTEM32\\SQLITE3.dll")]
            FileInfo = [
                [
                    _StringFileInfo(
                        [
                            _StringTable(
                                {
                                    b"FileVersion": b"3.45.6",
                                    b"ProductName": b"SQLite",
                                    b"LegalCopyright": b"public domain",
                                }
                            )
                        ]
                    )
                ]
            ]

            def __init__(self, name: str, fast_load: bool = True) -> None:
                pass

            def parse_data_directories(self, directories=None) -> None:
                pass

            def close(self) -> None:
                pass

        module = types.ModuleType("pefile")
        module.PE = _PE
        module.DIRECTORY_ENTRY = {
            "IMAGE_DIRECTORY_ENTRY_IMPORT": 1,
            "IMAGE_DIRECTORY_ENTRY_RESOURCE": 2,
        }
        return module

    def test_nested_stringfileinfo_bytes_extracted(self):
        staging = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(staging))
        target = staging / "sqlite3.dll"
        target.write_bytes(_fake_pe_bytes())
        with mock.patch.dict(sys.modules, {"pefile": self._fake_pefile_module()}):
            result = ir.pe_deps(target)
        self.assertIs(result["available"], True)
        self.assertEqual(result["imports"], ["SYSTEM32\\SQLITE3.dll"])
        self.assertIsNotNone(result["version_info"])
        version = result["version_info"]
        assert version is not None
        self.assertEqual(version["file_version"], "3.45.6")
        self.assertEqual(version["product_name"], "SQLite")
        self.assertEqual(version["strings"]["LegalCopyright"], "public domain")

    def test_version_info_null_when_no_resources(self):
        staging = portable_mkdtemp()
        self.addCleanup(lambda: _rmtree(staging))
        target = staging / "plain.dll"
        target.write_bytes(_fake_pe_bytes())
        module = self._fake_pefile_module()
        module.PE.FileInfo = []  # ресурсов нет — честный null, не выдумка
        with mock.patch.dict(sys.modules, {"pefile": module}):
            result = ir.pe_deps(target)
        self.assertIs(result["available"], True)
        self.assertIsNone(result["version_info"])


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
