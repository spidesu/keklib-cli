"""Тесты portable-упаковки (задача 11 плана, fix round 1).

Проверяются контракт `tools/build_portable.py`, целостность состава архива,
контрольные суммы, отказ от перезаписи, метаданные сборки, onedir/console
spec и ELF-сканер нижней границы glibc.

Fix round 1 покрывает замечания review task-11-review.md: семантика ELF
(тип секции/динамический сегмент, а не имя секции; строгие цепочки и
границы; неподтверждаемые ELF отвергаются fail-closed; провайдер не может
скрыть требование GLIBC) с независимой сверкой `readelf`; полный обход
дерева комплекта с containment-проверкой символических ссылок/reparse и
запретом внешних/битых/покидающих комплект ссылок (ZIP не содержит
посторонних байтов); обязательный preflight toolchain против пинов и
fail-closed release-гейт по SQLite-pin; машинный лицензионный гейт по
инвентарю фактического состава; протокол публикации пары архив+сумма с
восстановлением только своего опознанного файла.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.portable import portable_mkdtemp  # noqa: E402

from tools import build_portable as bp  # noqa: E402

FOUR_TARGETS = {"linux-x86_64", "windows-x86_64", "macos-x86_64", "macos-arm64"}

BUILD_INFO_KEYS = {
    "version",
    "target",
    "python",
    "sqlite",
    "pyinstaller",
    "schema_version",
    "rules_version",
    "minimum_os",
    "minimum_glibc",
}

READELF = shutil.which("readelf")


# ---------------------------------------------------------------------------
# Синтетические ELF64-файлы


def _elf_header(shoff: int, shnum: int, shstrndx: int, phoff: int = 0, phnum: int = 0) -> bytes:
    ident = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8
    return struct.pack(
        "<16sHHIQQQIHHHHHH",
        ident,
        2,  # ET_EXEC
        62,  # EM_X86_64
        1,
        0,  # e_entry
        phoff,
        shoff,
        0,
        64,
        56 if phnum else 0,
        phnum,
        64,
        shnum,
        shstrndx,
    )


def _write_min_elf(
    path: Path,
    glibc_version: str | None,
    *,
    section_name: str = ".gnu.version_r",
    provider: str = "libc.so.6",
    vn_aux: int = 16,
    vn_cnt: int = 1,
    size_override: int | None = None,
) -> None:
    """ELF64 LSB с секцией SHT_GNU_verneed (имя управляется отдельно).

    При ``glibc_version=None`` секция verneed не создаётся: файл ELF без
    требований версий glibc.
    """
    shstrtab = b"\x00.shstrtab\x00.dynstr\x00" + section_name.encode() + b"\x00"
    off_shstrtab_name = 1
    off_dynstr_name = 11
    off_verneed_name = 11 + len(".dynstr\x00")
    provider_bytes = provider.encode()
    if glibc_version is not None:
        dynstr = b"\x00" + provider_bytes + b"\x00" + b"GLIBC_%s\x00" % glibc_version.encode()
        provider_off = 1
        version_off = 1 + len(provider_bytes) + 1
    else:
        dynstr = b"\x00" + provider_bytes + b"\x00"
        provider_off = 1
        version_off = 0

    data = bytearray()
    data += b"\x00" * 64
    shstrtab_off = len(data)
    data += shstrtab
    dynstr_off = len(data)
    data += dynstr

    sections = [
        (off_shstrtab_name, 3, shstrtab_off, len(shstrtab), 0, 1),
        (off_dynstr_name, 3, dynstr_off, len(dynstr), 0, 1),
    ]
    if glibc_version is not None:
        verneed_off = len(data)
        data += struct.pack("<HHIII", 1, vn_cnt, provider_off, vn_aux, 0)
        data += struct.pack("<IHHII", 0, 0, 2, version_off, 0)
        size = size_override if size_override is not None else len(data) - verneed_off
        sections.append((off_verneed_name, 0x6FFFFFFe, verneed_off, size, 2, 4))

    while len(data) % 8:
        data += b"\x00"
    shoff = len(data)
    data += b"\x00" * 64
    for name, stype, off, size, link, align in sections:
        data += struct.pack(
            "<IIQQQQIIQQ", name, stype, 0, 0, off, size, link, 0, align, 0
        )
    data[0:64] = _elf_header(shoff, len(sections) + 1, 1)
    path.write_bytes(bytes(data))


def _write_dynamic_elf(
    path: Path,
    glibc_version: str,
    *,
    provider: str = "libc.so.6",
    dynamic_size_override: int | None = None,
    omit_null: bool = False,
) -> None:
    """ELF64 LSB без таблицы секций: требования живут в PT_DYNAMIC.

    ``dynamic_size_override`` подменяет p_filesz сегмента (0 — пустой
    «невидимый» сегмент, 8 — размер не кратен записи), ``omit_null``
    убирает завершающий DT_NULL.
    """
    provider_bytes = provider.encode()
    dynstr = b"\x00" + provider_bytes + b"\x00" + b"GLIBC_%s\x00" % glibc_version.encode()
    provider_off = 1
    version_off = 1 + len(provider_bytes) + 1

    data = bytearray()
    data += b"\x00" * 64  # заголовок
    phoff = len(data)
    data += b"\x00" * (56 * 2)  # PT_LOAD + PT_DYNAMIC
    dynstr_vaddr = len(data)
    data += dynstr
    verneed_vaddr = len(data)
    data += struct.pack("<HHIII", 1, 1, provider_off, 16, 0)
    data += struct.pack("<IHHII", 0, 0, 2, version_off, 0)
    dynamic = [
        (5, dynstr_vaddr),  # DT_STRTAB
        (10, len(dynstr)),  # DT_STRSZ
        (0x6FFFFFFe, verneed_vaddr),  # DT_VERNEED
        (0x6fffffff, 1),  # DT_VERNEEDNUM
    ]
    if not omit_null:
        dynamic.append((0, 0))  # DT_NULL
    dynamic_off = len(data)
    for tag, value in dynamic:
        data += struct.pack("<QQ", tag, value)
    dynamic_size = len(data) - dynamic_off
    if dynamic_size_override is not None:
        dynamic_size = dynamic_size_override

    data[0:64] = _elf_header(0, 0, 0, phoff=phoff, phnum=2)
    # PT_LOAD: весь файл отображается тождественно vaddr == offset.
    data[phoff : phoff + 56] = struct.pack(
        "<IIQQQQQQ", 1, 0, 0, 0, 0, len(data) + 64, len(data) + 64, 0x1000
    )
    data[phoff + 56 : phoff + 112] = struct.pack(
        "<IIQQQQQQ", 2, 0, dynamic_off, dynamic_off, dynamic_off, dynamic_size, dynamic_size, 8
    )
    path.write_bytes(bytes(data))


def _readelf_numeric_max(path: Path) -> tuple[int, ...] | None:
    """Максимум требований по readelf --version-info (None — нет данных).

    Семантика выровнена со сканером: числовые GLIBC_x.y плюс маркер
    GLIBC_ABI_DT_RELR, учитываемый как (2, 36).
    """
    result = subprocess.run(
        ["readelf", "--version-info", str(path)], capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    tokens = re.findall(r"GLIBC_(\d+(?:\.\d+)*)", result.stdout)
    versions = [tuple(int(piece) for piece in token.split(".")) for token in tokens]
    if "GLIBC_ABI_DT_RELR" in result.stdout:
        versions.append((2, 36))
    if not versions:
        return None
    return max(versions)


def _make_bundle_dir(
    root: Path,
    *,
    target: str = "linux-x86_64",
    exe_name: str | None = None,
    top_extra: list[str] | None = None,
    internal_extra: list[str] | None = None,
    version: str = "0.0.0-test",
) -> Path:
    """Синтетический staging-каталог комплекта со стандартным набором файлов."""
    spec = bp.load_targets()[target]
    exe_name = exe_name or spec["executable"]
    bundle = root / "bundle"
    (bundle / "_internal").mkdir(parents=True)
    exe = bundle / exe_name
    exe.write_bytes(b"#!/bin/sh\nexit 0\n")
    os.chmod(exe, 0o755)
    (bundle / "_internal" / "base_library.zip").write_bytes(b"PK\x05\x06")
    (bundle / "_internal" / "runtime.dynlib").write_bytes(b"stub")
    for name in ("README.md", "INSTALL.md", "THIRD_PARTY_NOTICES.md"):
        (bundle / name).write_text(f"# {name}\n", encoding="utf-8")
    info = {key: "x" for key in BUILD_INFO_KEYS}
    info.update(
        {
            "version": version,
            "target": target,
            "python": "3.14.8",
            "sqlite": "3.54.0",
            "pyinstaller": "6.22.3",
            "schema_version": 1,
            "rules_version": 1,
            "minimum_os": "Ubuntu 22.04 (glibc >= 2.35)",
            "minimum_glibc": spec["minimum_glibc"],
        }
    )
    (bundle / "build-info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for rel in top_extra or []:
        p = bundle / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"foreign\n")
    for rel in internal_extra or []:
        p = bundle / "_internal" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"foreign\n")
    return bundle


def _make_escaping_relative_link(internal: Path, secret: Path) -> Path:
    """Внешняя относительная symlink с нативной для ОС формой цели.

    Цель обязана вычисляться ``os.path.relpath`` текущей ОС: Windows
    не разрешает reparse-цель составной относительной ссылки, записанную
    с «/» (WinError 123 — единственный остаточный провал native source
    CI run 39246201467), поэтому raw-строки вида ``../../../x`` в
    фикстурах запрещены. Возвращённая ссылка до передачи в
    ``archive_bundle`` должна разрешаться strict=True в точный
    существующий внешний файл (контракт проверяет отдельный тест).
    """
    link = internal / "runtime.dat"
    os.symlink(os.path.relpath(secret, start=internal), link)
    return link


def _archive_names(path: Path, target: str) -> list[str]:
    fmt = bp.load_targets()[target]["archive_format"]
    if fmt == "zip":
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    with tarfile.open(path, "r:gz") as tf:
        return tf.getnames()


class PackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="keklib-packaging-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    # 1. Ровно четыре target-имени из общих ограничений.

    def test_four_target_names(self) -> None:
        targets = bp.load_targets()
        self.assertEqual(set(targets), FOUR_TARGETS)
        self.assertEqual(targets["linux-x86_64"]["archive_format"], "tar.gz")
        self.assertEqual(targets["macos-x86_64"]["archive_format"], "tar.gz")
        self.assertEqual(targets["macos-arm64"]["archive_format"], "tar.gz")
        self.assertEqual(targets["windows-x86_64"]["archive_format"], "zip")
        self.assertEqual(targets["linux-x86_64"]["executable"], "keklib-cli")
        self.assertEqual(targets["windows-x86_64"]["executable"], "keklib-cli.exe")
        for name, spec in targets.items():
            self.assertEqual(spec["runner_cpu"], "x86_64" if "x86_64" in name else "arm64")
            self.assertTrue(spec["minimum_os"], name)

    # 2. Кросс-сборка запрещена: target обязан совпадать с реальной ОС/CPU.

    def test_reject_cross_target(self) -> None:
        current = bp.current_target()
        self.assertIsNotNone(current, "нативная платформа должна распознаваться")
        targets = bp.load_targets()
        foreign = sorted(set(targets) - {current})[0]
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.build_portable(foreign, self.tmp / "out")
        self.assertIn("натив", str(ctx.exception).lower())

    # 3. Allowlist корня и запрет посторонних файлов внутри комплекта.

    def test_archive_allowlist(self) -> None:
        out = self.tmp / "out"
        bundle = _make_bundle_dir(self.tmp, top_extra=["books.zip", "config.json"])
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.archive_bundle(bundle, out, version="0.0.0-test", target="linux-x86_64")
        self.assertIn("books.zip", str(ctx.exception))
        self.assertIn("config.json", str(ctx.exception))

        out2 = self.tmp / "out2"
        bundle2 = _make_bundle_dir(
            self.tmp / "b2",
            internal_extra=[
                "collection.inpx",
                "index.sqlite3",
                "test_extra.py",
                "secret.pem",
                "fb2-060424-074391.zip",
            ],
        )
        with self.assertRaises(bp.PackagingError) as ctx2:
            bp.archive_bundle(bundle2, out2, version="0.0.0-test", target="linux-x86_64")
        message = str(ctx2.exception)
        for needle in (
            "collection.inpx",
            "index.sqlite3",
            "test_extra.py",
            "secret.pem",
            "fb2-060424-074391.zip",
        ):
            self.assertIn(needle, message)

        out3 = self.tmp / "out3"
        clean = _make_bundle_dir(self.tmp / "b3")
        archive = bp.archive_bundle(clean, out3, version="0.0.0-test", target="linux-x86_64")
        names = set(_archive_names(archive, "linux-x86_64"))
        expected = {"keklib-cli"}
        for dirpath, dirnames, filenames in os.walk(clean):
            rel_dir = Path(dirpath).relative_to(clean)
            dirnames.sort()
            for d in dirnames:
                # Имена записей архива — POSIX-пути на любой ОС (как пишет
                # упаковщик); native разделители Windows не меняют ожиданий.
                child = (rel_dir / d).as_posix() if str(rel_dir) != "." else d
                expected.add(f"keklib-cli/{child}")
            for f in sorted(filenames):
                rel = (rel_dir / f).as_posix() if str(rel_dir) != "." else f
                expected.add("keklib-cli/" + rel)
        self.assertEqual(names, expected)

        win = _make_bundle_dir(self.tmp / "b4", target="windows-x86_64")
        win_archive = bp.archive_bundle(
            win, self.tmp / "out4", version="0.0.0-test", target="windows-x86_64"
        )
        self.assertEqual(win_archive.suffix, ".zip")
        self.assertIn(
            "keklib-cli/keklib-cli.exe", _archive_names(win_archive, "windows-x86_64")
        )

    # 4. Бит исполнения сохраняется в tar.gz даже при неправильном staging.

    def test_posix_execute_bit(self) -> None:
        bundle = _make_bundle_dir(self.tmp)
        exe = bundle / "keklib-cli"
        os.chmod(exe, 0o644)
        archive = bp.archive_bundle(
            bundle, self.tmp / "out", version="0.0.0-test", target="linux-x86_64"
        )
        with tarfile.open(archive, "r:gz") as tf:
            modes = {m.name: m.mode for m in tf.getmembers()}
        self.assertEqual(modes["keklib-cli/keklib-cli"] & 0o777, 0o755)
        self.assertEqual(modes["keklib-cli/_internal"] & 0o777, 0o755)
        self.assertEqual(modes["keklib-cli/README.md"] & 0o777, 0o644)

    # 5. Рядом с архивом появляется корректный .sha256.

    def test_checksum_matches(self) -> None:
        bundle = _make_bundle_dir(self.tmp)
        out = self.tmp / "out"
        archive = bp.archive_bundle(bundle, out, version="0.0.0-test", target="linux-x86_64")
        checksum_path = archive.with_name(archive.name + ".sha256")
        self.assertTrue(checksum_path.is_file())
        line = checksum_path.read_text(encoding="utf-8").strip()
        digest, _, name = line.partition("  ")
        self.assertEqual(name, archive.name)
        self.assertEqual(digest, hashlib.sha256(archive.read_bytes()).hexdigest())

    # 6. Существующий архив и контрольная сумма не перезаписываются.

    def test_existing_archive_preserved(self) -> None:
        out = self.tmp / "out"
        out.mkdir()
        archive = out / "keklib-cli-0.0.0-test-linux-x86_64.tar.gz"
        checksum = archive.with_name(archive.name + ".sha256")
        archive.write_bytes(b"sentinel-archive\n")
        checksum.write_bytes(b"sentinel-checksum\n")
        bundle = _make_bundle_dir(self.tmp)
        with self.assertRaises(bp.PackagingError):
            bp.archive_bundle(bundle, out, version="0.0.0-test", target="linux-x86_64")
        self.assertEqual(archive.read_bytes(), b"sentinel-archive\n")
        self.assertEqual(checksum.read_bytes(), b"sentinel-checksum\n")
        leftovers = sorted(p.name for p in out.iterdir())
        self.assertEqual(leftovers, sorted([archive.name, checksum.name]))

    # 7. Документы лежат в корне комплекта и читаются из архива.

    def test_documents_at_root(self) -> None:
        bundle = _make_bundle_dir(self.tmp)
        archive = bp.archive_bundle(
            bundle, self.tmp / "out", version="0.0.0-test", target="linux-x86_64"
        )
        names = set(_archive_names(archive, "linux-x86_64"))
        for doc in ("README.md", "INSTALL.md", "THIRD_PARTY_NOTICES.md", "build-info.json"):
            self.assertIn(f"keklib-cli/{doc}", names)
        with tarfile.open(archive, "r:gz") as tf:
            info = json.loads(tf.extractfile("keklib-cli/build-info.json").read().decode("utf-8"))
        self.assertEqual(info["version"], "0.0.0-test")
        self.assertEqual(info["target"], "linux-x86_64")
        self.assertTrue(BUILD_INFO_KEYS.issubset(info))

    # 8. build-info.json: полные версии, закреплённые пины, SQLite/FTS5.

    def test_build_metadata(self) -> None:
        try:
            importlib.metadata.version("PyInstaller")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest(
                "pyinstaller не установлен: сборочные зависимости — "
                "packaging/requirements-build.lock"
            )
        info = bp.build_info("linux-x86_64")
        self.assertTrue(BUILD_INFO_KEYS.issubset(info))
        self.assertTrue(re.fullmatch(r"\d+\.\d+\.\d+", info["python"]))
        self.assertTrue(re.fullmatch(r"\d+\.\d+\.\d+", info["pyinstaller"]))
        self.assertNotIn("+", info["python"])
        self.assertTrue(info["sqlite"])
        self.assertNotEqual(info["sqlite"], "0")
        self.assertTrue(info["sqlite_fts5"])
        from keklib_cli import __version__
        from keklib_cli.index_store import RULES_VERSION, SCHEMA_VERSION

        self.assertEqual(info["version"], __version__)
        self.assertEqual(info["schema_version"], SCHEMA_VERSION)
        self.assertEqual(info["rules_version"], RULES_VERSION)
        # Зафиксированный baseline не должен незаметно уплывать.
        self.assertEqual(info["minimum_glibc"], "2.35")
        self.assertTrue(info["minimum_os"])
        for target in ("windows-x86_64", "macos-x86_64", "macos-arm64"):
            self.assertIsNone(bp.build_info(target)["minimum_glibc"], target)
        # Версии обязаны совпадать с пинами toolchain.json, а не только с форматом.
        toolchain = bp.load_toolchain()
        linux = toolchain["targets"]["linux-x86_64"]
        self.assertEqual(info["python"], linux["python"])
        self.assertEqual(info["pyinstaller"], linux["pyinstaller"])

    def test_toolchain_lock_consistency(self) -> None:
        """toolchain.json, targets.json и lock не расходятся; пины точные."""
        toolchain = bp.load_toolchain()
        pins = bp.lock_pins()
        self.assertIn("pyinstaller", pins)
        self.assertIn("setuptools", pins)
        for name, entry in toolchain["targets"].items():
            self.assertEqual(entry["pyinstaller"], pins["pyinstaller"], name)
            self.assertEqual(entry["setuptools"], pins["setuptools"], name)
            self.assertEqual(entry["python"], toolchain["python_required"], name)
            # SQLite-pin либо явно null (незавершён и блокирует релиз), либо точная версия.
            self.assertTrue(entry["sqlite"] is None or re.fullmatch(r"\d+\.\d+\.\d+", entry["sqlite"]))
        lock_text = (PROJECT_ROOT / "packaging" / "requirements-build.lock").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(">=", lock_text)
        self.assertNotIn("~=", lock_text)
        for name, version in pins.items():
            self.assertRegex(
                version, r"^\d", f"неточных пинов в lock быть не должно: {name}=={version}"
            )

    def test_native_targets_sqlite_pins_by_evidence(self) -> None:
        """Пины трёх native-target — факт инвентаризации, не приёмка.

        Диагностические inventory-отчёты pending-target (python.org runtime
        3.14.8 на windows-2022/macos-15-intel/macos-14) показали одинаковый
        SQLite 3.50.4 с FTS5 во всех трёх runtime. Пин фиксирует этот факт;
        ``native_build_verified`` остаётся ``false``/``verified_on`` null до
        реальной нативной сборки и полного изолированного smoke каждого
        target — пин не является заявлением о принятом выпуске.
        """
        toolchain = bp.load_toolchain()
        linux = toolchain["targets"]["linux-x86_64"]
        self.assertEqual(linux["sqlite"], "3.54.1")
        self.assertTrue(linux["native_build_verified"])
        for other in ("windows-x86_64", "macos-x86_64", "macos-arm64"):
            entry = toolchain["targets"][other]
            self.assertEqual(entry["sqlite"], "3.50.4", other)
            self.assertIs(entry["native_build_verified"], False, other)
            self.assertIsNone(entry["verified_on"], other)

    def test_native_release_gate_uses_new_pins(self) -> None:
        """Release-гейт сверяет фактический SQLite 3.50.4 с новым пином."""
        toolchain = bp.load_toolchain()
        for other in ("windows-x86_64", "macos-x86_64", "macos-arm64"):
            info = {"python": "3.14.8", "pyinstaller": "6.22.3", "sqlite": "3.50.4"}
            bp.verify_release_metadata(other, info, toolchain=toolchain,
                                       sqlite_version="3.50.4")

    # 9. onedir и console: не onefile и не GUI.

    def test_tooling_lock_subset_of_build_lock(self) -> None:
        """Test-tooling lock — точное подмножество requirements-build.lock.

        Второй lock-файл нужен source CI (тестам сборочного tooling нужен
        ``packaging``, но не весь build-lock с PyInstaller); чтобы пины не
        разошлись, каждая его запись обязана совпадать с requirements-build.lock
        до хеша: та же версия и тот же набор строк ``--hash``.
        """
        build_text = (
            PROJECT_ROOT / "packaging" / "requirements-build.lock"
        ).read_text(encoding="utf-8")
        tooling_text = (
            PROJECT_ROOT / "packaging" / "requirements-test-tooling.lock"
        ).read_text(encoding="utf-8")

        def entries(text: str) -> dict[str, tuple[str, frozenset[str]]]:
            logical = ""
            parsed: dict[str, tuple[str, frozenset[str]]] = {}
            for raw in text.splitlines():
                line = raw.rstrip()
                if line.endswith("\\"):
                    logical += line[:-1] + " "
                    continue
                logical += line
                entry = logical.strip()
                logical = ""
                if not entry or entry.startswith("#"):
                    continue
                hashes = frozenset(
                    part.strip() for part in entry.split("--hash")[1:]
                )
                requirement = entry.split("--hash")[0].strip().rstrip("\\").strip()
                name, _, version = requirement.partition("==")
                parsed[name] = (version, hashes)
            return parsed

        build = entries(build_text)
        tooling = entries(tooling_text)
        self.assertTrue(tooling, "test-tooling lock пуст")
        self.assertIn("packaging", tooling, "тестам tooling нужен packaging.requirements")
        for name, (version, hashes) in tooling.items():
            self.assertIn(name, build, f"{name} нет в requirements-build.lock")
            self.assertEqual(
                (version, hashes),
                build[name],
                f"{name}: test-tooling lock разошёлся с requirements-build.lock",
            )

    def test_no_onefile(self) -> None:
        spec = (PROJECT_ROOT / "packaging" / "keklib-cli.spec").read_text(encoding="utf-8")
        code = spec.split('"""', 2)[-1]
        self.assertIn("console=True", code)
        self.assertNotIn("console=False", code)
        self.assertIn("exclude_binaries=True", code)
        self.assertIn("COLLECT(", code)
        self.assertNotIn("onefile", code.lower())
        tool = (PROJECT_ROOT / "tools" / "build_portable.py").read_text(encoding="utf-8")
        self.assertNotIn("--onefile", tool)
        self.assertIn("--noconfirm", tool)

    # 10. ELF-сканер: базовый разбор .gnu.version_r.

    def test_elf_glibc_scan(self) -> None:
        f1 = self.tmp / "libhigh.so"
        _write_min_elf(f1, "2.38")
        self.assertEqual(bp.scan_elf_max_glibc(f1), (2, 38))
        f2 = self.tmp / "liblow.so"
        _write_min_elf(f2, "2.2.5")
        self.assertEqual(bp.scan_elf_max_glibc(f2), (2, 2, 5))
        f3 = self.tmp / "libnone.so"
        _write_min_elf(f3, None)
        self.assertEqual(bp.scan_elf_max_glibc(f3), ())
        f4 = self.tmp / "plain.txt"
        f4.write_bytes(b"not an elf")
        self.assertIsNone(bp.scan_elf_max_glibc(f4))
        # Регрессия ложного пропуска: нечисловые имена версий glibc.
        _write_min_elf(self.tmp / "librelr.so", "ABI_DT_RELR")
        self.assertEqual(bp.scan_elf_max_glibc(self.tmp / "librelr.so"), (2, 36))
        _write_min_elf(self.tmp / "libweird.so", "WEIRD_MARKER")
        with self.assertRaises(bp.PackagingError):
            bp.scan_elf_max_glibc(self.tmp / "libweird.so")
        # Приватный ABI glibc в составе комплекта несовместим с baseline.
        _write_min_elf(self.tmp / "libprivate.so", "PRIVATE")
        with self.assertRaises(bp.PackagingError) as ctx_private:
            bp.scan_elf_max_glibc(self.tmp / "libprivate.so")
        self.assertIn("GLIBC_PRIVATE", str(ctx_private.exception))

    # 11. Требования определяются семантикой ELF, а не именем секции.

    def test_elf_scan_ignores_section_names(self) -> None:
        renamed = self.tmp / "librenamed.so"
        _write_min_elf(renamed, "2.44", section_name=".xyz.version_r")
        self.assertEqual(bp.scan_elf_max_glibc(renamed), (2, 44))

    def test_elf_scan_dynamic_segment_without_sections(self) -> None:
        dynamic = self.tmp / "libdynamic.so"
        _write_dynamic_elf(dynamic, "2.38")
        self.assertEqual(bp.scan_elf_max_glibc(dynamic), (2, 38))

    def test_elf_scan_counts_any_provider(self) -> None:
        hidden = self.tmp / "libhidden.so"
        _write_min_elf(hidden, "2.40", provider="libweirdprovider.so.7")
        self.assertEqual(bp.scan_elf_max_glibc(hidden), (2, 40))
        dynamic = self.tmp / "libhiddendyn.so"
        _write_dynamic_elf(dynamic, "2.41", provider="libweirdprovider.so.7")
        self.assertEqual(bp.scan_elf_max_glibc(dynamic), (2, 41))

    # 12. Повреждённые цепочки/границы — отказ, а не пустой результат.

    def test_elf_scan_rejects_malformed(self) -> None:
        bad_aux = self.tmp / "libaux.so"
        _write_min_elf(bad_aux, "2.38", vn_aux=0xFFFF)
        with self.assertRaises(bp.PackagingError):
            bp.scan_elf_max_glibc(bad_aux)
        bad_cnt = self.tmp / "libcnt.so"
        _write_min_elf(bad_cnt, "2.38", vn_cnt=3)
        with self.assertRaises(bp.PackagingError):
            bp.scan_elf_max_glibc(bad_cnt)
        beyond_eof = self.tmp / "libbeyond.so"
        _write_min_elf(beyond_eof, "2.38", size_override=10_000)
        with self.assertRaises(bp.PackagingError):
            bp.scan_elf_max_glibc(beyond_eof)
        tiny = self.tmp / "libtiny.so"
        _write_min_elf(tiny, "2.38", size_override=8)
        with self.assertRaises(bp.PackagingError):
            bp.scan_elf_max_glibc(tiny)

    # 13. Неподтверждаемые ELF отвергаются fail-closed.

    def test_elf_scan_rejects_unprovable(self) -> None:
        short = self.tmp / "short.elf"
        short.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 20)
        with self.assertRaises(bp.PackagingError):
            bp.scan_elf_max_glibc(short)
        # Отдельно: сам ELF-magic (4 байта) — это усечённый ELF, не «не ELF».
        magic_only = self.tmp / "magic.elf"
        magic_only.write_bytes(b"\x7fELF")
        with self.assertRaises(bp.PackagingError):
            bp.scan_elf_max_glibc(magic_only)
        elf32 = self.tmp / "elf32"
        elf32.write_bytes(b"\x7fELF\x01\x01\x01\x00" + b"\x00" * 40)
        with self.assertRaises(bp.PackagingError):
            bp.scan_elf_max_glibc(elf32)
        msb = self.tmp / "elfmsb"
        msb.write_bytes(b"\x7fELF\x02\x02\x01\x00" + b"\x00" * 40)
        with self.assertRaises(bp.PackagingError):
            bp.scan_elf_max_glibc(msb)
        headerless = self.tmp / "headerless.so"
        headerless.write_bytes(
            b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 56  # нет phdr-таблиц и таблицы секций
        )
        with self.assertRaises(bp.PackagingError):
            bp.scan_elf_max_glibc(headerless)

    # 13b. Невалидный динамический сегмент не даёт «нет требований».

    def test_elf_scan_rejects_invalid_dynamic_segment(self) -> None:
        zero = self.tmp / "zero.so"
        _write_dynamic_elf(zero, "2.44", dynamic_size_override=0)
        with self.assertRaises(bp.PackagingError):
            bp.scan_elf_max_glibc(zero)
        no_null = self.tmp / "nonull.so"
        _write_dynamic_elf(no_null, "2.44", omit_null=True)
        with self.assertRaises(bp.PackagingError):
            bp.scan_elf_max_glibc(no_null)
        odd = self.tmp / "odd.so"
        _write_dynamic_elf(odd, "2.44", dynamic_size_override=8)
        with self.assertRaises(bp.PackagingError):
            bp.scan_elf_max_glibc(odd)

    # 14. Независимая сверка с readelf (там, где readelf доступен).

    def test_readelf_crosscheck(self) -> None:
        if READELF is None:
            self.skipTest("readelf недоступен: независимая сверка невозможна на этой ОС")
        normal = self.tmp / "normal.so"
        _write_min_elf(normal, "2.44")
        renamed = self.tmp / "renamed.so"
        _write_min_elf(renamed, "2.44", section_name=".xyz.version_r")
        dynamic = self.tmp / "dynamic.so"
        _write_dynamic_elf(dynamic, "2.38")
        for path, expected in ((normal, (2, 44)), (renamed, (2, 44)), (dynamic, (2, 38))):
            reference = _readelf_numeric_max(path)
            if reference is not None:
                self.assertEqual(reference, expected, f"readelf расходится для {path.name}")
            self.assertEqual(bp.scan_elf_max_glibc(path), expected)
        libc = Path("/usr/lib/libpython3.14.so")
        if libc.is_file():
            reference = _readelf_numeric_max(libc)
            self.assertIsNotNone(reference, "readelf должен показывать версии libpython")
            self.assertEqual(bp.scan_elf_max_glibc(libc), reference)

    # 15. Floor-гейт: файл с требованием выше границы обнаруживается.

    def test_glibc_floor_verification(self) -> None:
        root = self.tmp / "bundle"
        root.mkdir()
        _write_min_elf(root / "libpython3.14.so", "2.38")
        _write_min_elf(root / "_ssl.so", "2.17")
        (root / "notes.txt").write_text("not elf")
        violations = bp.verify_glibc_floor(root, "2.35")
        self.assertEqual(len(violations), 1)
        self.assertIn("libpython3.14.so", violations[0])
        self.assertIn("GLIBC_2.38", violations[0])
        self.assertEqual(bp.verify_glibc_floor(root, "2.38"), [])
        self.assertEqual(bp.verify_glibc_floor(root, "2.39"), [])

    def test_glibc_floor_rejects_relr_marker(self) -> None:
        """Комплект с GLIBC_ABI_DT_RELR требует glibc >= 2.36 > floor 2.35."""
        root = self.tmp / "b"
        root.mkdir()
        _write_min_elf(root / "libx.so", "ABI_DT_RELR")
        violations = bp.verify_glibc_floor(root, "2.35")
        self.assertEqual(len(violations), 1)
        self.assertIn("GLIBC_2.36", violations[0])

    # 16. Символические ссылки/reparse: полный обход и containment.

    def test_bundle_internal_symlink_tar(self) -> None:
        bundle = _make_bundle_dir(self.tmp)
        internal = bundle / "_internal"
        (internal / "libreal.so.1").write_bytes(b"internal-runtime")
        os.symlink("libreal.so.1", internal / "libreal.so")
        archive = bp.archive_bundle(
            bundle, self.tmp / "out", version="0.0.0-test", target="linux-x86_64"
        )
        with tarfile.open(archive, "r:gz") as tf:
            member = tf.getmember("keklib-cli/_internal/libreal.so")
            self.assertTrue(member.issym())
            self.assertEqual(member.linkname, "libreal.so.1")

    def test_bundle_symlink_tar_through_alias_ancestor(self) -> None:
        """Цель tar-ссылки считается в одной канонической системе координат.

        Путь комплекта через symlink-предка (macOS: ``/var`` →
        ``/private/var``; Windows: short/long форма) прежде смешивал
        resolved-цель с lexical-родителем: ``relpath`` выдавал хвост из
        ``..`` и абсолютных сегментов вместо имени цели внутри комплекта
        (первый CI, оба macOS). Здесь тот же предок-alias воспроизведён
        настоящим symlink на POSIX, и roundtrip-распаковка обязана дать
        рабочую относительную ссылку.
        """
        real_root = self.tmp / "real-root"
        real_root.mkdir()
        alias = self.tmp / "alias-root"
        os.symlink(real_root, alias, target_is_directory=True)
        bundle = _make_bundle_dir(real_root / "place")
        internal = bundle / "_internal"
        (internal / "libreal.so.1").write_bytes(b"internal-runtime")
        os.symlink("libreal.so.1", internal / "libreal.so")
        # Обращение к комплекту и выходу — только через alias-предка.
        aliased_bundle = alias / "place" / "bundle"
        archive = bp.archive_bundle(
            aliased_bundle, alias / "out", version="0.0.0-test", target="linux-x86_64"
        )
        with tarfile.open(archive, "r:gz") as tf:
            member = tf.getmember("keklib-cli/_internal/libreal.so")
            self.assertTrue(member.issym())
            linkname = member.linkname
        self.assertEqual(linkname, "libreal.so.1")
        self.assertNotIn("..", linkname)
        self.assertNotIn("\\", linkname, "цель ссылки хранится POSIX-формой")
        # Roundtrip: сохранённое имя цели, помещённое в каталог ссылки,
        # обязано разрешаться в файл внутри комплекта.
        restored_dir = self.tmp / "restored-internal"
        restored_dir.mkdir()
        (restored_dir / "libreal.so.1").write_bytes(b"internal-runtime")
        restored = restored_dir / "libreal.so"
        os.symlink(linkname, restored)
        self.assertEqual(
            restored.resolve(strict=True), (restored_dir / "libreal.so.1").resolve()
        )

    def test_bundle_internal_symlink_zip_materializes(self) -> None:
        bundle = _make_bundle_dir(self.tmp, target="windows-x86_64")
        internal = bundle / "_internal"
        (internal / "libreal.dll.1").write_bytes(b"INTERNAL-RUNTIME-BYTES")
        os.symlink("libreal.dll.1", internal / "libreal.dll")
        archive = bp.archive_bundle(
            bundle, self.tmp / "out", version="0.0.0-test", target="windows-x86_64"
        )
        with zipfile.ZipFile(archive) as zf:
            self.assertEqual(
                zf.read("keklib-cli/_internal/libreal.dll"), b"INTERNAL-RUNTIME-BYTES"
            )

    def test_bundle_rejects_bad_links(self) -> None:
        secret = self.tmp / "outside-secret"
        secret.write_bytes(b"SYNTHETIC_SECRET")
        for target, name in (("linux-x86_64", "escape"), ("windows-x86_64", "escape-win")):
            bundle = _make_bundle_dir(self.tmp / name, target=target)
            internal = bundle / "_internal"
            link = internal / "runtime.dat"
            os.symlink(secret, link)  # внешняя абсолютная цель
            # Предусловие: внешняя ссылка действительно разрешается
            # strict=True в точный существующий внешний файл.
            self.assertEqual(link.resolve(strict=True), secret.resolve(strict=True))
            out = self.tmp / ("out-" + name)
            with self.assertRaises(bp.PackagingError) as ctx:
                bp.archive_bundle(bundle, out, version="0.0.0-test", target=target)
            self.assertIn("_internal/runtime.dat", str(ctx.exception))
            self.assertIn("покидает", str(ctx.exception))
            published = list(out.rglob("*")) if out.exists() else []
            self.assertEqual(published, [], "отказ не публикует архив/сумму")
            self.assertEqual(secret.read_bytes(), b"SYNTHETIC_SECRET")
        dangling = _make_bundle_dir(self.tmp / "dangling")
        os.symlink("no-such-target.so", dangling / "_internal" / "runtime.dat")
        with self.assertRaises(bp.PackagingError):
            bp.archive_bundle(
                dangling, self.tmp / "out-dangling", version="0.0.0-test", target="linux-x86_64"
            )
        escaping = _make_bundle_dir(self.tmp / "escaping")
        escaping_link = _make_escaping_relative_link(escaping / "_internal", secret)
        # Предусловие фикстуры (нативные семантики ОС) ДО проверки
        # безопасности архива: ссылка разрешается strict=True в точный
        # существующий внешний файл. На Windows raw-цель составной
        # относительной ссылки с «/» падает именно здесь WinError 123
        # (run 39246201467), а не молча доходит до валидации «битой».
        self.assertEqual(
            escaping_link.resolve(strict=True), secret.resolve(strict=True)
        )
        self.assertTrue(os.path.samefile(escaping_link, secret))
        out_escaping = self.tmp / "out-escaping"
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.archive_bundle(
                escaping, out_escaping, version="0.0.0-test", target="linux-x86_64"
            )
        self.assertIn("покидает", str(ctx.exception))
        self.assertIn("_internal/runtime.dat", str(ctx.exception))
        published = list(out_escaping.rglob("*")) if out_escaping.exists() else []
        self.assertEqual(published, [], "отказ не публикует архив/сумму")
        self.assertEqual(secret.read_bytes(), b"SYNTHETIC_SECRET")

    def test_escaping_relative_link_fixture_contract(self) -> None:
        """Контракт фикстуры внешней относительной ссылки: нативная цель.

        Причина остаточного native-провала (run 39246201467, оба
        Windows): составная относительная reparse-цель, записанная с
        «/», не разрешается Win32 (WinError 123). Контракт: цель
        вычисляется os.path.relpath текущей ОС (на POSIX форма
        совпадает с прежней raw-строкой), сохраняется в ссылке дословно
        и разрешается strict=True в точный внешний файл вне комплекта.
        """
        secret = self.tmp / "contract-secret"
        secret.write_bytes(b"CONTRACT-SECRET")
        bundle = _make_bundle_dir(self.tmp / "contract")
        internal = bundle / "_internal"
        link = _make_escaping_relative_link(internal, secret)
        self.assertEqual(os.readlink(link), os.path.relpath(secret, start=internal))
        resolved = link.resolve(strict=True)
        self.assertEqual(resolved, secret.resolve(strict=True))
        self.assertTrue(os.path.samefile(link, secret))
        with self.assertRaises(ValueError):
            resolved.relative_to(bundle.resolve())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "mkfifo отсутствует на этой ОС")
    def test_bundle_rejects_fifo(self) -> None:
        bundle = _make_bundle_dir(self.tmp)
        os.mkfifo(bundle / "_internal" / "pipe.so")
        with self.assertRaises(bp.PackagingError):
            bp.archive_bundle(
                bundle, self.tmp / "out", version="0.0.0-test", target="linux-x86_64"
            )

    # 17. Preflight toolchain: фактические версии против пинов.

    def test_toolchain_preflight_passes(self) -> None:
        try:
            importlib.metadata.version("PyInstaller")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("pyinstaller не установлен: сборочные зависимости — lock")
        bp.verify_toolchain_preflight("linux-x86_64")

    def test_toolchain_preflight_rejects_mismatches(self) -> None:
        toolchain = bp.load_toolchain()
        tampered = json.loads(json.dumps(toolchain))
        tampered["targets"]["linux-x86_64"]["python"] = "3.11.0"
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.verify_toolchain_preflight(
                "linux-x86_64", toolchain=tampered, python_version="3.14.8"
            )
        self.assertIn("3.11.0", str(ctx.exception))

        def fake_version(name: str) -> str:
            if name.lower() == "pyinstaller":
                return "6.0.0"
            return importlib.metadata.version(name)

        with self.assertRaises(bp.PackagingError) as ctx2:
            bp.verify_toolchain_preflight(
                "linux-x86_64", distribution_version=fake_version
            )
        self.assertIn("6.0.0", str(ctx2.exception))

        def missing(name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(name)

        with self.assertRaises(bp.PackagingError) as ctx3:
            bp.verify_toolchain_preflight(
                "linux-x86_64", distribution_version=missing
            )
        self.assertIn("не установлен", str(ctx3.exception))

        lock = self.tmp / "bad.lock"
        lock.write_text("pyinstaller==6.0.0\n", encoding="utf-8")
        with self.assertRaises(bp.PackagingError) as ctx4:
            bp.verify_toolchain_preflight("linux-x86_64", lock_path=lock)
        self.assertIn("pyinstaller", str(ctx4.exception))

    # 18. Release-гейт по SQLite-pin: null блокирует, значение сверяется.

    def test_release_metadata_blocked_without_sqlite_pin(self) -> None:
        toolchain = bp.load_toolchain()
        tampered = json.loads(json.dumps(toolchain))
        tampered["targets"]["linux-x86_64"]["sqlite"] = None
        info = {"python": "3.14.8", "pyinstaller": "6.22.3", "sqlite": "3.54.0"}
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.verify_release_metadata(
                "linux-x86_64", info, toolchain=tampered, sqlite_version="3.54.0"
            )
        message = str(ctx.exception)
        self.assertIn("SQLite", message)
        self.assertIn("не зафиксирован", message)

    def test_release_metadata_matches_pin(self) -> None:
        toolchain = bp.load_toolchain()
        tampered = json.loads(json.dumps(toolchain))
        tampered["targets"]["linux-x86_64"]["sqlite"] = "3.54.0"
        info = {"python": "3.14.8", "pyinstaller": "6.22.3", "sqlite": "3.54.0"}
        bp.verify_release_metadata(
            "linux-x86_64", info, toolchain=tampered, sqlite_version="3.54.0"
        )

    def test_release_metadata_rejects_mismatch(self) -> None:
        toolchain = bp.load_toolchain()
        tampered = json.loads(json.dumps(toolchain))
        tampered["targets"]["linux-x86_64"]["sqlite"] = "3.35.0"
        info = {"python": "3.14.8", "pyinstaller": "6.22.3", "sqlite": "3.54.0"}
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.verify_release_metadata(
                "linux-x86_64", info, toolchain=tampered, sqlite_version="3.54.0"
            )
        self.assertIn("3.35.0", str(ctx.exception))

    # 19. Лицензионный гейт: рекурсивный инвентарь фактического состава.

    def test_license_inventory(self) -> None:
        bundle = _make_bundle_dir(self.tmp)
        internal = bundle / "_internal"
        (internal / "libpython3.14.so.1.0").write_bytes(b"x")
        (internal / "libz.so.1").write_bytes(b"x")
        (internal / "notlib.txt").write_bytes(b"x")
        (internal / "python3.14").mkdir()
        inventory = bp.inventory_bundle_libraries(bundle, target="linux-x86_64")
        self.assertEqual(
            inventory,
            ["_internal/base_library.zip", "_internal/libpython3.14.so.1.0", "_internal/libz.so.1", "keklib-cli"],
        )

    def test_license_inventory_recurses_and_sniffs_magic(self) -> None:
        """Вложенные библиотеки, модули расширений и ELF-magic без расширения."""
        bundle = _make_bundle_dir(self.tmp / "rec")
        internal = bundle / "_internal"
        (internal / "vendor").mkdir()
        (internal / "vendor" / "libsynthetic.so.1").write_bytes(b"x")
        dynload = internal / "python3.14" / "lib-dynload"
        dynload.mkdir(parents=True)
        (dynload / "_sqlite3.cpython-314-x86_64-linux-gnu.so").write_bytes(b"x")
        (dynload / "plain.pyc").write_bytes(b"x")
        (internal / "extensionless-binary").write_bytes(b"\x7fELFpayload")
        inventory = bp.inventory_bundle_libraries(bundle, target="linux-x86_64")
        self.assertIn("_internal/vendor/libsynthetic.so.1", inventory)
        self.assertIn("_internal/python3.14/lib-dynload/_sqlite3.cpython-314-x86_64-linux-gnu.so", inventory)
        self.assertIn("_internal/extensionless-binary", inventory)
        self.assertNotIn("_internal/python3.14/lib-dynload/plain.pyc", inventory)
        self.assertNotIn("_internal/runtime.dynlib", inventory)

    def test_license_inventory_detects_extensionless_macho(self) -> None:
        """Framework-runtime macOS (Mach-O без расширения) — в инвентаре.

        Реальный состав pending-target: файл
        ``_internal/Python.framework/Versions/3.14/Python`` — обычный Mach-O
        без библиотечного расширения. Раньше его видел только диагностический
        полный обход inventory-отчёта (``full_walk_magic``); лицензионный
        инвентарь сборки обязан учитывать его сам — иначе binary runtime
        выпускается без лицензионного покрытия.
        """
        bundle = _make_bundle_dir(self.tmp / "macho")
        framework = bundle / "_internal" / "Python.framework" / "Versions" / "3.14"
        framework.mkdir(parents=True)
        for magic in (
            b"\xcf\xfa\xed\xfe",  # MH_MAGIC_64 LE
            b"\xfe\xed\xfa\xcf",  # MH_MAGIC_64 BE
            b"\xce\xfa\xed\xfe",  # MH_MAGIC LE
            b"\xca\xfe\xba\xbe",  # FAT BE
            b"\xbe\xba\xfe\xca",  # FAT LE
        ):
            target_dir = bundle / "_internal" / f"macho-{magic.hex()}"
            target_dir.mkdir()
            (target_dir / "Python").write_bytes(magic + b"payload")
        inventory = bp.inventory_bundle_libraries(bundle, target="macos-x86_64")
        for magic in (
            b"\xcf\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf",
            b"\xce\xfa\xed\xfe",
            b"\xca\xfe\xba\xbe",
            b"\xbe\xba\xfe\xca",
        ):
            identifier = f"_internal/macho-{magic.hex()}/Python"
            self.assertIn(identifier, inventory)

    def test_license_inventory_detects_pe_magic_and_case_extensions(self) -> None:
        """PE-magic без расширения и расширения в верхнем регистре.

        Реальные имена Windows-состава регистрозависимы в ZIP (VCRUNTIME140.dll,
        libcrypto-3.dll); архиватор не обязан нормализовать регистр. Инвентарь
        обязан ловить ``SOMELIB.DLL``/``MODULE.PYD`` и extensionless PE-бинарь
        по magic «MZ + PE-сигнатура» — как это делает диагностический обход.
        """
        bundle = _make_bundle_dir(self.tmp / "pe")
        internal = bundle / "_internal"
        (internal / "SOMELIB.DLL").write_bytes(b"x")
        (internal / "MODULE.PYD").write_bytes(b"x")
        (internal / "Mixed.Dylib").write_bytes(b"x")
        (internal / "BOOTLOADER.EXE").write_bytes(b"x")
        (internal / "extensionless-pe").write_bytes(
            b"MZ" + b"\x00" * 0x3E + b"\xf0\x00\x00\x00PE\x00\x00payload"
        )
        inventory = bp.inventory_bundle_libraries(bundle, target="windows-x86_64")
        for name in (
            "_internal/SOMELIB.DLL",
            "_internal/MODULE.PYD",
            "_internal/Mixed.Dylib",
            "_internal/BOOTLOADER.EXE",
            "_internal/extensionless-pe",
        ):
            self.assertIn(name, inventory)

    def test_license_inventory_treats_broken_mz_as_binary(self) -> None:
        """MZ без валидной PE-сигнатуры — тоже binary: гейт решает явным покрытием.

        Fail-closed: файл с DOS-magic «MZ», но без подтверждаемой PE-структуры
        не может быть молча пропущен как «не библиотека» — неизвестный binary
        обязан попасть в инвентарь и блокировать выпуск без контракта.
        """
        bundle = _make_bundle_dir(self.tmp / "mz")
        internal = bundle / "_internal"
        (internal / "mystery-mz").write_bytes(b"MZbroken-without-pe-header")
        inventory = bp.inventory_bundle_libraries(bundle, target="windows-x86_64")
        self.assertIn("_internal/mystery-mz", inventory)

    def test_license_gate_blocks_unknown_macho_and_pe(self) -> None:
        """Неизвестный Mach-O/PE-бинарь без покрытия блокирует гейт."""
        bundle, notices = self._license_fixture()
        components = {
            "mit-test": {"license": "MIT", "notice": None, "distribution_ok": True}
        }
        coverage = self._full_coverage(bundle)
        manifest = self.tmp / "manifest.json"
        manifest.write_text(
            json.dumps(self._manifest_v2(components, coverage)), encoding="utf-8"
        )
        (bundle / "_internal" / "Python.framework").mkdir()
        (bundle / "_internal" / "Python.framework" / "Versions").mkdir()
        (bundle / "_internal" / "Python.framework" / "Versions" / "3.14").mkdir()
        (
            bundle / "_internal" / "Python.framework" / "Versions" / "3.14" / "Python"
        ).write_bytes(b"\xcf\xfa\xed\xfepayload")
        (bundle / "_internal" / "SNEAKY.DLL").write_bytes(b"x")
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.verify_license_closure(
                bundle, target="macos-x86_64", manifest_path=manifest, notices_root=notices
            )
        message = str(ctx.exception)
        self.assertIn("_internal/Python.framework/Versions/3.14/Python", message)
        self.assertIn("_internal/SNEAKY.DLL", message)

    def _license_fixture(self) -> tuple[Path, Path]:
        """Комплект с одной библиотекой + локальный манифест с текстами."""
        bundle = _make_bundle_dir(self.tmp / "lic")
        (bundle / "_internal" / "libtestlib.so.1").write_bytes(b"x")
        notices = self.tmp / "notices"
        (notices / "licenses" / "testlib").mkdir(parents=True)
        (notices / "licenses" / "testlib" / "LICENSE.txt").write_text(
            "TESTLIB LICENSE TEXT\n", encoding="utf-8"
        )
        return bundle, notices

    @staticmethod
    def _manifest_v2(
        components: dict[str, dict[str, object]], coverage: dict[str, str]
    ) -> dict[str, object]:
        return {"schema_version": 2, "components": components, "coverage": coverage}

    def _full_coverage(self, bundle: Path, component: str = "mit-test") -> dict[str, str]:
        coverage = {}
        for path in bp.inventory_bundle_libraries(bundle, target="linux-x86_64"):
            coverage[path] = component
        return coverage

    def test_license_gate_requires_coverage_and_clearance(self) -> None:
        bundle, notices = self._license_fixture()
        components = {
            "mit-test": {
                "license": "MIT",
                "notice": "licenses/testlib/LICENSE.txt",
                "distribution_ok": True,
            }
        }
        manifest = self.tmp / "manifest.json"
        manifest.write_text(
            json.dumps(self._manifest_v2(components, self._full_coverage(bundle))),
            encoding="utf-8",
        )
        bp.verify_license_closure(
            bundle,
            target="linux-x86_64",
            manifest_path=manifest,
            notices_root=notices,
            copy_notices_to=bundle,
        )
        copied = bundle / "licenses" / "testlib" / "LICENSE.txt"
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.read_text(encoding="utf-8"), "TESTLIB LICENSE TEXT\n")
        # Комплект с licenses/ по-прежнему архивируется (allowlist).
        bp.archive_bundle(
            bundle, self.tmp / "out", version="0.0.0-test", target="linux-x86_64"
        )

        uncovered = self.tmp / "uncovered.json"
        coverage = self._full_coverage(bundle)
        coverage.pop("_internal/libtestlib.so.1")
        uncovered.write_text(
            json.dumps(self._manifest_v2(components, coverage)), encoding="utf-8"
        )
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.verify_license_closure(
                bundle,
                target="linux-x86_64",
                manifest_path=uncovered,
                notices_root=notices,
            )
        self.assertIn("_internal/libtestlib.so.1", str(ctx.exception))

        blocked = self.tmp / "blocked.json"
        components_false = {
            "mit-test": {
                "license": "MIT",
                "notice": None,
                "distribution_ok": False,
            }
        }
        blocked.write_text(
            json.dumps(self._manifest_v2(components_false, self._full_coverage(bundle))),
            encoding="utf-8",
        )
        with self.assertRaises(bp.PackagingError) as ctx2:
            bp.verify_license_closure(
                bundle,
                target="linux-x86_64",
                manifest_path=blocked,
                notices_root=notices,
            )
        self.assertIn("distribution_ok", str(ctx2.exception))

    def test_license_nested_unknown_blocks(self) -> None:
        """Вложенная библиотека без записи в контракте блокирует гейт."""
        bundle, notices = self._license_fixture()
        components = {
            "mit-test": {"license": "MIT", "notice": None, "distribution_ok": True}
        }
        # Контракт построен ДО появления вложенной библиотеки.
        coverage = self._full_coverage(bundle)
        manifest = self.tmp / "manifest.json"
        manifest.write_text(
            json.dumps(self._manifest_v2(components, coverage)), encoding="utf-8"
        )
        (bundle / "_internal" / "vendor").mkdir()
        (bundle / "_internal" / "vendor" / "libsynthetic.so.1").write_bytes(b"x")
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.verify_license_closure(
                bundle, target="linux-x86_64", manifest_path=manifest, notices_root=notices
            )
        self.assertIn("_internal/vendor/libsynthetic.so.1", str(ctx.exception))

    def test_license_pattern_coverage_for_extension_modules(self) -> None:
        """Модули расширений покрываются шаблоном компонента runtime."""
        bundle = _make_bundle_dir(self.tmp / "dyn")
        dynload = bundle / "_internal" / "python3.14" / "lib-dynload"
        dynload.mkdir(parents=True)
        (dynload / "_sqlite3.cpython-314-x86_64-linux-gnu.so").write_bytes(b"x")
        components = {
            "runtime": {"license": "PSF", "notice": None, "distribution_ok": True},
            "own": {"license": "Proprietary", "notice": None, "distribution_ok": True},
        }
        coverage = {
            "keklib-cli": "own",
            "_internal/base_library.zip": "runtime",
            "_internal/libpython3.14.so.1.0": "runtime",
            "_internal/python3.14/lib-dynload/*": "runtime",
        }
        manifest = self.tmp / "manifest.json"
        manifest.write_text(
            json.dumps(self._manifest_v2(components, coverage)), encoding="utf-8"
        )
        bp.verify_license_closure(
            bundle, target="linux-x86_64", manifest_path=manifest
        )

    def test_license_strict_bool_and_contract_refs(self) -> None:
        bundle, notices = self._license_fixture()
        stringy = {
            "mit-test": {
                "license": "MIT",
                "notice": None,
                "distribution_ok": "true",  # строка, не JSON true
            }
        }
        manifest = self.tmp / "stringy.json"
        manifest.write_text(
            json.dumps(self._manifest_v2(stringy, self._full_coverage(bundle))),
            encoding="utf-8",
        )
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.verify_license_closure(
                bundle, target="linux-x86_64", manifest_path=manifest, notices_root=notices
            )
        self.assertIn("bool", str(ctx.exception))

        broken = self.tmp / "broken.json"
        broken.write_text(
            json.dumps(
                self._manifest_v2(
                    {"mit-test": {"license": "MIT", "notice": None, "distribution_ok": True}},
                    self._full_coverage(bundle, component="no-such-component"),
                )
            ),
            encoding="utf-8",
        )
        with self.assertRaises(bp.PackagingError) as ctx2:
            bp.verify_license_closure(
                bundle, target="linux-x86_64", manifest_path=broken, notices_root=notices
            )
        self.assertIn("no-such-component", str(ctx2.exception))

    def test_license_gate_notice_files_must_exist(self) -> None:
        bundle, notices = self._license_fixture()
        components = {
            "mit-test": {
                "license": "MIT",
                "notice": "licenses/testlib/MISSING.txt",
                "distribution_ok": True,
            }
        }
        manifest = self.tmp / "manifest.json"
        manifest.write_text(
            json.dumps(self._manifest_v2(components, self._full_coverage(bundle))),
            encoding="utf-8",
        )
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.verify_license_closure(
                bundle, target="linux-x86_64", manifest_path=manifest, notices_root=notices
            )
        self.assertIn("MISSING.txt", str(ctx.exception))

    def test_license_closure_committed_state_is_linux_cleared(self) -> None:
        """Коммитнутый манифест: linux-закрытие завершено, GPL не разрешён.

        Native-продолжение задачи 11: закрытие выполнено по фактическому
        инвентарю принятой сборки на ubuntu-22.04 builder'е — пермиссивные
        компоненты разрешены с существующими уведомлениями, GNU Readline
        (GPL-3.0) остаётся незакрытым и блокирует комплект, если попадёт
        в инвентарь (сборка его не включает: --without-readline).
        """
        manifest = json.loads(
            (PROJECT_ROOT / "packaging" / "license-closure.json").read_text(encoding="utf-8")
        )
        components = manifest["components"]
        for name in (
            "keklib-cli",
            "pyinstaller-bootloader",
            "cpython-runtime",
            "sqlite",
            "openssl",
        ):
            self.assertIs(components[name]["distribution_ok"], True, name)
            notice = components[name]["notice"]
            self.assertIsInstance(notice, str, name)
            self.assertTrue(
                (PROJECT_ROOT / "packaging" / notice).is_file(),
                f"{name}: отсутствует файл уведомления {notice}",
            )
        self.assertIs(components["readline-gpl"]["distribution_ok"], False)

        bundle = _make_bundle_dir(self.tmp / "state")
        (bundle / "_internal" / "libpython3.14.so.1.0").write_bytes(b"x")
        bp.verify_license_closure(bundle, target="linux-x86_64")  # PSF закрыт — проходит

        gpl = _make_bundle_dir(self.tmp / "gpl")
        (gpl / "_internal" / "libreadline.so.8").write_bytes(b"x")
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.verify_license_closure(gpl, target="linux-x86_64")
        self.assertIn("readline-gpl", str(ctx.exception))

    def test_license_manifest_attribution_of_base_library(self) -> None:
        """base_library.zip — модули CPython, а не «доказательство PyInstaller»."""
        manifest = json.loads(
            (PROJECT_ROOT / "packaging" / "license-closure.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["coverage"]["_internal/base_library.zip"], "cpython-runtime")
        self.assertIn("cpython-runtime", manifest["components"])
        self.assertIn("pyinstaller-bootloader", manifest["components"])

    def test_license_compound_executable_requires_all_components(self) -> None:
        """Составной executable требует явного разрешения каждого встроенного компонента."""
        bundle = _make_bundle_dir(self.tmp / "compound")
        notices = self.tmp / "notices"
        for rel in ("licenses/bootloader/NOTICE.txt", "licenses/own/NOTICE.txt"):
            path = notices / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"NOTICE {rel}\n", encoding="utf-8")

        def manifest_with_bootloader(*, bootloader_ok: bool, bootloader_notice: object) -> Path:
            components = {
                "own": {"license": "Proprietary", "notice": "licenses/own/NOTICE.txt", "distribution_ok": True},
                "cpython-runtime": {"license": "PSF", "notice": None, "distribution_ok": True},
                "pyinstaller-bootloader": {
                    "license": "GPL with bootloader exception",
                    "notice": bootloader_notice,
                    "distribution_ok": bootloader_ok,
                },
            }
            coverage = dict(self._full_coverage(bundle))
            coverage["keklib-cli"] = ["own", "pyinstaller-bootloader"]
            coverage["_internal/base_library.zip"] = "cpython-runtime"
            path = self.tmp / f"manifest-{bootloader_ok}-{bool(bootloader_notice)}.json"
            path.write_text(json.dumps(self._manifest_v2(components, coverage)), encoding="utf-8")
            return path

        # own/runtime разрешены, bootloader не разрешён — отказ.
        with self.assertRaises(bp.PackagingError) as ctx_false:
            bp.verify_license_closure(
                bundle,
                target="linux-x86_64",
                manifest_path=manifest_with_bootloader(bootloader_ok=False, bootloader_notice=None),
                notices_root=notices,
            )
        self.assertIn("pyinstaller-bootloader", str(ctx_false.exception))

        # bootloader разрешён, но обязательное уведомление отсутствует — отказ.
        with self.assertRaises(bp.PackagingError) as ctx_notice:
            bp.verify_license_closure(
                bundle,
                target="linux-x86_64",
                manifest_path=manifest_with_bootloader(
                    bootloader_ok=True, bootloader_notice="licenses/bootloader/MISSING.txt"
                ),
                notices_root=notices,
            )
        self.assertIn("MISSING.txt", str(ctx_notice.exception))

        # все компоненты разрешены, уведомление на месте — проход и копирование.
        target_dir = self.tmp / "ok-bundle"
        ok_bundle = _make_bundle_dir(target_dir)
        components = {
            "own": {"license": "Proprietary", "notice": "licenses/own/NOTICE.txt", "distribution_ok": True},
            "cpython-runtime": {"license": "PSF", "notice": None, "distribution_ok": True},
            "pyinstaller-bootloader": {
                "license": "GPL with bootloader exception",
                "notice": "licenses/bootloader/NOTICE.txt",
                "distribution_ok": True,
            },
        }
        coverage = dict(self._full_coverage(ok_bundle))
        coverage["keklib-cli"] = ["own", "pyinstaller-bootloader"]
        coverage["_internal/base_library.zip"] = "cpython-runtime"
        manifest = self.tmp / "manifest-ok.json"
        manifest.write_text(json.dumps(self._manifest_v2(components, coverage)), encoding="utf-8")
        bp.verify_license_closure(
            ok_bundle,
            target="linux-x86_64",
            manifest_path=manifest,
            notices_root=notices,
            copy_notices_to=ok_bundle,
        )
        copied = ok_bundle / "licenses" / "bootloader" / "NOTICE.txt"
        self.assertTrue(copied.is_file(), "уведомление разрешённого bootloader копируется")

    def test_license_unused_platform_components_need_not_be_cleared(self) -> None:
        """Неиспользуемые компоненты других платформ не обязаны быть true."""
        bundle, notices = self._license_fixture()
        components = {
            "mit-test": {"license": "MIT", "notice": "licenses/testlib/LICENSE.txt", "distribution_ok": True},
            "future-openssl": {"license": "Apache-2.0", "notice": None, "distribution_ok": False},
            "future-readline": {"license": "GPL-3.0", "notice": None, "distribution_ok": False},
        }
        coverage = self._full_coverage(bundle)
        coverage["_internal/libcrypto.so.3"] = "future-openssl"  # в инвентаре отсутствует
        manifest = self.tmp / "manifest.json"
        manifest.write_text(json.dumps(self._manifest_v2(components, coverage)), encoding="utf-8")
        bp.verify_license_closure(
            bundle, target="linux-x86_64", manifest_path=manifest, notices_root=notices
        )

    def test_license_coverage_schema_validation(self) -> None:
        """Контракт отвергает битые записи покрытия и ссылки на пустоту."""
        bundle, notices = self._license_fixture()
        components = {
            "mit-test": {"license": "MIT", "notice": None, "distribution_ok": True}
        }
        for bad_value, needle in (
            ([], "покрытия"),
            (17, "покрытия"),
            (["mit-test", "no-such-component"], "no-such-component"),
        ):
            coverage = self._full_coverage(bundle)
            coverage["keklib-cli"] = bad_value
            manifest = self.tmp / "bad.json"
            manifest.write_text(json.dumps(self._manifest_v2(components, coverage)), encoding="utf-8")
            with self.assertRaises(bp.PackagingError) as ctx:
                bp.verify_license_closure(
                    bundle, target="linux-x86_64", manifest_path=manifest, notices_root=notices
                )
            self.assertIn(needle, str(ctx.exception))

    def test_inventory_identifier_posix_contract(self) -> None:
        """Идентификаторы инвентаря — POSIX-пути независимо от разделителей ОС."""
        from pathlib import PureWindowsPath

        windows_style = bp.inventory_identifier(PureWindowsPath("_internal"), "base_library.zip")
        self.assertEqual(windows_style, "_internal/base_library.zip")
        posix_style = bp.inventory_identifier(Path("_internal/vendor"), "libx.so.1")
        self.assertEqual(posix_style, "_internal/vendor/libx.so.1")
        root_level = bp.inventory_identifier(Path("."), "keklib-cli")
        self.assertEqual(root_level, "keklib-cli")

    # 20. Протокол пары архив+сумма: публикация никогда не удаляет архив.

    def test_checksum_failure_never_deletes_archive_and_reports_repair(self) -> None:
        bundle = _make_bundle_dir(self.tmp)
        out = self.tmp / "out"
        real = bp.__dict__.get("_publish_no_replace")
        self.assertIsNotNone(real, "archive_bundle должен использовать внедряемую публикацию")
        calls: list[str] = []

        def flaky(temp: Path, target: Path) -> None:
            calls.append(target.name)
            if len(calls) == 1:
                real(temp, target)
                return
            raise OSError("publication failed")

        with mock.patch.object(bp, "_publish_no_replace", side_effect=flaky):
            with self.assertRaises(bp.PackagingError) as ctx:
                bp.archive_bundle(bundle, out, version="0.0.0-test", target="linux-x86_64")
        self.assertEqual(len(calls), 2, "должны были попытаться опубликовать оба файла")
        archive = out / "keklib-cli-0.0.0-test-linux-x86_64.tar.gz"
        self.assertTrue(archive.is_file(), "архив обязан остаться: удаление запрещено")
        message = str(ctx.exception)
        self.assertIn("--repair-checksum", message)
        digest = re.search(r"--expected-sha256 ([0-9a-f]{64})", message)
        self.assertIsNotNone(digest, "в диагностике должен быть фактический digest")
        # Явный ремонт восстанавливает пару без удаления/перезаписи.
        checksum = bp.repair_checksum(archive, digest.group(1))
        self.assertTrue(checksum.is_file())
        self.assertEqual(
            checksum.read_text(encoding="utf-8"),
            f"{digest.group(1)}  {archive.name}\n",
        )
        leftovers = sorted(p.name for p in out.iterdir())
        self.assertEqual(leftovers, sorted([archive.name, checksum.name]))

    def test_checksum_failure_same_byte_foreign_replacement_survives(self) -> None:
        """Чужая замена с теми же байтами (другой inode) не удаляется."""
        bundle = _make_bundle_dir(self.tmp / "b2")
        out = self.tmp / "out"
        real = bp.__dict__.get("_publish_no_replace")
        calls: list[str] = []

        def takeover(temp: Path, target: Path) -> None:
            calls.append(target.name)
            if len(calls) == 1:
                real(temp, target)
                staged = target.with_name(".foreign-takeover")
                staged.write_bytes(target.read_bytes())  # те же байты, другой inode
                os.replace(staged, target)
                return
            raise OSError("publication failed")

        with mock.patch.object(bp, "_publish_no_replace", side_effect=takeover):
            with self.assertRaises(bp.PackagingError):
                bp.archive_bundle(
                    bundle, out, version="0.0.0-test", target="linux-x86_64"
                )
        archive = out / "keklib-cli-0.0.0-test-linux-x86_64.tar.gz"
        self.assertTrue(archive.is_file(), "чужая same-byte замена обязана выжить")
        self.assertEqual(archive.stat().st_nlink, 1)

    def test_checksum_failure_preserves_foreign_mutation(self) -> None:
        """Мутация между публикациями также не удаляется (мутация выживает)."""
        bundle = _make_bundle_dir(self.tmp / "b3")
        out = self.tmp / "out"
        real = bp.__dict__.get("_publish_no_replace")
        calls: list[str] = []

        def race(temp: Path, target: Path) -> None:
            calls.append(target.name)
            if len(calls) == 1:
                real(temp, target)
                target.write_bytes(b"FOREIGN-BYTES")  # подмена между публикациями
                return
            raise OSError("publication failed")

        with mock.patch.object(bp, "_publish_no_replace", side_effect=race):
            with self.assertRaises(bp.PackagingError):
                bp.archive_bundle(
                    bundle, out, version="0.0.0-test", target="linux-x86_64"
                )
        real_archive = out / "keklib-cli-0.0.0-test-linux-x86_64.tar.gz"
        self.assertEqual(real_archive.read_bytes(), b"FOREIGN-BYTES", "чужой файл не тронут")

    # 21. Явный ремонт контрольной суммы.

    def test_repair_checksum_success_repeat_conflict_and_mismatch(self) -> None:
        bundle = _make_bundle_dir(self.tmp)
        out = self.tmp / "out"
        archive = bp.archive_bundle(
            bundle, out, version="0.0.0-test", target="linux-x86_64"
        )
        checksum = archive.with_name(archive.name + ".sha256")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum.unlink()  # имитация прерванной пары
        repaired = bp.repair_checksum(archive, digest)
        self.assertEqual(repaired, checksum)
        self.assertEqual(
            checksum.read_text(encoding="utf-8"), f"{digest}  {archive.name}\n"
        )
        # Повтор при существующей (пусть чужой) сумме — отказ, файл сохранён.
        checksum.unlink()
        checksum.write_bytes(b"foreign-checksum\n")
        with self.assertRaises(bp.PackagingError):
            bp.repair_checksum(archive, digest)
        self.assertEqual(checksum.read_bytes(), b"foreign-checksum\n")
        # Несовпадение байтов с ожидаемым digest — отказ без записи суммы.
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.repair_checksum(archive, "0" * 64)
        self.assertIn("не совпадают", str(ctx.exception))

    def test_repair_checksum_validates_inputs(self) -> None:
        bundle = _make_bundle_dir(self.tmp)
        out = self.tmp / "out"
        archive = bp.archive_bundle(
            bundle, out, version="0.0.0-test", target="linux-x86_64"
        )
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        with self.assertRaises(bp.PackagingError):
            bp.repair_checksum(archive, "not-a-digest")
        link = self.tmp / "archive-link.tar.gz"
        os.symlink(archive, link)
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.repair_checksum(link, digest)
        self.assertIn("обычным файлом", str(ctx.exception))
        self.assertFalse(link.with_name(link.name + ".sha256").exists())

    def test_cli_repair_mode(self) -> None:
        bundle = _make_bundle_dir(self.tmp)
        out = self.tmp / "out"
        archive = bp.archive_bundle(
            bundle, out, version="0.0.0-test", target="linux-x86_64"
        )
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum = archive.with_name(archive.name + ".sha256")
        checksum.unlink()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = bp.main(["--repair-checksum", str(archive), "--expected-sha256", digest])
        self.assertEqual(code, 0)
        self.assertEqual(stream.getvalue().strip(), str(checksum.resolve()))
        self.assertTrue(checksum.is_file())
        with self.assertRaises(SystemExit) as ctx:
            bp.main(["--repair-checksum", str(archive)])
        self.assertEqual(ctx.exception.code, 2)


#: Враждебный PATH реального прогона native-run-39260511786: Java_Temurin
#: из hosted toolcache — источник api-ms-win-*/ucrtbase.dll, попавших в
#: Windows COLLECT (toc_source каждой записи).
JAVA_TEMURIN_BIN = (
    r"C:\hostedtoolcache\windows\Java_Temurin-Hotspot_jdk\8.0.504-1\x64\bin"
)


def _hostile_windows_environ() -> dict[str, str]:
    """Родительское окружение Windows с посторонними каталогами в PATH."""
    return {
        "SystemRoot": r"C:\Windows",
        "PATH": ";".join(
            [
                JAVA_TEMURIN_BIN,
                r"C:\Windows\system32",
                r"C:\Windows",
                r"C:\Program Files\dotnet",
                r"C:\ProgramData\chocolatey\bin",
            ]
        ),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "TEMP": r"C:\Windows\TEMP",
        "TMP": r"C:\Windows\TEMP",
    }


def _trusted_path_entries(executable: str, system_root: str) -> list[str]:
    from pathlib import PureWindowsPath

    exe_parent = PureWindowsPath(executable).parent
    root = PureWindowsPath(system_root)
    return [
        str(exe_parent),
        str(exe_parent / "Scripts"),
        str(root / "System32"),
        str(root),
    ]


class PyInstallerSubprocessEnvTests(unittest.TestCase):
    """Окружение child-процесса PyInstaller: изоляция Windows от чужих DLL в PATH.

    Закреплённый PyInstaller 6.22.3 на Windows ищет DLL фолбэком по ВСЕМ
    каталогам PATH процесса (``bindepend.resolve_library_path``), а PATH
    родителя hosted-runner'а содержит Java_Temurin с собственными
    ucrtbase/api-ms-win-* — они попадали в COLLECT (native-run-39260511786).
    Контракт: на Windows PATH child-процесса ограничен доверенными
    каталогами (интерпретатор, его Scripts, System32, SystemRoot);
    на остальных ОС окружение родителя не меняется вовсе.
    """

    EXE = r"C:\hostedtoolcache\windows\Python314\x64\python.exe"
    ROOT = r"C:\Windows"

    def test_non_windows_returns_none_parent_env_inherited(self) -> None:
        self.assertIsNone(bp.pyinstaller_subprocess_env(windows=False))

    def test_default_branch_follows_native_platform(self) -> None:
        env = bp.pyinstaller_subprocess_env()
        if sys.platform == "win32":
            self.assertIsInstance(env, dict)
        else:
            self.assertIsNone(env)

    def test_windows_path_restricted_to_trusted_directories_in_order(self) -> None:
        env = bp.pyinstaller_subprocess_env(
            environ=_hostile_windows_environ(), executable=self.EXE, windows=True
        )
        self.assertIsNotNone(env)
        expected = _trusted_path_entries(self.EXE, self.ROOT)
        # Разделитель целевой ОС, а не хоста: ветка Windows-only.
        self.assertEqual(env["PATH"], ";".join(expected))
        self.assertEqual(env["PATH"].split(";"), expected)

    def test_windows_hostile_java_directory_excluded(self) -> None:
        parent = _hostile_windows_environ()
        env = bp.pyinstaller_subprocess_env(
            environ=parent, executable=self.EXE, windows=True
        )
        entries = env["PATH"].split(";")
        self.assertNotIn(JAVA_TEMURIN_BIN, entries)
        self.assertNotIn("Java_Temurin", env["PATH"])
        for foreign in (r"C:\Program Files\dotnet", r"C:\ProgramData\chocolatey\bin"):
            self.assertNotIn(foreign, entries)
        # Ровно четыре доверенные записи — посторонних каталогов нет.
        self.assertEqual(len(entries), 4)

    def test_windows_parent_environ_unchanged_and_result_is_copy(self) -> None:
        parent = _hostile_windows_environ()
        snapshot = dict(parent)
        env = bp.pyinstaller_subprocess_env(
            environ=parent, executable=self.EXE, windows=True
        )
        self.assertEqual(parent, snapshot)
        self.assertIsNot(env, parent)
        self.assertNotEqual(env["PATH"], snapshot["PATH"])

    def test_windows_preserves_other_environment_variables(self) -> None:
        parent = _hostile_windows_environ()
        env = bp.pyinstaller_subprocess_env(
            environ=parent, executable=self.EXE, windows=True
        )
        for key in ("SystemRoot", "PATHEXT", "TEMP", "TMP"):
            self.assertEqual(env[key], parent[key])

    def test_windows_requires_absolute_interpreter(self) -> None:
        parent = _hostile_windows_environ()
        for bad in ("python.exe", r"tools\python.exe", r"C:python\python.exe"):
            with self.assertRaises(
                bp.PackagingError, msg=f"ожидался отказ для {bad!r}"
            ):
                bp.pyinstaller_subprocess_env(
                    environ=parent, executable=bad, windows=True
                )

    def test_windows_requires_absolute_system_root_without_guessing(self) -> None:
        for bad in (
            {},  # SystemRoot отсутствует — C:\Windows не угадывается
            {"SystemRoot": ""},
            {"SystemRoot": "Windows"},
            {"SystemRoot": r"C:Windows"},  # drive-relative
        ):
            environ = dict(_hostile_windows_environ())
            environ.pop("SystemRoot", None)
            environ.update(bad)
            with self.assertRaises(
                bp.PackagingError, msg=f"ожидался отказ для SystemRoot={bad!r}"
            ):
                bp.pyinstaller_subprocess_env(
                    environ=environ, executable=self.EXE, windows=True
                )

    def test_run_pyinstaller_passes_env_from_shared_helper(self) -> None:
        """Проводка: env= приходит из общего helper'а как есть (обе ветки)."""
        for sentinel in (None, {"PATH": "trusted"}):
            with mock.patch.object(
                bp, "pyinstaller_subprocess_env", return_value=sentinel
            ) as helper, mock.patch("subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, "", "")
                bp._run_pyinstaller(self.tmp_path() / "dist", self.tmp_path() / "work")
            helper.assert_called_once_with()
            self.assertIs(run.call_args.kwargs["env"], sentinel)
            command = run.call_args.args[0]
            self.assertEqual(command[0], sys.executable)
            self.assertTrue(Path(command[0]).is_absolute())

    def test_run_pyinstaller_rejects_relative_interpreter(self) -> None:
        with mock.patch.object(bp.sys, "executable", "python.exe"), mock.patch(
            "subprocess.run"
        ) as run:
            with self.assertRaises(bp.PackagingError):
                bp._run_pyinstaller(self.tmp_path() / "dist", self.tmp_path() / "work")
        run.assert_not_called()

    def test_environment_facts_record_build_path_control(self) -> None:
        parent = _hostile_windows_environ()
        facts = bp.pyinstaller_environment_facts(
            environ=parent, executable=self.EXE, windows=True
        )
        self.assertEqual(facts["policy"], "windows-restricted-path")
        self.assertEqual(facts["python_executable"], self.EXE)
        self.assertEqual(facts["system_root"], self.ROOT)
        self.assertEqual(facts["parent_path"], parent["PATH"])
        self.assertEqual(
            facts["subprocess_path_entries"],
            _trusted_path_entries(self.EXE, self.ROOT),
        )
        self.assertIn(JAVA_TEMURIN_BIN, facts["parent_path"])
        self.assertNotIn("Java_Temurin", facts["subprocess_path"])

    def test_environment_facts_inherit_policy_on_non_windows(self) -> None:
        parent = {"PATH": "/usr/bin:/bin", "HOME": "/home/user"}
        facts = bp.pyinstaller_environment_facts(
            environ=parent, executable="/usr/bin/python3", windows=False
        )
        self.assertEqual(facts["policy"], "inherit-parent")
        self.assertEqual(facts["parent_path"], parent["PATH"])
        self.assertEqual(facts["subprocess_path"], parent["PATH"])
        self.assertEqual(facts["subprocess_path_entries"], [])
        self.assertIsNone(facts["system_root"])

    def tmp_path(self) -> Path:
        tmp = portable_mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return tmp


# ---------------------------------------------------------------------------
# Лицензионное закрытие трёх native-target по фактическим inventory-отчётам.
#
# Фикстуры ниже — ТОЧНЫЕ списки файлов bundle из диагностических отчётов
# native-run-39263502011 (schema 2, source SHA e8185f5, run SUCCESS 3/3:
# python.org CPython 3.14.8 на windows-2022 / macos-15-intel / macos-14;
# оба macOS-target имеют идентичные относительные пути). Байты файлов в
# фикстурах синтетические (magic/расширения сохранены), прогоны против
# реального манифеста packaging/license-closure.json — честная симуляция
# контракта, а НЕ нативная приёмка: реальные сборки/smoke не выполнялись.

_WINDOWS_REPORT_FILES = (
    "keklib-cli.exe",
    "_internal/VCRUNTIME140.dll",
    "_internal/VCRUNTIME140_1.dll",
    "_internal/_bz2.pyd",
    "_internal/_ctypes.pyd",
    "_internal/_decimal.pyd",
    "_internal/_hashlib.pyd",
    "_internal/_lzma.pyd",
    "_internal/_socket.pyd",
    "_internal/_sqlite3.pyd",
    "_internal/_ssl.pyd",
    "_internal/_wmi.pyd",
    "_internal/_zstd.pyd",
    "_internal/base_library.zip",
    "_internal/libcrypto-3.dll",
    "_internal/libffi-8.dll",
    "_internal/libssl-3.dll",
    "_internal/python314.dll",
    "_internal/select.pyd",
    "_internal/sqlite3.dll",
    "_internal/unicodedata.pyd",
)

_MACOS_REPORT_FILES = (
    "keklib-cli",
    "_internal/base_library.zip",
    "_internal/libcrypto.3.dylib",
    "_internal/libssl.3.dylib",
    "_internal/libzstd.1.dylib",
    "_internal/Python.framework/Versions/3.14/Python",
    "_internal/Python.framework/Versions/3.14/Resources/Info.plist",
    "_internal/python3.14/lib-dynload/_bisect.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_blake2.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_bz2.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_codecs_cn.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_codecs_hk.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_codecs_iso2022.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_codecs_jp.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_codecs_kr.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_codecs_tw.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_csv.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_ctypes.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_decimal.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_hashlib.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_heapq.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_json.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_lzma.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_md5.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_multibytecodec.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_pickle.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_posixsubprocess.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_random.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_scproxy.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_sha1.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_sha2.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_sha3.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_socket.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_sqlite3.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_ssl.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_statistics.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_struct.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/_zstd.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/array.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/binascii.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/fcntl.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/grp.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/math.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/pyexpat.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/readline.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/resource.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/select.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/unicodedata.cpython-314-darwin.so",
    "_internal/python3.14/lib-dynload/zlib.cpython-314-darwin.so",
)

#: Фактический состав ПРИНЯТОГО linux-выпуска (инвентарь архива
#: keklib-cli-0.1.0-linux-x86_64.tar.gz, SHA-256
#: a0d6eb6a29905ec38ae8a05113b5a364af19b897045797221722b28760910ea2,
#: native-приёмка задачи 11): точный список библиотечных позиций,
#: покрытие которых обязано сохраняться при любых изменениях контракта.
_LINUX_ACCEPTED_FILES = (
    "keklib-cli",
    "_internal/base_library.zip",
    "_internal/libbz2.so.1.0",
    "_internal/libcrypto.so.3",
    "_internal/libffi.so.8",
    "_internal/liblzma.so.5",
    "_internal/libpython3.14.so.1.0",
    "_internal/libsqlite3.so",
    "_internal/libssl.so.3",
    "_internal/libz.so.1",
    "_internal/python3.14/lib-dynload/array.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/binascii.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_bisect.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_blake2.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_bz2.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_codecs_cn.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_codecs_hk.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_codecs_iso2022.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_codecs_jp.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_codecs_kr.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_codecs_tw.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_csv.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_ctypes.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_decimal.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/fcntl.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/grp.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_hashlib.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_heapq.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_json.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_lzma.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/math.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_md5.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_multibytecodec.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_pickle.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_posixsubprocess.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_random.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/resource.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/select.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_sha1.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_sha2.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_sha3.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_socket.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_sqlite3.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_ssl.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_statistics.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/_struct.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/unicodedata.cpython-314-x86_64-linux-gnu.so",
    "_internal/python3.14/lib-dynload/zlib.cpython-314-x86_64-linux-gnu.so",
)


class NativeTargetsClosureTests(unittest.TestCase):
    """Закрытие фактического состава windows/macos по реальному манифесту."""

    maxDiff = None

    def setUp(self) -> None:
        self.tmp = portable_mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _synthetic_bundle(self, target: str, paths: Sequence[str]) -> Path:
        """Каталог комплекта с точным списком файлов отчёта.

        Расширения покрывают инвентарь по имени; extensionless бинари
        (framework-runtime macOS, исполнимый файл macOS) получают настоящий
        Mach-O magic, чтобы инвентарь видел их по формату, а не по списку.
        """
        spec = bp.load_targets()[target]
        bundle = self.tmp / f"bundle-{target}"
        for rel in paths:
            entry = bundle.joinpath(*rel.split("/"))
            entry.parent.mkdir(parents=True, exist_ok=True)
            if rel == "_internal/Python.framework/Versions/3.14/Python" or rel == "keklib-cli":
                entry.write_bytes(b"\xcf\xfa\xed\xfesynthetic mach-o payload")
            elif rel == "keklib-cli.exe":
                entry.write_bytes(b"MZ" + b"\x00" * 0x3E + b"\xf0\x00\x00\x00PE\x00\x00")
            elif rel.endswith(".Info.plist") or rel == (
                "_internal/Python.framework/Versions/3.14/Resources/Info.plist"
            ):
                entry.write_text('<?xml version="1.0"?>\n', encoding="utf-8")
            else:
                entry.write_bytes(b"x")
        self.assertEqual(str(spec["executable"]), paths[0].split("/")[-1])
        return bundle

    def test_windows_composition_covered_by_real_manifest(self) -> None:
        bundle = self._synthetic_bundle("windows-x86_64", _WINDOWS_REPORT_FILES)
        inventory = bp.inventory_bundle_libraries(bundle, target="windows-x86_64")
        self.assertEqual(len(inventory), len(_WINDOWS_REPORT_FILES))
        bp.verify_license_closure(bundle, target="windows-x86_64")

    def test_macos_composition_covered_by_real_manifest(self) -> None:
        for target in ("macos-x86_64", "macos-arm64"):
            with self.subTest(target=target):
                bundle = self._synthetic_bundle(target, _MACOS_REPORT_FILES)
                bp.verify_license_closure(bundle, target=target)

    def test_notices_copied_per_target_without_linux_set(self) -> None:
        """Уведомления копируются по составу target; Linux-набор не подмешивается."""
        win = self._synthetic_bundle("windows-x86_64", _WINDOWS_REPORT_FILES)
        bp.verify_license_closure(win, target="windows-x86_64", copy_notices_to=win)
        for notice in (
            "licenses/cpython-win/LICENSE.txt",
            "licenses/ms-vcrt/NOTICE.txt",
            "licenses/zlib-ng/LICENSE.md",
            "licenses/openssl/LICENSE-3.5.9.txt",
            "licenses/sqlite/PUBLIC-DOMAIN.txt",
            "licenses/bzip2/LICENSE-1.0.8.txt",
            "licenses/libffi/LICENSE-3.4.4.txt",
            "licenses/mpdecimal/COPYRIGHT-4.0.0.txt",
            "licenses/zstd/LICENSE.txt",
            # F1: hash-модули Windows встроены в python314.dll — уведомление
            # HACL* обязано попасть в комплект вместе с core-компонентом.
            "licenses/hacl/LICENSE.txt",
        ):
            self.assertTrue((win / notice).is_file(), notice)
        # Linux-закрытие (Ubuntu provenance) не подмешивается в Windows.
        self.assertFalse((win / "licenses/zlib/copyright.txt").exists())
        self.assertFalse((win / "licenses/xz/copyright.txt").exists())

        mac = self._synthetic_bundle("macos-arm64", _MACOS_REPORT_FILES)
        bp.verify_license_closure(mac, target="macos-arm64", copy_notices_to=mac)
        for notice in (
            "licenses/cpython-mac/LICENSE.txt",
            "licenses/openssl/LICENSE-3.5.9.txt",
            "licenses/sqlite/PUBLIC-DOMAIN.txt",
            "licenses/zstd/LICENSE.txt",
            # F2: mac-recipe зависимости, а не Windows/Linux-тексты.
            "licenses/xz/COPYING-5.2.3.txt",
            "licenses/mpdecimal/COPYRIGHT-4.0.1.txt",
            "licenses/expat/COPYING.txt",
        ):
            self.assertTrue((mac / notice).is_file(), notice)
        # Windows-условия Microsoft не копируются в macOS-комплект; тексты
        # других платформ (vendored 2.5.1, Windows 4.0.0/5.2.5) не подмешиваются.
        self.assertFalse((mac / "licenses/ms-vcrt/NOTICE.txt").exists())
        self.assertFalse((mac / "licenses/mpdecimal/COPYING.txt").exists())
        self.assertFalse((mac / "licenses/mpdecimal/COPYRIGHT-4.0.0.txt").exists())
        self.assertFalse((mac / "licenses/xz/COPYING-5.2.5.txt").exists())

    def test_unknown_native_binary_blocks_gate(self) -> None:
        bundle = self._synthetic_bundle("windows-x86_64", _WINDOWS_REPORT_FILES)
        (bundle / "_internal" / "unknown-vendor.dll").write_bytes(b"x")
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.verify_license_closure(bundle, target="windows-x86_64")
        self.assertIn("_internal/unknown-vendor.dll", str(ctx.exception))

    @staticmethod
    def _manifest() -> dict[str, object]:
        return json.loads(
            (PROJECT_ROOT / "packaging" / "license-closure.json").read_text(
                encoding="utf-8"
            )
        )

    def test_windows_component_sets(self) -> None:
        coverage = self._manifest()["coverage"]

        def components(path: str) -> set[str]:
            value = coverage[path]
            return set([value] if isinstance(value, str) else value)

        self.assertEqual(
            components("keklib-cli.exe"), {"keklib-cli", "pyinstaller-bootloader"}
        )
        # python314.dll = CPython-core (включая статические части dtoa/getopt/
        # cfuhash/qsbr/secret-labs) + статически вкомпилированный zlib-ng
        # (zlib-модуля отдельным файлом в составе нет, ZLIB_VERSION
        # «1.3.1.zlib-ng» в отчёте runtime) + HACL*: официальный
        # PCbuild/pythoncore.vcxproj v3.14.8 компилирует Hacl_Hash_MD5/
        # SHA1/SHA2/SHA3, HMAC, Blake2 в pythoncore (hash-модули Windows
        # встроены в core DLL, а не отдельные _sha*.pyd) — F1 review.
        self.assertEqual(
            components("_internal/python314.dll"),
            {
                "cpython-win-runtime",
                "dtoa",
                "getopt",
                "cfuhash",
                "qsbr",
                "secret-labs",
                "zlib-ng",
                "hacl",
            },
        )
        self.assertEqual(
            components("_internal/VCRUNTIME140.dll"), {"ms-vcrt-python-dist"}
        )
        self.assertEqual(
            components("_internal/VCRUNTIME140_1.dll"), {"ms-vcrt-python-dist"}
        )
        self.assertEqual(
            components("_internal/_bz2.pyd"), {"cpython-win-runtime", "bzip2-1.0.8"}
        )
        self.assertEqual(
            components("_internal/_decimal.pyd"),
            {"cpython-win-runtime", "mpdecimal-4.0.0"},
        )
        self.assertEqual(
            components("_internal/_hashlib.pyd"),
            {"cpython-win-runtime", "openssl-3.5.9"},
        )
        self.assertEqual(
            components("_internal/_ssl.pyd"),
            {"cpython-win-runtime", "openssl-3.5.9"},
        )
        self.assertEqual(
            components("_internal/_lzma.pyd"), {"cpython-win-runtime", "xz-5.2.5"}
        )
        self.assertEqual(
            components("_internal/_zstd.pyd"), {"cpython-win-runtime", "zstd"}
        )
        self.assertEqual(components("_internal/sqlite3.dll"), {"sqlite-3.50.4"})
        self.assertEqual(components("_internal/libffi-8.dll"), {"libffi-3.4.4"})
        self.assertEqual(components("_internal/libcrypto-3.dll"), {"openssl-3.5.9"})
        self.assertEqual(components("_internal/libssl-3.dll"), {"openssl-3.5.9"})
        # Системные DLL Windows (api-ms-*, KERNEL32 и т.п.) в комплекте нет:
        # их появление — неизвестная позиция инвентаря и блокирует выпуск.

    def test_windows_notice_set_includes_hacl(self) -> None:
        """HACL*-уведомление реально копируется в Windows-комплект (F1).

        python314.dll несёт hacl-компонент ⇒ полный текст MIT-уведомления
        HACL* обязан оказаться в licenses/ скопированного комплекта, а не
        только существовать в репозитории.
        """
        win = self._synthetic_bundle("windows-x86_64", _WINDOWS_REPORT_FILES)
        bp.verify_license_closure(win, target="windows-x86_64", copy_notices_to=win)
        hacl = win / "licenses" / "hacl" / "LICENSE.txt"
        self.assertTrue(hacl.is_file(), "licenses/hacl/LICENSE.txt не скопирован")
        self.assertIn("INRIA", hacl.read_text(encoding="utf-8"))

    def test_macos_component_sets(self) -> None:
        """Состав macOS по фактическому официальному recipe python.org.

        Mac/BuildScript/build-installer.py v3.14.8: XZ 5.2.3 (строка 317),
        libmpdec 4.0.1 (строка 381) с ``--with-system-libmpdec``
        (строка 1161) — НЕ vendored 2.5.1 и НЕ внешняя 4.0.0 Windows-сборки.
        Runtime-отчёты versions этих статических зависимостей не измеряли
        (подтвердит следующий native inventory: decimal.__libmpdec_version__
        и ctypes-проба lzma_version_string).
        """
        coverage = self._manifest()["coverage"]

        def components(path: str) -> set[str]:
            self.assertIn(path, coverage, f"{path}: нет точного покрытия")
            value = coverage[path]
            return set([value] if isinstance(value, str) else value)

        # Framework-runtime: CPython-core + те же статические части исходника;
        # zlib статически НЕ входит (модуль zlib.so линкуется к системной
        # /usr/lib/libz.1.dylib, которая в комплекте не распространяется).
        self.assertEqual(
            components("_internal/Python.framework/Versions/3.14/Python"),
            {
                "cpython-mac-runtime",
                "dtoa",
                "getopt",
                "cfuhash",
                "qsbr",
                "secret-labs",
            },
        )
        # readline.so линкуется к системной /usr/lib/libedit.3.dylib (НЕ GNU
        # readline, НЕ распространяется в комплекте) — GNU-readline-компонент
        # на macOS не применяется и остаётся блокирующим для libreadline.so*.
        self.assertEqual(
            components("_internal/python3.14/lib-dynload/readline.cpython-314-darwin.so"),
            {"cpython-mac-runtime"},
        )
        # bzip2 на macOS — системная библиотека (в отличие от Windows, где
        # bzip2 статически в _bz2.pyd): компонент bzip2 не приписывается.
        self.assertEqual(
            components("_internal/python3.14/lib-dynload/_bz2.cpython-314-darwin.so"),
            {"cpython-mac-runtime"},
        )
        # F2: mac _lzma — статическая liblzma из XZ 5.2.3 официального
        # recipe (отдельный Windows-компонент xz-5.2.5 неприменим).
        self.assertEqual(
            components("_internal/python3.14/lib-dynload/_lzma.cpython-314-darwin.so"),
            {"cpython-mac-runtime", "xz-5.2.3"},
        )
        # F2: mac _decimal — системная (статическая) libmpdec 4.0.1 из recipe
        # c --with-system-libmpdec; это НЕ vendored 2.5.1 (linux) и НЕ
        # внешняя 4.0.0 Windows-сборки.
        self.assertEqual(
            components(
                "_internal/python3.14/lib-dynload/_decimal.cpython-314-darwin.so"
            ),
            {"cpython-mac-runtime", "libmpdec-4.0.1"},
        )
        self.assertEqual(
            components(
                "_internal/python3.14/lib-dynload/_sqlite3.cpython-314-darwin.so"
            ),
            {"cpython-mac-runtime", "sqlite-3.50.4"},
        )
        self.assertEqual(
            components("_internal/python3.14/lib-dynload/pyexpat.cpython-314-darwin.so"),
            {"cpython-mac-runtime", "expat"},
        )
        for module in ("_sha2", "_sha3", "_blake2", "_md5", "_sha1"):
            self.assertEqual(
                components(
                    f"_internal/python3.14/lib-dynload/{module}.cpython-314-darwin.so"
                ),
                {"cpython-mac-runtime", "hacl"},
                module,
            )
        self.assertEqual(components("_internal/libzstd.1.dylib"), {"zstd"})
        self.assertEqual(components("_internal/libcrypto.3.dylib"), {"openssl-3.5.9"})
        self.assertEqual(components("_internal/libssl.3.dylib"), {"openssl-3.5.9"})

    def test_linux_accepted_composition_still_covered(self) -> None:
        """Принятый linux-состав проходит гейт после ухода от wildcard (F3).

        Фикстура — точный библиотечный инвентарь принятого архива
        linux-x86_64 (см. _LINUX_ACCEPTED_FILES): перечисление путей не
        должно ни потерять известные позиции, ни изменить компоненты
        принятого Linux-закрытия.
        """
        bundle = self._synthetic_bundle("linux-x86_64", _LINUX_ACCEPTED_FILES)
        bp.verify_license_closure(bundle, target="linux-x86_64")

    def test_unknown_dynload_binaries_blocked(self) -> None:
        """Неизвестные бинари внутри lib-dynload блокируют гейт (F3).

        Раньше общий шаблон ``_internal/python*/lib-dynload/*`` выдавал
        PSF-разрешение любым потомкам (fnmatch не ограничивает ``*``
        компонентом пути): вложенный extensionless Mach-O
        ``vendor/unknown.bin`` и неизвестный ``.so`` проходили как
        cpython-runtime. После перечисления путей оба обязаны блокировать.
        """
        bundle = self._synthetic_bundle("macos-arm64", _MACOS_REPORT_FILES)
        dynload = bundle / "_internal" / "python3.14" / "lib-dynload"
        (dynload / "vendor").mkdir()
        (dynload / "vendor" / "unknown.bin").write_bytes(b"\xcf\xfa\xed\xfepayload")
        (dynload / "unknownlib.cpython-314-darwin.so").write_bytes(b"x")
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.verify_license_closure(bundle, target="macos-arm64")
        message = str(ctx.exception)
        self.assertIn("_internal/python3.14/lib-dynload/vendor/unknown.bin", message)
        self.assertIn(
            "_internal/python3.14/lib-dynload/unknownlib.cpython-314-darwin.so",
            message,
        )

    def test_manifest_dynload_coverage_is_enumerated_without_duplicates(self) -> None:
        """Контракт lib-dynload — только точные пути; дублей ключей нет (F3+minor).

        Шаблонов по lib-dynload быть не должно (неизвестный потомок не может
        наследовать PSF-разрешение); ключ ``keklib-cli.exe`` встречается в
        JSON-тексте ровно один раз (дубли ключей молча берутся последним
        значением и скрывают дрейф).
        """
        raw = (PROJECT_ROOT / "packaging" / "license-closure.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("lib-dynload/*", raw)
        self.assertEqual(raw.count('"keklib-cli.exe"'), 1)
        coverage = self._manifest()["coverage"]
        dynload_keys = [key for key in coverage if "lib-dynload" in key]
        self.assertTrue(dynload_keys)
        for key in dynload_keys:
            self.assertNotIn("*", key, f"шаблон вместо точного пути: {key}")

    def test_pe_predicate_has_no_dead_helper(self) -> None:
        """Мёртвый _is_pe удалён: предикат принимает любой MZ (minor)."""
        source = (PROJECT_ROOT / "tools" / "build_portable.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("_is_pe", source)

    def test_readline_gpl_still_blocks_and_libedit_distinction(self) -> None:
        """GNU readline остаётся запрещённым компонентом на любой платформе."""
        manifest = self._manifest()
        readline = manifest["components"]["readline-gpl"]
        self.assertIsNot(readline["distribution_ok"], True)
        self.assertEqual(manifest["coverage"]["_internal/libreadline.so*"], "readline-gpl")
        # Появление реального GNU readline-бинаря в macOS-комплекте блокирует.
        bundle = self._synthetic_bundle("macos-x86_64", _MACOS_REPORT_FILES)
        (bundle / "_internal" / "libreadline.so.8").write_bytes(b"x")
        with self.assertRaises(bp.PackagingError):
            bp.verify_license_closure(bundle, target="macos-x86_64")

    def test_native_notice_texts(self) -> None:
        """Уведомления platform-компонентов существуют и содержат ключевые тексты."""
        manifest = self._manifest()
        components = manifest["components"]
        needles = {
            "cpython-win-runtime": (
                "licenses/cpython-win/LICENSE.txt",
                ("Microsoft Distributable Code", "bzip2", "libffi",
                 "Python Software Foundation"),
            ),
            "cpython-mac-runtime": (
                "licenses/cpython-mac/LICENSE.txt",
                ("Python Software Foundation", "BeOpen", "CNRI"),
            ),
            "ms-vcrt-python-dist": (
                "licenses/ms-vcrt/NOTICE.txt",
                ("Microsoft Distributable Code", "microsoft.com"),
            ),
            "zlib-ng": ("licenses/zlib-ng/LICENSE.md", ("Jean-loup Gailly", "Mark Adler")),
            "openssl-3.5.9": (
                "licenses/openssl/LICENSE-3.5.9.txt",
                ("Apache License", "Version 2.0, January 2004"),
            ),
            "sqlite-3.50.4": ("licenses/sqlite/PUBLIC-DOMAIN.txt", ("public domain",)),
            "xz-5.2.5": ("licenses/xz/COPYING-5.2.5.txt", ("public domain",)),
            "bzip2-1.0.8": ("licenses/bzip2/LICENSE-1.0.8.txt", ("Julian R Seward",)),
            "libffi-3.4.4": ("licenses/libffi/LICENSE-3.4.4.txt", ("Anthony Green",)),
            "mpdecimal-4.0.0": ("licenses/mpdecimal/COPYRIGHT-4.0.0.txt", ("Stefan Krah",)),
            "zstd": ("licenses/zstd/LICENSE.txt", ("Meta Platforms",)),
            "xz-5.2.3": (
                "licenses/xz/COPYING-5.2.3.txt",
                ("liblzma is in the public domain",),
            ),
            "libmpdec-4.0.1": (
                "licenses/mpdecimal/COPYRIGHT-4.0.1.txt",
                ("Copyright (c) 2008-2025 Stefan Krah",),
            ),
        }
        for name, (notice, expected) in needles.items():
            with self.subTest(component=name):
                entry = components[name]
                self.assertIs(entry["distribution_ok"], True, name)
                self.assertEqual(entry["notice"], notice, name)
                text = (PROJECT_ROOT / "packaging" / notice).read_text(encoding="utf-8")
                for needle in expected:
                    self.assertIn(needle, text, f"{name}: {needle!r}")
        # macOS-файл LICENSE не содержит Windows-условий Microsoft.
        mac_text = (PROJECT_ROOT / "packaging" / "licenses/cpython-mac/LICENSE.txt").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("Microsoft Distributable Code", mac_text)
        # Все разрешённые новые компоненты имеют существующие файлы уведомлений.
        for name, entry in components.items():
            if entry.get("distribution_ok") is True:
                notice = entry.get("notice")
                self.assertIsInstance(notice, str, name)
                self.assertTrue((PROJECT_ROOT / "packaging" / notice).is_file(), name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
