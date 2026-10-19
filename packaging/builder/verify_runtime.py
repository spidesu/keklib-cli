"""Проверка собранного CPython-runtime внутри образа билдера (fail-closed).

Запускается Containerfile'ом после сборки CPython/SQLite: образ не
собирается, если runtime не соответствует пинам builder-pins.json —
точная версия Python, точная версия SQLite с FTS5, наличие ctypes и
отсутствие readline/tkinter/gdbm, общая libpython (--enable-shared —
обязательное требование PyInstaller 6.x). CONFIG_ARGS сохраняется
в /opt/builder для отчётов.
"""

from __future__ import annotations

import ctypes
import importlib.util
import sqlite3
import sys
import sysconfig

EXPECTED_PYTHON = "3.14.8"
EXPECTED_SQLITE = "3.54.1"


def main() -> int:
    assert sys.version.split()[0] == EXPECTED_PYTHON, sys.version
    assert sqlite3.sqlite_version == EXPECTED_SQLITE, sqlite3.sqlite_version

    connection = sqlite3.connect(":memory:")
    try:
        options = [row[0] for row in connection.execute("PRAGMA compile_options")]
    finally:
        connection.close()
    assert "ENABLE_FTS5" in options, options

    for module in ("readline", "_tkinter", "_gdbm"):
        assert importlib.util.find_spec(module) is None, (
            f"{module} не должен входить в runtime (GPL/ненужный состав)"
        )
    ctypes.CDLL(None)  # ctypes обязателен приложению (нативные операции)

    config_args = sysconfig.get_config_var("CONFIG_ARGS") or ""
    assert "--without-readline" in config_args, config_args
    assert "--enable-shared" in config_args, (
        "PyInstaller 6.x требует общую libpython: --enable-shared обязателен"
    )
    assert "--disable-test-modules" in config_args, config_args

    with open(
        "/opt/builder/cpython-config-args.txt", "w", encoding="utf-8"
    ) as stream:
        stream.write(config_args + "\n")
    print(f"runtime-ok python={EXPECTED_PYTHON} sqlite={EXPECTED_SQLITE} fts5=yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
