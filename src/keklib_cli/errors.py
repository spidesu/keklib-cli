"""Предметные ошибки keklib-cli.

`UserError` — базовый класс ошибок, о которых сообщается пользователю без
traceback. Наследники привязаны к слоям: конфигурация, каталог библиотеки,
построение индекса, поиск и извлечение.
"""

from __future__ import annotations


class UserError(Exception):
    """Базовая ошибка, адресованная пользователю (без traceback)."""


class ConfigError(UserError):
    """Конфигурация отсутствует, не читается или имеет неверный формат."""


class CatalogError(UserError):
    """Корень библиотеки или раскладка INPX/архивов не проходят проверку."""


class IndexBuildError(UserError):
    """Не удалось построить или обновить поисковый индекс."""


class SearchError(UserError):
    """Поиск не выполнен: индекс недоступен или запрос некорректен."""


class ExtractionError(UserError):
    """Не удалось безопасно извлечь книгу."""


class IndexBusyError(IndexBuildError):
    """Индекс заблокирован другим процессом."""


class TargetExistsError(ExtractionError):
    """Целевой файл извлечения уже существует; перезапись запрещена."""
