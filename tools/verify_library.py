#!/usr/bin/env python3
"""Явная приёмка библиотеки Lib.rus.ec публичными компонентами keklib-cli.

Инструмент задачи 10 плана: измеряет хеширование каталога, постройку индекса
и поисковый запрос, извлекает известную книгу и сверяет неизменность
источников. Используются только публичные интерфейсы ``keklib_cli`` и
никогда — пользовательские config/cache: всё состояние (индекс, отчёт,
извлечённая книга) живёт в выделенном ``--workdir`` вне библиотеки.

Правила безопасности:

- владение ``--workdir`` устанавливается ДО первой записи: собственный
  маркер ``keklib-cli-verify.owner`` с точным известным содержимым (не
  только факт наличия). Чужой или занятый каталог не очищается и не
  используется — выберите другой ``--workdir``;
- барьер управляемого дерева действует одинаково для нового и повторного
  workdir: все существующие компоненты путей ``state``, ``state/cache``,
  ``state/cache/index.sqlite3``, ``state/cache/index.lock``, ``output`` и
  ``report.json`` обязаны быть реальными записями — symlink/reparse-точки
  (включая битые ссылки) и чужие типы отвергаются до любой записи, а их
  реальное разрешение не может попадать в библиотеку. Прямая запись по
  пути-ссылке в управляемом дереве невозможна; отчёт публикуется через
  собственный временный файл и ``os.replace`` (подмена самой записи, не
  запись сквозь ссылку);
- ``--workdir`` обязан лежать вне реальной библиотеки, а библиотека — вне
  ``--workdir`` (проверка по реальным разрешённым путям с учётом
  symlink/junction, не по строковым префиксам);
- источники только читаются: побайтово сверяются INPX и выбранный ZIP;
  остальные архивы (~451 ГБ в реальной библиотеке) — только lstat-снимок
  имя/тип/размер/mtime_ns, без чтения содержимого;
- известная цель приёмки — точная пара ``fb2-060424-074391.zip`` /
  ``66372.fb2`` («Мастер и Маргарита» Булгакова). Извлечение и чтение
  ``open_book`` разрешены только после полной проверки известной цели
  (точная пара И название): отсутствие или подмена — явный отказ без
  какого-либо доступа к файлу книги, а не выбор случайного издания;
- ``--reuse-index`` требует существующий собственный workdir и индекс без
  пересборки; уже извлечённая книга никогда не перезаписывается.

Отчёт JSON пишется в ``<workdir>/report.json`` и дублируется в stdout.
Код завершения: 0 — все проверки пройдены; 1 — любой отказ; 2 — аргументы.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from keklib_cli.archive import open_book
from keklib_cli.config import inspect_library
from keklib_cli.errors import UserError
from keklib_cli.indexer import ensure_index, sha256_file
from keklib_cli.models import Book, Library, StatePaths
from keklib_cli.output import extract_book, safe_filename
from keklib_cli.platforms import peak_rss_kib
from keklib_cli.search import PAGE_SIZE, open_search
from keklib_cli.text import normalize

EXIT_OK = 0
EXIT_FAIL = 1

#: Маркер владения workdir (по образцу маркера build-каталогов индексатора).
WORKDIR_MARKER = "keklib-cli-verify.owner"
REPORT_NAME = "report.json"
OUTPUT_DIR_NAME = "output"
STATE_DIR_NAME = "state"

#: Известная цель приёмки: точная пара из реальной библиотеки Lib.rus.ec.
QUERY = "Булгаков Мастер"
TARGET_ARCHIVE = "fb2-060424-074391.zip"
TARGET_MEMBER = "66372.fb2"
TARGET_TITLE = "Мастер и Маргарита"

ARCHIVES_CHECK_NOTE = (
    "остальные архивы библиотеки проверены только lstat-снимком "
    "(имя/тип/размер/mtime_ns); побайтовое хеширование всех архивов "
    "(~451 ГБ реальной библиотеки) не выполнялось — содержимое сверено "
    "только у INPX и выбранного ZIP"
)

_MARKER_PAYLOAD = (
    json.dumps({"purpose": "keklib-cli verify workdir"}, sort_keys=True) + "\n"
).encode("utf-8")
_CHUNK = 1 << 20

#: Управляемые пути внутри workdir: (относительный путь, ожидаемый тип
#: конечной записи). Существующие компоненты проверяются до первой записи —
#: symlink/reparse-точки и чужие типы запрещены на всей глубине.
_MANAGED_PATHS: tuple[tuple[str, str], ...] = (
    (STATE_DIR_NAME, "dir"),
    (f"{STATE_DIR_NAME}/cache", "dir"),
    (f"{STATE_DIR_NAME}/cache/index.sqlite3", "file"),
    (f"{STATE_DIR_NAME}/cache/index.lock", "file"),
    (OUTPUT_DIR_NAME, "dir"),
    (REPORT_NAME, "file"),
)


class _Refuse(RuntimeError):
    """Явный отказ приёмки с понятной причиной."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_library",
        description="Явная приёмка библиотеки: измерения, точная известная "
        "пара, извлечение и проверка неизменности источников.",
    )
    parser.add_argument("--library", required=True, help="корень библиотеки")
    parser.add_argument(
        "--workdir",
        required=True,
        help="выделенный рабочий каталог вне библиотеки (допускает ~)",
    )
    parser.add_argument(
        "--reuse-index",
        action="store_true",
        help="переиспользовать индекс из собственного workdir; пересборка "
        "считается отказом",
    )
    args = parser.parse_args(argv)

    def note(message: str) -> None:
        print(f"verify_library: {message}", file=sys.stderr, flush=True)

    try:
        library = inspect_library(Path(args.library))
    except UserError as exc:
        print(f"verify_library: библиотека не проходит проверку: {exc}", file=sys.stderr)
        return EXIT_FAIL

    # Владение workdir и containment — строго до любой записи.
    try:
        workdir = _establish_workdir(
            Path(args.workdir).expanduser(), library, reuse=args.reuse_index
        )
    except _Refuse as exc:
        print(f"verify_library: отказ: {exc}", file=sys.stderr)
        return EXIT_FAIL

    state = workdir / STATE_DIR_NAME
    paths = StatePaths(
        config=state / "config.json",
        cache=state / "cache",
        index=state / "cache" / "index.sqlite3",
    )
    output_dir = workdir / OUTPUT_DIR_NAME

    failures: list[str] = []
    try:
        report = _accept(
            library, paths, output_dir, workdir, args.reuse_index, failures, note
        )
    except UserError as exc:
        print(f"verify_library: предметная ошибка: {exc}", file=sys.stderr)
        return EXIT_FAIL

    report["ok"] = not failures
    report["failures"] = failures
    try:
        report_path = _publish_report(workdir, report)
    except OSError as exc:
        print(
            f"verify_library: не удалось записать отчёт в {workdir / REPORT_NAME}: {exc}",
            file=sys.stderr,
        )
        return EXIT_FAIL
    print(json.dumps(report, ensure_ascii=False, indent=2))
    for failure in failures:
        print(f"verify_library: ОТКАЗ: {failure}", file=sys.stderr)
    return EXIT_OK if not failures else EXIT_FAIL


def _accept(
    library: Library,
    paths: StatePaths,
    output_dir: Path,
    workdir: Path,
    reuse_index: bool,
    failures: list[str],
    note: Callable[[str], None],
) -> dict:
    """Выполнить приёмку и собрать отчёт; отказа — записи в ``failures``."""
    # Снимки источников ДО: stat-only для архивов и каталога, байты — у INPX
    # и выбранного ZIP.
    archives_before = _stat_snapshot(library.archives)
    catalog_stat_before = _stat_entry(library.catalog)
    selected_zip = library.archives / TARGET_ARCHIVE

    started = time.perf_counter()
    catalog_sha256 = sha256_file(library.catalog)
    hash_seconds = time.perf_counter() - started
    note(f"INPX SHA-256: {catalog_sha256} ({hash_seconds:.3f} с)")
    selected_sha_before = _file_sha_or_none(selected_zip)

    started = time.perf_counter()
    result = ensure_index(library, paths, progress=note)
    build_seconds = time.perf_counter() - started
    if reuse_index and result.rebuilt:
        failures.append(
            "--reuse-index: ожидалось переиспользование индекса, но индекс "
            f"перестроен: {paths.index}"
        )
    note(
        f"индекс {'перестроен' if result.rebuilt else 'актуален без пересборки'} "
        f"({build_seconds:.3f} с): {paths.index}"
    )
    index_sha256 = sha256_file(paths.index)

    query_target_met = False
    target: Book | None = None
    selected_title: str | None = None
    total_matches: int | None = None
    pages_scanned = 0
    query_seconds = 0.0
    output_sha256: str | None = None
    output_path: Path | None = None
    output_reused = False

    with open_search(library, paths, progress=note) as session:
        page_number = 0
        while True:
            started = time.perf_counter()
            page = session.search(QUERY, page=page_number)
            query_seconds += time.perf_counter() - started
            pages_scanned += 1
            total_matches = page.total
            for candidate in page.books:
                if (
                    candidate.archive == TARGET_ARCHIVE
                    and candidate.member == TARGET_MEMBER
                ):
                    target = candidate
                    break
            if target is not None:
                break
            if (page_number + 1) * PAGE_SIZE >= page.total:
                break
            page_number += 1
        note(
            f"запрос «{QUERY}»: совпадений {total_matches}, страниц прочитано "
            f"{pages_scanned} ({query_seconds:.3f} с, включая COUNT)"
        )
        if target is None:
            failures.append(
                f"известная пара {TARGET_ARCHIVE}/{TARGET_MEMBER} не найдена по "
                f"запросу «{QUERY}» (совпадений: {total_matches}); случайное "
                "издание не выбирается"
            )
        elif normalize(target.title) != normalize(TARGET_TITLE):
            selected_title = target.title
            failures.append(
                f"пара {TARGET_ARCHIVE}/{TARGET_MEMBER} найдена, но название "
                f"{selected_title!r} не совпадает с известным "
                f"{TARGET_TITLE!r}"
            )
        else:
            selected_title = target.title
            query_target_met = True

        # Извлечение и open_book разрешены только полностью проверенной
        # известной цели: подменённое название — отказ ДО любого доступа к
        # файлу книги, а не «отказ после извлечения».
        if query_target_met:
            expected_sha, error = _member_sha256(library, target)
            if error is not None:
                failures.append(error)
            else:
                output_path = output_dir / safe_filename(target)
                if output_path.exists():
                    # Повторный запуск: существующая книга не перезаписывается.
                    output_reused = True
                    output_sha256 = sha256_file(output_path)
                    if output_sha256 != expected_sha:
                        failures.append(
                            f"существующий FB2 {output_path} не совпадает с "
                            "текущим содержимым пары; перезапись не выполняется"
                        )
                    else:
                        note(f"существующий FB2 сверен без перезаписи: {output_path}")
                else:
                    try:
                        published = extract_book(library, target, output_dir)
                    except UserError as exc:
                        failures.append(f"извлечение не выполнено: {exc}")
                    else:
                        if published != output_path:
                            failures.append(
                                f"извлечён неожиданный путь {published} вместо "
                                f"{output_path}"
                            )
                        output_sha256 = sha256_file(output_path)
                        if output_sha256 != expected_sha:
                            failures.append(
                                "SHA-256 извлечённого FB2 не совпадает с "
                                "чтением той же пары через open_book"
                            )
                        else:
                            note(f"FB2 извлечён и сверен: {output_path}")

    # Источники после приёмки: байты INPX и выбранного ZIP, stat — остальные.
    source_unchanged = True
    if sha256_file(library.catalog) != catalog_sha256:
        source_unchanged = False
        failures.append(f"INPX изменился за время приёмки: {library.catalog}")
    if _stat_entry(library.catalog) != catalog_stat_before:
        source_unchanged = False
        failures.append(f"lstat-признаки INPX изменились: {library.catalog}")
    if selected_sha_before is None:
        source_unchanged = False
        failures.append(f"выбранный ZIP не читается до приёмки: {selected_zip}")
    elif sha256_file(selected_zip) != selected_sha_before:
        source_unchanged = False
        failures.append(f"выбранный ZIP изменился за время приёмки: {selected_zip}")
    archives_after = _stat_snapshot(library.archives)
    if archives_after != archives_before:
        source_unchanged = False
        failures.append(
            "lstat-снимок архивов изменился: "
            f"{_first_difference(archives_before, archives_after)}"
        )

    peak = peak_rss_kib()
    memory_note = (
        f"пиковая RSS процесса приёмки: {peak} KiB"
        if peak is not None
        else "peak_rss_kib вернул None: измерение недоступно на этой платформе"
    )

    report = {
        "catalog_sha256": catalog_sha256,
        "row_count": result.meta.stats.rows,
        "book_count": result.meta.stats.books,
        "hash_seconds": round(hash_seconds, 3),
        "build_seconds": round(build_seconds, 3),
        "query_seconds": round(query_seconds, 3),
        "peak_rss_kib": peak,
        "memory_note": memory_note,
        "rebuilt": result.rebuilt,
        "index_sha256": index_sha256,
        "selected_archive": target.archive if target is not None else None,
        "selected_member": target.member if target is not None else None,
        "output_sha256": output_sha256,
        "source_unchanged": source_unchanged,
        "query_target_met": query_target_met,
        # Дополнительные факты приёмки (не заменяют ключи выше).
        "query": QUERY,
        "query_total_matches": total_matches,
        "query_pages_scanned": pages_scanned,
        "selected_title": selected_title,
        "output_path": str(output_path) if output_path is not None else None,
        "output_reused": output_reused,
        "archives_stat_entries": len(archives_after),
        "archives_check_note": ARCHIVES_CHECK_NOTE,
        "catalog_bytes": catalog_stat_before["size"] if catalog_stat_before else None,
        "index_bytes": paths.index.stat().st_size,
        "workdir": str(workdir),
    }
    return report


def _establish_workdir(raw: Path, library: Library, *, reuse: bool) -> Path:
    """Проверить/создать workdir с маркером владения; запись — после проверок.

    Барьер одинаков для нового и повторного workdir: управляемое дерево не
    может содержать symlink/reparse-точки (включая битые), а маркер обязан
    быть регулярным файлом нашего точного содержимого. Существующее
    содержимое никогда не удаляется.

    :raises _Refuse: workdir пересекается с библиотекией, занят, чужой или
        не может быть создан.
    """
    literal = Path(os.path.abspath(raw))
    if os.path.lexists(literal) and _is_link(literal.stat(follow_symlinks=False)):
        raise _Refuse(
            f"путь workdir {literal} сам является ссылкой/reparse-точкой; "
            "укажите реальный каталог"
        )
    resolved = raw.expanduser().resolve()
    if resolved == resolved.parent:
        raise _Refuse(f"workdir не может быть корнем файловой системы: {resolved}")
    if resolved == library.root or _within(resolved, library.root):
        raise _Refuse(
            f"workdir {resolved} лежит внутри библиотеки {library.root}; "
            "источники библиотеки только читаются — выберите workdir вне библиотеки"
        )
    if _within(library.root, resolved):
        raise _Refuse(
            f"библиотека {library.root} лежит внутри workdir {resolved}; "
            "выберите workdir, не содержащий библиотеку"
        )
    marker = resolved / WORKDIR_MARKER
    if resolved.exists():
        if not resolved.is_dir():
            raise _Refuse(f"workdir {resolved} существует, но не является каталогом")
        # Управляемое дерево проверяется до маркера и до любой записи:
        # symlink/reparse-потомки делали бы state/output/report.json
        # записями сквозь ссылку — в том числе внутрь библиотеки.
        _check_managed_tree(resolved, library)
        try:
            mode = marker.stat(follow_symlinks=False).st_mode
        except OSError:
            mode = None
        if mode is None or not stat.S_ISREG(mode):
            raise _Refuse(
                f"workdir {resolved} занят или чужой: нет собственного маркера "
                f"{WORKDIR_MARKER}; выберите новый каталог, существующее "
                "содержимое не очищается"
            )
        try:
            marker_payload = marker.read_bytes()
        except OSError as exc:
            raise _Refuse(
                f"собственный маркер {marker} не читается: {exc}"
            ) from exc
        if marker_payload != _MARKER_PAYLOAD:
            raise _Refuse(
                f"маркер workdir {marker} имеет чужое содержимое; workdir не "
                "принадлежит этому инструменту — выберите другой каталог"
            )
        return resolved
    if reuse:
        raise _Refuse(
            f"--reuse-index требует существующий собственный workdir; "
            f"{resolved} отсутствует"
        )
    try:
        resolved.mkdir(parents=True)
        marker.write_bytes(_MARKER_PAYLOAD)
    except OSError as exc:
        raise _Refuse(f"не удалось создать workdir {resolved}: {exc}") from exc
    return resolved


def _check_managed_tree(workdir: Path, library: Library) -> None:
    """Отвергнуть symlink/reparse и чужие типы в управляемом дереве workdir.

    Для каждого управляемого пути проверяются все его существующие
    компоненты (по самой записи — ``lexists``/lstat, поэтому битые symlink
    тоже ловятся): промежуточные обязаны быть реальными каталогами,
    конечные — реальным каталогом/файлом своего назначения. Существующие
    записи дополнительно сверяются реальным разрешением путей: оно должно
    оставаться внутри workdir и не попадать в библиотеку (защита от
    монтирований, которые lstat не отличает).

    :raises _Refuse: найдена ссылка/reparse-точка, чужой тип записи или
        реальный путь выходит за workdir/в библиотеку.
    """
    for relative, final_kind in _MANAGED_PATHS:
        parts = Path(relative).parts
        for depth in range(1, len(parts) + 1):
            entry = workdir.joinpath(*parts[:depth])
            if not os.path.lexists(entry):
                continue  # отсутствующий компонент создаётся самим инструментом
            info = entry.stat(follow_symlinks=False)
            if _is_link(info):
                raise _Refuse(
                    f"управляемый путь {entry} — символическая ссылка или "
                    "reparse-точка; управляемое дерево workdir обязано "
                    "состоять из реальных записей (запись сквозь ссылку "
                    "недопустима)"
                )
            is_dir = stat.S_ISDIR(info.st_mode)
            if depth < len(parts):
                if not is_dir:
                    raise _Refuse(
                        f"промежуточный управляемый путь {entry} не является "
                        "реальным каталогом"
                    )
                continue
            if final_kind == "dir" and not is_dir:
                raise _Refuse(f"управляемый путь {entry} должен быть каталогом")
            if final_kind == "file" and not stat.S_ISREG(info.st_mode):
                raise _Refuse(f"управляемый путь {entry} должен быть обычным файлом")
            real = entry.resolve()
            if real == library.root or _within(real, library.root):
                raise _Refuse(
                    f"управляемый путь {entry} реально разрешается внутрь "
                    f"библиотеки {library.root}"
                )
            if not _within(real, workdir):
                raise _Refuse(
                    f"управляемый путь {entry} реально разрешается вне workdir "
                    f"{workdir}"
                )


def _is_link(info: os.stat_result) -> bool:
    """Symlink или другой reparse point по lstat-данным (junction Windows)."""
    if stat.S_ISLNK(info.st_mode):
        return True
    # На Windows junction — reparse point без бит symlink; ненулевой
    # st_reparse_tag есть у любой reparse-точки, на POSIX атрибута нет.
    return bool(getattr(info, "st_reparse_tag", 0))


def _publish_report(workdir: Path, report: dict) -> Path:
    """Опубликовать отчёт безопасно: свой временный файл + ``os.replace``.

    ``os.replace`` подменяет саму запись каталога: даже если у назначения
    неожиданно оказалась ссылка, подменится она, а не её цель — записи
    сквозь symlink не происходит. Прямая запись по пути назначения не
    используется вовсе.

    :raises OSError: отчёт не удалось записать; временный файл убирается.
    """
    report_path = workdir / REPORT_NAME
    handle, temp_name = tempfile.mkstemp(
        dir=workdir, prefix=f".{REPORT_NAME}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, report_path)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise
    return report_path


def _within(child: Path, parent: Path) -> bool:
    """``child`` лежит строго внутри ``parent`` (пути уже разрешены)."""
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _stat_entry(path: Path) -> dict:
    """lstat-признаки записи: тип, размер, mtime_ns (без чтения содержимого)."""
    info = path.stat(follow_symlinks=False)
    mode = info.st_mode
    kind = (
        "symlink"
        if stat.S_ISLNK(mode)
        else "dir"
        if stat.S_ISDIR(mode)
        else "file"
        if stat.S_ISREG(mode)
        else "other"
    )
    return {"kind": kind, "size": info.st_size, "mtime_ns": info.st_mtime_ns}


def _stat_snapshot(root: Path) -> dict[str, dict]:
    """Рекурсивный lstat-снимок каталога без следования по symlink."""
    snapshot: dict[str, dict] = {}

    def visit(directory: Path, prefix: str) -> None:
        with os.scandir(directory) as entries:
            for entry in entries:
                rel = f"{prefix}{entry.name}"
                info = entry.stat(follow_symlinks=False)
                kind = (
                    "symlink"
                    if entry.is_symlink()
                    else "dir"
                    if entry.is_dir(follow_symlinks=False)
                    else "file"
                )
                snapshot[rel] = {
                    "kind": kind,
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                }
                if kind == "dir":
                    visit(Path(entry.path), f"{rel}/")

    visit(root, "")
    return snapshot


def _first_difference(before: dict[str, dict], after: dict[str, dict]) -> str:
    """Короткая диагностика первого расхождения снимков (для отказа)."""
    for name in sorted(set(before) | set(after)):
        if before.get(name) != after.get(name):
            return f"{name}: {before.get(name)} -> {after.get(name)}"
    return "без имени"


def _file_sha_or_none(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except OSError:
        return None


def _member_sha256(library: Library, book: Book) -> tuple[str | None, str | None]:
    """SHA-256 точной пары через публичный ``open_book`` (единственное чтение)."""
    digest = hashlib.sha256()
    try:
        with open_book(library, book) as stream:
            while chunk := stream.read(_CHUNK):
                digest.update(chunk)
    except UserError as exc:
        return None, (
            f"чтение точной пары {book.archive}/{book.member} через open_book "
            f"не удалось: {exc}"
        )
    return digest.hexdigest(), None


if __name__ == "__main__":
    sys.exit(main())
