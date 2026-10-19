"""Нативные операции POSIX: блокировки через flock, память через getrusage,
публикация без перезаписи через hardlink с fallback на no-replace rename.

Модуль импортируется только вне Windows (см. выбор в ``platforms/__init__``):
``fcntl`` и ``resource`` здесь допустимы на верхнем уровне. Общие модули пакета
не должны импортировать их безусловно.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import platform
import resource
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..errors import ExtractionError, IndexBusyError, TargetExistsError


@contextmanager
def index_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
    """Shared (reader) или exclusive (writer) lock файла ``path`` без ожидания.

    Блокировка — ``flock(LOCK_SH | LOCK_NB)`` либо ``flock(LOCK_EX | LOCK_NB)``
    на одном открытом описании файла; живёт до закрытия дескриптора (или смерти
    процесса). Файл открывается ``O_RDWR | O_CREAT``: создаётся при отсутствии и
    никогда не удаляется и не заменяется ради «освобождения». Занятость
    (``EWOULDBLOCK``) отбрасывается как ``IndexBusyError``, прочие ``OSError``
    (права, IO) проходят как есть. Дескриптор закрывается в ``finally``.
    """
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as exc:
            raise IndexBusyError(
                f"индекс заблокирован другим процессом: {path}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _maxrss_to_kib(raw: int, system: str) -> int:
    """Пересчёт ``ru_maxrss`` в KiB: Linux — уже KiB, Darwin — байты."""
    if system == "Darwin":
        return raw // 1024
    return raw


def peak_rss_kib() -> int | None:
    """Пиковый RSS процесса в KiB через ``getrusage``.

    Возвращает ``None``, если измерение недоступно, а не 0. Разделение единиц
    берётся по реальной ОС: на Linux ``ru_maxrss`` уже в KiB, на Darwin — в
    байтах и делится на 1024.
    """
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
    except (OSError, ValueError):
        return None
    return _maxrss_to_kib(usage.ru_maxrss, platform.system())


# Публикация без перезаписи (§9.6).
#
# Базовый примитив — hardlink temp→target: атомарный no-clobber (EEXIST при
# существующем имени в любом виде, включая symlink) на всех обычных локальных
# ФС POSIX. Если ФС не поддерживает hardlink, используется атомарный rename
# без перезаписи: Linux — renameat2(2) c RENAME_NOREPLACE, Darwin —
# renamex_np(2) c RENAME_EXCL. Небезопасного fallback (copy/replace) нет:
# отсутствие безопасного примитива — ExtractionError.

#: Флаг renameat2(2): отказ вместо перезаписи существующего имени.
_RENAME_NOREPLACE = 1

#: Флаг renamex_np (Darwin): отказ вместо перезаписи существующего имени.
_RENAME_EXCL = 0x00000040

#: Относительные пути передаются syscall относительно cwd текущего потока.
_AT_FDCWD = -100

#: errno, при которых hardlink «не поддерживается» и допустим fallback на
#: no-replace rename: ФС без hardlink (EPERM/EOPNOTSUPP/ENOTSUP), ядро или
#: libc без флага (EINVAL/ENOSYS), исчерпание счётчика ссылок temp (EMLINK —
#: rename атомарен и не заботится о счётчике, fallback остаётся безопасным).
_UNSUPPORTED_LINK_ERRNOS = frozenset(
    {
        errno.EPERM,
        errno.EINVAL,
        errno.ENOSYS,
        errno.EOPNOTSUPP,
        errno.ENOTSUP,
        errno.EMLINK,
    }
)

#: errno «переименование без перезаписи не поддерживается».
_UNSUPPORTED_RENAME_ERRNOS = frozenset(
    {errno.EPERM, errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP}
)


def publish_no_replace(temp: Path, target: Path) -> None:
    """Опубликовать закрытый ``temp`` как ``target`` без перезаписи (§9.6).

    Вызывается после закрытия temp и источника. Существующее имя ``target``
    (файл, каталог, живая или битая symlink, коллизия регистра/нормализации
    на нечувствительных ФС) — ``TargetExistsError``; ``target`` никогда не
    удаляется для восстановления. Успех: hardlink temp→target и unlink
    только своего temp; при неподдерживаемости hardlink — атомарный
    no-replace rename (см. ``_rename_noreplace``). Прочие ``OSError``
    (права, I/O) проходят как есть.
    """
    try:
        os.link(temp, target)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise TargetExistsError(f"файл уже существует: {target}") from exc
        if exc.errno not in _UNSUPPORTED_LINK_ERRNOS:
            raise
        _rename_noreplace(temp, target)
    else:
        os.unlink(temp)


def _rename_noreplace(temp: Path, target: Path) -> None:
    """Атомарный rename без перезаписи: renameat2 (Linux) / renamex_np (Darwin).

    Нативные вызовы изолированы в ``_renameat2``/``_renamex_np`` и
    интерпретируют errno явно: EEXIST — конфликт, «неподдерживается» —
    ExtractionError (без ослабления гарантии), прочее — OSError.
    """
    temp_bytes = os.fsencode(temp)
    target_bytes = os.fsencode(target)
    if sys.platform == "darwin":
        code = _renamex_np(temp_bytes, target_bytes)
    else:
        code = _renameat2(temp_bytes, target_bytes)
    if code == 0:
        return
    if code == errno.EEXIST:
        raise TargetExistsError(f"файл уже существует: {target}")
    if code in _UNSUPPORTED_RENAME_ERRNOS:
        raise ExtractionError(
            f"файловая система не поддерживает безопасную публикацию без "
            f"перезаписи: {target} (ошибка {code}: {os.strerror(code)})"
        )
    raise OSError(code, os.strerror(code), str(target))


def _renameat2(temp: bytes, target: bytes) -> int:
    """Вызвать renameat2(2) c RENAME_NOREPLACE; вернуть errno (0 — успех).

    ``ctypes.CDLL(None)`` — символы libc текущего процесса: glibc экспортирует
    ``renameat2`` с 2.28; отсутствие символа — ENOSYS (безопасного примитива
    нет), а не обход через номера syscall, зависящие от архитектуры.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        function = libc.renameat2
    except AttributeError:
        return errno.ENOSYS
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    if function(_AT_FDCWD, temp, _AT_FDCWD, target, _RENAME_NOREPLACE) == 0:
        return 0
    return ctypes.get_errno()


def _renamex_np(temp: bytes, target: bytes) -> int:
    """Вызвать renamex_np(2) c RENAME_EXCL (Darwin); errno (0 — успех)."""
    libsystem = ctypes.CDLL(None, use_errno=True)
    function = libsystem.renamex_np
    function.argtypes = (
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    if function(temp, target, _RENAME_EXCL) == 0:
        return 0
    return ctypes.get_errno()
