"""Нативные платформенные операции keklib-cli (§5, §9.6 спецификации).

Экспортируются ровно четыре функции: ``state_paths``, ``index_lock``,
``peak_rss_kib`` и ``publish_no_replace``. Вычисление путей чистое и допускает
симуляцию ОС параметром ``system``; диспетчеризация нативных операций
(блокировки, память, публикация без перезаписи) всегда идёт по реальной ОС:
модуль ``posix`` импортируется только вне Windows, ``windows`` — только на
win32.
"""

from __future__ import annotations

import sys

from .paths import state_paths

if sys.platform == "win32":
    from .windows import index_lock, peak_rss_kib, publish_no_replace
else:
    from .posix import index_lock, peak_rss_kib, publish_no_replace

__all__ = ["index_lock", "peak_rss_kib", "publish_no_replace", "state_paths"]
