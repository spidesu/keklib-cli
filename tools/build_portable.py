"""Сборка portable-комплектов keklib-cli (задача 11 плана).

Одна точка входа для нативной сборки: ``python tools/build_portable.py
--target TARGET --output DIR``. Target обязан совпадать с реальной
ОС/CPU машины (кросс-сборка запрещена: каждый комплект собирается на
своей платформе), staging создаётся собственным ``tempfile`` под
``build/`` и не затрагивает чужие каталоги.

Стадии ``build_portable`` (каждая — fail-closed гейт):

1. проверка target против нативной платформы и ``packaging/targets.json``;
2. **preflight toolchain**: фактический CPython и установленные build-зависимости
   сверяются с пинами ``packaging/toolchain.json`` и
   ``packaging/requirements-build.lock`` до запуска PyInstaller;
3. PyInstaller onedir-сборка по ``packaging/keklib-cli.spec`` в staging;
   на Windows child-процесс получает ограниченный PATH (только каталог
   интерпретатора, его ``Scripts``, ``System32`` и ``SystemRoot``) —
   закреплённый PyInstaller ищет DLL по всем каталогам PATH процесса, и
   посторонние записи hosted-окружения затягивают свои DLL в комплект
   (см. ``pyinstaller_subprocess_env``);
4. README.md/INSTALL.md/THIRD_PARTY_NOTICES.md и ``build-info.json`` с
   фактическими версиями runtime;
5. самопроверка готового executable: ``--help`` и полная индексация
   синтетической библиотеки через реальную точку входа (индексация
   использует FTS5 — это проверка включённого SQLite runtime);
6. для Linux — **ELF-гейт нижней границы glibc** по всем ELF-файлам
   комплекта: требования читаются из динамического сегмента
   (``PT_DYNAMIC``/``DT_VERNEED``), а при его отсутствии — из секций
   типа ``SHT_GNU_verneed`` независимо от их имени; повреждённые
   цепочки/границы и неподтверждаемые ELF (усечённые, неподдерживаемый
   класс/порядок байт, без таблиц) — отказ, а не пропуск; провайдер
   verneed не может скрыть требование GLIBC; превышение floor
   останавливает публикацию точным списком файлов;
7. **release-гейт метаданных runtime**: SQLite-pin в toolchain.json
   обязан быть зафиксирован (``null`` блокирует принятый релиз) и совпадать
   с фактической версией включённого SQLite; версии python/pyinstaller в
   build-info обязаны совпадать с пинами;
8. **лицензионный гейт**: инвентарь фактических библиотек комплекта
   (исполнимый файл, ``base_library.zip``, общие библиотриеки верхнего
   уровня ``_internal``) сверяется с ``packaging/license-closure.json``:
   каждая позиция должна быть покрыта, помечена ``distribution_ok`` и
   снабжена существующим файлом уведомления; уведомления копируются в
   ``licenses/`` комплекта;
9. упаковка tar.gz/zip с корнем ``keklib-cli/`` и контрольной суммой
   SHA-256.

Состав архива ограничен allowlist (исполнимый файл, ``_internal``,
README.md, INSTALL.md, THIRD_PARTY_NOTICES.md, build-info.json,
опционально ``licenses/``); каталоги книг, INPX, индексы, настройки,
тесты и секреты отвергаются. Все записи дерева проверяются по типу:
регулярные файлы и каталоги, символические ссылки/reparse-точки — только
при цели внутри комплекта (containment по реальному разрешению пути);
внешние, битые и покидающие комплект ссылки запрещены, специальные файлы
(FIFO/socket/device) запрещены. tar хранит внутренние ссылки ссылками с
относительным именем цели; ZIP материализует внутренние ссылки файлов
байтами цели (посторонние байты в архив попасть не могут), ссылки на
каталоги в ZIP непредставимы и отвергаются.

Протокол публикации пары «архив + .sha256»: оба временных файла
готовятся и fsync-ятся ДО первой публикации, затем атомарно без
перезаписи публикуется архив и рядом сумма. Опубликованный архив
**никогда не удаляется** — ни при отказе публикации суммы, ни при сбое
(равенство байтов не доказывает владение файлом, а удаление чужой записи
недопустимо). Прерванная пара ремонтируется отдельной явной операцией
``--repair-checksum ARCHIVE --expected-sha256 DIGEST``: байты архива
проверяются против digest, отсутствующая сумма создаётся атомарно без
перезаписи; удалений нет, операция не подтверждает provenance и не
является приёмом выпуска. Повторная сборка по-прежнему отказывается
перезаписывать существующий архив. Пара не транзакционна при сбое
процесса между публикациями (может остаться архив без суммы) —
crash-transaction гарантия не реализована и не заявляется.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import gzip
import hashlib
import importlib.metadata
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
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PureWindowsPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = PROJECT_ROOT / "packaging"
TARGETS_FILE = PACKAGING_DIR / "targets.json"
TOOLCHAIN_FILE = PACKAGING_DIR / "toolchain.json"
LICENSE_MANIFEST = PACKAGING_DIR / "license-closure.json"
SPEC_FILE = PACKAGING_DIR / "keklib-cli.spec"
DOCS = ("README.md", "INSTALL.md")
NOTICES = "THIRD_PARTY_NOTICES.md"
BUILD_DIR = PROJECT_ROOT / "build"
BUNDLE_ROOT = "keklib-cli"

TARGETS_SCHEMA_VERSION = 1
TOOLCHAIN_SCHEMA_VERSION = 1
LICENSE_SCHEMA_VERSION = 2

#: Записи корня комплекта: обязательные и допустимые.
_REQUIRED_TOP = {"_internal", "README.md", "INSTALL.md", "build-info.json", NOTICES}
_ALLOWED_TOP = _REQUIRED_TOP | {"LICENSE", "LICENSE.txt", "LICENSE.md", "NOTICE", "licenses"}

#: Посторонние файлы: каталоги книг, состояние, тесты, секреты.
#: Denylist имён — один из слоёв; он не доказывает отсутствие секретов
#: под произвольными именами и не претендует на это.
_FORBIDDEN_NAMES = (
    "*.inpx",
    "*.fb2",
    "config.json",
    "*.sqlite3",
    "*.sqlite3-journal",
    "*.sqlite3-wal",
    "*.sqlite3-shm",
    "index.lock",
    "report.json",
    "smoke-report.json",
    "test_*.py",
    "*_test.py",
    "*.pem",
    "*.key",
    "*.secret",
)
_FORBIDDEN_DIRS = {"tests", "src", ".git", "__pycache__", "build", "dist", ".venv"}
_ALLOWED_ZIPS = {"base_library.zip"}

#: Аргументы PyInstaller: onedir по умолчанию, без подтверждений и кеша.
PYINSTALLER_ARGS = ("--noconfirm", "--clean")

#: Публикация одного файла без перезаписи; модульный псевдоним внедряется
#: тестами протокола пары архив+сумма.
from keklib_cli.platforms import publish_no_replace as _publish_no_replace  # noqa: E402

# --- Константы ELF -----------------------------------------------------------

_ELF_MAGIC = b"\x7fELF"
_ELFCLASS64 = 2
_ELFDATA2LSB = 1
_SHT_GNU_VERNEED = 0x6FFFFFFE
_PT_LOAD = 1
_PT_DYNAMIC = 2
_DT_NULL = 0
_DT_STRTAB = 5
_DT_STRSZ = 10
_DT_VERNEED = 0x6FFFFFFE
_DT_VERNEEDNUM = 0x6FFFFFFF


class PackagingError(Exception):
    """Сборка или упаковка не может быть продолжена безопасно."""


# ---------------------------------------------------------------------------
# Целевые платформы


def load_targets() -> dict[str, dict[str, object]]:
    """Прочитать и проверить packaging/targets.json."""
    try:
        data = json.loads(TARGETS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackagingError(f"нечитаем файл целей {TARGETS_FILE}: {exc}") from exc
    if data.get("schema_version") != TARGETS_SCHEMA_VERSION:
        raise PackagingError(
            f"неизвестная схема targets.json: {data.get('schema_version')!r}"
        )
    targets = data.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise PackagingError("targets.json не содержит описаний target")
    for name, spec in targets.items():
        for key in (
            "system",
            "machine",
            "runner",
            "runner_cpu",
            "baseline_os",
            "minimum_os",
            "minimum_glibc",
            "archive_format",
            "executable",
        ):
            if key not in spec:
                raise PackagingError(f"target {name}: отсутствует поле {key}")
    return targets


def load_toolchain() -> dict[str, object]:
    """Прочитать и проверить packaging/toolchain.json."""
    try:
        data = json.loads(TOOLCHAIN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackagingError(f"нечитаем toolchain {TOOLCHAIN_FILE}: {exc}") from exc
    if data.get("schema_version") != TOOLCHAIN_SCHEMA_VERSION:
        raise PackagingError(
            f"неизвестная схема toolchain.json: {data.get('schema_version')!r}"
        )
    targets = data.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise PackagingError("toolchain.json не содержит описаний target")
    for name, entry in targets.items():
        for key in ("python", "pyinstaller", "setuptools", "sqlite"):
            if key not in entry:
                raise PackagingError(f"toolchain {name}: отсутствует поле {key}")
    return data


def _normalize_machine(machine: str) -> str:
    value = machine.lower()
    if value in ("x86_64", "amd64"):
        return "x86_64"
    if value in ("arm64", "aarch64"):
        return "arm64"
    return value


def current_target(targets: dict[str, dict[str, object]] | None = None) -> str | None:
    """Определить target нативной платформы или ``None``, если его нет в матрице."""
    targets = targets if targets is not None else load_targets()
    system = platform.system()
    machine = _normalize_machine(platform.machine())
    for name, spec in targets.items():
        if spec["system"] == system and _normalize_machine(str(spec["machine"])) == machine:
            return name
    return None


def assert_native_target(target: str) -> None:
    """Запретить кросс-сборку: target обязан совпадать с реальной ОС/CPU."""
    targets = load_targets()
    if target not in targets:
        raise PackagingError(
            f"неизвестный target {target!r}; доступные: {', '.join(sorted(targets))}"
        )
    native = current_target(targets)
    if native != target:
        raise PackagingError(
            f"target {target} не совпадает с нативной платформой "
            f"({platform.system()}/{platform.machine()} → {native or 'нет в матрице'}); "
            "кросс-сборка запрещена: каждый комплект собирается на своей ОС/CPU"
        )


# ---------------------------------------------------------------------------
# Метаданные сборки


def build_info(target: str) -> dict[str, object]:
    """Фактические метаданные сборки для build-info.json.

    Версии Python/SQLite берутся из текущего интерпретатора (тот же, что
    замораживается PyInstaller), версия PyInstaller — из метаданных
    установленного пакета. Отсутствие PyInstaller или FTS5 в SQLite —
    ошибка сборки, а не пустое поле.
    """
    targets = load_targets()
    if target not in targets:
        raise PackagingError(f"неизвестный target {target!r}")
    try:
        pyinstaller = importlib.metadata.version("PyInstaller")
    except importlib.metadata.PackageNotFoundError as exc:
        raise PackagingError(
            "PyInstaller не установлен: установите сборочные зависимости "
            "из packaging/requirements-build.lock"
        ) from exc
    connection = sqlite3.connect(":memory:")
    try:
        options = [row[0] for row in connection.execute("PRAGMA compile_options")]
    finally:
        connection.close()
    fts5 = "ENABLE_FTS5" in options
    sqlite_version = sqlite3.sqlite_version
    if not fts5:
        raise PackagingError(
            "включённый SQLite не собран с FTS5 (PRAGMA compile_options без "
            "ENABLE_FTS5): portable-комплект требует FTS5"
        )
    if not sqlite_version or sqlite_version == "0":
        raise PackagingError("невнятная версия SQLite встроенного runtime")
    from keklib_cli import __version__
    from keklib_cli.index_store import RULES_VERSION, SCHEMA_VERSION

    return {
        "version": __version__,
        "target": target,
        "python": platform.python_version(),
        "sqlite": sqlite_version,
        "sqlite_fts5": True,
        "pyinstaller": pyinstaller,
        "schema_version": SCHEMA_VERSION,
        "rules_version": RULES_VERSION,
        "minimum_os": str(targets[target]["minimum_os"]),
        "minimum_glibc": targets[target]["minimum_glibc"],
    }


# ---------------------------------------------------------------------------
# Preflight toolchain и release-гейт метаданных runtime


def lock_pins(lock_path: Path | None = None) -> dict[str, str]:
    """Точные ==-пины применимых к текущей платформе записей lock-файла."""
    try:
        from packaging.requirements import Requirement
    except ImportError as exc:  # pragma: no cover - packaging сам есть в lock
        raise PackagingError(
            "packaging не установлен: lock-файл не разбирается (это тоже "
            "сборочная зависимость из packaging/requirements-build.lock)"
        ) from exc
    path = lock_path if lock_path is not None else PACKAGING_DIR / "requirements-build.lock"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PackagingError(f"нечитаем lock {path}: {exc}") from exc

    def canonical(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    pins: dict[str, str] = {}
    logical = ""
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
        requirement_text = entry.split("--hash")[0].strip()
        try:
            requirement = Requirement(requirement_text)
        except Exception as exc:  # noqa: BLE001 - разбор произвольного текста
            raise PackagingError(
                f"lock: неразборная запись {requirement_text!r}: {exc}"
            ) from exc
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        specs = list(requirement.specifier)
        if len(specs) != 1 or specs[0].operator != "=" + "=":
            raise PackagingError(
                f"lock: {requirement.name} без единственного точного == пина"
            )
        pins[canonical(requirement.name)] = specs[0].version
    if logical.strip():
        raise PackagingError(f"lock {path}: запись обрывается continuation-строкой")
    return pins


def verify_toolchain_preflight(
    target: str,
    *,
    toolchain: dict[str, object] | None = None,
    lock_path: Path | None = None,
    python_version: str | None = None,
    distribution_version: Callable[[str], str] | None = None,
) -> None:
    """Фактический CPython и build-зависимости обязаны совпадать с пинами.

    Проверка выполняется до PyInstaller: расхождение интерпретатора или
    любой применимой записи ``requirements-build.lock`` (включая полное
    отсутствие пакета) — отказ сборки, а не молчаливая замена toolchain.
    ``toolchain.json`` и lock дополнительно сверяются между собой по
    pyinstaller/setuptools, чтобы пины не разошлись в двух файлах.
    """
    chain = toolchain if toolchain is not None else load_toolchain()
    entries = chain.get("targets")
    if not isinstance(entries, dict) or target not in entries:
        raise PackagingError(f"toolchain.json не описывает target {target!r}")
    pinned = entries[target]
    pins = lock_pins(lock_path)

    problems: list[str] = []
    actual_python = python_version if python_version is not None else platform.python_version()
    if actual_python != pinned["python"]:
        problems.append(f"CPython {actual_python} != пин {pinned['python']}")

    resolve = distribution_version if distribution_version is not None else importlib.metadata.version
    for name, version in sorted(pins.items()):
        try:
            actual = resolve(name)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"{name}: не установлен (пин {version})")
        except Exception as exc:  # noqa: BLE001 - любая ошибка метаданных
            problems.append(f"{name}: версия не определяется: {exc}")
        else:
            if actual != version:
                problems.append(f"{name} {actual} != пин {version}")

    for key in ("pyinstaller", "setuptools"):
        toolchain_pin = pinned.get(key)
        lock_pin = pins.get(key)
        if toolchain_pin is not None and lock_pin is not None and toolchain_pin != lock_pin:
            problems.append(
                f"toolchain.json {key}={toolchain_pin} расходится с lock {lock_pin}"
            )
    if problems:
        raise PackagingError(
            "toolchain не соответствует закреплённым пинам:\n  "
            + "\n  ".join(problems)
        )


def verify_release_metadata(
    target: str,
    info: dict[str, object],
    *,
    toolchain: dict[str, object] | None = None,
    sqlite_version: str | None = None,
) -> None:
    """Гейт метаданных принимаемого к выпуску runtime.

    SQLite-pin в ``toolchain.json`` обязан быть зафиксирован: ``null``
    означает, что проверяемая фиксация включённого SQLite не выполнена, и
    принятый релиз блокируется (fail-closed) — выдумывать значение нельзя,
    pin заполняется только фактической инвентаризацией на согласованном
    builder. Непустое значение сверяется с фактической версией включённого
    SQLite и с build-info.json.
    """
    chain = toolchain if toolchain is not None else load_toolchain()
    entries = chain.get("targets")
    if not isinstance(entries, dict) or target not in entries:
        raise PackagingError(f"toolchain.json не описывает target {target!r}")
    pinned = entries[target]
    runtime_sqlite = (
        sqlite_version if sqlite_version is not None else sqlite3.sqlite_version
    )
    pin = pinned.get("sqlite")
    if pin is None:
        raise PackagingError(
            f"SQLite runtime pin не зафиксирован в toolchain.json для target {target}: "
            "принятый к выпуску комплект блокируется (fail-closed). Pin заполняется "
            "только фактической версией включённого SQLite на согласованном builder; "
            "выдуманное значение недопустимо (review task-11, ruling progress.md)"
        )
    problems: list[str] = []
    if pin != runtime_sqlite:
        problems.append(f"SQLite runtime {runtime_sqlite} != пин {pin}")
    if info.get("sqlite") != runtime_sqlite:
        problems.append(
            f"build-info sqlite {info.get('sqlite')!r} != runtime {runtime_sqlite!r}"
        )
    if info.get("python") != pinned["python"]:
        problems.append(
            f"build-info python {info.get('python')!r} != пин {pinned['python']!r}"
        )
    if info.get("pyinstaller") != pinned["pyinstaller"]:
        problems.append(
            f"build-info pyinstaller {info.get('pyinstaller')!r} "
            f"!= пин {pinned['pyinstaller']!r}"
        )
    if problems:
        raise PackagingError(
            "метаданные runtime не соответствуют закреплённому toolchain:\n  "
            + "\n  ".join(problems)
        )


# ---------------------------------------------------------------------------
# Лицензионный гейт по фактическому составу


#: Mach-O magic (LE/BE, 32/64-bit) и FAT-контейнеры — тот же набор, что в
#: диагностическом tools/inventory_runtime.py; лицензионный инвентарь сборки
#: обязан видеть extensionless Mach-O (framework-runtime macOS вида
#: ``_internal/Python.framework/Versions/3.14/Python``) сам, а не только в
#: диагностическом отчёте.
_MACHO_MAGICS = (
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
)


def _is_macho(head: bytes) -> bool:
    return head[:4] in _MACHO_MAGICS


def _is_macho(head: bytes) -> bool:
    return head[:4] in _MACHO_MAGICS


def _library_like_file(entry: Path, name: str) -> bool:
    """Библиотечный файл по расширению (без учёта регистра) или binary-magic.

    Расширения сопоставляются в нижнем регистре: реальные имена Windows-
    состава регистрозависимы (``VCRUNTIME140.dll``, ``libcrypto-3.dll``, а
    архиваторы не нормализуют регистр), и пропуск ``SOMELIB.DLL`` выпускал
    бы binary без лицензионного покрытия. Безрасширенированные бинари
    распознаются по magic аналогично ELF: Mach-O (все варианты magic,
    включая FAT-контейнеры) и PE — предикат принимает любой файл с
    DOS-magic ``MZ`` (подтверждаемая PE-сигнатура не требуется): файл с
    «MZ», но без полной PE-структуры — всё равно binary, гейт решает
    явным покрытием, а не молчаливым пропуском (fail-closed).
    """
    lowered = name.lower()
    if fnmatch.fnmatch(lowered, "*.so") or fnmatch.fnmatch(lowered, "*.so.*"):
        return True
    if lowered.endswith((".dll", ".dylib", ".pyd", ".exe")):
        return True
    if name == "base_library.zip":
        return True
    try:
        with entry.open("rb") as stream:
            head = stream.read(0x40)
    except OSError:
        return False
    if head[:4] == b"\x7fELF" or _is_macho(head):
        return True
    return head[:2] == b"MZ"


def inventory_identifier(relative: Path, name: str) -> str:
    """Контрактный идентификатор файла инвентаря — относительный POSIX-путь.

    На любой ОС (включая Windows-разделители в `relative`) идентификатор
    нормализуется к POSIX-форме, совпадающей с ключами контракта
    покрытия манифеста.
    """
    return (relative / name).as_posix()


def inventory_bundle_libraries(bundle: Path, *, target: str) -> list[str]:
    """Рекурсивный фактический состав библиотек комплекта.

    Идентификаторы — относительные POSIX-пути от корня комплекта
    (однозначные, в отличие от голых имён файлов; на Windows тоже
    нормализуются к `/` — см. ``inventory_identifier``). Обходится всё
    дерево: вложенные библиотеки (например `_internal/vendor/libx.so.1`)
    не могут быть пропущены. Учитываются: исполнимый файл target, общие
    библиотеки, модули расширений и исполнимые файлы по расширению в
    любом регистре (`.so*`/`.dll`/`.dylib`/`.pyd`/`.exe`), `base_library.zip`
    и файлы с
    binary-magic без библиотечного расширения — ELF, Mach-O (включая
    FAT-контейнеры и extensionless framework-runtime macOS) и PE (`MZ`).
    Символические ссылки не разыменовываются: их допустимость
    проверяет containment-гейт упаковки, а цель учитывается в собственном
    месте дерева. Составные runtime (CPython: libpython + lib-dynload +
    base_library.zip) и составной executable не считаются автоматически
    закрытыми — их покрытие задаётся явным контрактом манифеста
    (в т.ч. набором компонентов на один путь).
    """
    executable = str(load_targets()[target]["executable"])
    names = {executable}
    for dirpath, dirnames, filenames in os.walk(bundle, followlinks=False):
        dirnames.sort()
        current = Path(dirpath)
        relative = current.relative_to(bundle)
        for name in sorted(filenames):
            entry = current / name
            if not stat.S_ISREG(entry.lstat().st_mode):
                continue  # ссылки и особые файлы — не часть инвентаря
            if _library_like_file(entry, name):
                names.add(inventory_identifier(relative, name))
    return sorted(names)


def _coverage_components(value: object) -> list[str]:
    """Компоненты записи покрытия: одно имя или непустой набор имён.

    Составные файлы (например executable, встраивающий загрузчик
    PyInstaller) выражаются списком компонентов — каждый из них обязателен
    для этого пути. Контракт плоский (путь → набор имён), имена ссылаются
    только на ``components``: циклы в этой модели невозможны по построению.
    """
    if isinstance(value, str) and value:
        return [value]
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, str) and item for item in value)
    ):
        return sorted(set(value))
    raise PackagingError(
        f"некорректная запись покрытия {value!r}: ожидается имя компонента "
        "или непустой список имён"
    )


def verify_license_closure(
    bundle: Path,
    *,
    target: str,
    manifest_path: Path | None = None,
    notices_root: Path | None = None,
    copy_notices_to: Path | None = None,
) -> None:
    """Лицензионное закрытие фактического состава комплекта (fail-closed).

    Манифест (schema 2) — статический контракт покрытия: ``components``
    описывает компоненты (лицензия, файл уведомления, строго булев
    ``distribution_ok``), ``coverage`` сопоставляет относительные
    POSIX-пути (точные или fnmatch-шаблоны) одному компоненту **или
    набору компонентов**: составные файлы — например executable =
    собственный код + встроенный загрузчик PyInstaller — требуют явного
    разрешения каждого включённого компонента. Гейт требует, чтобы каждая
    позиция рекурсивного инвентаря была покрыта контрактом однозначно, а
    все компоненты её набора были подтверждены: ``distribution_ok``
    обязан быть JSON ``true`` (строка/число не принимаются), объявленный
    файл уведомления обязан существовать и копируется в комплект.
    Неиспользуемые компоненты (другие платформы, будущие библиотеки)
    проверяться не обязаны. Неизвестные вложенные библиотеки и
    непокрытые составные runtime блокируют выпуск; битые записи покрытия
    и ссылки контракта на отсутствующие компоненты — ошибка схемы
    (контракт плоский: путь → набор имён, циклы в нём невозможны).
    Статически включённые сторонние части CPython не считаются закрытыми
    одной записью PSF: фактическое закрытие состава runtime — отдельное
    evidence. Заполнение ``distribution_ok: true`` — явное решение по
    фактическому составу принятого комплекта, не автоматический вывод.
    """
    manifest_file = manifest_path if manifest_path is not None else LICENSE_MANIFEST
    notices = Path(notices_root) if notices_root is not None else PACKAGING_DIR
    inventory = inventory_bundle_libraries(bundle, target=target)
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackagingError(
            f"лицензионный манифест {manifest_file} не читается: {exc}"
        ) from exc
    components = manifest.get("components") if isinstance(manifest, dict) else None
    coverage = manifest.get("coverage") if isinstance(manifest, dict) else None
    if manifest.get("schema_version") != LICENSE_SCHEMA_VERSION or not isinstance(
        components, dict
    ) or not isinstance(coverage, dict):
        raise PackagingError(
            f"лицензионный манифест {manifest_file} повреждён "
            "(schema_version=2/components/coverage)"
        )
    try:
        declared = {key: _coverage_components(value) for key, value in coverage.items()}
    except PackagingError as exc:
        raise PackagingError(f"{manifest_file}: {exc}") from exc
    dangling = sorted(
        {name for names in declared.values() for name in names if name not in components}
    )
    if dangling:
        raise PackagingError(
            "контракт покрытия ссылается на отсутствующие компоненты: "
            + ", ".join(dangling)
        )
    patterns = [pattern for pattern in declared if any(ch in pattern for ch in "*?[")]

    problems: list[str] = []
    mapped: dict[str, tuple[str, ...]] = {}
    for path in inventory:
        if path in declared:
            mapped[path] = tuple(declared[path])
            continue
        matched = [pattern for pattern in patterns if fnmatch.fnmatch(path, pattern)]
        if not matched:
            problems.append(
                f"{path}: нет покрытия в лицензионном контракте "
                "(неизвестная/вложенная библиотека)"
            )
        elif len(matched) > 1:
            problems.append(f"{path}: неоднозначное покрытие: {sorted(matched)}")
        else:
            mapped[path] = tuple(declared[matched[0]])
    if problems:
        raise PackagingError(
            "лицензионное закрытие не покрывает фактический состав комплекта:\n  "
            + "\n  ".join(problems)
        )

    problems = []
    for path, path_components in sorted(mapped.items()):
        for component in path_components:
            entry = components[component]
            flag = entry.get("distribution_ok")
            if type(flag) is not bool or flag is not True:
                problems.append(
                    f"{path} → {component}: distribution_ok обязан быть JSON true "
                    f"(bool, получено {flag!r}); лицензия: {entry.get('license', '?')}"
                )
            notice = entry.get("notice")
            if notice:
                if not isinstance(notice, str) or not notice.startswith("licenses/"):
                    problems.append(f"{path}: notice {notice!r} вне licenses/")
                elif not (notices / notice).is_file():
                    problems.append(f"{path}: файл уведомления отсутствует: {notice}")
    if problems:
        raise PackagingError(
            "лицензионное закрытие не завершено, публикация заблокирована "
            "(fail-closed):\n  " + "\n  ".join(problems)
        )
    if copy_notices_to is not None:
        for component in sorted({name for names in mapped.values() for name in names}):
            notice = components[component].get("notice")
            if not notice:
                continue
            destination = Path(copy_notices_to) / Path(notice)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(notices / notice, destination)


# ---------------------------------------------------------------------------
# ELF-сканер нижней границы glibc


def _cstr(table: bytes, offset: int, name: str, what: str) -> str:
    """Строка C из таблицы со строгой проверкой границ."""
    if offset < 0 or offset >= len(table):
        raise PackagingError(f"{name}: {what} {offset} вне таблицы строк")
    end = table.find(b"\x00", offset)
    if end < 0:
        end = len(table)
    return table[offset:end].decode("latin-1")


def _parse_verneed_chain(
    data: bytes,
    name: str,
    dynstr: bytes,
    offset: int,
    end: int,
    *,
    expected_count: int | None,
) -> tuple[int, ...]:
    """Максимум GLIBC-версий одной области Verneed со строгими проверками.

    ``expected_count`` — обязательное количество записей Verneed
    (``DT_VERNEEDNUM``) или ``None`` для секционного варианта, где
    количество определяется границами секции. Проверяются: версия Verneed,
    ``vn_cnt >= 1``, полный проход цепочки Vernaux ровно по ``vn_cnt``
    записей, границы всех смещений и длина цепочки Verneed.
    """
    required: tuple[int, ...] = ()
    position = offset
    seen = 0
    while True:
        if position + 16 > end:
            raise PackagingError(f"{name}: обрезанная запись Verneed")
        vn_version, vn_cnt, vn_file, vn_aux, vn_next = struct.unpack_from(
            "<HHIII", data, position
        )
        if vn_version != 1:
            raise PackagingError(f"{name}: неизвестная версия Verneed {vn_version}")
        if vn_cnt < 1:
            raise PackagingError(f"{name}: Verneed с vn_cnt=0")
        provider = _cstr(dynstr, vn_file, name, "смещение имени провайдера")
        aux = position + vn_aux
        if vn_aux < 16 or aux + 16 > end:
            raise PackagingError(
                f"{name}: vn_aux=0x{vn_aux:x} выводит цепочку Vernaux "
                f"провайдера {provider} за границы области"
            )
        for index in range(vn_cnt):
            if aux + 16 > end:
                raise PackagingError(
                    f"{name}: цепочка Vernaux провайдера {provider} обрезана "
                    f"({index} из {vn_cnt})"
                )
            _hash, _flags, _other, vna_name, vna_next = struct.unpack_from(
                "<IHHII", data, aux
            )
            symbol = _cstr(dynstr, vna_name, name, "смещение имени символа")
            if symbol.startswith("GLIBC_"):
                # Провайдер не может скрыть требование: любая GLIBC-версия
                # в любом Verneed учитывается.
                parts = _parse_glibc_symbol(name, symbol)
                if parts > required:
                    required = parts
            last = index == vn_cnt - 1
            if last and vna_next != 0:
                raise PackagingError(
                    f"{name}: цепочка Vernaux провайдера {provider} короче "
                    f"vn_cnt={vn_cnt}"
                )
            if not last and vna_next < 16:
                raise PackagingError(
                    f"{name}: vna_next=0 внутри цепочки Vernaux провайдера "
                    f"{provider} (заявлено {vn_cnt})"
                )
            aux += vna_next
        seen += 1
        if expected_count is not None and seen >= expected_count and vn_next != 0:
            raise PackagingError(
                f"{name}: цепочка Verneed длиннее заявленных {expected_count} записей"
            )
        if vn_next == 0:
            if expected_count is not None and seen != expected_count:
                raise PackagingError(
                    f"{name}: Verneed меньше заявленных {expected_count} "
                    f"записей ({seen})"
                )
            break
        position += vn_next
    return required


def _scan_dynamic_verneed(
    data: bytes, name: str, phoff: int, phentsize: int, phnum: int
) -> tuple[int, ...] | None:
    """Требования из PT_DYNAMIC (семантика загрузчика).

    ``None`` — PT_DYNAMIC отсутствует (вызывающий код пробует секции).
    '' Пустой кортеж — PT_DYNAMIC есть, но DT_VERNEED отсутствует
    (версионных требований нет).
    """
    loads: list[tuple[int, int, int]] = []
    dynamic: tuple[int, int] | None = None
    for index in range(phnum):
        base = phoff + index * phentsize
        p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz, _memsz, _align = (
            struct.unpack_from("<IIQQQQQQ", data, base)
        )
        if p_type == _PT_LOAD:
            loads.append((p_vaddr, p_filesz, p_offset))
        elif p_type == _PT_DYNAMIC:
            dynamic = (p_offset, p_filesz)
    if dynamic is None:
        return None

    def vaddr_to_offset(vaddr: int, what: str) -> int:
        for base, filesz, offset in loads:
            if base <= vaddr < base + filesz:
                return offset + (vaddr - base)
        raise PackagingError(f"{name}: {what} vaddr {vaddr:#x} не покрыт PT_LOAD")

    strtab_vaddr: int | None = None
    strsz: int | None = None
    verneed_vaddr: int | None = None
    verneednum: int | None = None
    position, dynamic_size = dynamic
    end = position + dynamic_size
    if dynamic_size <= 0:
        raise PackagingError(
            f"{name}: PT_DYNAMIC пуст или усечён (p_filesz={dynamic_size})"
        )
    if dynamic_size % 16 != 0:
        raise PackagingError(
            f"{name}: PT_DYNAMIC усечён (p_filesz={dynamic_size} не кратен записи 16)"
        )
    if end > len(data):
        raise PackagingError(
            f"{name}: PT_DYNAMIC выходит за пределы файла "
            f"(off={position}, size={dynamic_size})"
        )
    saw_null = False
    while position + 16 <= end:
        tag, value = struct.unpack_from("<QQ", data, position)
        position += 16
        if tag == _DT_NULL:
            saw_null = True
            break
        if tag == _DT_STRTAB:
            strtab_vaddr = value
        elif tag == _DT_STRSZ:
            strsz = value
        elif tag == _DT_VERNEED:
            verneed_vaddr = value
        elif tag == _DT_VERNEEDNUM:
            verneednum = value
    if not saw_null:
        raise PackagingError(
            f"{name}: динамическая таблица не завершена DT_NULL — требования "
            "не могут быть подтверждены"
        )
    if verneed_vaddr is None:
        if verneednum is not None:
            raise PackagingError(
                f"{name}: DT_VERNEEDNUM без DT_VERNEED — противоречивая таблица"
            )
        return ()
    if strtab_vaddr is None or strsz is None:
        raise PackagingError(f"{name}: DT_VERNEED без DT_STRTAB/DT_STRSZ")
    if not isinstance(verneednum, int) or verneednum < 1:
        raise PackagingError(f"{name}: DT_VERNEEDNUM отсутствует или невалиден")
    strtab_off = vaddr_to_offset(strtab_vaddr, "DT_STRTAB")
    dynstr = data[strtab_off : strtab_off + strsz]
    if len(dynstr) < strsz:
        raise PackagingError(f"{name}: DT_STRSZ выходит за пределы файла")
    verneed_off = vaddr_to_offset(verneed_vaddr, "DT_VERNEED")
    return _parse_verneed_chain(
        data,
        name,
        dynstr,
        verneed_off,
        end=len(data),
        expected_count=verneednum,
    )


def _scan_section_verneed(
    data: bytes, name: str, shoff: int, shentsize: int, shnum: int
) -> tuple[int, ...]:
    """Требования из секций типа SHT_GNU_verneed — независимо от имени секции.

    Имя секции не определяет семантику для загрузчика: переименованная
    секция не снимает требование. Секционный путь используется, когда
    динамический сегмент недоступен (синтетика/специальные файлы).
    """
    if shentsize < 64:
        raise PackagingError(f"{name}: e_shentsize={shentsize} меньше заголовка секции")
    if shoff + shentsize * shnum > len(data):
        raise PackagingError(f"{name}: таблица секций за пределами файла")

    headers = []
    for index in range(shnum):
        base = shoff + index * shentsize
        _name, sh_type, _flags, _addr, sh_offset, sh_size, sh_link, _info, _align, _ent = (
            struct.unpack_from("<IIQQQQIIQQ", data, base)
        )
        if sh_type == _SHT_GNU_VERNEED:
            headers.append((sh_offset, sh_size, sh_link))
    if not headers:
        return ()
    required: tuple[int, ...] = ()
    for sh_offset, sh_size, sh_link in headers:
        if sh_size < 16 or sh_offset + sh_size > len(data):
            raise PackagingError(
                f"{name}: секция verneed повреждена или обрезана "
                f"(off={sh_offset}, size={sh_size})"
            )
        if sh_link >= shnum:
            raise PackagingError(f"{name}: verneed ссылается на секцию {sh_link} вне таблицы")
        str_base = shoff + sh_link * shentsize
        _n, _t, _f, _a, str_offset, str_size, _l, _i, _al, _e = struct.unpack_from(
            "<IIQQQQIIQQ", data, str_base
        )
        if str_offset + str_size > len(data):
            raise PackagingError(f"{name}: таблица строк verneed за пределами файла")
        dynstr = data[str_offset : str_offset + str_size]
        found = _parse_verneed_chain(
            data, name, dynstr, sh_offset, sh_offset + sh_size, expected_count=None
        )
        if found > required:
            required = found
    return required


def scan_elf_max_glibc(path: Path) -> tuple[int, ...] | None:
    """Максимальная требуемая версия GLIBC_x.y в ELF64 LSB-файле.

    Требования читаются по семантике ELF: сначала динамический сегмент
    (``PT_DYNAMIC`` → ``DT_VERNEED``/``DT_VERNEEDNUM`` — то, что использует
    загрузчик), при его отсутствии — секции типа ``SHT_GNU_verneed``
    независимо от имени. ``None`` — файл вообще не ELF; пустой кортеж —
    ELF без требований версий glibc. Любой ELF, чьи требования не
    удаётся доказать (усечённый заголовок, неподдерживаемый класс или
    порядок байт, отсутствие и program-, и section-таблиц, повреждённые
    цепочки/границы), отвергается ``PackagingError``: граница совместимости
    обязана fail-closed, молчаливый пропуск недопустим.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PackagingError(f"не читается ELF-файл {path}: {exc}") from exc
    # ELF-magic определяется отдельно от минимальной длины: файл из одних
    # четырёх байт magic — это усечённый ELF, а не «не ELF».
    if data[:4] != _ELF_MAGIC:
        return None
    if len(data) < 64:
        raise PackagingError(f"{path.name}: усеченный ELF-заголовок ({len(data)} байт)")
    if data[4] != _ELFCLASS64:
        raise PackagingError(
            f"{path.name}: неподдерживаемый ELF-class {data[4]}: требования glibc "
            "не могут быть подтверждены"
        )
    if data[5] != _ELFDATA2LSB:
        raise PackagingError(
            f"{path.name}: неподдерживаемый порядок байт ELF ({data[5]}): требования "
            "glibc не могут быть подтверждены"
        )
    try:
        (e_phoff,) = struct.unpack_from("<Q", data, 0x20)
        (e_shoff,) = struct.unpack_from("<Q", data, 0x28)
        e_phentsize, e_phnum = struct.unpack_from("<HH", data, 0x36)
        e_shentsize, e_shnum, _e_shstrndx = struct.unpack_from("<HHH", data, 0x3A)
        if e_phnum:
            if e_phentsize < 56:
                raise PackagingError(f"{path.name}: e_phentsize={e_phentsize} меньше phdr")
            if e_phoff + e_phentsize * e_phnum > len(data):
                raise PackagingError(f"{path.name}: таблица program-заголовков обрезана")
            required = _scan_dynamic_verneed(data, path.name, e_phoff, e_phentsize, e_phnum)
            if required is not None:
                return required
        if e_shnum:
            return _scan_section_verneed(data, path.name, e_shoff, e_shentsize, e_shnum)
        raise PackagingError(
            f"{path.name}: нет ни program-заголовков с PT_DYNAMIC, ни таблицы секций: "
            "требования glibc не могут быть подтверждены"
        )
    except struct.error as exc:
        raise PackagingError(f"{path.name}: повреждённые структуры ELF: {exc}") from exc


def _parse_glibc_symbol(file_name: str, symbol: str) -> tuple[int, ...]:
    """Числовая версия из имени GLIBC_*: 2.38 → (2, 38).

    ``GLIBC_ABI_DT_RELR`` — ABI-маркер формата DT_RELR, появившийся в
    glibc 2.36: файл с таким требованием не запустится на glibc < 2.36 и
    учитывается как требование (2, 36). ``GLIBC_PRIVATE`` — приватный ABI
    конкретной сборки glibc (внутренние символы libc/ld): файл в составе
    комплекта с таким требованием привязан к одной сборке glibc и
    совместимость с baseline не подтверждается — отказ. Прочие нечисловые
    имена — отказ разбора (fail-closed): неизвестный маркер нельзя молча
    считать совместимым.
    """
    if symbol == "GLIBC_ABI_DT_RELR":
        return (2, 36)
    if symbol == "GLIBC_PRIVATE":
        raise PackagingError(
            f"{file_name}: требование GLIBC_PRIVATE — приватный ABI конкретной "
            "сборки glibc; совместимость с baseline не подтверждается"
        )
    value = symbol[len("GLIBC_") :]
    try:
        return tuple(int(piece) for piece in value.split("."))
    except ValueError as exc:
        raise PackagingError(
            f"{file_name}: неизвестное имя версии glibc {symbol!r}; "
            "совместимость не подтверждается, требуется ручной разбор"
        ) from exc


def _parse_glibc(version: str) -> tuple[int, ...]:
    return tuple(int(piece) for piece in version.split("."))


def verify_glibc_floor(root: Path, floor: str) -> list[str]:
    """Список нарушений floor среди всех файлов каталога (пустой — чисто)."""
    floor_version = _parse_glibc(floor)
    violations: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            required = scan_elf_max_glibc(path)
            if required and required > floor_version:
                violations.append(
                    f"{path.relative_to(root)}: требуется "
                    f"GLIBC_{'.'.join(str(p) for p in required)} "
                    f"(floor {floor})"
                )
    return violations


# ---------------------------------------------------------------------------
# Самопроверка готового executable на синтетической библиотеке


_STRUCTURE_FIELDS = (
    "AUTHOR",
    "GENRE",
    "TITLE",
    "SERIES",
    "SERNO",
    "FILE",
    "SIZE",
    "LIBID",
    "DEL",
    "EXT",
    "DATE",
    "LANG",
)
_SELFCHECK_ROW = {
    "AUTHOR": "Булгаков Михаил",
    "GENRE": "russian_prose",
    "TITLE": "Мастер и Маргарита (синтетика самопроверки сборки)",
    "SERIES": "",
    "SERNO": "",
    "FILE": "66372",
    "SIZE": "1024",
    "LIBID": "66372",
    "DEL": "0",
    "EXT": "fb2",
    "DATE": "",
    "LANG": "ru",
}


def _write_selfcheck_library(root: Path) -> None:
    """Синтетическая библиотека для прогона готового executable (без тестов приложения)."""
    archives = root / "lib.rus.ec"
    archives.mkdir(parents=True)
    row = "\x04".join(_SELFCHECK_ROW[field] for field in _STRUCTURE_FIELDS) + "\x04\n"
    structure = ";".join(_STRUCTURE_FIELDS) + ";"
    stamp = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(
        root / "librusec_flib.inpx", "w", zipfile.ZIP_STORED
    ) as catalog:
        catalog.writestr(zipfile.ZipInfo("structure.info", stamp), structure.encode("utf-8"))
        catalog.writestr(zipfile.ZipInfo("selfcheck.inp", stamp), row.encode("utf-8"))
    with zipfile.ZipFile(archives / "selfcheck.zip", "w", zipfile.ZIP_STORED) as book:
        book.writestr(
            zipfile.ZipInfo("66372.fb2", stamp), b"synthetic fb2 payload for selfcheck"
        )


def _selfcheck_env(sandbox: Path) -> dict[str, str]:
    """Окружение child-процесса: состояние изолировано в sandbox, без Python-переменных."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PYTHON")
    }
    home = sandbox / "home"
    for directory in (
        home,
        home / ".config",
        home / ".cache",
        sandbox / "tmp",
        home / "AppData" / "Roaming",
        home / "AppData" / "Local",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    if sys.platform == "win32":
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
        env["TEMP"] = str(sandbox / "tmp")
        env["TMP"] = str(sandbox / "tmp")
    else:
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["XDG_CACHE_HOME"] = str(home / ".cache")
        env["TMPDIR"] = str(sandbox / "tmp")
    return env


def _run_selfcheck(exe: Path, sandbox: Path) -> None:
    """Прогнать реальную точку входа: --help, setup, index (FTS5), наличие индекса."""
    from keklib_cli.platforms import state_paths

    env = _selfcheck_env(sandbox)
    library = sandbox / "library"
    library.mkdir()
    _write_selfcheck_library(library)

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(exe), *args],
            env=env,
            cwd=str(sandbox),
            capture_output=True,
            text=True,
            timeout=300,
        )

    def require(result: subprocess.CompletedProcess[str], step: str) -> None:
        if result.returncode != 0:
            raise PackagingError(
                f"самопроверка executable провалилась на шаге {step} "
                f"(код {result.returncode}); stderr: {result.stderr.strip()}"
            )

    require(run("--help"), "--help")
    require(run("setup", "--library", str(library)), "setup")
    require(run("index", "--refresh"), "index --refresh (FTS5)")
    expected_index = state_paths(env=env, home=Path(env["HOME"])).index
    if not expected_index.is_file():
        raise PackagingError(
            f"самопроверка: индекс не создан по ожидаемому пути {expected_index}"
        )


# ---------------------------------------------------------------------------
# PyInstaller


#: Разделитель PATH целевой ОС Windows-ветки (не хоста: ветка Windows-only,
#: и её результат обязан быть корректен при вычислении на любой ОС).
_WINDOWS_PATH_SEPARATOR = ";"


def _windows_system_root(environ: Mapping[str, str]) -> str:
    """SystemRoot из окружения: обязательное нативное Windows-имя, без догадок.

    Поиск регистронезависимый (нативный ``os.environ`` на Windows
    безрегистровый; словари тестов могут нести любой регистр). Значение
    обязано быть абсолютным путём Windows (диск/UNC, не drive-relative) —
    ``C:\\Windows`` никогда не подставляется предположением.
    """
    raw = None
    for key, value in environ.items():
        if key.upper() == "SYSTEMROOT":
            raw = value
            break
    if raw is None or not str(raw).strip():
        raise PackagingError(
            "переменная окружения SystemRoot отсутствует или пуста: доверенные "
            "каталоги System32/SystemRoot для PATH child-процесса PyInstaller "
            "не выводятся из предположений (C:\\Windows не угадывается); "
            "исправьте окружение сборки"
        )
    root = PureWindowsPath(str(raw))
    if not root.is_absolute():
        raise PackagingError(
            f"SystemRoot {raw!r} не является абсолютным путём Windows "
            "(drive-relative/относительное значение): доверенные каталоги "
            "System32/SystemRoot не выводятся из предположений; исправьте "
            "окружение сборки"
        )
    return str(root)


def pyinstaller_subprocess_env(
    *,
    environ: Mapping[str, str] | None = None,
    executable: str | None = None,
    windows: bool | None = None,
) -> dict[str, str] | None:
    """Окружение child-процесса PyInstaller: Windows — ограниченный PATH.

    Причина (закреплённый PyInstaller 6.22.3, ``depend/bindepend.py``):
    на Windows разрешение DLL fallback-ом проходит по **всем** каталогам
    ``PATH`` процесса (``resolve_library_path``: ``win_search_paths`` из
    ``getenv('PATH')``), а ``building/build_main.py`` дополнительно
    трактует унаследованный PATH как базу DLL-поиска при импорте
    собираемых пакетов. Hosted PATH с посторонними каталогами (реальный
    случай native-run-39260511786: ``Java_Temurin .../bin`` из hosted
    toolcache) затягивает чужие ``ucrtbase.dll``/``api-ms-win-*`` в
    COLLECT — с пруфаненсом в каждой ``toc_source`` записи.

    Поэтому на Windows PATH child-процесса ограничен четырьмя доверенными
    записями в точном порядке: каталог ``sys.executable``
    (``pythonXY.dll``, ``vcruntime140*.dll``), его ``Scripts``,
    ``SystemRoot\\System32`` и ``SystemRoot`` (UCRT: ``ucrtbase.dll`` и
    ``api-ms-win-*`` — PyInstaller рассчитывает находить их именно через
    системные записи PATH). Прочие потребности разрешения библиотек
    pinned PyInstaller удовлетворяет сам, без PATH: каталог каждого
    анализируемого бинаря — первая позиция поиска
    (``_get_imports_pefile``), покрывающая и ``DLLs`` установленного
    Python.

    ``None`` (не-Windows): окружение родителя наследуется без изменений —
    ``subprocess.run(env=None)``. Глобальный ``os.environ`` не меняется
    ни на одной ОС; результат — свежая копия окружения с заменённым
    только ``PATH``. ``windows=None`` означает реальную платформу
    (``sys.platform == "win32"``); явное значение — для тестов ветки.
    ``executable`` обязан быть абсолютным путём Windows: относительный
    или drive-relative интерпретатор — отказ, а не молчаливый запуск.
    """
    if windows is None:
        windows = sys.platform == "win32"
    if not windows:
        return None
    source = dict(os.environ if environ is None else environ)
    exe = sys.executable if executable is None else executable
    exe_path = PureWindowsPath(exe)
    if not exe_path.is_absolute():
        raise PackagingError(
            f"интерпретатор {exe!r} не является абсолютным путём Windows: "
            "запуск PyInstaller с относительным/недоопределённым "
            "интерпретатором не детерминирован и запрещён"
        )
    system_root = _windows_system_root(source)
    entries = (
        str(exe_path.parent),
        str(exe_path.parent / "Scripts"),
        str(PureWindowsPath(system_root) / "System32"),
        system_root,
    )
    env = dict(source)
    env["PATH"] = _WINDOWS_PATH_SEPARATOR.join(entries)
    return env


def pyinstaller_environment_facts(
    *,
    environ: Mapping[str, str] | None = None,
    executable: str | None = None,
    windows: bool | None = None,
) -> dict[str, object]:
    """Факты контроля окружения сборки для диагностического отчёта.

    Переиспользуется ``tools/inventory_runtime.py``: отчёт фиксирует
    фактический PATH child-процесса PyInstaller и PATH родителя, чтобы
    нативный повтор доказал исключение посторонних каталогов (враждебный
    ``PATH`` родителя остаётся в фактах как есть — рядом с ограниченным
    ``PATH`` child-процесса).
    """
    source = os.environ if environ is None else environ
    parent_path = source.get("PATH", "")
    env = pyinstaller_subprocess_env(
        environ=environ, executable=executable, windows=windows
    )
    exe = sys.executable if executable is None else executable
    restricted = env is not None
    system_root = _windows_system_root(env) if restricted else None
    return {
        "platform": sys.platform,
        "policy": "windows-restricted-path" if restricted else "inherit-parent",
        "python_executable": exe,
        "parent_path": parent_path,
        "subprocess_path": env["PATH"] if restricted else parent_path,
        "subprocess_path_entries": (
            env["PATH"].split(_WINDOWS_PATH_SEPARATOR) if restricted else []
        ),
        "system_root": system_root,
    }


def _run_pyinstaller(distpath: Path, workpath: Path) -> None:
    """Нативная onedir-сборка по spec в staging-каталоги.

    На Windows child-процесс получает ограниченный PATH (см.
    ``pyinstaller_subprocess_env``): закреплённый PyInstaller ищет DLL по
    всем каталогам PATH процесса, и посторонние записи hosted-окружения
    затягивают свои DLL в комплект. Остальные ОС наследуют окружение
    родителя без изменений (``env=None``). Интерпретатор запускается
    только по абсолютному пути ``sys.executable``.
    """
    if not Path(sys.executable).is_absolute():
        raise PackagingError(
            f"sys.executable {sys.executable!r} не абсолютный: запуск "
            "PyInstaller с относительным интерпретатором запрещён"
        )
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        *PYINSTALLER_ARGS,
        f"--distpath={distpath}",
        f"--workpath={workpath}",
        str(SPEC_FILE),
    ]
    result = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        env=pyinstaller_subprocess_env(),
    )
    if result.returncode != 0:
        raise PackagingError(
            "PyInstaller завершился с ошибкой "
            f"(код {result.returncode}); stderr: {result.stderr.strip()[-4000:]}"
        )


# ---------------------------------------------------------------------------
# Проверка состава, ссылки/reparse, упаковка


def _link_like(st: os.stat_result) -> bool:
    """Символьная ссылка или reparse-точка (Windows) по lstat-данным."""
    if stat.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _require_contained_link(entry: Path, bundle_real: Path, rel: str) -> bool:
    """Ссылка обязана существовать и вести внутрь комплекта; возвращает, каталог ли цель."""
    try:
        resolved = entry.resolve(strict=True)
    except OSError as exc:
        raise PackagingError(f"битая ссылка {rel}: {exc}") from exc
    try:
        resolved.relative_to(bundle_real)
    except ValueError:
        raise PackagingError(
            f"ссылка {rel} покидает комплект: {resolved}"
        ) from None
    return resolved.is_dir()


def _validate_bundle(bundle: Path, target: str, version: str) -> None:
    """Allowlist корня, типы/containment всех записей и запрет посторонних файлов."""
    targets = load_targets()
    spec = targets[target]
    executable = str(spec["executable"])

    top = sorted(entry.name for entry in bundle.iterdir())
    unexpected = [name for name in top if name not in _ALLOWED_TOP | {executable}]
    if unexpected:
        raise PackagingError(
            "недопустимые записи в корне комплекта "
            f"{bundle}: {', '.join(unexpected)}; допустимы только "
            f"{executable}, _internal, README.md, INSTALL.md, "
            f"{NOTICES}, build-info.json, licenses/"
        )
    missing = sorted(name for name in _REQUIRED_TOP if name not in top)
    if executable not in top:
        missing.append(executable)
    if missing:
        raise PackagingError(f"в комплекте {bundle} отсутствуют: {', '.join(missing)}")
    for entry in bundle.iterdir():
        if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
            raise PackagingError(f"корень комплекта содержит не файл/каталог: {entry.name}")

    # Полный обход дерева: типы записей и containment ссылок/reparse.
    bundle_real = bundle.resolve()
    special: list[str] = []
    for dirpath, dirnames, filenames in os.walk(bundle, followlinks=False):
        current = Path(dirpath)
        relative = current.relative_to(bundle)
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            entry = current / name
            # Диагностика — относительный POSIX-путь на любой ОС (как и
            # контрактные идентификаторы инвентаря): на Windows native
            # разделители не должны менять текст отказов.
            rel = (relative / name).as_posix() if str(relative) != "." else name
            st = entry.lstat()
            if _link_like(st):
                _require_contained_link(entry, bundle_real, rel)
            elif not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                special.append(rel)
    if special:
        raise PackagingError(
            "комплект содержит записи особого типа (FIFO/socket/device и т.п.):\n  "
            + "\n  ".join(special)
        )

    violations: list[str] = []
    for dirpath, dirnames, filenames in os.walk(bundle):
        current = Path(dirpath)
        for name in sorted(set(dirnames) & _FORBIDDEN_DIRS):
            violations.append(
                f"запрещённый каталог {name}: "
                f"{(current / name).relative_to(bundle).as_posix()}"
            )
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(".zip") and name not in _ALLOWED_ZIPS:
                violations.append(
                    f"посторонний ZIP (допустим только base_library.zip): "
                    f"{(current / name).relative_to(bundle).as_posix()}"
                )
                continue
            matched = [pattern for pattern in _FORBIDDEN_NAMES if fnmatch.fnmatch(name, pattern)]
            if matched:
                violations.append(
                    f"запрещённый файл {matched[0]}: "
                    f"{(current / name).relative_to(bundle).as_posix()}"
                )
    if violations:
        raise PackagingError("запрещённое содержимое комплекта:\n  " + "\n  ".join(violations))

    info_path = bundle / "build-info.json"
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackagingError(f"build-info.json не читается: {exc}") from exc
    required_keys = {
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
    absent = sorted(required_keys - set(info))
    if absent:
        raise PackagingError(f"build-info.json без обязательных ключей: {', '.join(absent)}")
    if info["target"] != target:
        raise PackagingError(
            f"build-info.json объявляет target {info['target']!r}, упаковывается {target!r}"
        )
    if info["version"] != version:
        raise PackagingError(
            f"build-info.json объявляет версию {info['version']!r}, упаковывается {version!r}"
        )


def _write_tar(bundle: Path, destination: Path, executable: str) -> None:
    """tar.gz с корнем keklib-cli/, нормализованными правами и mtime=0 в gzip.

    Внутренние символические ссылки сохраняются ссылками с относительным
    именем цели (переносимое представление); цели всегда внутри комплекта
    по результатам валидации.
    """
    with open(destination, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(
                fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
            ) as archive:

                def add(path: Path, arcname: str) -> None:
                    st = os.lstat(path)
                    if _link_like(st):
                        resolved = path.resolve(strict=True)
                        # Обе стороны relpath — в одной канонической системе
                        # координат: resolved-цель И resolved-родитель. Путь
                        # комплекта через symlink-предок (macOS /var →
                        # /private/var, Windows short/long) прежде смешивал
                        # resolved с lexical и выпускал хвост из «..» вместо
                        # имени цели внутри комплекта. Разделители — POSIX и
                        # на Windows: os.path.relpath там возвращает «\».
                        parent = path.parent.resolve(strict=True)
                        item = tarfile.TarInfo(arcname)
                        item.type = tarfile.SYMTYPE
                        item.linkname = os.path.relpath(resolved, parent).replace(
                            os.sep, "/"
                        )
                        item.mode = 0o777
                        item.size = 0
                        item.uid = item.gid = 0
                        item.uname = item.gname = ""
                        item.mtime = int(st.st_mtime)
                        archive.addfile(item)
                        return
                    item = archive.gettarinfo(str(path), arcname=arcname)
                    item.uid = item.gid = 0
                    item.uname = item.gname = ""
                    if item.isdir():
                        item.mode = 0o755
                    elif arcname == f"{BUNDLE_ROOT}/{executable}":
                        item.mode = 0o755
                    else:
                        item.mode = 0o644
                    if item.isreg():
                        with path.open("rb") as stream:
                            archive.addfile(item, stream)
                    else:
                        archive.addfile(item)

                add(bundle, BUNDLE_ROOT)
                for dirpath, dirnames, filenames in os.walk(bundle, followlinks=False):
                    relative = Path(dirpath).relative_to(bundle)
                    prefix = (
                        BUNDLE_ROOT
                        if not relative.parts
                        else f"{BUNDLE_ROOT}/{relative.as_posix()}"
                    )
                    dirnames.sort()
                    for name in sorted(dirnames) + sorted(filenames):
                        add(Path(dirpath) / name, f"{prefix}/{name}")
        raw.flush()
        os.fsync(raw.fileno())


def _write_zip(bundle: Path, destination: Path) -> None:
    """ZIP (Windows-target) с корнем keklib-cli/ без посторонних байтов.

    Внутренние ссылки файлов материализуются байтами своей цели внутри
    комплекта; ссылки на каталоги в ZIP непредставимы и отвергаются.
    """
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for dirpath, dirnames, filenames in os.walk(bundle, followlinks=False):
            current = Path(dirpath)
            relative = current.relative_to(bundle)
            prefix = (
                BUNDLE_ROOT
                if not relative.parts
                else f"{BUNDLE_ROOT}/{relative.as_posix()}"
            )
            dirnames.sort()
            for name in dirnames:
                entry = current / name
                if _link_like(entry.lstat()):
                    raise PackagingError(
                        "символическая ссылка на каталог не представима в ZIP: "
                        f"{prefix}/{name}"
                    )
            for name in sorted(filenames):
                entry = current / name
                arcname = f"{prefix}/{name}"
                if _link_like(entry.lstat()):
                    resolved = entry.resolve(strict=True)
                    if resolved.is_dir():
                        raise PackagingError(
                            f"символическая ссылка на каталог не представима в ZIP: {arcname}"
                        )
                    archive.writestr(arcname, resolved.read_bytes())
                else:
                    archive.write(entry, arcname)


def _fsync_file(path: str | Path) -> None:
    """fsync уже записанного файла (ZIP-путь пишет через ZipFile без fsync)."""
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _cleanup_temp(path: str | None) -> None:
    """Удалить свой временный файл, если он ещё не опубликован."""
    if path is None:
        return
    with contextlib.suppress(OSError):
        os.unlink(path)


def repair_checksum(archive: Path, expected_sha256: str) -> Path:
    """Явное восстановление отсутствующей контрольной суммы архива.

    Контроллером закреплён безопасный протокол без отката: при отказе
    публикации суммы опубликованный архив **никогда не удаляется** (даже
    свой), а пара ремонтируется отдельной явной операцией. Здесь
    проверяется, что путь — обычный файл (не ссылка), байты архива
    совпадают с ожидаемым SHA-256, после чего соседний
    ``<archive>.sha256`` создаётся атомарно без перезаписи. Ничего не
    удаляется и не перезаписывается. Операция восстанавливает парность
    файлов выпуска, но **не подтверждает происхождение архива и не
    является приёмом/acceptance**: это ремонт целостности пары по
    известному digest.
    """
    digest = str(expected_sha256).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PackagingError(
            "--expected-sha256: ожидается 64 hex-символа SHA-256, "
            f"получено {expected_sha256!r}"
        )
    try:
        st = archive.lstat()
    except OSError as exc:
        raise PackagingError(f"архив не читается: {archive}: {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise PackagingError(
            f"архив не является обычным файлом: {archive} (ссылки не ремонтируются)"
        )
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != digest:
        raise PackagingError(
            f"байты архива не совпадают с ожидаемым digest: {archive}: "
            f"фактически {actual}, ожидалось {digest}"
        )
    checksum = archive.with_name(archive.name + ".sha256")
    if checksum.exists():
        raise PackagingError(
            f"контрольная сумма уже существует: {checksum}; перезапись запрещена"
        )
    handle, temp_name = tempfile.mkstemp(dir=archive.parent, prefix=".checksum-repair-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{digest}  {archive.name}\n")
            stream.flush()
            os.fsync(stream.fileno())
        _publish_no_replace(Path(temp_name), checksum)
    except BaseException:
        _cleanup_temp(temp_name)
        raise
    return checksum


def archive_bundle(bundle: Path, output: Path, *, version: str, target: str) -> Path:
    """Упаковать готовый комплект в ``<output>/keklib-cli-<version>-<target>.<ext>``.

    Протокол пары «архив + .sha256»: оба временных файла готовятся и
    fsync-ятся до первой публикации, затем атомарно без перезаписи
    публикуется архив и рядом сумма. При обычном отказе второй публикации
    восстанавливается только наш опознанный по SHA-256 архив; чужие файлы
    не трогаются. Пара не транзакционна при сбое процесса между
    публикациями (см. докстринг модуля): crash-гарантия не заявляется.
    """
    targets = load_targets()
    if target not in targets:
        raise PackagingError(
            f"неизвестный target {target!r}; доступные: {', '.join(sorted(targets))}"
        )
    spec = targets[target]
    executable = str(spec["executable"])
    archive_format = str(spec["archive_format"])
    _validate_bundle(bundle, target, version)

    output.mkdir(parents=True, exist_ok=True)
    final = output / f"keklib-cli-{version}-{target}.{archive_format}"
    checksum = final.with_name(final.name + ".sha256")
    if final.exists() or checksum.exists():
        raise PackagingError(
            f"отказ перезаписи: уже существуют {final}" if final.exists()
            else f"отказ перезаписи: уже существует {checksum}"
        )

    digest: str | None = None
    archive_temp: str | None = None
    checksum_temp: str | None = None
    archive_published = False
    try:
        handle, archive_temp = tempfile.mkstemp(
            dir=output, prefix=".archive-", suffix="." + archive_format
        )
        os.close(handle)
        if archive_format == "tar.gz":
            _write_tar(bundle, Path(archive_temp), executable)
        elif archive_format == "zip":
            _write_zip(bundle, Path(archive_temp))
            _fsync_file(archive_temp)
        else:
            raise PackagingError(f"неизвестный формат архива: {archive_format}")
        digest = hashlib.sha256(Path(archive_temp).read_bytes()).hexdigest()

        checksum_handle, checksum_temp = tempfile.mkstemp(dir=output, prefix=".checksum-")
        with os.fdopen(
            checksum_handle, "w", encoding="utf-8", newline="\n"
        ) as stream:
            stream.write(f"{digest}  {final.name}\n")
            stream.flush()
            os.fsync(stream.fileno())

        _publish_no_replace(Path(archive_temp), final)
        archive_temp = None
        archive_published = True
        _publish_no_replace(Path(checksum_temp), checksum)
        checksum_temp = None
    except KeyboardInterrupt:
        _cleanup_temp(archive_temp)
        _cleanup_temp(checksum_temp)
        raise
    except Exception as exc:
        _cleanup_temp(archive_temp)
        _cleanup_temp(checksum_temp)
        hint = ""
        if archive_published and digest is not None:
            # Протокол контроллера: опубликованный архив НИКОГДА не
            # удаляется — ни свой, ни совпадающий по байтам чужой. Пара
            # ремонтируется явной операцией по фактическому digest.
            hint = (
                f"; пара прервана: архив опубликован без суммы "
                f"(digest {digest}). Опубликованный архив не удаляется. "
                "Восстановите сумму явной операцией: "
                "python tools/build_portable.py --repair-checksum "
                f"{final} --expected-sha256 {digest}. Повторная сборка "
                "откажется перезаписывать существующий архив "
                "(crash-гарантии у пары нет)."
            )
        raise PackagingError(
            f"публикация пары архив/сумма не завершена: {exc}{hint}"
        ) from exc
    return final


# ---------------------------------------------------------------------------
# Полный конвейер


def build_portable(target: str, output: Path) -> Path:
    """Собрать portable-комплект для нативного target и вернуть путь архива."""
    assert_native_target(target)
    print(f"[build] preflight toolchain: {target}", file=sys.stderr)
    verify_toolchain_preflight(target)
    targets = load_targets()
    executable = str(targets[target]["executable"])
    version_module = importlib.import_module("keklib_cli")
    version = str(version_module.__version__)

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"portable-{target}-", dir=BUILD_DIR))
    try:
        dist = staging / "dist"
        work = staging / "work"
        print(f"[build] PyInstaller onedir: {target}", file=sys.stderr)
        _run_pyinstaller(dist, work)
        bundle = dist / BUNDLE_ROOT
        if not (bundle / executable).is_file():
            raise PackagingError(
                f"после сборки отсутствует исполняемый файл {bundle / executable}"
            )
        for name in (*DOCS, NOTICES):
            source = PACKAGING_DIR / name if name == NOTICES else PROJECT_ROOT / name
            shutil.copyfile(source, bundle / name)
        info = build_info(target)
        (bundle / "build-info.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"[build] самопроверка executable ({executable}) на синтетической "
            "библиотеке",
            file=sys.stderr,
        )
        _run_selfcheck(bundle / executable, staging / "selfcheck")

        floor = targets[target]["minimum_glibc"]
        if isinstance(floor, str):
            violations = verify_glibc_floor(bundle, floor)
            if violations:
                details = "\n  ".join(violations)
                raise PackagingError(
                    f"комплект требует glibc выше закреплённого floor {floor} "
                    f"({targets[target]['minimum_os']}):\n  {details}\n"
                    "Комплект НЕ совместим с baseline и не публикуется; нельзя "
                    "молча маркировать его совместимым. Нужен сборщик на "
                    f"{targets[target]['runner']} или отдельное решение пользователя."
                )
            print(f"[build] glibc floor {floor}: нарушений нет", file=sys.stderr)

        print("[build] release-гейт метаданных runtime", file=sys.stderr)
        verify_release_metadata(target, info)
        print("[build] лицензионный гейт по фактическому составу", file=sys.stderr)
        verify_license_closure(bundle, target=target, copy_notices_to=bundle)

        print(f"[build] упаковка архива в {output}", file=sys.stderr)
        return archive_bundle(bundle, output, version=version, target=target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: сборка или явный ремонт контрольной суммы.

    Режим сборки: ``--target TARGET --output DIR``. Режим ремонта пары:
    ``--repair-checksum ARCHIVE --expected-sha256 DIGEST`` — проверяет
    байты архива против digest и создаёт отсутствующую соседнюю сумму
    атомарно без перезаписи; удалений нет, provenance/acceptance не
    подтверждается.
    """
    targets = load_targets()
    parser = argparse.ArgumentParser(
        prog="build_portable",
        description="Нативная сборка portable-комплекта keklib-cli (onedir, PyInstaller).",
    )
    parser.add_argument(
        "--target",
        choices=sorted(targets),
        default=None,
        help="целевая ОС/CPU (режим сборки)",
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="каталог архива (не очищается)"
    )
    parser.add_argument(
        "--repair-checksum",
        type=Path,
        default=None,
        metavar="ARCHIVE",
        help="явный ремонт: создать отсутствующую <archive>.sha256",
    )
    parser.add_argument(
        "--expected-sha256",
        default=None,
        metavar="DIGEST",
        help="ожидаемый SHA-256 архива (только с --repair-checksum)",
    )
    args = parser.parse_args(argv)
    if args.repair_checksum is not None:
        if args.expected_sha256 is None:
            parser.error("--repair-checksum требует --expected-sha256")
        try:
            checksum = repair_checksum(args.repair_checksum, args.expected_sha256)
        except PackagingError as exc:
            print(f"build_portable: ошибка: {exc}", file=sys.stderr)
            return 1
        print(checksum.resolve())
        return 0
    if args.expected_sha256 is not None:
        parser.error("--expected-sha256 используется только с --repair-checksum")
    if args.target is None or args.output is None:
        parser.error("режим сборки требует --target и --output")
    try:
        archive = build_portable(args.target, args.output)
    except PackagingError as exc:
        print(f"build_portable: ошибка: {exc}", file=sys.stderr)
        return 1
    print(archive.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
