"""Точка входа portable-сборки keklib-cli (задача 11 плана).

Вызывается тот же `keklib_cli.cli.main`, что и в Python-пакете: логика CLI
не дублируется, аргументы и коды завершения передаются без изменений.
"""

from __future__ import annotations

import sys

from keklib_cli.cli import main

if __name__ == "__main__":
    sys.exit(main())
