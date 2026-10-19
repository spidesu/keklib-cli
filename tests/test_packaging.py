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
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
                expected.add(
                    "keklib-cli" + ("/" + str(rel_dir / d) if str(rel_dir) != "." else "/" + d)
                )
            for f in sorted(filenames):
                rel = str(rel_dir / f) if str(rel_dir) != "." else f
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

    # 9. onedir и console: не onefile и не GUI.

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
            os.symlink(secret, internal / "runtime.dat")  # внешняя цель
            with self.assertRaises(bp.PackagingError) as ctx:
                bp.archive_bundle(
                    bundle, self.tmp / ("out-" + name), version="0.0.0-test", target=target
                )
            self.assertIn("_internal/runtime.dat", str(ctx.exception))
        dangling = _make_bundle_dir(self.tmp / "dangling")
        os.symlink("no-such-target.so", dangling / "_internal" / "runtime.dat")
        with self.assertRaises(bp.PackagingError):
            bp.archive_bundle(
                dangling, self.tmp / "out-dangling", version="0.0.0-test", target="linux-x86_64"
            )
        escaping = _make_bundle_dir(self.tmp / "escaping")
        os.symlink("../../../outside-secret", escaping / "_internal" / "runtime.dat")
        with self.assertRaises(bp.PackagingError) as ctx:
            bp.archive_bundle(
                escaping, self.tmp / "out-escaping", version="0.0.0-test", target="linux-x86_64"
            )
        self.assertIn("покидает", str(ctx.exception))

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
