"""Контекст read-only каталога для тестов (задача 12).

`readonly_bundle` делает распакованный portable-комплект доступным только
для чтения и **реально проверяет** невозможность создать в нём файл: если
изоляция не действует (например, запуск от root, где chmod обходится), контекст
поднимает `PermissionIsolationError`, а не «зелёнеет» флагом. Восстановление
в `finally` касается только записей, изменённых самим контекстом.

POSIX — chmod со снятием битов записи (символические ссылки не трогаются:
chmod по ссылке менял бы цель). Windows — NTFS ACL через `icacls` (deny
на создание файлов с наследованием), а не DOS-атрибут readonly папки: ACL
корня сохраняется перед изменением и восстанавливается точно; адресация
save/restore передаётся с явным cwd (каталог родителя корня). Windows-ветка
написана fail-closed, но **нативной NTFS-валидации не выполнялось** — до
нативного Windows-прогона любые заявления о ней исключены; синтетическая
регрессия argv/cwd — в tests/test_release.py, реальную работу на NTFS
проверяет только нативный прогон.

Модуль не импортирует приложение `keklib_cli` и пригоден вне checkout.
"""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

__all__ = ["PermissionIsolationError", "readonly_bundle"]

#: Имя файла-пробы: создаётся только если изоляция НЕ действует (сбой).
PROBE_NAME = ".keklib-readonly-probe"

#: Deny-ACE «Все» на создание файлов/каталогов с наследованием вниз по дереву.
_WINDOWS_DENY = "*S-1-1-0:(OI)(CI)(WD,AD)"


class PermissionIsolationError(RuntimeError):
    """Read-only изоляция не действует или не восстановилась."""


@contextlib.contextmanager
def readonly_bundle(path: Path) -> Iterator[None]:
    """Выполнить блок с каталогом ``path``, доступным только для чтения.

    Перед входом проверяет реальный отказ в создании файла; после блока
    восстанавливает исходные права/ACL и проверяет, что запись снова
    возможна. Любой сбой изоляции или восстановления — исключение, а не
    молчаливый успех.
    """
    path = Path(path)
    if not path.is_dir():
        raise PermissionIsolationError(f"каталог не существует: {path}")
    if sys.platform == "win32":
        yield from _readonly_windows(path)
    else:
        yield from _readonly_posix(path)


# --- POSIX -------------------------------------------------------------------


def _walk_entries(root: Path) -> list[Path]:
    """Все реальные записи дерева без следования по ссылкам (сами ссылки не входят)."""
    entries: list[Path] = [root]
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in dirs + files:
            candidate = current_path / name
            if stat.S_ISLNK(candidate.lstat().st_mode):
                continue  # chmod по ссылке менял бы её цель — не трогаем
            entries.append(candidate)
    return entries


def _readonly_posix(root: Path) -> Iterator[None]:
    entries = _walk_entries(root)
    original = [
        (entry, stat.S_IMODE(entry.lstat().st_mode)) for entry in entries
    ]
    try:
        for entry, _mode in original:
            os.chmod(entry, stat.S_IMODE(entry.lstat().st_mode) & ~0o222)
        _probe_denied(root, entries)
        yield
    finally:
        restore_error: BaseException | None = None
        try:
            for entry, mode in original:
                os.chmod(entry, mode)
        except OSError as exc:
            restore_error = PermissionIsolationError(
                f"не восстановлены права {root}: {exc}"
            )
        _probe_allowed(root)
        if restore_error is not None:
            raise restore_error


# --- Windows -------------------------------------------------------------------


def _icacls(argv: list[str], *, cwd: Path | None = None) -> None:
    """Выполнить icacls; ``cwd`` задаётся явно адресующему шагу.

    Адресация save/restore относительная (имя корня / ``.``) и осмысленна
    только в каталоге родителя корня — поэтому cwd передаётся явно каждым
    вызовом, а не наследуется от cwd процесса (он может быть каким угодно:
    checkout, workspace).
    """
    result = subprocess.run(
        ["icacls", *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
    )
    if result.returncode != 0:
        raise PermissionIsolationError(
            f"icacls {' '.join(argv[:2])}… вернул {result.returncode}: "
            f"{result.stderr.strip()}"
        )


def _readonly_windows(root: Path) -> Iterator[None]:
    # ACL корня сохраняется до изменения и восстанавливается точно: изменяем
    # только свою deny-запись на корне, наследование распространяет её вниз.
    # /save и /restore адресуются относительными именами из root.parent —
    # cwd обоих вызовов задаётся явно (не cwd процесса). Нативная проверка
    # NTFS-ACL не выполнялась; синтетическая регрессия argv/cwd — в
    # tests/test_release.py.
    with tempfile.TemporaryDirectory(prefix="keklib-acl-") as tmp:
        saved = Path(tmp) / "acl.txt"
        _icacls([root.name, "/save", str(saved)], cwd=root.parent)
        try:
            _icacls([str(root), "/deny", _WINDOWS_DENY])
            _probe_denied(root, [root])
            yield
        finally:
            restore_error: BaseException | None = None
            try:
                _icacls([".", "/restore", str(saved)], cwd=root.parent)
            except PermissionIsolationError as exc:
                restore_error = exc
            _probe_allowed(root)
            if restore_error is not None:
                raise restore_error


# --- Пробы -------------------------------------------------------------------


def _probe_dirs(root: Path, entries: list[Path]) -> list[Path]:
    """Каталоги для пробы: корень и самый глубокий подкаталог дерева."""
    dirs = [entry for entry in entries if entry != root and entry.is_dir()]
    deepest = max(dirs, key=lambda item: len(item.parts), default=None)
    return [root] + ([deepest] if deepest is not None else [])


def _probe_denied(root: Path, entries: list[Path]) -> None:
    """Создание файла обязано падать; успех — отказ изоляции, не успех теста."""
    problems = []
    for directory in _probe_dirs(root, entries):
        probe = directory / PROBE_NAME
        try:
            handle = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except OSError:
            continue  # ожидаемо: создание запрещено
        os.close(handle)
        probe.unlink()
        problems.append(str(directory))
    if problems:
        raise PermissionIsolationError(
            "запись в read-only каталог оказалась возможна (запуск от root или "
            f"изоляция не действует) в: {', '.join(problems)}"
        )


def _probe_allowed(root: Path) -> None:
    """После восстановления запись должна снова быть возможной."""
    probe = root / PROBE_NAME
    try:
        handle = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError as exc:
        raise PermissionIsolationError(
            f"после восстановления права записи не вернулись в {root}: {exc}"
        ) from exc
    os.close(handle)
    probe.unlink()
