"""Нативные операции Windows: LockFileEx/UnlockFileEx, GetProcessMemoryInfo
и публикация файла без перезаписи.

Все вызовы WinAPI идут через ``ctypes`` с зафиксированными argtypes/restype;
``msvcrt.locking`` не используется, потому что он не даёт shared-блокировок.
Модуль импортируется на любой ОС, но привязки DLL создаются только на win32:
импорт пакета на POSIX не должен требовать Windows DLL. Вызов нативных функций
вне Windows — ошибка программирования (``index_lock``, ``publish_no_replace``)
или недоступное измерение (``peak_rss_kib`` → ``None``).

Все процессы блокируют один и тот же общий байтовый диапазон (1 байт со смещения
0) одного и того же файла; файл не удаляется и не заменяется ради «освобождения».
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..errors import ExtractionError, IndexBusyError, TargetExistsError

# Типы WinAPI без ctypes.wintypes, чтобы импорт модуля не тянул лишнего вне Windows.
_DWORD = ctypes.c_ulong
_BOOL = ctypes.c_int
_HANDLE = ctypes.c_void_p
_LPCWSTR = ctypes.c_wchar_p
_LPSECURITY_ATTRIBUTES = ctypes.c_void_p


class _OVERLAPPED(ctypes.Structure):
    """OVERLAPPED с указательными полями (ULONG_PTR Internal/InternalHigh, HANDLE hEvent)."""

    _fields_ = (
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        # union { DWORD Offset; DWORD OffsetHigh; }: 8 байт на 32- и 64-бит.
        ("Offset", _DWORD),
        ("OffsetHigh", _DWORD),
        ("hEvent", _HANDLE),
    )


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS; поля размера — SIZE_T (указательный размер).

    Порядок полей — точно по WinAPI. Layout не проверяется на POSIX:
    `c_ulong` на Linux не обязан совпадать по размеру с DWORD Windows,
    поэтому Linux-размер структуры не доказательство Windows ABI; нативная
    проверка возможна только на Windows.
    """

    _fields_ = (
        ("cb", _DWORD),
        ("PageFaultCount", _DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    )


_ERROR_LOCK_VIOLATION = 33
_ERROR_FILE_EXISTS = 80  # ERROR_FILE_EXISTS
_ERROR_ALREADY_EXISTS = 183  # ERROR_ALREADY_EXISTS
# Winerror, при которых hardlink «не поддерживается» и допустим fallback на
# os.rename: ERROR_INVALID_FUNCTION (1), ERROR_NOT_SUPPORTED (50),
# ERROR_INVALID_PARAMETER (87) — например, ФС без hardlink.
_UNSUPPORTED_LINK_WINERRORS = frozenset({1, 50, 87})
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_kernel32: ctypes.WinDLL | None = None
_get_process_memory_info = None

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        _psapi = ctypes.WinDLL("psapi", use_last_error=True)
        _get_process_memory_info = _psapi.GetProcessMemoryInfo
    except OSError:
        # Начиная с Windows 7 экспорт живёт в kernel32 под именем K32*.
        _get_process_memory_info = _kernel32.K32GetProcessMemoryInfo

    _kernel32.LockFileEx.argtypes = (
        _HANDLE,
        _DWORD,
        _DWORD,
        _DWORD,
        _DWORD,
        ctypes.POINTER(_OVERLAPPED),
    )
    _kernel32.LockFileEx.restype = _BOOL
    _kernel32.UnlockFileEx.argtypes = (
        _HANDLE,
        _DWORD,
        _DWORD,
        _DWORD,
        ctypes.POINTER(_OVERLAPPED),
    )
    _kernel32.UnlockFileEx.restype = _BOOL
    _kernel32.CreateFileW.argtypes = (
        _LPCWSTR,
        _DWORD,
        _DWORD,
        _LPSECURITY_ATTRIBUTES,
        _DWORD,
        _DWORD,
        _HANDLE,
    )
    _kernel32.CreateFileW.restype = _HANDLE
    _kernel32.CloseHandle.argtypes = (_HANDLE,)
    _kernel32.CloseHandle.restype = _BOOL
    _kernel32.GetCurrentProcess.argtypes = ()
    _kernel32.GetCurrentProcess.restype = _HANDLE
    _get_process_memory_info.argtypes = (
        _HANDLE,
        ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
        _DWORD,
    )
    _get_process_memory_info.restype = _BOOL


@contextmanager
def index_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
    """Shared (reader) или exclusive (writer) lock файла ``path`` без ожидания.

    Exclusive-флаг (``LOCKFILE_EXCLUSIVE_LOCK``) ставится только для writer;
    readers берут общий диапазон с ``LOCKFILE_FAIL_IMMEDIATELY`` без
    exclusive-флага. Отказ по занятости — ``ERROR_LOCK_VIOLATION`` →
    ``IndexBusyError``; прочие ошибки WinAPI (права, IO) поднимаются через
    ``ctypes.WinError``. Дескриптор закрывается в ``finally``.
    """
    if _kernel32 is None:
        raise RuntimeError("windows.index_lock доступен только на Windows")
    handle = _kernel32.CreateFileW(
        str(path),
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_ALWAYS,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    overlapped = _OVERLAPPED()  # Offset = OffsetHigh = 0: диапазон с начала файла.
    flags = _LOCKFILE_FAIL_IMMEDIATELY | (
        _LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0
    )
    try:
        if not _kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
            error_code = ctypes.get_last_error()
            if error_code == _ERROR_LOCK_VIOLATION:
                raise IndexBusyError(
                    f"индекс заблокирован другим процессом: {path}"
                )
            raise ctypes.WinError(error_code)
        try:
            yield
        finally:
            # Снят из-под успешно взятой блокировки того же дескриптора; результат
            # не проверяем, чтобы не маскировать исходное исключение.
            _kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
    finally:
        _kernel32.CloseHandle(handle)


def peak_rss_kib() -> int | None:
    """Пиковый RSS процесса в KiB через ``GetProcessMemoryInfo``.

    ``PeakWorkingSetSize`` приходит в байтах и делится на 1024. Возвращает
    ``None``, если измерение недоступно (вызов вне Windows или отказ WinAPI),
    а не 0.
    """
    if _kernel32 is None or _get_process_memory_info is None:
        return None
    counters = _PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    process = _kernel32.GetCurrentProcess()
    if not _get_process_memory_info(
        process, ctypes.byref(counters), counters.cb
    ):
        return None
    return counters.PeakWorkingSetSize // 1024


def _is_conflict(error: OSError) -> bool:
    """Существующее имя цели: EEXIST либо Windows ERROR_FILE_EXISTS/ALREADY_EXISTS.

    Конфликт — всегда отказ, а не повод для перезаписи (§9.6).
    """
    if error.errno == errno.EEXIST:
        return True
    return getattr(error, "winerror", None) in (
        _ERROR_FILE_EXISTS,
        _ERROR_ALREADY_EXISTS,
    )


def publish_no_replace(temp: Path, target: Path) -> None:
    """Опубликовать закрытый ``temp`` как ``target`` без перезаписи (§9.6).

    Вызывается после закрытия temp и источника. Базовый примитив — hardlink
    ``os.link`` (NTFS); при неподдерживаемости — ``os.rename``: на Windows он
    выполняет MoveFileExW без MOVEFILE_REPLACE_EXISTING, то есть отказывает,
    если имя существует. Существующее имя (файл, каталог, symlink, коллизия
    регистра/нормализации на NTFS) — ``TargetExistsError``; ``target``
    никогда не удаляется. Небезопасного fallback нет: отсутствие безопасного
    примитива — ``ExtractionError``. Прочие ``OSError`` проходят как есть.
    """
    if _kernel32 is None:
        raise RuntimeError("windows.publish_no_replace доступен только на Windows")
    try:
        os.link(temp, target)
    except OSError as exc:
        if _is_conflict(exc):
            raise TargetExistsError(f"файл уже существует: {target}") from exc
        if getattr(exc, "winerror", None) not in _UNSUPPORTED_LINK_WINERRORS:
            raise
        try:
            os.rename(temp, target)
        except OSError as rename_error:
            if _is_conflict(rename_error):
                raise TargetExistsError(
                    f"файл уже существует: {target}"
                ) from rename_error
            if getattr(rename_error, "winerror", None) in (
                _UNSUPPORTED_LINK_WINERRORS
            ):
                raise ExtractionError(
                    f"файловая система не поддерживает безопасную публикацию "
                    f"без перезаписи: {target} (winerror "
                    f"{getattr(rename_error, 'winerror', None)})"
                ) from rename_error
            raise
    else:
        os.unlink(temp)
