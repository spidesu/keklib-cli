"""Изолированный smoke-прогон portable-комплекта keklib-cli (задача 12).

Инструмент test-only и работает из вспомогательного harness-архива без
checkout исходников: приложение ``keklib_cli`` не импортируется ни здесь,
ни в harness (``tests.harness``), ни в child. Запуск:

``python tools/smoke_portable.py --archive PATH --workdir PATH``

Что делает и фиксирует в ``smoke-report.json``:

- распаковку **только в новый собственный workdir** собственным безопасным
  распаковщиком (без ``extractall``): имена проверяются до касания диска,
  traversal/абсолютные пути/дубликаты/спецфайлы/жёсткие ссылки/внешние и
  абсолютные symlink отвергаются; внутренние относительные symlink
  (Linux onedir) сохраняются;
- прогон **абсолютного frozen executable** из архива через нативный TTY
  (``tests.harness.tty``): ``--help``, ``setup``, поиск с выбором номера,
  обновление INPX синтетической библиотеки, повторный поиск и SHA-256
  извлечённого FB2;
- изоляцию child как наблюдаемые условия, а не флаги: PATH без python
  (проверяется поиском executables и ``shutil.which``), без ``PYTHON*``,
  cwd вне checkout и комплекта, HOME/XDG/AppData в собственном sandbox;
- read-only комплекта (``tests.harness.permissions``) с реальной пробой
  невозможности записи на время всех запусков;
- нативные пути состояния (config/index) вне комплекта и неизменность
  источников (SHA-256 + mtime_ns) на своих прогонах.

``passed=True`` ставится только по фактическим наблюдениям; любой сбой —
честный ``passed=False`` и код выхода 1.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import platform
import posixpath
import re
import shutil
import stat
import sys
import tarfile
import time
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: Корень вспомогательного harness-архива (родитель tools/).
HARNESS_ROOT = Path(__file__).resolve().parents[1]

ARCHIVE_ROOT = "keklib-cli"
REPORT_NAME = "smoke-report.json"
REPORT_SCHEMA = 1

#: Максимальный размер одного файла комплекта (защита от мусора).
_MAX_MEMBER_BYTES = 4 << 30

#: Исполняемый файл комплекта по соглашению targets.json.
_EXECUTABLE_BY_SYSTEM = {
    "Linux": "keklib-cli",
    "Windows": "keklib-cli.exe",
    "Darwin": "keklib-cli",
}
_SYSTEM_BY_TARGET_PREFIX = {"linux": "Linux", "windows": "Windows", "macos": "Darwin"}

#: Имена python-интерпретаторов, которых не должно быть в PATH child.
_PYTHON_NAME = re.compile(r"^python(\d+(\.\d+)*)?(\.exe)?$", re.IGNORECASE)
_WINDOWS_LAUNCHERS = ("py.exe", "py")

_TTY_TIMEOUT = 300.0

#: Синтетика приёмки (та же пара, что в tests/test_integration.py).
KNOWN_INP = "fb2-060424-074391"
KNOWN_FILE = "66372"
KNOWN_MEMBER = "66372.fb2"
KNOWN_AUTHOR = "Булгаков, Михаил Афансьевич"
KNOWN_TITLE = "Мастер и Маргарита"
KNOWN_PAYLOAD = b"<FictionBook id='66372'/>"
NEW_FILE = "777"
NEW_TITLE = "Книга Новая"
NEW_PAYLOAD = b"<FictionBook id='777' new/>"

_STATE_NAMES = {"config.json", "index.sqlite3", "index.lock"}
_STATE_SUFFIXES = (".sqlite3-journal", ".sqlite3-wal", ".sqlite3-shm")


class SmokeError(RuntimeError):
    """Smoke не может быть выполнен или прошёл с нарушением изоляции."""


def _ensure_harness_imports() -> None:
    """Добавить корень harness-архива в sys.path для импорта tests/tools.

    Единственное разрешённое дополнение пути: каталоги checkout/src
    приложения не добавляются (ограничение задачи 12).
    """
    root = str(HARNESS_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Безопасная распаковка


def _member_parts(name: str) -> list[str]:
    """Компоненты проверенного относительного POSIX-имени записи архива."""
    if not isinstance(name, str) or not name:
        raise SmokeError(f"пустое имя записи архива: {name!r}")
    if name.startswith(("/", "\\")) or "\\" in name or ":" in name or "\x00" in name:
        raise SmokeError(f"недопустимое имя записи архива: {name!r}")
    if any(ord(char) < 0x20 for char in name):
        raise SmokeError(f"управляющие символы в имени записи: {name!r}")
    parts = name.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise SmokeError(f"недопустимый компонент пути записи: {name!r}")
    return parts


def _bundle_relative(name: str) -> list[str]:
    parts = _member_parts(name)
    if parts[0] != ARCHIVE_ROOT:
        raise SmokeError(f"запись вне корня {ARCHIVE_ROOT}/: {name!r}")
    return parts[1:]


def _link_inside(member_name: str, target: str) -> None:
    """Лексическая проверка цели ссылки (полное разрешение — в ``_Plan.finalize``)."""
    if not isinstance(target, str) or not target:
        raise SmokeError(f"пустая цель ссылки: {member_name}")
    if target.startswith(("/", "\\")) or "\\" in target or ":" in target or "\x00" in target:
        raise SmokeError(f"ссылка {member_name} покидает комплект: {target!r}")
    base = "/".join(_bundle_relative(member_name)[:-1])
    joined = posixpath.normpath(f"{ARCHIVE_ROOT}/{base}/{target}")
    if joined == ".." or joined.startswith("../"):
        raise SmokeError(f"ссылка {member_name} покидает комплект: {target!r}")
    parts = _member_parts(joined)
    if parts[0] != ARCHIVE_ROOT:
        raise SmokeError(f"ссылка {member_name} покидает комплект: {target!r}")


#: Предохранитель разрешения цепочек ссылок (циклы дают бесконечный спуск).
_MAX_LINK_STEPS = 256


def _resolve_link_components(
    name: str, plan: "_Plan", visited: frozenset[str]
) -> list[str]:
    """Разрешить цель ссылки по графу плана с семантикой каталогов.

    Компоненты цели обрабатываются слева направо от каталога ссылки;
    компонент, сам являющийся ссылкой плана, заменяется её полным
    разрешением (рекурсивно, с детекцией циклов). ``..`` выше корня
    комплекта, проход через файл и цикл — отказ. Возвращает компоненты
    конечной цели внутри комплекта.
    """
    parts = name.split("/")
    out = list(parts[:-1])
    components = list(plan.links[name].split("/"))
    index = 0
    steps = 0
    while index < len(components):
        part = components[index]
        if part in ("", "."):
            index += 1
            continue
        if part == "..":
            if not out:
                raise SmokeError(
                    f"ссылка {name} покидает комплект: {plan.links[name]!r}"
                )
            out.pop()
            index += 1
            continue
        candidate = "/".join(out + [part])
        if candidate in plan.files and index + 1 < len(components):
            # Файл как промежуточный каталог — ошибка; конечная цель-файл
            # (например, листовая ссылка на executable) легитимна.
            raise SmokeError(
                f"ссылка {name} проходит через файл: {candidate!r}"
            )
        if candidate in plan.links:
            steps += 1
            if steps > _MAX_LINK_STEPS or candidate in visited:
                raise SmokeError(
                    f"цикл символических ссылок плана: {candidate!r}"
                )
            out = _resolve_link_components(candidate, plan, visited | {candidate})
            components = components[index + 1 :]
            index = 0
            continue
        out.append(part)
        index += 1
    return out


class _Plan:
    """Проверенный план распаковки: все имена проверены до касания диска."""

    def __init__(self) -> None:
        self.dirs: set[str] = set()
        self.files: set[str] = set()
        self.links: dict[str, str] = {}
        self.seen: set[str] = set()

    def _register(self, name: str) -> list[str]:
        parts = _bundle_relative(name)
        normal = "/".join(parts)
        if normal in self.seen:
            raise SmokeError(f"повтор записи архива: {name!r}")
        self.seen.add(normal)
        return parts

    def add_dir(self, name: str) -> None:
        parts = self._register(name)
        if parts:
            self.dirs.add("/".join(parts))

    def add_file(self, name: str, size: int) -> None:
        parts = self._register(name)
        if not parts:
            raise SmokeError(f"файл с именем корня комплекта: {name!r}")
        if size < 0 or size > _MAX_MEMBER_BYTES:
            raise SmokeError(f"недопустимый размер записи {name!r}: {size}")
        self.files.add("/".join(parts))

    def add_link(self, name: str, target: str) -> None:
        parts = self._register(name)
        if not parts:
            raise SmokeError(f"ссылка с именем корня комплекта: {name!r}")
        _link_inside(name, target)
        self.links["/".join(parts)] = target

    def finalize(self) -> None:
        """Разрешить граф ссылок и конфликты предков ДО любых записей.

        Извлечение через symlink-родителя запрещено целиком: любые записи
        (файлы/каталоги/ссылки), чей предок — ссылка плана, отвергаются,
        поэтому легитимные листовые ссылки onedir сохраняются, а записать
        что-либо по цепочке за пределы комплекта невозможно. Отдельно
        отвергаются предки-файлы (конфликт типов), транзитивные побеги и
        циклы цепочек (см. ``_resolve_link_components``).
        """
        for name in sorted(self.dirs | self.files | set(self.links)):
            parts = name.split("/")
            for depth in range(1, len(parts)):
                prefix = "/".join(parts[:depth])
                if prefix in self.files:
                    raise SmokeError(
                        f"конфликт типов: «{name}» внутри файла «{prefix}»"
                    )
                if prefix in self.links:
                    raise SmokeError(
                        f"извлечение через символическую ссылку-родителя "
                        f"запрещено: «{name}» через «{prefix}»"
                    )
        for name in sorted(self.links):
            _resolve_link_components(name, self, frozenset({name}))


def _plan_tar(archive: Path, plan: _Plan) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name.rstrip("/")
            if member.isdir():
                plan.add_dir(name or member.name)
            elif member.isreg():
                plan.add_file(name, member.size)
            elif member.issym():
                plan.add_link(name, member.linkname)
            else:
                raise SmokeError(
                    f"запись-спецфайл/жёсткая ссылка запрещены: {member.name!r} "
                    f"(тип {member.type!r})"
                )


def _zip_unix_mode(info: zipfile.ZipInfo) -> int | None:
    if info.create_system != 3:
        return None
    return (info.external_attr >> 16) & 0xFFFF


def _plan_zip(archive: Path, plan: _Plan) -> None:
    with zipfile.ZipFile(archive) as zipped:
        for info in zipped.infolist():
            if info.filename.endswith("/"):
                plan.add_dir(info.filename.rstrip("/"))
                continue
            mode = _zip_unix_mode(info)
            if mode is not None:
                kind = stat.S_IFMT(mode)
                if kind not in (0, stat.S_IFREG):
                    raise SmokeError(
                        f"zip-запись не является обычным файлом (zip-поставка "
                        f"материализует ссылки): {info.filename!r}"
                    )
            plan.add_file(info.filename, info.file_size)


def _extract_tar(archive: Path, bundle: Path, executable: str) -> None:
    root = bundle.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isdir():
                continue  # каталоги созданы заранее по плану
            name = member.name.rstrip("/")
            target = bundle / "/".join(_bundle_relative(name))
            if member.isreg():
                source = tar.extractfile(member)
                if source is None:
                    raise SmokeError(f"запись не читается: {name!r}")
                mode = 0o755 if name == f"{ARCHIVE_ROOT}/{executable}" else 0o644
                _write_stream(source, target, mode, root)
            elif member.issym():
                _make_link(target, member.linkname, root)


def _extract_zip(archive: Path, bundle: Path, executable: str) -> None:
    root = bundle.resolve()
    with zipfile.ZipFile(archive) as zipped:
        for info in zipped.infolist():
            if info.filename.endswith("/"):
                continue
            target = bundle / "/".join(_bundle_relative(info.filename))
            with zipped.open(info) as source:
                mode = (
                    0o755
                    if info.filename == f"{ARCHIVE_ROOT}/{executable}"
                    else 0o644
                )
                _write_stream(source, target, mode, root)


def _guard_parent(target: Path, root: Path) -> None:
    """Запись возможна только внутрь реального каталога комплекта.

    Плановые проверки уже запрещают symlink-родителей; эта защита
    проверяет фактическое разрешение пути перед каждым созданием листа —
    попытка писать по ссылке наружу невозможна даже при ошибке плана.
    """
    parent = target.parent.resolve()
    if not parent.is_relative_to(root):
        raise SmokeError(f"путь записи разрешается вне комплекта: {target}")


def _write_stream(source: Any, target: Path, mode: int, root: Path) -> None:
    _guard_parent(target, root)
    handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    written = 0
    try:
        with os.fdopen(handle, "wb") as stream:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                written += len(chunk)
                if written > _MAX_MEMBER_BYTES:
                    raise SmokeError(f"размер записи превышает лимит: {target}")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink()
        raise


def _make_link(target: Path, link_target: str, root: Path) -> None:
    _guard_parent(target, root)
    if target.exists() or target.is_symlink():
        raise SmokeError(f"цель ссылки уже существует: {target}")
    os.symlink(link_target, target)


def _read_build_info_payload(archive: Path) -> dict[str, object]:
    member = f"{ARCHIVE_ROOT}/build-info.json"
    payload: bytes
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zipped:
            with zipped.open(member) as stream:
                payload = stream.read(1 << 20)
    else:
        with tarfile.open(archive, "r:gz") as tar:
            handle = tar.extractfile(member)
            if handle is None:
                raise SmokeError(f"{member} не является файлом")
            payload = handle.read(1 << 20)
    try:
        info = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SmokeError(f"build-info.json не читается: {exc}") from exc
    if not isinstance(info, dict):
        raise SmokeError("build-info.json не объект")
    for key in ("version", "target", "minimum_os"):
        if not isinstance(info.get(key), str) or not info[key]:
            raise SmokeError(f"build-info.json без корректного поля {key}")
    return info


def _target_system(target: str) -> str:
    prefix = target.split("-", 1)[0]
    system = _SYSTEM_BY_TARGET_PREFIX.get(prefix)
    if system is None:
        raise SmokeError(f"неизвестный target: {target!r}")
    return system


def unpack_bundle(archive: Path, destination: Path) -> tuple[Path, dict[str, object]]:
    """Безопасно распаковать комплект в ``destination``; вернуть (корень, build-info).

    Порядок: build-info → полный план → **разрешение графа ссылок и
    конфликтов предков до единой записи на диск** → создание всех
    каталогов (явных и неявных предков) → потокочное извлечение файлов и
    листовых ссылок с проверкой фактического разрешения каждого листа.
    ``extractall`` не используется; извлечение через символическую
    ссылку-родителя невозможно по построению.
    """
    archive = Path(archive)
    destination = Path(destination)
    info = _read_build_info_payload(archive)
    executable = _EXECUTABLE_BY_SYSTEM[_target_system(str(info["target"]))]

    plan = _Plan()
    if archive.name.endswith(".zip"):
        _plan_zip(archive, plan)
    else:
        _plan_tar(archive, plan)

    if executable not in plan.files:
        raise SmokeError(
            f"в архиве нет исполняемого файла комплекта: {ARCHIVE_ROOT}/{executable}"
        )
    plan.finalize()

    bundle = destination / ARCHIVE_ROOT
    bundle.mkdir(parents=True, exist_ok=False)
    # Замыкание неявных предков — по всем видам записей, включая пустые
    # каталоги: валидный архив может содержать ``a/b/c/`` без отдельной
    # записи ``a/b``. Создание строго по глубине сохраняет гарантию
    # «никаких записей через symlink-родителя» (граф уже проверен
    # finalize) и отказ до первой записи при запрещённом плане.
    all_dirs = set(plan.dirs)
    for name in plan.dirs | plan.files | set(plan.links):
        parts = name.split("/")
        for depth in range(1, len(parts)):
            all_dirs.add("/".join(parts[:depth]))
    for relative in sorted(all_dirs, key=lambda item: item.count("/")):
        (bundle / relative).mkdir(exist_ok=True)  # родители уже созданы порядком глубины

    if archive.name.endswith(".zip"):
        _extract_zip(archive, bundle, executable)
    else:
        _extract_tar(archive, bundle, executable)

    if not bundle.resolve().is_relative_to(destination.resolve()):
        raise SmokeError("распакованный комплект покинул назначенный каталог")
    return bundle, info


def prepare_workdir(workdir: Path) -> None:
    """Требовать несуществующий workdir: smoke не перезаписывает чужие каталоги."""
    workdir = Path(workdir)
    if workdir.is_symlink() or workdir.exists():
        raise SmokeError(
            f"workdir уже существует; smoke распаковывается только в новый "
            f"собственный каталог: {workdir}"
        )
    workdir.mkdir(parents=True)


# ---------------------------------------------------------------------------
# Изоляция child-окружения


def _python_in_dir(directory: Path) -> str | None:
    """Наблюдаемое наличие python-интерпретатора в каталоге PATH."""
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    for name in entries:
        if _PYTHON_NAME.match(name) or name.lower() in _WINDOWS_LAUNCHERS:
            if sys.platform == "win32" or os.access(directory / name, os.X_OK):
                return name
    return None


def python_free_path() -> str:
    """PATH без единого python-исполняемого файла; иначе честный отказ."""
    items: list[str] = []
    source = os.environ.get("PATH", "")
    if source:
        items.extend(source.split(os.pathsep))
    if sys.platform == "win32":
        items.extend(
            [
                r"C:\Windows\System32",
                r"C:\Windows",
                r"C:\Windows\System32\Wbem",
                r"C:\Windows\System32\WindowsPowerShell\v1.0",
            ]
        )
    else:
        items.extend(["/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin"])

    keep: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item:
            continue
        key = item.lower() if sys.platform == "win32" else item
        if key in seen:
            continue
        seen.add(key)
        directory = Path(item)
        if not directory.is_dir():
            continue
        if _python_in_dir(directory) is None:
            keep.append(item)
    result = os.pathsep.join(keep)

    probes = ("python", "python3") + (("py",) if sys.platform == "win32" else ())
    for probe in probes:
        if shutil.which(probe, path=result) is not None:
            raise SmokeError(
                f"не удалось построить PATH без python: найден {probe}; "
                "изоляция child не гарантируется"
            )
    return result


def child_environment(sandbox: Path) -> dict[str, str]:
    """Окружение child: без PYTHON*, состояние и tmp в собственном sandbox.

    Создаёт домашние каталоги sandbox и возвращает полное окружение с PATH
    без python (наблюдаемая проверка — иначе ``SmokeError``).
    """
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
        home / "AppData" / "Roaming",
        home / "AppData" / "Local",
        home / "Library" / "Application Support",
        home / "Library" / "Caches",
        sandbox / "cwd",
        sandbox / "tmp",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    if sys.platform == "win32":
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
        env["TEMP"] = str(sandbox / "tmp")
        env["TMP"] = str(sandbox / "tmp")
    else:
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["XDG_CACHE_HOME"] = str(home / ".cache")
        env["TMPDIR"] = str(sandbox / "tmp")
    env["PATH"] = python_free_path()
    return env


# ---------------------------------------------------------------------------
# Факты хоста и заявленный минимум


def _normalize_machine(machine: str) -> str:
    """Нормализация архитектуры как в tools/build_portable.py (согласованный протокол)."""
    value = machine.lower()
    if value in ("x86_64", "amd64"):
        return "x86_64"
    if value in ("arm64", "aarch64"):
        return "arm64"
    return value


def _normalize_arch_facts(facts: dict[str, object]) -> dict[str, object]:
    """Нормализовать host_arch (AMD64→x86_64, aarch64/arm64→arm64).

    Исходное наблюдаемое значение сохраняется в ``host_arch_raw``: отчёт
    не скрывает, что вернул хост (например, штатное ``AMD64`` Windows).
    """
    raw = str(facts.get("host_arch") or "")
    facts["host_arch"] = _normalize_machine(raw)
    facts["host_arch_raw"] = raw
    return facts


def collect_host_facts() -> dict[str, object]:
    system = platform.system()
    facts: dict[str, object] = {
        "host_os": system,
        "host_arch": platform.machine() or "",
        "host_os_version": platform.release() or "",
    }
    _normalize_arch_facts(facts)
    if system == "Linux":
        release: dict[str, str] = {}
        try:
            content = Path("/etc/os-release").read_text(encoding="utf-8")
        except OSError:
            content = ""
        for line in content.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                release[key.strip()] = value.strip().strip('"')
        facts["os_id"] = release.get("ID")
        facts["os_version_id"] = release.get("VERSION_ID")
        facts["os_pretty_name"] = release.get("PRETTY_NAME")
        glibc = None
        try:
            glibc = os.confstr("CS_GNU_LIBC_VERSION")
        except (OSError, ValueError):
            glibc = None
        facts["host_glibc"] = glibc.split()[-1] if glibc else None
    elif system == "Darwin":
        version = platform.mac_ver()[0]
        facts["macos_version"] = version or None
    elif system == "Windows":
        facts["windows_release"] = platform.release() or None
    return facts


def _expected_system_machine(target: str) -> tuple[str, str]:
    system = _target_system(target)
    machine = target.split("-", 1)[1] if "-" in target else ""
    return system, machine


def _expected_state_paths(env: Mapping[str, str], home: Path) -> tuple[Path, Path]:
    """Зеркало нативных путей состояния приложения (smoke не импортирует его)."""
    system = platform.system()
    if system == "Windows":
        config_root = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
        cache_root = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return (
            config_root / "keklib-cli" / "config.json",
            cache_root / "keklib-cli" / "Cache" / "index.sqlite3",
        )
    if system == "Darwin":
        return (
            home / "Library" / "Application Support" / "keklib-cli" / "config.json",
            home / "Library" / "Caches" / "keklib-cli" / "index.sqlite3",
        )
    config_root = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    cache_root = Path(env.get("XDG_CACHE_HOME") or home / ".cache")
    return (
        config_root / "keklib-cli" / "config.json",
        cache_root / "keklib-cli" / "index.sqlite3",
    )


# ---------------------------------------------------------------------------
# Синтетическая библиотека (tests.harness.library_fixture, без keklib_cli)


def _write_library(root: Path, *, with_new_book: bool) -> None:
    from tests.harness.library_fixture import write_library

    rows_known = [
        {
            "AUTHOR": KNOWN_AUTHOR,
            "TITLE": KNOWN_TITLE,
            "FILE": KNOWN_FILE,
            "DEL": "0",
            "EXT": "fb2",
            "LANG": "ru",
            "SIZE": str(len(KNOWN_PAYLOAD)),
        },
        {
            "AUTHOR": "Автор, Раз",
            "TITLE": "Книга Раз",
            "FILE": "2",
            "DEL": "0",
            "EXT": "fb2",
        },
    ]
    row_new = {
        "AUTHOR": "Автор, Нов",
        "TITLE": NEW_TITLE,
        "FILE": NEW_FILE,
        "DEL": "0",
        "EXT": "fb2",
        "LANG": "ru",
    }
    row_other = {
        "AUTHOR": "Автор, Три",
        "TITLE": "Книга Три",
        "FILE": "3",
        "DEL": "0",
        "EXT": "fb2",
    }
    members: dict[str, dict[str, bytes]] = {
        KNOWN_INP: {
            KNOWN_MEMBER: KNOWN_PAYLOAD,
            "2.fb2": b"<FictionBook id='2'/>",
        },
        "fb2-other-000": {"3.fb2": b"<FictionBook id='3'/>"},
    }
    if with_new_book:
        rows_known.append(row_new)
        members[KNOWN_INP][f"{NEW_FILE}.fb2"] = NEW_PAYLOAD
    write_library(
        root,
        {KNOWN_INP: rows_known, "fb2-other-000": [row_other]},
        members=members,
    )


def _sources_snapshot(library_root: Path) -> dict[str, tuple[str, int]]:
    from tests.harness.library_fixture import ARCHIVES_DIR, CATALOG_NAME

    files = [library_root / CATALOG_NAME]
    files.extend(sorted((library_root / ARCHIVES_DIR).glob("*.zip")))
    return {
        str(path): (sha256_file(path), path.stat().st_mtime_ns)
        for path in files
        if path.exists()
    }


# ---------------------------------------------------------------------------
# Основной прогон


def _single_fb2(directory: Path) -> Path | None:
    found = sorted(directory.glob("*.fb2"))
    return found[0] if len(found) == 1 else None


def _flow_steps(
    library: Path,
    out1: Path,
    out2: Path,
    *,
    run,
) -> tuple[list[dict[str, object]], dict[str, str], bool, bool]:
    """Сценарий TTY с двухэтапным наблюдением неизменности источников.

    ``run(step, argv, commands) -> (returncode, transcript)``. Снимок
    источников берётся **до** setup и первого поиска/извлечения и
    сравнивается с снимком непосредственно перед намеренным обновлением
    синтетики (этап 1); затем второй снимок — вокруг повторного поиска
    после обновления (этап 2). Итоговая конъюнкция — в вызывающем:
    порча источника любым этапом не может остаться незамеченной
    перегенерацией библиотеки.
    """
    flow: list[dict[str, object]] = []
    extractions: dict[str, str] = {}

    def do(step: str, argv: list[str], commands: list[str]) -> str:
        code, transcript = run(step, argv, commands)
        flow.append({"step": step, "returncode": code})
        return transcript

    before_setup = _sources_snapshot(library)

    transcript = do("help", ["--help"], [])
    flow[-1]["usage_shown"] = "usage" in transcript
    transcript = do("setup", ["setup", "--library", str(library)], [])
    flow[-1]["configured"] = "библиотека настроена" in transcript
    transcript = do(
        "search",
        ["search", "булгаков мастер", "--output", str(out1)],
        ["1"],
    )
    extracted = _single_fb2(out1)
    flow[-1]["title_shown"] = KNOWN_TITLE in transcript
    flow[-1]["prompt_seen"] = "введите номер книги" in transcript
    flow[-1]["selected"] = extracted is not None
    if extracted is not None:
        extractions[KNOWN_TITLE] = sha256_file(extracted)

    before_update = _sources_snapshot(library)
    stage1_unchanged = before_update == before_setup

    _write_library(library, with_new_book=True)
    after_update = _sources_snapshot(library)

    transcript = do(
        "search_after_update",
        ["search", "новая", "--output", str(out2)],
        ["1"],
    )
    extracted_new = _single_fb2(out2)
    flow[-1]["title_shown"] = NEW_TITLE in transcript
    flow[-1]["prompt_seen"] = "введите номер книги" in transcript
    flow[-1]["selected"] = extracted_new is not None
    if extracted_new is not None:
        extractions[NEW_TITLE] = sha256_file(extracted_new)

    stage2_unchanged = _sources_snapshot(library) == after_update
    return flow, extractions, stage1_unchanged, stage2_unchanged


def _state_files_inside(bundle: Path) -> list[str]:
    violations: list[str] = []
    for current, _dirs, files in os.walk(bundle):
        for name in files:
            if name in _STATE_NAMES or name.endswith(_STATE_SUFFIXES):
                violations.append(str(Path(current) / name))
    return violations


def run_smoke(archive: Path, workdir: Path) -> dict[str, object]:
    """Выполнить изолированный smoke и записать ``workdir/smoke-report.json``."""
    _ensure_harness_imports()
    from tests.harness.permissions import readonly_bundle
    from tests.harness.tty import run_tty
    from tools.release_gate import minimum_os_evidence

    archive = Path(archive).resolve()
    workdir = Path(workdir).resolve()
    report: dict[str, object] = {
        "schema_version": REPORT_SCHEMA,
        "tool": "smoke_portable",
        "started_unix": time.time(),
        "archive": str(archive),
        "workdir": str(workdir),
        "failures": [],
    }
    flow: list[dict[str, object]] = []
    extractions: dict[str, str] = {}
    report["flow"] = flow
    report["extractions"] = extractions
    own_workdir = False

    try:
        if not archive.is_file():
            raise SmokeError(f"архив не найден: {archive}")
        prepare_workdir(workdir)
        own_workdir = True
        report["archive_sha256"] = sha256_file(archive)
        sidecar = archive.with_name(archive.name + ".sha256")
        if sidecar.is_file():
            declared = sidecar.read_text(encoding="utf-8").split()
            if not declared or declared[0].lower() != str(report["archive_sha256"]):
                raise SmokeError(f"sidecar-сумма архива не совпадает: {sidecar.name}")

        bundle, info = unpack_bundle(archive, workdir / "bundle")
        report["bundle_dir"] = str(bundle)
        report["build_info"] = info
        report["version"] = info["version"]
        report["target"] = info["target"]
        report["minimum_os"] = info["minimum_os"]
        report["minimum_glibc"] = info.get("minimum_glibc")

        facts = collect_host_facts()
        report.update(facts)
        system, machine = _expected_system_machine(str(info["target"]))
        if facts["host_os"] != system or facts["host_arch"] != machine:
            raise SmokeError(
                f"архив target {info['target']!r} не совпадает с этой машиной "
                f"({facts['host_os']}/{facts['host_arch']}); прогон на чужой "
                "архитектуре запрещён"
            )
        confirmed, reason = minimum_os_evidence(
            info["minimum_os"], info.get("minimum_glibc"), facts
        )
        report["minimum_os_verified"] = confirmed
        report["minimum_os_reason"] = reason

        sandbox = workdir / "sandbox"
        env = child_environment(sandbox)
        cwd = sandbox / "cwd"
        # Достигнуто только если python_free_path() не отказал: PATH реально
        # проверен поиском python/python3(/py) — наблюдение, а не флаг.
        report["no_system_python"] = True
        report["no_system_python_probes"] = list(
            ("python", "python3") + (("py",) if sys.platform == "win32" else ())
        )

        executable = bundle / _EXECUTABLE_BY_SYSTEM[system]
        report["bundle_executable"] = str(executable)
        if not executable.is_file():
            raise SmokeError(f"исполняемый файл комплекта отсутствует: {executable}")
        try:
            importable = importlib.util.find_spec("keklib_cli") is not None
        except (ImportError, ValueError):
            importable = False
        report["harness_keklib_cli_importable"] = importable
        report["source_checkout_absent"] = (not importable) and executable.resolve().is_relative_to(
            workdir.resolve()
        )

        library = workdir / "library"
        _write_library(library, with_new_book=False)
        out1 = sandbox / "out1"
        out2 = sandbox / "out2"
        out1.mkdir()
        out2.mkdir()
        config_path, index_path = _expected_state_paths(env, Path(env["HOME"]))

        with readonly_bundle(bundle):
            # Внутри блока запись в комплект реально запрещена (контекст
            # проверил это пробой до yield и поднимет исключение иначе).
            report["readonly_bundle"] = True

            def run(step: str, argv: list[str], commands: list[str]):
                result = run_tty(
                    [str(executable), *argv],
                    commands,
                    env=env,
                    cwd=cwd,
                    timeout=_TTY_TIMEOUT,
                )
                return result.returncode, result.stdout or ""

            flow, extractions, stage1, stage2 = _flow_steps(
                library, out1, out2, run=run
            )
            report["flow"] = flow
            report["extractions"] = extractions
            report["source_stage1_unchanged"] = stage1
            report["source_stage2_unchanged"] = stage2
            report["source_unchanged"] = stage1 and stage2

        report["fts5"] = (
            index_path.is_file()
            and KNOWN_TITLE in extractions
            and NEW_TITLE in extractions
        )
        report["extractions_expected"] = {
            KNOWN_TITLE: hashlib.sha256(KNOWN_PAYLOAD).hexdigest(),
            NEW_TITLE: hashlib.sha256(NEW_PAYLOAD).hexdigest(),
        }
        report["state_config"] = str(config_path)
        report["state_index"] = str(index_path)
        state_inside = _state_files_inside(bundle)
        report["state_outside_bundle"] = (
            config_path.is_file() and index_path.is_file() and not state_inside
        )
        report["state_inside_bundle_violations"] = state_inside

        for step in flow:
            if step["returncode"] != 0:
                raise SmokeError(
                    f"шаг {step['step']} завершился кодом {step['returncode']}"
                )
        if extractions != dict(report["extractions_expected"]):  # type: ignore[arg-type]
            raise SmokeError(
                f"SHA-256 извлечённых FB2 не совпадают с ожидаемыми: {extractions}"
            )
        for flag in ("fts5", "source_unchanged", "source_checkout_absent",
                     "readonly_bundle"):
            if report.get(flag) is not True:
                raise SmokeError(f"изоляция/наблюдение {flag} не подтверждено")
        if report["state_outside_bundle"] is not True:
            raise SmokeError("нативные пути состояния не подтверждены вне комплекта")
        # minimum_os_verified сознательно НЕ переводит smoke в отказ: это
        # наблюдение о хосте (dev-хост может не быть baseline-ОС). Отсутствие
        # нативного подтверждения минимума честно фиксируется False и
        # блокируется выпускным гейтом (validate_reports), а не подменяется
        # здесь успехом.
    except Exception as exc:  # честный отказ с полной диагностикой в отчёте
        report["failures"].append(f"{type(exc).__name__}: {exc}")
        flow.append({"step": "aborted", "error": f"{type(exc).__name__}: {exc}"})

    report["passed"] = not report["failures"]
    report["finished_unix"] = time.time()
    if own_workdir:
        write_report(workdir, report)
    print(console_payload(report))
    return report


def console_payload(report: Mapping[str, object]) -> str:
    """JSON-строка для stdout: чистый ASCII, безопасный для узких терминалов.

    ``ensure_ascii=True`` кодирует все не-ASCII символы как ``\\uXXXX``:
    консоль нативного CI (cp1252/ascii на Windows) не умеет кириллицу
    путей/диагностики, но ``json.loads`` вывода восстанавливает точные
    исходные поля и Unicode-семантику значений. Кодировки окружения не
    подменяются и UnicodeEncodeError не глотается: вывод просто не
    содержит не-ASCII байтов. Сохранённый ``smoke-report.json``
    остаётся UTF-8 c ``ensure_ascii=False`` (см. ``write_report``).
    """
    return json.dumps(report, ensure_ascii=True, indent=2, default=str)


def write_report(workdir: Path, report: Mapping[str, object]) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    target = workdir / REPORT_NAME
    temporary = workdir / (REPORT_NAME + ".tmp")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)  # собственный workdir — замена своей записи


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smoke_portable",
        description=(
            "Изолированный smoke portable-комплекта keklib-cli: безопасная "
            "распаковка в новый workdir, frozen executable через нативный TTY, "
            "изоляция child и отчёт smoke-report.json"
        ),
    )
    parser.add_argument(
        "--archive", type=Path, required=True, help="проверенный архив комплекта"
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        required=True,
        help="новый несуществующий собственный каталог прогона",
    )
    args = parser.parse_args(argv)

    _ensure_harness_imports()
    report = run_smoke(args.archive, args.workdir)
    return 0 if report.get("passed") is True else 1


if __name__ == "__main__":
    sys.exit(main())
