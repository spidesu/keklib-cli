"""Тесты нативных путей состояния и shared/exclusive блокировок (§5 спецификации).

Проверки блокировок используют настоящие дочерние процессы (`sys.executable`)
с синхронизацией через pipe: готовность и завершение — обмен строками, без
sleep как условия корректности и без наследования уже взятой блокировки. Все
ожидания ограничены защитными таймаутами: зависший или блокирующийся дочерний
процесс завершается terminate/kill, и тест падает ограниченно, а не зависает;
таймаут — только защита от зависания, не замена синхронизации. Нативные
проверки Windows/macOS присутствуют в файле, но на чужой ОС пропускаются с
честным указанием, что нативный прогон недоступен; симуляция путей другой ОС
дополняет, а не заменяет нативные проверки.
"""

from __future__ import annotations

import contextlib
import errno
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from keklib_cli.errors import ExtractionError, TargetExistsError
from keklib_cli.models import StatePaths
from keklib_cli.platforms import peak_rss_kib, publish_no_replace, state_paths

_WAIT_SECONDS = 5.0  # защитный предел ожидания события дочернего процесса
_SUBPROCESS_SECONDS = 30.0  # защитный предел для импорт-подпроцессов
_REGRESSION_SECONDS = 1.5  # ускоренный предел для регрессионных проверок

# Дочерний процесс: берёт блокировку, сообщает READY в stdout, ждёт строку из
# stdin (режим hold) либо умирает, держа блокировку, без явного unlock (die).
_LOCK_CHILD = """
import os
import sys
from pathlib import Path

from keklib_cli.platforms import index_lock

mode, lock_path, behaviour = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
with index_lock(lock_path, exclusive=(mode == "exclusive")):
    sys.stdout.write("READY\\n")
    sys.stdout.flush()
    if behaviour == "die":
        os._exit(0)
    sys.stdin.readline()
sys.stdout.write("RELEASED\\n")
sys.stdout.flush()
"""

# Конфликтующая попытка входа: печатает BUSY при IndexBusyError либо ACQUIRED,
# если блокировка ошибочно взята (после ACQUIRED держит до строки из stdin).
_ATTEMPT_CHILD = """
import sys
from pathlib import Path

from keklib_cli.errors import IndexBusyError
from keklib_cli.platforms import index_lock

mode, lock_path = sys.argv[1], Path(sys.argv[2])
try:
    with index_lock(lock_path, exclusive=(mode == "exclusive")):
        sys.stdout.write("ACQUIRED\\n")
        sys.stdout.flush()
        sys.stdin.readline()
except IndexBusyError:
    sys.stdout.write("BUSY\\n")
    sys.stdout.flush()
"""

# Никогда не сообщает READY — симуляция зависшего процесса (регресс I3).
_NEVER_READY_CHILD = """
import sys

sys.stdin.readline()
sys.stdout.write("READY\\n")
sys.stdout.flush()
"""

# Блокирующая (без LOCK_NB) попытка writer-блокировки против shared-держателя
# — симуляция потери немедленного отказа (регресс I3); сознательно POSIX-only.
_BLOCKING_CHILD = """
import fcntl
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(descriptor, fcntl.LOCK_EX)  # намеренно без LOCK_NB
sys.stdout.write("READY\\n")
sys.stdout.flush()
sys.stdin.readline()
os.close(descriptor)
"""

# Гонка публикации: создаёт temp-файлы всех раундов, сообщает READY, ждёт GO
# из stdin (двусторонний барьер — попытки обоих процессов начинаются только
# после готовности обоих) и параллельно с родителем публикует в общие цели.
# Печатает «W <round>» за каждую победу и «DONE <count>»; проигранные temp
# удаляет сам.
_PUBLISH_RACE_CHILD = """
import os
import sys
import tempfile
from pathlib import Path

from keklib_cli.errors import TargetExistsError
from keklib_cli.platforms import publish_no_replace

base, rounds = Path(sys.argv[1]), int(sys.argv[2])
temps = {}
for r in range(rounds):
    fd, name = tempfile.mkstemp(dir=base, prefix=f".race-child-{r}-")
    with os.fdopen(fd, "wb") as sink:
        sink.write(f"child-{r}".encode("ascii"))
    temps[r] = Path(name)
sys.stdout.write("READY\\n")
sys.stdout.flush()
sys.stdin.readline()  # ждём GO родителя: одновременный старт попыток
wins = []
for r in range(rounds):
    try:
        publish_no_replace(temps[r], base / f"race-{r}.target")
    except TargetExistsError:
        temps[r].unlink()
    else:
        wins.append(r)
for r in wins:
    sys.stdout.write(f"W {r}\\n")
sys.stdout.write(f"DONE {len(wins)}\\n")
sys.stdout.flush()
"""


def _file_identity(path: Path) -> tuple[int, int]:
    """Идентичность файла, поддерживаемая платформой (POSIX: dev/inode).

    На Windows `os.stat` заполняет st_dev/st_ino индексом файла; равенство
    идентичности до и после сценария — отсутствие удаления/замены файла,
    более сильное утверждение, чем одно `exists()`.
    """
    status = os.stat(path)
    return (status.st_dev, status.st_ino)


class PlatformTests(unittest.TestCase):
    """Нативные пути (§5), блокировки index.lock, пиковая память и публикация."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir(parents=True)

    # Пути.

    def test_native_paths(self) -> None:
        """Все три строки таблицы §5: Linux (XDG и умолчания), Windows, macOS."""
        xdg_config = self.home / "xdg-config"
        xdg_cache = self.home / "xdg-cache"
        linux = state_paths(
            system="Linux",
            env={
                "XDG_CONFIG_HOME": str(xdg_config),
                "XDG_CACHE_HOME": str(xdg_cache),
            },
            home=self.home,
        )
        self.assertEqual(linux.config, xdg_config / "keklib-cli" / "config.json")
        self.assertEqual(linux.cache, xdg_cache / "keklib-cli")
        self.assertEqual(linux.index, xdg_cache / "keklib-cli" / "index.sqlite3")

        linux_default = state_paths(system="Linux", env={}, home=self.home)
        self.assertEqual(
            linux_default.config,
            self.home / ".config" / "keklib-cli" / "config.json",
        )
        self.assertEqual(
            linux_default.index,
            self.home / ".cache" / "keklib-cli" / "index.sqlite3",
        )

        appdata = self.home / "AppData" / "Roaming"
        localappdata = self.home / "AppData" / "Local"
        windows = state_paths(
            system="Windows",
            env={"APPDATA": str(appdata), "LOCALAPPDATA": str(localappdata)},
            home=self.home,
        )
        self.assertEqual(windows.config, appdata / "keklib-cli" / "config.json")
        self.assertEqual(windows.cache, localappdata / "keklib-cli" / "Cache")
        self.assertEqual(
            windows.index,
            localappdata / "keklib-cli" / "Cache" / "index.sqlite3",
        )

        darwin = state_paths(system="Darwin", env={}, home=self.home)
        self.assertEqual(
            darwin.config,
            self.home / "Library" / "Application Support" / "keklib-cli" / "config.json",
        )
        self.assertEqual(
            darwin.index,
            self.home / "Library" / "Caches" / "keklib-cli" / "index.sqlite3",
        )

        # Нативная ОС без подмен: пути строятся из реального окружения.
        native = state_paths()
        self.assertIsInstance(native, StatePaths)
        self.assertTrue(native.config.is_absolute())
        self.assertTrue(native.cache.is_absolute())
        self.assertTrue(native.index.is_absolute())
        self.assertEqual(native.index.parent, native.cache)

    def test_xdg_fallback(self) -> None:
        """Пустые и относительные XDG-переменные игнорируются: `.config`/`.cache`."""
        for value in ("", "relative/config", "./config"):
            with self.subTest(xdg=value):
                paths = state_paths(
                    system="Linux",
                    env={"XDG_CONFIG_HOME": value, "XDG_CACHE_HOME": value},
                    home=self.home,
                )
                self.assertEqual(
                    paths.config,
                    self.home / ".config" / "keklib-cli" / "config.json",
                )
                self.assertEqual(
                    paths.index,
                    self.home / ".cache" / "keklib-cli" / "index.sqlite3",
                )

    def test_windows_appdata_fallback(self) -> None:
        """Без APPDATA/LOCALAPPDATA — `home/AppData/Roaming` и `home/AppData/Local`."""
        for env in ({}, {"APPDATA": "", "LOCALAPPDATA": ""}):
            with self.subTest(env=env):
                paths = state_paths(system="Windows", env=env, home=self.home)
                self.assertEqual(
                    paths.config,
                    self.home / "AppData" / "Roaming" / "keklib-cli" / "config.json",
                )
                self.assertEqual(
                    paths.cache,
                    self.home / "AppData" / "Local" / "keklib-cli" / "Cache",
                )
                self.assertEqual(
                    paths.index,
                    self.home
                    / "AppData"
                    / "Local"
                    / "keklib-cli"
                    / "Cache"
                    / "index.sqlite3",
                )

    # Блокировки (настоящие процессы, нативный механизм текущей ОС).

    def _reap(self, proc: subprocess.Popen[str]) -> None:
        """Гарантированный teardown дочернего процесса: cleanup не зависает.

        Закрывает stdin (hold-процессы освобождают блокировку и выходят),
        дожидается завершения с пределом, при превышении — terminate/kill.
        """
        self._send_go(proc)
        try:
            proc.wait(timeout=_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            self._kill_proc(proc)
        finally:
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except (BrokenPipeError, OSError, ValueError):
                        pass

    def _start_child(
        self, mode: str, lock_path: Path, behaviour: str = "hold"
    ) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [sys.executable, "-c", _LOCK_CHILD, mode, str(lock_path), behaviour],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._reap, proc)
        return proc

    def _start_attempt_child(
        self, mode: str, lock_path: Path
    ) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [sys.executable, "-c", _ATTEMPT_CHILD, mode, str(lock_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._reap, proc)
        return proc

    def _start_raw_child(self, script: str, *args: str) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [sys.executable, "-c", script, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._reap, proc)
        return proc

    def _send_go(self, proc: subprocess.Popen[str]) -> None:
        try:
            assert proc.stdin is not None
            if not proc.stdin.closed:
                proc.stdin.write("go\n")
                proc.stdin.flush()
                proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def _kill_proc(self, proc: subprocess.Popen[str]) -> None:
        """Принудительное завершение с гарантированным ожиданием (wait)."""
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=_WAIT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        proc.kill()
        proc.wait(timeout=_WAIT_SECONDS)

    def _expect_line(
        self,
        proc: subprocess.Popen[str],
        expected: str,
        timeout: float = _WAIT_SECONDS,
    ) -> float:
        """Ограниченное по времени ожидание строки из stdout дочернего процесса.

        Чтение идёт в отдельном потоке (pipe остаётся источником
        синхронизации); при превышении предела процесс завершается
        terminate/kill и тест падает ограниченно, а не зависает. Возвращает
        затраченное время в секундах.
        """
        assert proc.stdout is not None
        stdout = proc.stdout
        received: list[str] = []

        def _read() -> None:
            received.append(stdout.readline())

        reader = threading.Thread(target=_read, daemon=True)
        started = time.monotonic()
        reader.start()
        reader.join(timeout)
        elapsed = time.monotonic() - started
        if reader.is_alive():
            self._kill_proc(proc)
            reader.join(_WAIT_SECONDS)
            self.fail(
                f"таймаут {timeout} с ожидания строки {expected!r} "
                "от дочернего процесса"
            )
        self.assertEqual(
            (received[0] if received else "").strip(),
            expected,
            "неожиданный ответ дочернего процесса",
        )
        return elapsed

    def _wait_exit(
        self, proc: subprocess.Popen[str], timeout: float = _WAIT_SECONDS
    ) -> int:
        """Ограниченное ожидание завершения; при превышении — kill и провал."""
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_proc(proc)
            self.fail(f"таймаут {timeout} с ожидания завершения дочернего процесса")

    def _finish(self, proc: subprocess.Popen[str]) -> None:
        """Освободить hold-процесс: go, ограниченное завершение, RELEASED."""
        self._send_go(proc)
        code = self._wait_exit(proc)
        assert proc.stdout is not None
        rest = proc.stdout.read().split()
        self.assertIn("RELEASED", rest, "дочерний процесс не освободил блокировку")
        self.assertEqual(code, 0, "ненулевой код завершения держателя блокировки")

    def test_two_readers(self) -> None:
        """Два настоящих процесса одновременно удерживают shared-блокировку."""
        lock_path = self.home / "index.lock"
        first = self._start_child("shared", lock_path)
        self._expect_line(first, "READY")
        second = self._start_child("shared", lock_path)
        self._expect_line(second, "READY")
        self._finish(first)
        self._finish(second)

    def test_writer_excludes_readers_and_writers(self) -> None:
        """Writer держит exclusive: попытки writer/reader в отдельных процессах получают IndexBusyError."""
        lock_path = self.home / "index.lock"
        writer = self._start_child("exclusive", lock_path)
        self._expect_line(writer, "READY")
        for mode in ("exclusive", "shared"):
            with self.subTest(attempt=mode):
                attempt = self._start_attempt_child(mode, lock_path)
                self._expect_line(attempt, "BUSY")
                self.assertEqual(self._wait_exit(attempt), 0)
        self._finish(writer)

    def test_reader_excludes_writer(self) -> None:
        """Reader держит shared: writer в отдельном процессе получает IndexBusyError."""
        lock_path = self.home / "index.lock"
        reader = self._start_child("shared", lock_path)
        self._expect_line(reader, "READY")
        attempt = self._start_attempt_child("exclusive", lock_path)
        self._expect_line(attempt, "BUSY")
        self.assertEqual(self._wait_exit(attempt), 0)
        self._finish(reader)

    def test_process_death_releases_lock(self) -> None:
        """Смерть держателя освобождает lock; файл не удаляется и не заменяется."""
        lock_path = self.home / "index.lock"
        holder = self._start_child("exclusive", lock_path, behaviour="die")
        self._expect_line(holder, "READY")
        identity_before = _file_identity(lock_path)
        self.assertEqual(self._wait_exit(holder), 0)
        identity_after_death = _file_identity(lock_path)
        attempt = self._start_attempt_child("exclusive", lock_path)
        self._expect_line(attempt, "ACQUIRED")
        self._send_go(attempt)
        self.assertEqual(self._wait_exit(attempt), 0)
        identity_after = _file_identity(lock_path)
        self.assertTrue(lock_path.exists())
        self.assertEqual(
            identity_before,
            identity_after_death,
            "lock-файл изменил идентичность после смерти держителя",
        )
        self.assertEqual(
            identity_after_death,
            identity_after,
            "lock-файл удалён или заменён в сценарии нового входа",
        )

    def test_import_isolation(self) -> None:
        """Импорт пакета на Windows не требует fcntl, на POSIX — Windows DLL."""
        if sys.platform != "win32":
            # POSIX: реальный dispatch пакета не тянет windows-модуль (нет
            # Windows DLL) и msvcrt; fcntl при этом доступен и ожидаем.
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; import keklib_cli.platforms; "
                    "assert 'keklib_cli.platforms.windows' not in sys.modules, "
                    "'на POSIX подтянулся windows-модуль'; "
                    "assert 'msvcrt' not in sys.modules, 'пакет потянул msvcrt'",
                ],
                check=True,
                timeout=_SUBPROCESS_SECONDS,
            )
        else:
            # Нативный Windows: пакет обязан импортировать windows-модуль и не
            # тянуть posix (иначе fcntl, которого на Windows нет).
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; import keklib_cli.platforms; "
                    "assert 'keklib_cli.platforms.windows' in sys.modules, "
                    "'на Windows не подтянулся windows-модуль'; "
                    "assert 'keklib_cli.platforms.posix' not in sys.modules, "
                    "'на Windows подтянулся posix-модуль'; "
                    "assert 'fcntl' not in sys.modules",
                ],
                check=True,
                timeout=_SUBPROCESS_SECONDS,
            )
        # Дополнительно (исполнимо на любой ОС): сам windows.py не требует
        # fcntl/msvcrt при заблокированных импортах — как на Windows, где этих
        # модулей нет. Это проверка импорт-зависимостей модуля из файла с
        # лёгким пакетным контекстом, а НЕ доказательство реального
        # Windows-dispatch пакета: диспетчеризация проверяется только нативной
        # веткой win32 выше. На CPython 3.14 pathlib сам тянет fcntl на POSIX,
        # поэтому проверка sys.modules бессмысленна: вместо этого fcntl и
        # msvcrt блокируются meta_path-перехватчиком до загрузки модуля.
        loader = (
            "import ctypes\n"
            "import importlib.util\n"
            "import sys\n"
            "import types\n"
            "import keklib_cli  # только каталог пакета\n"
            "from pathlib import Path\n"
            "\n"
            "class _WindowsOnly:\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        root = fullname.partition('.')[0]\n"
            "        if root in ('fcntl', 'msvcrt'):\n"
            "            raise ImportError(f'{fullname}: нет на Windows')\n"
            "        return None\n"
            "\n"
            "sys.modules.pop('fcntl', None)\n"
            "sys.meta_path.insert(0, _WindowsOnly())\n"
            "\n"
            "base = Path(keklib_cli.__file__).parent\n"
            "pkg = types.ModuleType('keklib_cli')\n"
            "pkg.__path__ = [str(base)]\n"
            "plat = types.ModuleType('keklib_cli.platforms')\n"
            "plat.__path__ = [str(base / 'platforms')]\n"
            "sys.modules['keklib_cli'] = pkg\n"
            "sys.modules['keklib_cli.platforms'] = plat\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'keklib_cli.platforms.windows', base / 'platforms' / 'windows.py')\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "sys.modules[spec.name] = module\n"
            "spec.loader.exec_module(module)\n"
            "assert 'fcntl' not in sys.modules\n"
            "assert 'msvcrt' not in sys.modules\n"
        )
        subprocess.run(
            [sys.executable, "-c", loader],
            check=True,
            timeout=_SUBPROCESS_SECONDS,
        )

    def test_memory_units(self) -> None:
        """Linux — уже KiB; Darwin — байты делятся на 1024; недоступно — None, не 0."""
        measured = peak_rss_kib()
        self.assertIsNotNone(measured, "измерение памяти недоступно на текущей ОС")
        assert measured is not None
        self.assertGreater(measured, 0)

        if sys.platform != "win32":
            # POSIX-измеритель: единицы и отказ источника. Импорт posix-модуля
            # допустим только вне Windows (fcntl/resource там отсутствуют).
            from keklib_cli.platforms import posix as posix_module

            # Чистая логика пересчёта: симуляция единиц другой ОС.
            self.assertEqual(posix_module._maxrss_to_kib(2048, "Linux"), 2048)
            self.assertEqual(posix_module._maxrss_to_kib(2048, "Darwin"), 2)
            self.assertEqual(posix_module._maxrss_to_kib(1, "Darwin"), 0)

            # Недоступное измерение — None, а не 0 (инжекция отказа источника).
            with mock.patch.object(
                posix_module.resource, "getrusage", side_effect=OSError("denied")
            ):
                self.assertIsNone(posix_module.peak_rss_kib())

            # Измерение Windows вне Windows недоступно — None, а не 0.
            from keklib_cli.platforms import windows as windows_module

            self.assertIsNone(windows_module.peak_rss_kib())
        else:
            # Нативный Windows: реальный измеритель и его отказ — без импорта
            # POSIX-зависимостей (fcntl/resource на Windows недоступны).
            from keklib_cli.platforms import windows as windows_module

            self.assertIsNotNone(windows_module.peak_rss_kib())
            with mock.patch.object(
                windows_module, "_get_process_memory_info", return_value=0
            ):
                self.assertIsNone(windows_module.peak_rss_kib())

    def test_never_ready_child_fails_bounded_without_leak(self) -> None:
        """Регресс I3: never-ready процесс — ограниченный по времени провал без утечки."""
        proc = self._start_raw_child(_NEVER_READY_CHILD)
        started = time.monotonic()
        with self.assertRaises(AssertionError):
            self._expect_line(proc, "READY", timeout=_REGRESSION_SECONDS)
        elapsed = time.monotonic() - started
        self.assertLess(
            elapsed, _REGRESSION_SECONDS * 3, "провал не ограничен по времени"
        )
        self.assertIsNotNone(
            proc.poll(), "дочерний процесс не завершён после таймаута (утечка)"
        )

    def test_blocking_child_fails_bounded_without_leak(self) -> None:
        """Регресс I3: блокирующая попытка (без LOCK_NB) — ограниченный провал без утечки."""
        if sys.platform == "win32":
            self.skipTest(
                "блокирующая симуляция через flock — POSIX-only; "
                "нативная Windows-симуляция в этой среде недоступна"
            )
        lock_path = self.home / "index.lock"
        holder = self._start_child("shared", lock_path)
        self._expect_line(holder, "READY")
        blocker = self._start_raw_child(_BLOCKING_CHILD, str(lock_path))
        started = time.monotonic()
        with self.assertRaises(AssertionError):
            self._expect_line(blocker, "READY", timeout=_REGRESSION_SECONDS)
        elapsed = time.monotonic() - started
        self.assertLess(
            elapsed, _REGRESSION_SECONDS * 3, "провал не ограничен по времени"
        )
        self.assertIsNotNone(
            blocker.poll(), "блокирующийся процесс не завершён после таймаута (утечка)"
        )
        self._finish(holder)

    def test_publish_race_reaches_primitive(self) -> None:
        """Реальная межпроцессная гонка до примитива публикации (§9.6).

        Двусторонний барьер: дочерний процесс создаёт temp-файлы всех раундов
        и печатает READY, родитель создаёт свои temp-файлы и только после
        этого отправляет GO — попытки публикации обоих процессов начинаются
        одновременно, а не по готовности одного участника. Каждый раунд
        обязан иметь ровно одного победителя: сумма побед равна числу
        раундов, множества не пересекаются, а содержимое каждой цели — байты
        именно победителя (перезапись обнаружилась бы сдвигом содержимого
        или второй победой).
        """
        rounds = 40
        child = self._start_raw_child(_PUBLISH_RACE_CHILD, str(self.home), str(rounds))
        self._expect_line(child, "READY")
        parent_temps: dict[int, Path] = {}
        for round_index in range(rounds):
            fd, temp_name = tempfile.mkstemp(
                dir=self.home, prefix=f".race-parent-{round_index}-"
            )
            with os.fdopen(fd, "wb") as sink:
                sink.write(f"parent-{round_index}".encode("ascii"))
            parent_temps[round_index] = Path(temp_name)
        self._send_go(child)  # барьер: оба процесса начинают попытки вместе
        parent_wins: set[int] = set()
        for round_index in range(rounds):
            temp = parent_temps[round_index]
            try:
                publish_no_replace(temp, self.home / f"race-{round_index}.target")
            except TargetExistsError:
                temp.unlink()
            else:
                parent_wins.add(round_index)
        self._wait_exit(child)
        assert child.stdout is not None
        tokens = child.stdout.read().split()
        self.assertIn("DONE", tokens)
        child_wins = {
            int(value)
            for token, value in zip(tokens[::2], tokens[1::2])
            if token == "W"
        }
        done_count = int(tokens[tokens.index("DONE") + 1])
        self.assertEqual(done_count, len(child_wins))
        self.assertFalse(child_wins & parent_wins, "раунд выигран дважды")
        self.assertEqual(
            len(child_wins) + len(parent_wins),
            rounds,
            "часть раундов не имеет победителя",
        )
        self.assertEqual(child_wins | parent_wins, set(range(rounds)))
        for round_index in range(rounds):
            content = (self.home / f"race-{round_index}.target").read_bytes()
            expected = (
                f"parent-{round_index}".encode("ascii")
                if round_index in parent_wins
                else f"child-{round_index}".encode("ascii")
            )
            self.assertEqual(
                content, expected, f"раунд {round_index}: победитель перезаписан"
            )
        self.assertEqual(
            len(list(self.home.iterdir())), rounds, "остались temp-файлы гонки"
        )

    def test_winapi_struct_field_order(self) -> None:
        """Порядок полей структур WinAPI — контракт по документации WinAPI.

        Проверяется именно порядок имён полей; это не доказательство Windows
        ABI через Linux-layout (размер `c_ulong` на Linux не обязан совпадать
        с DWORD Windows), нативная проверка layout возможна только на Windows.
        """
        from keklib_cli.platforms import windows as windows_module

        self.assertEqual(
            [name for name, _ in windows_module._PROCESS_MEMORY_COUNTERS._fields_],
            [
                "cb",
                "PageFaultCount",
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
            ],
        )
        self.assertEqual(
            [name for name, _ in windows_module._OVERLAPPED._fields_],
            ["Internal", "InternalHigh", "Offset", "OffsetHigh", "hEvent"],
        )

    @unittest.skipUnless(
        sys.platform == "win32",
        "нативный прогон Windows недоступен: исполнение в среде Linux "
        "(LockFileEx/GetProcessMemoryInfo проверены только изоляцией импорта)",
    )
    def test_windows_native_lock_and_memory(self) -> None:
        """Нативный Windows: те же сценарии блокировок против LockFileEx + память."""
        self.test_two_readers()
        self.test_writer_excludes_readers_and_writers()
        from keklib_cli.platforms import windows as windows_module

        self.assertIsNotNone(windows_module.peak_rss_kib())

    @unittest.skipUnless(
        sys.platform == "darwin",
        "нативный прогон macOS недоступен: исполнение в среде Linux "
        "(пересчёт ru_maxrss из байтов проверен только симуляцией)",
    )
    def test_darwin_native_maxrss_units(self) -> None:
        """Нативный macOS: getrusage отдаёт байты, результат — байты // 1024."""
        from keklib_cli.platforms import posix as posix_module

        raw = posix_module.resource.getrusage(
            posix_module.resource.RUSAGE_SELF
        ).ru_maxrss
        self.assertGreater(raw, 0)
        self.assertEqual(peak_rss_kib(), raw // 1024)


class PublishTests(unittest.TestCase):
    """``publish_no_replace``: реальный no-clobber примитив и изолированные fallback.

    Успех и конфликт проверяются настоящими примитивами текущей ОС без
    подмен; fallback-ветки POSIX (renameat2/renamex_np) вызываются реально.
    Симуляция (mock) применяется только там, где нельзя воспроизвести
    переносимо: отказ файловой системы в поддержке примитива — такая
    подмена управляет выбором ветки, но не доказывает семантику ФС; сами
    конфликты при этом идут через настоящий syscall.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def _temp(self, name: str, payload: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(payload)
        return path

    def test_facade_exports(self) -> None:
        """Фасад платформы экспортирует ровно четыре функции (§5, §9.6)."""
        import keklib_cli.platforms as facade

        self.assertEqual(
            sorted(facade.__all__),
            ["index_lock", "peak_rss_kib", "publish_no_replace", "state_paths"],
        )
        for name in facade.__all__:
            self.assertTrue(callable(getattr(facade, name)))

    def test_publish_success_removes_temp(self) -> None:
        """Успешная публикация: содержимое перенесено, свой temp удалён."""
        temp = self._temp("temp-1", b"NEW")
        target = self.dir / "book.fb2"
        publish_no_replace(temp, target)
        self.assertEqual(target.read_bytes(), b"NEW")
        self.assertFalse(temp.exists(), "собственный temp не удалён после публикации")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["book.fb2"])

    def test_publish_conflict_preserves_target(self) -> None:
        """Существующая цель — TargetExistsError; байты цели и temp сохранены."""
        target = self._temp("book.fb2", b"OLD")
        temp = self._temp("temp-2", b"NEW")
        with self.assertRaises(TargetExistsError) as ctx:
            publish_no_replace(temp, target)
        self.assertIn(str(target), str(ctx.exception))
        self.assertEqual(target.read_bytes(), b"OLD", "цель перезаписана при конфликте")
        self.assertEqual(temp.read_bytes(), b"NEW", "temp изменён при конфликте")

    def test_publish_conflict_symlink(self) -> None:
        """Живая и битая symlink на месте цели — конфликт; ссылка сохранена."""
        elsewhere = self._temp("elsewhere", b"DATA")
        target = self.dir / "link.fb2"
        os.symlink(elsewhere, target)
        temp = self._temp("temp-3", b"NEW")
        with self.assertRaises(TargetExistsError):
            publish_no_replace(temp, target)
        self.assertTrue(target.is_symlink())
        # Каноническая идентичность: на Windows os.readlink отдаёт
        # extended-путь \\?\C:\..., строковое равенство там не обязано
        # выполняться.
        self.assertEqual(os.path.realpath(target), os.path.realpath(elsewhere))
        self.assertEqual(elsewhere.read_bytes(), b"DATA")

        dangling = self.dir / "dangling.fb2"
        os.symlink(self.dir / "no-such-file", dangling)
        with self.assertRaises(TargetExistsError):
            publish_no_replace(temp, dangling)
        self.assertTrue(dangling.is_symlink())

    def test_publish_unsupported_refuses(self) -> None:
        """Нет безопасного примитива — ExtractionError без небезопасного fallback.

        Подменяется только выбор ветки (отказ os.link) и возврат выбранной
        ОС низкоуровневой функции — той, что диспетчер берёт реально
        (`_renamex_np` на Darwin, `_renameat2` на Linux); факт её вызова
        проверяется явно. Утверждение — цель не перезаписана, temp не
        удалён никем, кроме владельца, а ошибка — именно Unsupported, не
        замаскированный конфликт (`TargetExistsError` — наследник
        `ExtractionError`, поэтому проверяется отдельно).
        """
        target = self._temp("book.fb2", b"OLD")
        temp = self._temp("temp-4", b"NEW")
        if sys.platform == "win32":
            link_error = OSError()
            link_error.winerror = 50  # ERROR_NOT_SUPPORTED
            rename_error = OSError()
            rename_error.winerror = 50
            patches = [
                mock.patch.object(os, "link", side_effect=link_error),
                mock.patch.object(os, "rename", side_effect=rename_error),
            ]
        else:
            from keklib_cli.platforms import posix as posix_module

            syscall_name = (
                "_renamex_np" if sys.platform == "darwin" else "_renameat2"
            )
            calls: list[str] = []

            def _stub(temp_bytes: bytes, target_bytes: bytes) -> int:
                calls.append(syscall_name)
                return errno.ENOSYS

            patches = [
                mock.patch.object(
                    os, "link", side_effect=OSError(errno.EPERM, "no links")
                ),
                mock.patch.object(
                    posix_module, syscall_name, side_effect=_stub
                ),
            ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with self.assertRaises(ExtractionError) as ctx:
                publish_no_replace(temp, target)
        self.assertNotIsInstance(ctx.exception, TargetExistsError)
        self.assertEqual(target.read_bytes(), b"OLD", "цель перезаписана fallback")
        self.assertEqual(temp.read_bytes(), b"NEW")
        if sys.platform != "win32":
            self.assertEqual(calls, [syscall_name], "выбранная syscall не вызвана")

    def test_darwin_rename_excl_flag_matches_xnu(self) -> None:
        """Флаг renamex_np — официальный RENAME_EXCL XNU, а не 0x40.

        XNU ``bsd/sys/stdio.h`` задаёт ``RENAME_EXCL 0x00000004``;
        прежнее значение 0x40 не является ни одним флагом renamex_np, и
        нативный macOS получал EINVAL из каждого fallback-вызова (первый
        CI, macOS 3.11/3.14). Регрессия проверяет третий аргумент,
        реально дошедший до libc-функции через настоящий код
        ``_renamex_np`` (подменён только ``ctypes.CDLL``), — а не только
        экспорт константы. Диспетчеризация Darwin подменяется
        ``sys.platform``; сам syscall на macOS проверяет
        ``test_darwin_native_publish``.
        """
        from keklib_cli.platforms import posix as posix_module

        self.assertEqual(posix_module._RENAME_EXCL, 0x00000004)
        captured: list[tuple[bytes, bytes, int]] = []

        def fake_renamex_np(temp: bytes, target: bytes, flags: int) -> int:
            captured.append((temp, target, flags))
            return 0

        class _RecordingLibc:
            def __getattr__(self, name: str):
                if name == "renamex_np":
                    return fake_renamex_np
                raise AttributeError(name)

        recording = _RecordingLibc()

        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(
                posix_module.ctypes, "CDLL", lambda *a, **k: recording
            ),
        ):
            temp = self._temp("temp-excl", b"X")
            target = self.dir / "excl.fb2"
            posix_module._rename_noreplace(temp, target)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], os.fsencode(temp))
        self.assertEqual(captured[0][1], os.fsencode(target))
        self.assertEqual(captured[0][2], 0x00000004)

    def test_posix_fallback_dispatch_real(self) -> None:
        """Отказ hardlink (симуляция ФС) — реальный renameat2/renamex_np.

        Подменён только os.link, чтобы направить выполнение в fallback;
        сам no-replace rename — настоящий syscall текущей POSIX-системы.
        """
        if sys.platform == "win32":
            self.skipTest(
                "POSIX-fallback (renameat2/renamex_np) неприменим на Windows; "
                "нативный эквивалент проверяет test_windows_native_publish"
            )
        temp = self._temp("temp-5", b"FALLBACK")
        target = self.dir / "book.fb2"
        with mock.patch.object(
            os, "link", side_effect=OSError(errno.EPERM, "no hardlinks here")
        ):
            publish_no_replace(temp, target)
        self.assertEqual(target.read_bytes(), b"FALLBACK")
        self.assertFalse(temp.exists())

        second = self._temp("temp-6", b"SECOND")
        with mock.patch.object(
            os, "link", side_effect=OSError(errno.EPERM, "no hardlinks here")
        ):
            with self.assertRaises(TargetExistsError):
                publish_no_replace(second, target)
        self.assertEqual(target.read_bytes(), b"FALLBACK")
        self.assertEqual(second.read_bytes(), b"SECOND")

    def test_posix_rename_noreplace_real(self) -> None:
        """Изолированный fallback-примитив: успех, конфликт, неподдерживаемость."""
        if sys.platform == "win32":
            self.skipTest(
                "переименование без перезаписи через renameat2/renamex_np — "
                "POSIX-only; нативный Windows-прогон недоступен в среде Linux"
            )
        from keklib_cli.platforms import posix as posix_module

        rename = posix_module._rename_noreplace
        temp = self._temp("temp-7", b"RENAMED")
        target = self.dir / "renamed.fb2"
        rename(temp, target)  # настоящий syscall
        self.assertEqual(target.read_bytes(), b"RENAMED")
        self.assertFalse(temp.exists())

        second = self._temp("temp-8", b"SECOND")
        with self.assertRaises(TargetExistsError):
            rename(second, target)
        self.assertEqual(target.read_bytes(), b"RENAMED")
        self.assertEqual(second.read_bytes(), b"SECOND")

        # Неподдерживаемая ФС: подменяется возврат выбранной ОС syscall —
        # той, что диспетчер вызывает реально (renamex_np на Darwin,
        # renameat2 на Linux), — ENOSYS. Отказ обязан быть Unsupported-ошибкой,
        # а не замаскированным конфликтом: TargetExistsError — наследник
        # ExtractionError, поэтому проверяется отдельно.
        syscall_name = "_renamex_np" if sys.platform == "darwin" else "_renameat2"
        calls: list[str] = []

        def _stub(temp_bytes: bytes, target_bytes: bytes) -> int:
            calls.append(syscall_name)
            return errno.ENOSYS

        third = self._temp("temp-9", b"THIRD")
        with mock.patch.object(posix_module, syscall_name, side_effect=_stub):
            with self.assertRaises(ExtractionError) as ctx:
                rename(third, target)
        self.assertNotIsInstance(ctx.exception, TargetExistsError)
        self.assertTrue(calls, "выбранная низкоуровневая syscall не вызвана")
        self.assertEqual(target.read_bytes(), b"RENAMED")
        self.assertEqual(third.read_bytes(), b"THIRD")

    def test_windows_publish_branch_reachability(self) -> None:
        """Регресс P1: ветка Windows-публикации достижима без NameError.

        Симуляция на POSIX-хосте (guard ``_kernel32`` подменён truthy):
        проверяется связность кода Windows-ветки — раньше любой вызов падал
        ``NameError: name 'os' is not defined`` до примитива. Прогон success
        использует настоящие ``os.link``/``os.unlink`` хоста, а ветки
        fallback/конфликта — подменные os-функции с winerror-атрибутами.
        Это НЕ нативный Windows-прогон: семантика MoveFileExW/CreateHardLinkW
        проверяется только на Windows (test_windows_native_publish).
        """
        if sys.platform == "win32":
            self.skipTest(
                "нативный прогон Windows покрывается test_windows_native_publish"
            )
        from keklib_cli.platforms import windows as windows_module

        # Success-ветка: настоящий os.link и os.unlink хоста, guard подменён.
        temp = self._temp("temp-reach", b"REACH")
        target = self.dir / "reach.fb2"
        with mock.patch.object(windows_module, "_kernel32", object()):
            windows_module.publish_no_replace(temp, target)
        self.assertEqual(target.read_bytes(), b"REACH")
        self.assertFalse(temp.exists())

        # Fallback-конфликт: os.link «не поддерживается» (winerror 50),
        # os.rename возвращает конфликт (winerror 183) — обе вызваны.
        calls: list[str] = []
        link_error = OSError()
        link_error.winerror = 50
        conflict = OSError()
        conflict.winerror = 183

        def _link(_temp: Path, _target: Path) -> None:
            calls.append("link")
            raise link_error

        def _rename(_temp: Path, _target: Path) -> None:
            calls.append("rename")
            raise conflict

        second = self._temp("temp-reach-2", b"SECOND")
        with mock.patch.object(windows_module, "_kernel32", object()), \
                mock.patch.object(os, "link", side_effect=_link), \
                mock.patch.object(os, "rename", side_effect=_rename):
            with self.assertRaises(TargetExistsError):
                windows_module.publish_no_replace(second, target)
        self.assertEqual(calls, ["link", "rename"])
        self.assertEqual(target.read_bytes(), b"REACH")

        # Неподдержка обоих примитивов — ExtractionError, не конфликт.
        unsupported = OSError()
        unsupported.winerror = 50
        calls.clear()

        def _unsupported(_temp: Path, _target: Path) -> None:
            calls.append("rename")
            raise unsupported

        third = self._temp("temp-reach-3", b"THIRD")
        with mock.patch.object(windows_module, "_kernel32", object()), \
                mock.patch.object(os, "link", side_effect=_link), \
                mock.patch.object(os, "rename", side_effect=_unsupported):
            with self.assertRaises(ExtractionError) as ctx:
                windows_module.publish_no_replace(third, target)
        self.assertNotIsInstance(ctx.exception, TargetExistsError)
        self.assertEqual(calls, ["link", "rename"])
        self.assertEqual(target.read_bytes(), b"REACH")

    @unittest.skipUnless(
        sys.platform == "win32",
        "нативный прогон Windows недоступен: исполнение в среде Linux "
        "(os.rename-fallback и winerror-коды проверены только изоляцией кода)",
    )
    def test_windows_native_publish(self) -> None:
        """Нативный Windows: публикация, конфликт и реальный fallback os.rename."""
        from keklib_cli.platforms import windows as windows_module

        temp = self._temp("temp-win", b"WIN")
        target = self.dir / "win.fb2"
        windows_module.publish_no_replace(temp, target)
        self.assertEqual(target.read_bytes(), b"WIN")
        self.assertFalse(temp.exists())

        second = self._temp("temp-win-2", b"SECOND")
        with self.assertRaises(TargetExistsError):
            windows_module.publish_no_replace(second, target)
        self.assertEqual(target.read_bytes(), b"WIN")

        # ФС без hardlink (winerror 50 — подмена выбора ветки): реальный
        # os.rename с no-overwrite семантикой MoveFileExW.
        third = self._temp("temp-win-3", b"FALLBACK")
        target3 = self.dir / "win3.fb2"
        link_error = OSError()
        link_error.winerror = 50
        with mock.patch.object(os, "link", side_effect=link_error):
            windows_module.publish_no_replace(third, target3)
        self.assertEqual(target3.read_bytes(), b"FALLBACK")
        self.assertFalse(third.exists())

    @unittest.skipUnless(
        sys.platform == "darwin",
        "нативный прогон macOS недоступен: исполнение в среде Linux "
        "(ветка renamex_np проверена только изоляцией кода)",
    )
    def test_darwin_native_publish(self) -> None:
        """Нативный macOS: реальный renamex_np(RENAME_EXCL) через диспетчер."""
        from keklib_cli.platforms import posix as posix_module

        temp = self._temp("temp-mac", b"MAC")
        target = self.dir / "mac.fb2"
        posix_module._rename_noreplace(temp, target)
        self.assertEqual(target.read_bytes(), b"MAC")
        self.assertFalse(temp.exists())

        second = self._temp("temp-mac-2", b"SECOND")
        with self.assertRaises(TargetExistsError):
            posix_module._rename_noreplace(second, target)
        self.assertEqual(target.read_bytes(), b"MAC")


if __name__ == "__main__":
    unittest.main()
