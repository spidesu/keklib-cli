"""Нативный TTY-harness: запуск процесса в настоящем терминале.

POSIX — pseudoterminal через ``pty``/``select``: stdin, stdout и stderr
дочернего процесса подключаются к одному PTY, поэтому transcript сливается
естественно, а код завершения — настоящий, от ``Popen.wait``. Windows —
ConPTY через test-only зависимость ``pywinpty`` (зафиксирована в
``packaging/requirements-test-windows.lock``); приложение и общий CLI от
этой зависимости не зависят.

Команды передаются строго после появления приглашения: harness ищет в
накопленном transcript маркер приглашения (по умолчанию — приглашение
выбора книги keklib-cli) и только затем пишет следующую команду. Истечение
``timeout`` — ``TtyTimeout`` с уже прочитанным transcript; собственный
подпроцесс гарантированно завершается (terminate/kill + wait), чужие
процессы не затрагиваются.

Модуль не импортирует приложение ``keklib_cli`` и пригоден вне checkout.
``pty`` (POSIX) и ``pywinpty`` (Windows) импортируются лениво внутри ветки
своей ОС, поэтому сам модуль импортируется на любой платформе — в том
числе тестом наличия Windows-harness на POSIX.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

__all__ = ["TtyTimeout", "run_tty"]

#: Маркер приглашения по умолчанию — суффикс приглашения выбора книги
#: keklib-cli («Страница i из k; введите номер книги, n, p или q: »).
DEFAULT_PROMPT_MARKER = "введите номер книги, n, p или q: "

_CHUNK_BYTES = 4096


class TtyTimeout(RuntimeError):
    """TTY-прогон не уложился в timeout; ``transcript`` — прочитанное."""

    def __init__(self, message: str, transcript: str) -> None:
        super().__init__(message)
        self.transcript = transcript


def run_tty(
    argv: Sequence[str],
    commands: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float = 30.0,
    prompt_marker: str = DEFAULT_PROMPT_MARKER,
) -> subprocess.CompletedProcess[str]:
    """Выполнить ``argv`` в настоящем TTY, передавая команды по приглашению.

    stdout и stderr дочернего процесса сливаются в один transcript — он же
    эхо-вывод терминала на введённые команды. Команда отправляется только
    после появления ``prompt_marker`` в ещё не обработанной части
    transcript; если процесс завершился раньше (например, пустая выдача),
    оставшиеся команды не отправляются, и это не ошибка.

    :param argv: команда и аргументы (элементы приводятся к ``str``).
    :param commands: строки для отправки после каждого приглашения.
    :param env: окружение дочернего процесса (полное, не надстройка).
    :param cwd: рабочий каталог дочернего процесса.
    :param timeout: общий бюджет на запуск и завершение, секунды.
    :param prompt_marker: подстрока-признак приглашения.
    :return: ``CompletedProcess`` с реальным ``returncode``; слитый
        transcript — в ``stdout`` (``stderr`` равен ``None``).
    :raises TtyTimeout: бюджет исчерпан; дочерний процесс остановлен,
        частичный transcript — в исключении.
    :raises RuntimeError: на Windows не установлен test-only ``pywinpty``
        либо он не сообщил код завершения.
    """
    if sys.platform == "win32":
        return _run_windows(
            argv,
            commands,
            env=env,
            cwd=cwd,
            timeout=timeout,
            prompt_marker=prompt_marker,
        )
    return _run_posix(
        argv,
        commands,
        env=env,
        cwd=cwd,
        timeout=timeout,
        prompt_marker=prompt_marker,
    )


def _run_posix(
    argv: Sequence[str],
    commands: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float,
    prompt_marker: str,
) -> subprocess.CompletedProcess[str]:
    """PTY через ``pty.openpty``: один терминал на stdin/stdout/stderr."""
    import pty
    import select

    argv_list = [str(item) for item in argv]
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            argv_list,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=dict(env),
            cwd=str(cwd),
            close_fds=True,
        )
    except BaseException:
        os.close(master)
        os.close(slave)
        raise
    os.close(slave)  # родитель держит только master
    # Инкрементный декодер: os.read может разделить UTF-8 кодовую точку
    # между чтениями, и независимый decode каждого чанка необратимо заменял
    # бы её символом замены — кириллическое приглашение никогда не совпало бы.
    import codecs

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    transcript = ""
    search_from = 0
    pending = list(commands)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if pending:
                index = transcript.find(prompt_marker, search_from)
                if index != -1:
                    try:
                        _write_all(master, (pending.pop(0) + "\n").encode("utf-8"))
                    except OSError:
                        break  # дочерняя сторона закрылась между приглашением и записью
                    search_from = index + len(prompt_marker)
                    continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TtyTimeout(
                    f"TTY-таймаут {timeout} с истёк: {argv_list}", transcript
                )
            ready, _, _ = select.select([master], [], [], remaining)
            if not ready:
                continue
            try:
                chunk = os.read(master, _CHUNK_BYTES)
            except OSError:
                break  # EIO: дочерняя сторона закрыла PTY
            if not chunk:
                break
            transcript += decoder.decode(chunk)
        # Незавершённая кодовая точка в самом конце потока фиксируется
        # символом замены финализацией декодера, а не теряется.
        transcript += decoder.decode(b"", final=True)
        try:
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise TtyTimeout(
                f"процесс не завершился за {timeout} с: {argv_list}", transcript
            ) from None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        os.close(master)
    return subprocess.CompletedProcess(argv_list, returncode, stdout=transcript, stderr=None)


def _run_windows(
    argv: Sequence[str],
    commands: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float,
    prompt_marker: str,
) -> subprocess.CompletedProcess[str]:
    """ConPTY через test-only ``winpty`` (дистрибутив ``pywinpty``).

    Импорт именно из модуля ``winpty``: upstream ``pywinpty==3.1.0``
    собирает расширение как ``winpty._winpty`` и экспортирует
    ``PtyProcess`` из пакета ``winpty`` — модуля ``pywinpty`` не существует.
    """
    import queue
    import threading

    try:
        from winpty import PtyProcess
    except ImportError as exc:
        raise RuntimeError(
            "нативный Windows TTY-harness не установлен: модуль winpty "
            "(дистрибутива pywinpty) отсутствует; установите test-only "
            "зависимость из packaging/requirements-test-windows.lock"
        ) from exc

    argv_list = [str(item) for item in argv]
    process = PtyProcess.spawn(argv_list, cwd=str(cwd), env=dict(env))
    chunks: queue.Queue = queue.Queue()

    def _reader() -> None:
        try:
            while True:
                chunks.put(process.read())
        except BaseException:
            chunks.put(None)  # EOFError/закрытие PTY — конец вывода

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    transcript = ""
    search_from = 0
    pending = list(commands)
    deadline = time.monotonic() + timeout
    primary: BaseException | None = None
    try:
        while True:
            if pending:
                index = transcript.find(prompt_marker, search_from)
                if index != -1:
                    try:
                        process.write(pending.pop(0) + "\r\n")
                    except EOFError:
                        continue  # child закрыл PTY между приглашением и записью
                    search_from = index + len(prompt_marker)
                    continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TtyTimeout(
                    f"TTY-таймаут {timeout} с истёк: {argv_list}", transcript
                )
            try:
                piece = chunks.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if piece is None:
                break
            transcript += piece
        while process.isalive():
            if time.monotonic() >= deadline:
                raise TtyTimeout(
                    f"процесс не завершился за {timeout} с: {argv_list}", transcript
                )
            time.sleep(0.01)
        returncode = process.exitstatus
        if not isinstance(returncode, int):
            raise RuntimeError(
                f"winpty не сообщил код завершения (exitstatus={returncode!r}): "
                f"{argv_list}"
            )
    except BaseException as exc:
        primary = exc
        raise
    finally:
        # Явное освобождение ресурсов и завершение reader на каждом пути;
        # сбой cleanup не маскирует первичное исключение, но на чистом пути
        # сообщает о себе RuntimeError.
        failure = _shutdown_windows(process, thread)
        if primary is None and failure is not None:
            raise RuntimeError(failure)
    return subprocess.CompletedProcess(argv_list, returncode, stdout=transcript, stderr=None)


#: Бюджет ожидания reader-потока при закрытии; превышение фиксируется
#: диагностикой, а не «гарантируется» непроверенным join.
_READER_JOIN_SECONDS = 1.0


def _shutdown_windows(process, thread) -> str | None:
    """Явное освобождение ресурсов pinned ``winpty`` v3.1.0 на каждом пути.

    Точная семантика upstream (``winpty/ptyprocess.py``, тег v3.1.0):
    ``isalive()`` присваивает ``closed = not alive``, а ``close()`` закрывает
    ``fileobj``/``_server`` только при ``not closed``. Поэтому после смерти
    child — включая wait-цикл основного пути и ``terminate``, которые сами
    зовут ``isalive`` — вызов ``close()`` уже ничего не закрывает. Здесь
    закрытие выполняется явно и независимо от статуса child и флага
    ``closed``; сам флаг не сбрасывается (побочные эффекты такого сброса
    не обоснованы). Живой child сначала останавливается
    ``terminate(force=True)`` с проверкой результата; провал — штатный
    ``close(force=True)`` upstream (kill; IOError — диагностика).
    ``shutdown`` сокета harness будит то, что блокировано на его стороне,
    ещё до ``close``. Каждый шаг защищён: сбой одного не отменяет остальные.
    ``thread.join`` проверяется ``is_alive``: переживший возврат reader —
    диагностика, а не молчаливый «успех».

    Синтетические регрессии этой Python-семантики — в
    ``tests/test_harness_tty.py``; нативная Windows-проверка — задача 12.
    Функция не поднимает исключений: диагностикой управляет вызывающий.
    """
    import socket

    problems: list[str] = []
    try:
        if process.isalive():
            terminated = process.terminate(True)
            if terminated is not True and process.isalive():
                try:
                    process.close(True)
                except Exception as exc:
                    problems.append(
                        "не удалось гарантированно завершить собственный "
                        f"Windows-процесс: {exc}"
                    )
    except Exception as exc:
        problems.append(f"завершение Windows-процесса не удалось: {exc}")
    # Явное закрытие собственных socket-ресурсов harness независимо от
    # флага closed (см. докстринг): единственный путь, гарантирующий
    # освобождение дескрипторов после смерти child.
    fileobj = getattr(process, "fileobj", None)
    if fileobj is not None:
        try:
            fileobj.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # уже закрыт/не подключён — close ниже обязателен в любом случае
        try:
            fileobj.close()
        except OSError as exc:
            problems.append(f"закрытие fileobj PTY не удалось: {exc}")
    server = getattr(process, "_server", None)
    if server is not None:
        try:
            server.close()
        except OSError as exc:
            problems.append(f"закрытие серверного сокета PTY не удалось: {exc}")
    thread.join(timeout=_READER_JOIN_SECONDS)
    if thread.is_alive():
        problems.append(
            f"reader-поток PTY не завершился за {_READER_JOIN_SECONDS:g} с"
        )
    if not problems:
        return None
    return "; ".join(problems)


def _write_all(fd: int, data: bytes) -> None:
    """Записать всё; частичная запись PTY добирается повтором."""
    while data:
        written = os.write(fd, data)
        data = data[written:]
