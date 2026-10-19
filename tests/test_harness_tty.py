"""Фокусные тесты нативного TTY-harness (`tests.harness.tty`).

Настоящие дочерние процессы в терминале: протокол приглашений (включая
разрыв многобайтного символа UTF-8 между чтениями PTY), реальный код
завершения, слитый transcript и таймаут. На POSIX исполняется PTY-ветка;
на Windows те же сценарии идут через ConPTY (модуль ``winpty``
дистрибутива ``pywinpty``) после установки
``packaging/requirements-test-windows.lock`` — это будет нативная
проверка, не mock. Статическая проверка имени импортируемого модуля ниже
прямо помечена статической и нативной проверкой не является.

Отдельный класс — синтетические регрессии cleanup-семантики pinned
``winpty`` (v3.1.0, Python-часть) на настоящих локальных
socket-дескрипторах: это воспроизведение точной логики upstream-модуля, а
НЕ нативный ConPTY-прогон (нативная Windows-проверка — задача 12).
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.harness import tty
from tests.harness.tty import (
    DEFAULT_PROMPT_MARKER,
    TtyTimeout,
    _shutdown_windows,
    run_tty,
)

#: Первый байт многобайтного символа маркера пишется отдельно от остатка:
#: воспроизводит разрыв UTF-8 кодовой точки между чтениями PTY.
_CHILD_SPLIT_MARKER = r"""
import sys, time
marker = sys.argv[1].encode("utf-8")
sys.stdout.buffer.write(marker[:1])
sys.stdout.flush()
time.sleep(0.15)
sys.stdout.buffer.write(marker[1:])
sys.stdout.flush()
answer = sys.stdin.readline()
sys.stdout.write("ECHO:" + answer.strip() + "\n")
sys.stdout.flush()
sys.exit(0 if answer.strip() == sys.argv[2] else 3)
"""

_CHILD_TWO_PROMPTS = r"""
import sys
marker = sys.argv[1]
answers = []
for name in ("FIRST", "SECOND"):
    sys.stdout.write(marker)
    sys.stdout.flush()
    answers.append(sys.stdin.readline().strip())
sys.stdout.write("FIRST:" + answers[0] + ";SECOND:" + answers[1] + "\n")
sys.stdout.flush()
"""

_CHILD_NEVER_EXITS = r"""
import sys, time
sys.stdout.write(sys.argv[1])
sys.stdout.flush()
time.sleep(120)
"""

_CHILD_EXIT_CODE = r"""
import sys
print("OUT-LINE")
print("ERR-LINE", file=sys.stderr)
sys.exit(7)
"""


class TtyHarnessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def _run(self, code, commands, *, timeout=30.0, argv=()):
        return run_tty(
            [sys.executable, "-c", code, *argv],
            commands,
            env=dict(os.environ),
            cwd=self.base,
            timeout=timeout,
        )

    def test_real_returncode_and_merged_transcript(self):
        """Реальный код завершения процесса; stdout и stderr слиты."""
        result = self._run(_CHILD_EXIT_CODE, [])
        self.assertEqual(result.returncode, 7)
        self.assertIn("OUT-LINE", result.stdout)
        self.assertIn("ERR-LINE", result.stdout)

    def test_prompt_split_inside_character(self):
        """Разрыв многобайтного символа маркера между чтениями PTY.

        Первый байт «в» маркера приходит отдельным чтением; независимый
        decode каждого чанка необратимо портил бы кодовую точку, и приглашение
        никогда не совпало бы — harness обязан дочитывать символ целиком.
        """
        result = self._run(
            _CHILD_SPLIT_MARKER, ["GO"], argv=(DEFAULT_PROMPT_MARKER, "GO")
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("ECHO:GO", result.stdout)
        self.assertNotIn("\ufffd", result.stdout.split("ECHO")[0])

    def test_commands_after_each_prompt(self):
        """Каждая команда отправляется после своего приглашения, по порядку."""
        result = self._run(
            _CHILD_TWO_PROMPTS, ["one", "two"], argv=(DEFAULT_PROMPT_MARKER,)
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("FIRST:one;SECOND:two", result.stdout)

    def test_timeout_raises_with_transcript(self):
        """Таймаут — TtyTimeout с уже прочитанным transcript, не зависание."""
        with self.assertRaises(TtyTimeout) as ctx:
            self._run(
                _CHILD_NEVER_EXITS, ["never"], timeout=1.0, argv=(DEFAULT_PROMPT_MARKER,)
            )
        self.assertIn(DEFAULT_PROMPT_MARKER.strip(), ctx.exception.transcript)

    def test_windows_import_uses_distribution_module(self):
        """Статическая проверка (НЕ нативный прогон): импорт из модуля winpty.

        Дистрибутив ``pywinpty==3.1.0`` предоставляет модуль ``winpty``
        (upstream pyproject: ``module-name = "winpty._winpty"``); модуля
        ``pywinpty`` не существует, и импорт из него не устранил бы
        ImportError на Windows. Нативная доступность проверяется отдельным
        тестом на самой Windows (``test_windows_tty_required``).
        """
        source = Path(tty.__file__).read_text(encoding="utf-8")
        self.assertIn("from winpty import PtyProcess", source)
        self.assertNotIn("from pywinpty import", source)


class _PinnedWinptyDouble:
    """Точный double Python-семантики ``winpty.PtyProcess`` v3.1.0.

    Воспроизводит существенное для ``_shutdown_windows`` по исходнику
    upstream ``winpty/ptyprocess.py`` (тег v3.1.0): ``isalive()``
    присваивает ``closed = not alive`` и возвращает статус; ``close()``
    закрывает ``fileobj``/``_server`` только при ``not closed`` (после
    смерти child — no-op); ``terminate(force=True)`` возвращает ``bool``
    через ``isalive``; ``close(force=True)`` поднимает ``IOError``, если
    child остался жив. Сокеты — настоящие локальные (listening + accepted,
    как в upstream), «child» — управляемый флаг. Синтетическая регрессия
    Python-логики pinned-зависимости, не нативный ConPTY-прогон.
    """

    def __init__(
        self,
        *,
        alive: bool,
        terminate_works: bool = True,
        terminate_raises: bool = False,
    ) -> None:
        self._alive = alive
        self._terminate_works = terminate_works
        self._terminate_raises = terminate_raises
        self.closed = False
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.socket()
        client.connect(server.getsockname())
        self.fileobj, _ = server.accept()  # сторона harness, как в upstream
        self._server = server
        self._client = client

    def isalive(self) -> bool:
        alive = self._alive
        self.closed = not alive  # точная строка pinned-реализации
        return alive

    def terminate(self, force=False):
        if not self.isalive():
            return True
        if self._terminate_raises:
            raise RuntimeError("terminate exploded")
        if self._terminate_works:
            self._alive = False
            return True
        return not force  # pinned: force-провал — False, без force — None

    def close(self, force=False):
        if not self.closed:  # gated: после смерти child — no-op
            self.fileobj.close()
            self._server.close()
            if self.isalive() and not self.terminate(force):
                raise IOError("Could not terminate the child.")
            self.closed = True

    def release(self) -> None:
        """Закрыть оставшиеся дескрипторы теста (пробуждение ждущих recv)."""
        for sock in (self._client, self.fileobj, self._server):
            try:
                sock.close()
            except OSError:
                pass


class WindowsShutdownSyntheticTests(unittest.TestCase):
    """Синтетические регрессии ``_shutdown_windows`` по pinned-семантике.

    НЕ нативная Windows-проверка: двойник повторяет точную Python-логику
    ``winpty`` v3.1.0 на реальных локальных socket-дескрипторах; нативный
    ConPTY-прогон (выход/exitcode/timeout) — задача 12 плана.
    """

    def setUp(self):
        self._processes: list[_PinnedWinptyDouble] = []
        self.addCleanup(self._release_all)

    def _release_all(self) -> None:
        for process in self._processes:
            process.release()

    def _process(self, **kwargs) -> _PinnedWinptyDouble:
        process = _PinnedWinptyDouble(**kwargs)
        self._processes.append(process)
        return process

    @staticmethod
    def _finished_thread() -> threading.Thread:
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
        return thread

    @staticmethod
    def _fds(process):
        return process.fileobj.fileno(), process._server.fileno()

    def test_dead_child_closes_owned_sockets(self):
        """Мёртвый child: pinned close() — no-op, ресурсы закрываются явно.

        Основной путь уже вызывал isalive (wait-цикл), поэтому closed=True и
        close() по upstream ничего не закрывает — дескрипторы обязаны
        закрываться явно, независимо от флага.
        """
        process = self._process(alive=False)
        self.assertFalse(process.isalive())
        self.assertTrue(process.closed)

        failure = _shutdown_windows(process, self._finished_thread())

        self.assertIsNone(failure)
        self.assertEqual(self._fds(process), (-1, -1))

    def test_socket_shutdown_wakes_blocked_reader(self):
        """shutdown сокета будит поток, заблокированный в recv, до close."""
        process = self._process(alive=False)
        process.isalive()
        received: list[bytes] = []
        reader = threading.Thread(
            target=lambda: received.append(process.fileobj.recv(16)), daemon=True
        )
        reader.start()
        time.sleep(0.15)  # дать потоку заблокироваться в recv на реальном сокете

        failure = _shutdown_windows(process, reader)
        reader.join(timeout=2.0)

        self.assertIsNone(failure)
        self.assertFalse(reader.is_alive())
        self.assertEqual(received, [b""])
        self.assertEqual(self._fds(process), (-1, -1))

    def test_terminate_exception_still_closes_sockets(self):
        """Исключение из terminate/провал — диагностика, но ВСЕ ресурсы закрыты."""
        process = self._process(alive=True, terminate_raises=True)

        failure = _shutdown_windows(process, self._finished_thread())

        self.assertIsNotNone(failure)
        self.assertIn("Windows", failure)
        self.assertEqual(self._fds(process), (-1, -1))

    def test_terminate_failure_reports_and_closes(self):
        """terminate(force=True) провален и close(force) бессилен: отчёт + закрытие."""
        process = self._process(alive=True, terminate_works=False)

        failure = _shutdown_windows(process, self._finished_thread())

        self.assertIsNotNone(failure)
        self.assertIn("гарантированно", failure)
        self.assertEqual(self._fds(process), (-1, -1))

    def test_stuck_reader_is_reported(self):
        """Незавершившийся reader фиксируется, а не молча проглатывается."""
        process = self._process(alive=False)
        process.isalive()
        stuck = threading.Thread(target=lambda: time.sleep(30), daemon=True)
        stuck.start()

        failure = _shutdown_windows(process, stuck)

        self.assertIsNotNone(failure)
        self.assertIn("reader", failure)
        self.assertEqual(self._fds(process), (-1, -1))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
