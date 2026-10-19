"""Платформенно-нативный temp-каталог для тестов.

Волна native-tooling тестов использовала жёстко заданный путь ``/tmp`` как
default ``TMPDIR`` — на Windows-hosted runners CI (run 39271547601) это
дало 36 ERROR: ``TMPDIR`` там не задан, а такого пути не существует.
Контракт: ``TMPDIR`` уважается, если задан; иначе нативный default
``tempfile`` самой ОС. Никакого создания посторонних каталогов, масков
окружения и OS-skip.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["portable_mkdtemp"]


def portable_mkdtemp() -> Path:
    """Новый temp-каталог: TMPDIR если задан, иначе нативный default ОС."""
    return Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
