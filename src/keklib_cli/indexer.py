"""Индексатор: свежесть каталога и атомарная пересборка индекса (§7).

``ensure_index`` проверяет актуальность готового индекса под общей
блокировкой чтения: quick-валидация, метаданные против живого INPX
(SHA-256, корень, относительный путь, версии схемы и правил). При
неактуальности общая блокировка освобождается и берётся исключительная
без ожидания (никакого повышения shared → exclusive) с повторной
проверкой: другой процесс мог опубликовать свежий индекс за это время.

Пересборка идёт в собственном помеченном каталоге ``build-*`` рядом с
индексом: локальный закрытый снимок INPX с SHA-256 (вычисляется при
копировании, источник читается один поток), новая база по отсутствующему
пути через ``write_index``, полная валидация, повторный хеш живого
источника и лишь затем атомарная ``os.replace``. Перед каждым открытием
назначения и повторно перед публикацией отвергаются чужие sidecar-записи
``<index>-journal``/``-wal``/``-shm`` (включая битые symlink): горячий
rollback-журнал аварийного стороннего writer делает базу неоткрываемой
для ro-читателя и способен откатить только что опубликованную новую базу
к старым страницам; чужие файлы не удаляются и не восстанавливаются.
Все соединения закрываются до замены; старый индекс не удаляется и не
перезаписывается до успешной замены, поэтому при ошибке, нехватке места
или отмене он остаётся прежним. После возврата нет открытых
SQLite-соединений и удерживаемых блокировок.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from keklib_cli.errors import CatalogError, IndexBuildError
from keklib_cli.index_store import (
    RULES_VERSION,
    SCHEMA_VERSION,
    read_meta,
    validate_index,
    write_index,
)
from keklib_cli.inpx import iter_books
from keklib_cli.models import (
    ImportStats,
    IndexMeta,
    IndexResult,
    Library,
    StatePaths,
)
from keklib_cli.platforms import index_lock

#: Файл блокировки рядом с индексом (§5); создаётся при отсутствии и
#: никогда не удаляется и не заменяется ради «освобождения».
INDEX_LOCK_NAME = "index.lock"
#: Префикс собственных временных каталогов постройки в каталоге индекса.
BUILD_PREFIX = "build-"
#: Маркер принадлежности каталога постройки. Имя уникально для
#: инструмента: посторонние каталоги ``build-*`` без маркера-файла не
#: трогаются автоочисткой.
BUILD_MARKER = "keklib-cli-build.owner"

_SNAPSHOT_NAME = "snapshot.inpx"
_CANDIDATE_NAME = "index.sqlite3"
#: Суффиксы соседних с назначением публикацией записей SQLite. Существование
#: любой из них до открытия/публикации — отказ: записи чужие, их удаление
#: или восстановление не выполняются.
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_CHUNK_SIZE = 1 << 20
_MARKER_PAYLOAD = (
    json.dumps({"purpose": "keklib-cli index build"}, sort_keys=True) + "\n"
).encode("utf-8")


def sha256_file(path: Path) -> str:
    """Потоковый SHA-256 файла; источник читается, но не изменяется.

    :raises OSError: файл не читается (вызывающие стороны заворачивают в
        предметные ошибки).
    """
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_index(
    library: Library,
    paths: StatePaths,
    *,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> IndexResult:
    """Вернуть актуальный индекс, при необходимости атомарно перестроив.

    Свежесть проверяется под shared-блокировкой; пересборка и публикация —
    только под exclusive, взятой без ожидания (несовместимая активная
    блокировка — ``IndexBusyError``). ``force`` всегда перестраивает, даже
    если индекс актуален. При любой ошибке или отмене старый индекс
    сохраняется, работа завершается диагностикой без устаревшего фолбэка.

    :param library: проверенная библиотека (задача 1).
    :param paths: нативные пути состояния; индекс — ``paths.index``.
    :param progress: необязательный приёмник коротких сообщений о стадиях.
    :return: ``IndexResult`` по ``paths.index``; после возврата не остаётся
        открытых SQLite-соединений и удерживаемых блокировок.
    :raises IndexBusyError: индекс блокирован несовместимой операцией.
    :raises CatalogError: живой INPX не читается или каталог повреждён.
    :raises IndexBuildError: постройка, валидация или публикация не удалась;
        в том числе рядом с назначением есть чужие sidecar-записи
        ``-journal``/``-wal``/``-shm`` (включая битые symlink).
    """
    try:
        paths.index.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IndexBuildError(
            f"cannot create index directory {paths.index.parent}: {exc}"
        ) from exc
    lock_path = paths.index.parent / INDEX_LOCK_NAME
    if not force:
        # Быстрый путь: совместимые читатели работают одновременно, поэтому
        # свежесть проверяется под shared без конфликта с другими читателями.
        with index_lock(lock_path, exclusive=False):
            # До любого открытия назначения: чужие sidecar-записи делают
            # базу неоткрываемой или опасной для замены.
            _reject_foreign_sidecars(paths.index)
            current = _current_meta(library, paths)
        if current is not None:
            _notify(progress, "index is up to date")
            return IndexResult(path=paths.index, rebuilt=False, meta=current)
    # shared освобождён до попытки exclusive: повышение с удержанием shared
    # запрещено (взаимная блокировка). force всегда идёт сюда, минуя общую.
    with index_lock(lock_path, exclusive=True):
        # Повторно под exclusive и ещё раз непосредственно перед публикацией:
        # чужой writer мог аварийно завершиться в любой момент.
        _reject_foreign_sidecars(paths.index)
        if not force and (current := _current_meta(library, paths)) is not None:
            # Другой процесс опубликовал свежий индекс, пока ждали exclusive.
            _notify(progress, "index is up to date")
            return IndexResult(path=paths.index, rebuilt=False, meta=current)
        return _rebuild(library, paths, progress)


def _reject_foreign_sidecars(index_path: Path) -> None:
    """Отказать, если рядом с индексом назначения есть чужие sidecar-записи.

    Проверяется сама запись каталога (``os.path.lexists``), включая битые
    symlink. Горячий rollback-журнал завершившегося аварийно стороннего
    writer не только делает базу неоткрываемой для ro-читателя: первое
    rw-открытие выполнит recovery и способно откатить страницы только что
    опубликованной новой базы к старым. Поэтому любая существующая запись
    ``<index>-journal``/``-wal``/``-shm`` — повод отказаться до открытия и
    публикации; удаление или восстановление чужих файлов не выполняется.

    :raises IndexBuildError: найдена хотя бы одна sidecar-запись; диагностика
        называет записи и требует устранить их вручную.
    """
    present = [
        Path(f"{index_path}{suffix}")
        for suffix in _SIDECAR_SUFFIXES
        if os.path.lexists(f"{index_path}{suffix}")
    ]
    if present:
        names = [entry.name for entry in present]
        raise IndexBuildError(
            f"refusing to open or publish index {index_path}: existing "
            f"SQLite sidecar entries {names} belong to another writer "
            "(crashed or running); close it and remove the leftover files "
            "manually, then run again: "
            + ", ".join(str(entry) for entry in present)
        )


def _current_meta(library: Library, paths: StatePaths) -> IndexMeta | None:
    """Метаданные актуального индекса или ``None``, если нужна пересборка.

    Повреждение, чужая схема, несовпадающая версия и будущая версия —
    ``IndexBuildError`` хранилища, здесь сводятся к «не актуален».
    """
    try:
        validate_index(paths.index, quick=True)
        stored = read_meta(paths.index)
    except IndexBuildError:
        return None
    if stored.root != str(library.root):
        return None
    if stored.catalog_rel != _catalog_rel(library):
        return None
    if stored.sha256 != _catalog_digest(library):
        return None
    if (
        stored.schema_version != SCHEMA_VERSION
        or stored.rules_version != RULES_VERSION
    ):
        return None
    return stored


def _rebuild(
    library: Library, paths: StatePaths, progress: Callable[[str], None] | None
) -> IndexResult:
    """Перестроить и опубликовать индекс; вызывается только под exclusive."""
    _purge_stale_builds(paths, progress)
    try:
        build_dir = Path(
            tempfile.mkdtemp(prefix=BUILD_PREFIX, dir=paths.index.parent)
        )
    except OSError as exc:
        raise IndexBuildError(
            f"cannot create index build directory in {paths.index.parent}: {exc}"
        ) from exc
    try:
        try:
            (build_dir / BUILD_MARKER).write_bytes(_MARKER_PAYLOAD)
        except OSError as exc:
            raise IndexBuildError(
                f"cannot mark index build directory {build_dir}: {exc}"
            ) from exc
        _notify(progress, f"copying catalog snapshot: {library.catalog}")
        snapshot = build_dir / _SNAPSHOT_NAME
        digest = _copy_snapshot(library.catalog, snapshot)
        # stats — тот же объект, который обновляет iter_books; write_index
        # сериализует его только после исчерпания потока книг.
        stats = ImportStats()
        meta = IndexMeta(
            root=str(library.root),
            catalog_rel=_catalog_rel(library),
            sha256=digest,
            schema_version=SCHEMA_VERSION,
            rules_version=RULES_VERSION,
            built_at=datetime.now(timezone.utc).isoformat(),
            stats=stats,
        )
        candidate = build_dir / _CANDIDATE_NAME
        _notify(progress, "building index")
        try:
            write_index(candidate, iter_books(snapshot, stats), meta)
        except CatalogError as exc:
            # Ошибка данных каталога дополняется путём живого источника:
            # снимок — внутренняя копия, пользователь видит свой INPX.
            raise CatalogError(
                f"INPX catalog {library.catalog} is unreadable: {exc}"
            ) from exc
        _notify(progress, "validating index")
        validate_index(candidate)
        live = _catalog_digest(library)
        if live != digest:
            raise IndexBuildError(
                f"INPX catalog changed during indexing ({library.catalog}); "
                "повторите запуск после завершения обновления библиотеки"
            )
        # Повторная проверка непосредственно перед заменой: чужой writer
        # мог оставить журнал у назначения уже во время нашей постройки.
        _reject_foreign_sidecars(paths.index)
        _notify(progress, f"publishing index: {paths.index}")
        # Все соединения закрыты write_index и validate_index; открытых
        # дескрипторов нет, поэтому replace безопасен и на Windows.
        try:
            os.replace(candidate, paths.index)
        except OSError as exc:
            raise IndexBuildError(
                f"cannot publish index {paths.index}: {exc} "
                "(старый индекс сохранён)"
            ) from exc
    except BaseException:
        # Отмена (KeyboardInterrupt) и ошибки одинаково убирают только
        # собственный каталог постройки; старый индекс не затронут.
        _discard_own(build_dir, progress)
        raise
    _discard_own(build_dir, progress)
    return IndexResult(path=paths.index, rebuilt=True, meta=meta)


def _copy_snapshot(source: Path, target: Path) -> str:
    """Скопировать INPX в закрытый снимок и вернуть SHA-256 копии.

    Источник читается один поток: хеш и копия согласованы по байтам.
    """
    digest = hashlib.sha256()
    try:
        with source.open("rb") as src, target.open("wb") as dst:
            while chunk := src.read(_CHUNK_SIZE):
                digest.update(chunk)
                dst.write(chunk)
    except OSError as exc:
        raise CatalogError(
            f"cannot copy INPX catalog {source}: {exc}"
        ) from exc
    return digest.hexdigest()


def _catalog_digest(library: Library) -> str:
    """SHA-256 живого INPX; нечитаемый источник — предметная ошибка."""
    try:
        return sha256_file(library.catalog)
    except OSError as exc:
        raise CatalogError(
            f"cannot read INPX catalog {library.catalog}: {exc}"
        ) from exc


def _catalog_rel(library: Library) -> str:
    """Относительный путь INPX в корне библиотеки (портативная запись)."""
    return library.catalog.relative_to(library.root).as_posix()


def _purge_stale_builds(
    paths: StatePaths, progress: Callable[[str], None] | None
) -> None:
    """Удалить остатки собственных каталогов постройки после аварий.

    Выполняется только под exclusive. Удаляются исключительно каталоги
    ``build-*`` с маркером инструмента; посторонние каталоги, файлы и
    symlink не трогаются, цели symlink не посещаются. Неудача удаления
    одного каталога не останавливает пересборку: остаток будет убран
    следующим запуском.
    """
    try:
        entries = list(os.scandir(paths.index.parent))
    except OSError:
        return  # каталог пуст или недоступен — убирать нечего
    for entry in entries:
        if not entry.name.startswith(BUILD_PREFIX) or entry.is_symlink():
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        candidate = Path(entry.path)
        if not _is_own_build(candidate):
            continue
        try:
            _remove_tree(candidate)
        except OSError as exc:
            _notify(progress, f"cannot remove stale build dir {candidate}: {exc}")
        else:
            _notify(progress, f"removed stale build dir {candidate}")


def _is_own_build(directory: Path) -> bool:
    """Каталог однозначно принадлежит инструменту: наш маркер-файл.

    Проверяется сама запись каталога (``follow_symlinks=False``): маркер в
    виде symlink маркером не считается.
    """
    try:
        mode = (directory / BUILD_MARKER).stat(follow_symlinks=False).st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode)


def _remove_tree(root: Path) -> None:
    """Удалить собственный каталог постройки без следования по symlink.

    Каждый элемент снимается lstat-семантикой: symlink (в том числе на
    каталог) удаляется как ссылка, рекурсия идёт только в реальные
    каталоги; цель ссылки не затрагивается.

    :raises OSError: удаление не удалось; частично удалённое остаётся
        убранному следующим запуском (маркер сохранён вместе с каталогом).
    """
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            os.unlink(entry.path)
        else:
            _remove_tree(Path(entry.path))
    os.rmdir(root)


def _discard_own(directory: Path, progress: Callable[[str], None] | None) -> None:
    """Best-effort удаление собственного каталога постройки.

    Опубликованный индекс важнее остатков: неудача удаления не подменяет
    успешный результат (каталог помечен и будет убран позже).
    """
    try:
        _remove_tree(directory)
    except OSError as exc:
        _notify(progress, f"cannot remove build directory {directory}: {exc}")


def _notify(progress: Callable[[str], None] | None, message: str) -> None:
    """Передать сообщение о стадии, если приёмник задан."""
    if progress is not None:
        progress(message)
