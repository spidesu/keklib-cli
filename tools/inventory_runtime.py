"""Диагностическая runtime-инвентаризация pending-targets (native artifacts,
этап 1).

Инструмент собирает на нативной ОС/CPU одного из трёх pending-target
(Windows/macOS из ``packaging/targets.json``) диагностический onedir-комплект
и пишет один JSON-отчёт с фактическими фактами runtime: версии
Python/SQLite/FTS5/OpenSSL/zlib/libffi, фактические метаданные build-info,
полный побайтово хешированный инвентарь комплекта, нативные зависимости
библиотек (macOS ``otool -L`` по Mach-O, Windows ``pefile`` — импорты и
VERSION-ресурсы), provenance каждого файла по COLLECT TOC PyInstaller и
тексты LICENSE/LICENSE.txt из известных мест установленного runtime.
Сырой комплект и его архив не публикуются — единственный выход это отчёт.

Отчёт помечен ``diagnostic_only: true``: он не является acceptance, не
устанавливает ``native_build_verified``, не создаёт release-asset и не
заполняет пины ``packaging/toolchain.json`` (нулевые SQLite-пины остаются
нулевыми и фиксируются в отчёте как null). Выпускные гейты не вызываются и
не обходятся: их время — после закрепления пинов по этим фактам.

Переиспользуются примитивы ``tools/build_portable.py`` (алгоритмы не
копируются): проверка нативности target, preflight toolchain, запуск
PyInstaller, фактические метаданные build-info, самопроверка executable на
синтетической библиотеке в собственном sandbox и рекурсивный инвентарь
библиотек комплекта. Staging — собственный ``tempfile`` под ``build/`` и
удаляется целиком; отчёт публикуется вне staging атомарно и без перезаписи
существующего файла.

Схема 2 дополняет отчёт фактом ``build_environment`` (общий helper
``build_portable.pyinstaller_environment_facts``): фактический PATH
child-процесса PyInstaller против PATH родителя — доказательство
исключения посторонних каталогов (реальный случай Java_Temurin из hosted
toolcache, чьи ucrtbase/api-ms-win-* попадали в Windows COLLECT) на
нативном повторе.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import build_portable as bp  # noqa: E402

#: Pending-targets диагностического этапа: ровно три незакрытых target из
#: packaging/targets.json. Состав и runner-метки зафиксированы брифом;
#: совпадение с targets.json проверяется тестами. Linux уже принят
#: (native_build_verified) и сюда не входит.
PENDING_TARGETS = frozenset({"windows-x86_64", "macos-x86_64", "macos-arm64"})

REPORT_SCHEMA_VERSION = 2

_SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}")

#: Mach-O magic (LE/BE, 32/64-bit) и FAT-контейнеры.
_MACHO_MAGICS = (
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
)

#: Точные имена лицензионных файлов в известных местах runtime.
_LICENSE_FILE_NAMES = {"license", "license.txt"}


# ---------------------------------------------------------------------------
# Обязательные факты: source SHA, хост, runtime, preflight


def resolve_source_sha(explicit: str | None) -> str:
    """Source SHA коммита: явный аргумент или GITHUB_SHA, строго 40 hex.

    Факт происхождения сборки обязателен: без валидного SHA коммита отчёт
    не привязывается к исходникам и не создаётся.
    """
    raw = explicit if explicit is not None else os.environ.get("GITHUB_SHA")
    if raw is None or not str(raw).strip():
        raise bp.PackagingError(
            "source SHA обязателен: передайте --source-sha или переменную "
            "GITHUB_SHA (40 hex-символов коммита исходников)"
        )
    value = str(raw).strip().lower()
    if not _SOURCE_SHA_RE.fullmatch(value):
        raise bp.PackagingError(
            f"source SHA {str(raw).strip()!r} не является 40-символьным "
            "hex SHA коммита"
        )
    return value


def host_facts() -> dict[str, object]:
    """Фактические факты хоста; необязательные поля явно nullable."""
    system = platform.system()
    macos_version = platform.mac_ver()[0]
    return {
        "os": system,
        "arch_raw": platform.machine(),
        "arch": bp._normalize_machine(platform.machine()),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "macos_version": macos_version or None,
        "windows_release": platform.release() if system == "Windows" else None,
        # Поле явно nullable: ни один pending-target не является Linux.
        "glibc": None,
    }


def runtime_facts() -> dict[str, object]:
    """Факты замораживаемого runtime (тот же интерпретатор, что собирается).

    Версии компонентов не выдумываются: недоступный факт записывается
    ``None`` с пометкой, недоступный пакет — ``available: false``.
    """
    connection = sqlite3.connect(":memory:")
    try:
        options = [row[0] for row in connection.execute("PRAGMA compile_options")]
    finally:
        connection.close()

    try:
        import ssl
    except ImportError:  # pragma: no cover - дефектный runtime
        openssl = {"available": False, "openssl_version": None}
    else:
        openssl = {"available": True, "openssl_version": ssl.OPENSSL_VERSION}

    try:
        import zlib
    except ImportError:  # pragma: no cover - дефектный runtime
        zlib_facts = {"available": False, "version": None, "runtime_version": None}
    else:
        zlib_facts = {
            "available": True,
            "version": zlib.ZLIB_VERSION,
            "runtime_version": zlib.ZLIB_RUNTIME_VERSION,
        }

    libffi = {
        "available": importlib.util.find_spec("_ctypes") is not None,
        # Стандартная библиотека не раскрывает версию libffi — поле явно
        # nullable, фактическая версия выясняется нативными зависимостями.
        "version": None,
    }

    return {
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "base_prefix": sys.base_prefix,
        },
        "sqlite": {
            "version": sqlite3.sqlite_version,
            "fts5": "ENABLE_FTS5" in options,
            "compile_options": options,
        },
        "openssl": openssl,
        "zlib": zlib_facts,
        "libffi": libffi,
    }


def preflight_facts(target: str) -> dict[str, object]:
    """Прошедший preflight и закреплённые пины toolchain как есть.

    Нулевой SQLite-пин pending-target фиксируется ``null`` — это факт
    «закрепление не выполнено», а не значение для подстановки.
    """
    pinned = bp.load_toolchain()["targets"][target]  # type: ignore[index]
    return {
        "passed": True,
        "pins": {
            "python": pinned.get("python"),
            "pyinstaller": pinned.get("pyinstaller"),
            "setuptools": pinned.get("setuptools"),
            "sqlite": pinned.get("sqlite"),
        },
    }


# ---------------------------------------------------------------------------
# Инвентарь комплекта: полный с хешами, библиотеки с зависимостями и TOC


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def full_bundle_inventory(root: Path) -> list[dict[str, object]]:
    """Полный инвентарь: относительные POSIX-пути, тип, размер, SHA-256.

    Символические ссылки не разыменовываются: тип ``symlink`` с целью,
    без хеша (цель учитывается в собственном месте дерева).
    """
    entries: list[dict[str, object]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        current = Path(dirpath)
        relative = current.relative_to(root)
        for name in sorted(dirnames + filenames):
            entry = current / name
            rel = (relative / name).as_posix() if str(relative) != "." else name
            st = entry.lstat()
            if stat.S_ISLNK(st.st_mode):
                entries.append(
                    {
                        "path": rel,
                        "type": "symlink",
                        "size": st.st_size,
                        "sha256": None,
                        "link": os.readlink(entry),
                    }
                )
            elif stat.S_ISREG(st.st_mode):
                entries.append(
                    {
                        "path": rel,
                        "type": "file",
                        "size": st.st_size,
                        "sha256": sha256_file(entry),
                    }
                )
    return entries


def is_macho(head: bytes) -> bool:
    return head[:4] in _MACHO_MAGICS


def is_pe(head: bytes) -> bool:
    if head[:2] != b"MZ" or len(head) < 0x40:
        return False
    (offset,) = struct.unpack_from("<I", head, 0x3C)
    if offset < 0 or offset > len(head) - 4:
        return False
    return head[offset : offset + 4] == b"PE\x00\x00"


def macho_deps(path: Path) -> dict[str, object]:
    """Нативные зависимости Mach-O через ``otool -L`` (факт, не разбор)."""
    otool = shutil.which("otool")
    if otool is None:
        return {
            "tool": "otool -L",
            "available": False,
            "error": "otool не найден в PATH этой ОС",
        }
    try:
        result = subprocess.run(
            [otool, "-L", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"tool": "otool -L", "available": False, "error": str(exc)}
    if result.returncode != 0:
        return {
            "tool": "otool -L",
            "available": False,
            "returncode": result.returncode,
            "error": result.stderr.strip() or "otool завершился с ошибкой",
        }
    return {
        "tool": "otool -L",
        "available": True,
        "returncode": 0,
        "output": result.stdout,
    }


def _pe_string(value: object) -> str:
    """Строка VERSION-ресурса: ключи/значения pefile — bytes, декодируем явно.

    ``str(bytes)`` дал бы ``"b'FileVersion'"`` и потерю всех полей,
    поэтому bytes декодируются, прочее приводится к str как есть.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def pe_deps(path: Path) -> dict[str, object]:
    """Импорты DLL и VERSION-ресурсы PE-файла через pefile (если установлен)."""
    try:
        import pefile  # noqa: PLC0415 - сборочная зависимость только на Windows
    except ImportError:
        return {
            "tool": "pefile",
            "available": False,
            "error": "pefile не установлен в этом окружении",
        }
    try:
        pe = pefile.PE(str(path), fast_load=True)
    except Exception as exc:  # noqa: BLE001 - любые повреждения PE
        return {
            "tool": "pefile",
            "available": False,
            "error": f"PE не разбирается: {exc}",
        }
    facts: dict[str, object] = {"tool": "pefile", "available": True}
    imports: list[str] | None = None
    version: dict[str, object] | None = None
    try:
        pe.parse_data_directories(
            directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            ]
        )
        entries = getattr(pe, "DIRECTORY_ENTRY_IMPORT", None) or []
        imports = sorted(
            {
                entry.dll.decode("utf-8", "replace")
                for entry in entries
                if getattr(entry, "dll", None)
            }
        )
    except Exception:  # noqa: BLE001 - факты nullable, не причина отказа
        imports = None
    try:
        pe.parse_data_directories(
            directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"],
            ]
        )
        strings: dict[str, str] = {}
        # Фактическая форма API: FileInfo — список списков структур
        # (StringFileInfo/VarFileInfo); StringTable.entries — bytes.
        for info_list in getattr(pe, "FileInfo", None) or []:
            for info in info_list or []:
                for table in getattr(info, "StringTable", None) or []:
                    for key, value in (
                        getattr(table, "entries", None) or {}
                    ).items():
                        text_key = _pe_string(key)
                        if text_key not in strings:
                            strings[text_key] = _pe_string(value)
        if strings:
            version = {
                "file_version": strings.get("FileVersion"),
                "product_name": strings.get("ProductName"),
                "strings": strings,
            }
    except Exception:  # noqa: BLE001 - факты nullable, не причина отказа
        version = None
    try:
        pe.close()
    except Exception:  # noqa: BLE001 - уже разобранные факты не отменяются
        pass
    facts["imports"] = imports
    facts["version_info"] = version
    return facts


def native_binary_deps(path: Path) -> dict[str, object] | None:
    """Проба зависимостей нативного бинария по фактическому формату."""
    try:
        with path.open("rb") as stream:
            head = stream.read(512)
    except OSError:
        return None
    if is_macho(head):
        return macho_deps(path)
    if is_pe(head):
        return pe_deps(path)
    return None


def native_walk_paths(
    bundle: Path, files: list[dict[str, object]]
) -> list[str]:
    """Нативные бинари полного обхода по magic (страховка инвентаря сборки).

    Библиотечный предикат сборки распознаёт расширения (в любом регистре) и
    magic ELF/Mach-O/PE, включая extensionless Mach-O framework-runtime
    macOS вида ``_internal/Python.framework/Versions/3.14/Python``. Этот
    полный обход остаётся диагностической страховкой: любой бинарь, видимый
    здесь, но не в контракте лицензионного инвентаря, означает, что предикат
    надо расширять. symlink-записи не разыменовываются (цель учитывается в
    собственном месте дерева).
    """
    paths: list[str] = []
    for entry in files:
        if entry.get("type") != "file":
            continue
        path = bundle.joinpath(*str(entry["path"]).split("/"))
        try:
            with path.open("rb") as stream:
                head = stream.read(512)
        except OSError:
            continue
        if is_macho(head) or is_pe(head) or head[:4] == b"\x7fELF":
            paths.append(str(entry["path"]))
    return paths


def _toc_entries(data: object) -> list[tuple[str, str, str]]:
    """Записи TOC из python-literal файла PyInstaller (dest, src, typecode)."""
    while isinstance(data, tuple) and len(data) == 1:
        data = data[0]
    if not isinstance(data, (list, tuple)):
        return []
    entries: list[tuple[str, str, str]] = []
    for entry in data:
        if (
            isinstance(entry, (list, tuple))
            and len(entry) >= 3
            and all(isinstance(piece, str) for piece in entry[:3])
        ):
            entries.append((entry[0], entry[1], entry[2]))
    return entries


def collect_toc_sources(workpath: Path) -> dict[str, dict[str, str]]:
    """Provenance COLLECT TOC: путь в комплекте → исходный файл сборки.

    Закреплённый PyInstaller кладёт рабочие файлы не в сам ``--workpath``,
    а во вложенный каталог имени spec (``workpath = join(workpath,
    specnm)`` — ``building/build_main.py``); ищутся оба места: вложенное
    фактическое и, для полноты, сам workpath. Читаются только
    ``COLLECT-*.toc``; повреждённый или нечитаемый файл пропускается
    (факт nullable, не отказ).
    """
    roots = [workpath / bp.SPEC_FILE.stem, workpath]
    files: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for toc_file in sorted(root.glob("COLLECT-*.toc")):
            try:
                real = toc_file.resolve()
            except OSError:
                real = toc_file
            if real in seen:
                continue
            seen.add(real)
            files.append(toc_file)
    sources: dict[str, dict[str, str]] = {}
    for toc_file in files:
        try:
            data = ast.literal_eval(toc_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, SyntaxError, MemoryError, RecursionError):
            continue
        for dest, src, typecode in _toc_entries(data):
            normalized = dest.replace("\\", "/")
            sources.setdefault(
                normalized, {"dest": normalized, "src": src, "typecode": typecode}
            )
    return sources


#: Contents-каталог onedir PyInstaller (spec его не переопределяет —
#: значение по умолчанию закреплённой версии).
CONTENTS_DIRECTORY = "_internal"


def _provenance_for(
    path: str, toc: dict[str, dict[str, str]]
) -> dict[str, str] | None:
    """Provenance по правилам COLLECT, без неоднозначного basename.

    Исполняемый файл (EXECUTABLE/PKG) попадает в корень комплекта — dest
    совпадает с путём записи; все остальные назначения PyInstaller
    раскладывает под contents-каталогом, и в dest самого префикса нет —
    сопоставление отбрасывает префикс у пути из комплекта. fallback по
    одному basename запрещён: при коллизии имён в разных каталогах он
    подставил бы чужой источник вместо честного «неизвестно».
    """
    if path in toc:
        return toc[path]
    prefix = f"{CONTENTS_DIRECTORY}/"
    if path.startswith(prefix):
        stripped = path[len(prefix) :]
        if stripped in toc:
            return toc[stripped]
    return None


# ---------------------------------------------------------------------------
# Лицензионные тексты установленного runtime


def collect_runtime_licenses(
    roots: Sequence[Path] | None = None,
) -> list[dict[str, object]]:
    """LICENSE/LICENSE.txt из известных мест runtime, без обхода чужих путей.

    Обследуются только заданные корни (по умолчанию ``sys.base_prefix`` и
    каталог stdlib текущего интерпретатора), точные имена файлов, без
    рекурсии: дом, проект и произвольные деревья не обходятся. Каждый
    файл фиксируется путём, SHA-256, размером и содержимым.
    """
    if roots is None:
        candidates = [Path(sys.base_prefix)]
        stdlib = sysconfig.get_paths().get("stdlib")
        if stdlib:
            candidates.append(Path(stdlib))
    else:
        candidates = [Path(root) for root in roots]
    seen: set[Path] = set()
    collected: list[dict[str, object]] = []
    for root in candidates:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        try:
            names = sorted(os.listdir(resolved))
        except OSError:
            continue
        for name in names:
            if name.lower() not in _LICENSE_FILE_NAMES:
                continue
            entry = root / name
            try:
                st = entry.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            try:
                data = entry.read_bytes()
            except OSError:
                continue
            collected.append(
                {
                    "path": str(entry),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                    "text": data.decode("utf-8", errors="replace"),
                }
            )
    return collected


# ---------------------------------------------------------------------------
# Отчёт: сборка фактов и атомарная публикация без перезаписи


def build_report(
    target: str,
    *,
    sha: str,
    info: dict[str, object],
    files: list[dict[str, object]],
    libraries: list[dict[str, object]],
    licenses: list[dict[str, object]],
    build_environment: dict[str, object],
) -> dict[str, object]:
    spec = bp.load_targets()[target]  # type: ignore[index]
    from keklib_cli import __version__

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool": {"name": "inventory_runtime", "version": __version__},
        "diagnostic_only": True,
        "note": (
            "Диагностический отчёт инвентаризации pending-target: не является "
            "acceptance, не устанавливает native_build_verified, не создаёт "
            "release-asset и не заполняет пины toolchain.json. Факты "
            "предназначены для следующего этапа закрепления пинов и "
            "лицензионного закрытия. Библиотечный инвентарь сборки "
            "(recognized_by=\"library_inventory\") распознаёт бинари по "
            "расширению и magic ELF/Mach-O/PE, включая extensionless Mach-O "
            "framework runtime. Записи recognized_by=\"full_walk_magic\" — "
            "диагностическая страховка полного обхода для форматов, которые "
            "контракт лицензионного инвентаря ещё не распознаёт; их "
            "появление означает, что инвентарь сборки надо расширять."
        ),
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_sha": sha,
        "target": target,
        "target_spec": dict(spec),
        "host": host_facts(),
        "runtime": runtime_facts(),
        "build_environment": build_environment,
        "preflight": preflight_facts(target),
        "build_info": info,
        "selfcheck": {"passed": True},
        "bundle": {
            "root": bp.BUNDLE_ROOT,
            "file_count": len(files),
            "files": files,
        },
        "libraries": libraries,
        "runtime_licenses": licenses,
    }


def write_report(report: dict[str, object], output: Path, *, target: str) -> Path:
    """Атомарная публикация отчёта вне staging, без перезаписи существующего."""
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"inventory-report-{target}.json"
    if destination.exists():
        raise bp.PackagingError(
            f"отказ перезаписи: уже существует {destination}"
        )
    payload = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    handle, temp_name = tempfile.mkstemp(dir=output, prefix=f".inventory-{target}-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        bp._publish_no_replace(Path(temp_name), destination)
    except KeyboardInterrupt:
        bp._cleanup_temp(temp_name)
        raise
    except bp.PackagingError:
        bp._cleanup_temp(temp_name)
        raise
    except Exception as exc:
        bp._cleanup_temp(temp_name)
        raise bp.PackagingError(
            f"публикация отчёта {destination} не завершена: {exc}"
        ) from exc
    return destination


def _require_pending_target(target: str) -> None:
    if target not in PENDING_TARGETS:
        raise bp.PackagingError(
            f"target {target!r} не входит в pending-цели диагностического "
            f"этапа {', '.join(sorted(PENDING_TARGETS))}; диагностическая "
            "инвентаризация остальных target не выполняется"
        )


def inventory_runtime(
    target: str, *, source_sha: str | None, output: Path
) -> Path:
    """Диагностическая инвентаризация одного pending-target на нативной ОС/CPU.

    Порядок обязательных проверок: pending-цель → source SHA → нативность
    target → preflight toolchain; любая неудача останавливает работу до
    тяжёлых стадий. Отчёт — единственный выход: комплект не упаковывается,
    не публикуется и удаляется вместе со staging.
    """
    _require_pending_target(target)
    sha = resolve_source_sha(source_sha)
    bp.assert_native_target(target)
    print(f"[inventory] preflight toolchain: {target}", file=sys.stderr)
    bp.verify_toolchain_preflight(target)

    targets = bp.load_targets()
    executable = str(targets[target]["executable"])  # type: ignore[index]

    bp.BUILD_DIR.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f"inventory-{target}-", dir=bp.BUILD_DIR)
    )
    try:
        dist = staging / "dist"
        work = staging / "work"
        # Факты контроля окружения собираются ДО прогона тем же общим
        # helper'ом, которым пользуется _run_pyinstaller: в отчёте —
        # фактический PATH child-процесса и PATH родителя.
        build_environment = bp.pyinstaller_environment_facts()
        print(f"[inventory] диагностический onedir: {target}", file=sys.stderr)
        bp._run_pyinstaller(dist, work)
        bundle = dist / bp.BUNDLE_ROOT
        if not (bundle / executable).is_file():
            raise bp.PackagingError(
                f"после сборки отсутствует исполняемый файл {bundle / executable}"
            )
        info = bp.build_info(target)
        print(
            f"[inventory] самопроверка executable ({executable}) в sandbox",
            file=sys.stderr,
        )
        bp._run_selfcheck(bundle / executable, staging / "selfcheck")

        print("[inventory] инвентарь и факты runtime", file=sys.stderr)
        files = full_bundle_inventory(bundle)
        inventory = bp.inventory_bundle_libraries(bundle, target=target)
        inventory_set = set(inventory)
        supplemented = sorted(inventory_set | set(native_walk_paths(bundle, files)))
        toc = collect_toc_sources(work)
        libraries = []
        for rel in supplemented:
            libraries.append(
                {
                    "path": rel,
                    # Переиспользованный библиотечный инвентарь или
                    # диагностическое дополнение по magic (без расширения).
                    "recognized_by": (
                        "library_inventory"
                        if rel in inventory_set
                        else "full_walk_magic"
                    ),
                    "toc_source": _provenance_for(rel, toc),
                    "deps": native_binary_deps(bundle.joinpath(*rel.split("/"))),
                }
            )
        licenses = collect_runtime_licenses()
        report = build_report(
            target,
            sha=sha,
            info=info,
            files=files,
            libraries=libraries,
            licenses=licenses,
            build_environment=build_environment,
        )
        print(f"[inventory] публикация отчёта в {output}", file=sys.stderr)
        return write_report(report, Path(output), target=target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: диагностический отчёт одного pending-target."""
    parser = argparse.ArgumentParser(
        prog="inventory_runtime",
        description=(
            "Диагностическая runtime-инвентаризация pending-target "
            "(diagnostic_only, не acceptance): факты для закрепления пинов."
        ),
    )
    parser.add_argument(
        "--target",
        required=True,
        choices=sorted(bp.load_targets()),
        help="pending-target из packaging/targets.json (нативная ОС/CPU)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="каталог отчёта (не очищается; без перезаписи существующего)",
    )
    parser.add_argument(
        "--source-sha",
        default=None,
        metavar="SHA",
        help="40-hex SHA коммита исходников (по умолчанию GITHUB_SHA)",
    )
    args = parser.parse_args(argv)
    try:
        report = inventory_runtime(
            args.target, source_sha=args.source_sha, output=args.output
        )
    except bp.PackagingError as exc:
        print(f"inventory_runtime: ошибка: {exc}", file=sys.stderr)
        return 1
    print(report.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
