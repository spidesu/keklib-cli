"""Управление rootless-контейнерной сборкой portable linux-x86_64 (задача 11).

Baseline-билдер — собственный rootless-контейнер Podman на закреплённом
digest'е Ubuntu 22.04 (glibc 2.35), определение —
``packaging/builder/linux-x86_64.Containerfile``, пины —
``packaging/builder/builder-pins.json`` (образ, upstream-исходники
CPython 3.14.8 и SQLite 3.54.1 с хешами, apt-состав, конфигурационные
флаги). Никаких привилегированных режимов и монтирования домашнего
каталога/сокетов хоста: проект монтируется только read-only, готовый
комплект собирается в одноразовой копии внутри контейнера, результат
публикуется в один явно указанный каталог вывода.

Подкоманды (можно объединять ``--all``):

- ``--build-image`` — собрать образ билдера из пинов
  (исходники скачиваются и проверяются хешами внутри сборки образа);
- ``--build-bundle --output DIR`` — собрать portable-комплект
  ``linux-x86_64`` в контейнере билдера: snapshot проекта без
  ``.venv``/``build``/``__pycache__``, затем штатный конвейер
  ``tools/build_portable.py`` со всеми fail-closed гейтами;
- ``--smoke --archive PATH`` — приёмочный дым в чистом baseline-образе
  (Ubuntu 22.04 без каких-либо инструментов сборки): проверка SHA-256,
  распаковка архива, ``--help``, ``setup`` и ``index --refresh`` на
  синтетической библиотеке с фактом FTS5-индекса в XDG-пути.

Фикстура дыма генерируется ``--write-fixture DIR`` (переиспользует
синтетическую библиотеку самопроверки сборки). Скрипт запускает podman
через ``subprocess``; сами argv строятся чистыми функциями
(``podman_build_argv``/``podman_bundle_argv``/``podman_smoke_argv``) и
покрыты тестами без запуска podman.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = PROJECT_ROOT / "packaging"
BUILDER_DIR = PACKAGING_DIR / "builder"
PINS_FILE = BUILDER_DIR / "builder-pins.json"
CONTAINERFILE_NAME = "linux-x86_64.Containerfile"

PINS_SCHEMA_VERSION = 1
DEFAULT_TAG = "localhost/keklib-cli-builder:linux-x86_64"

#: Что не попадает в snapshot проекта для сборки внутри контейнера.
SNAPSHOT_EXCLUDES = (".venv", "build", "dist", "__pycache__", ".git", ".mypy_cache")

_TARGET = "linux-x86_64"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX64_COLON = re.compile(r"^sha256:[0-9a-f]{64}$")


class BuilderError(Exception):
    """Пины или команда контейнерной сборки некорректны."""


def load_builder_pins(path: Path | None = None) -> dict[str, object]:
    """Прочитать и проверить packaging/builder/builder-pins.json (fail-closed)."""
    pins_path = path if path is not None else PINS_FILE
    try:
        pins = json.loads(pins_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuilderError(f"нечитаемы пины билдера {pins_path}: {exc}") from exc
    if pins.get("schema_version") != PINS_SCHEMA_VERSION:
        raise BuilderError(f"неизвестная схема пинов билдера: {pins.get('schema_version')!r}")

    def hex64(value: object, what: str) -> str:
        if not isinstance(value, str) or not _HEX64.match(value):
            raise BuilderError(f"{what}: ожидается 64 hex-символа, получено {value!r}")
        return value

    image = pins.get("image")
    if not isinstance(image, dict):
        raise BuilderError("пины билдера без image")
    for key in ("reference", "digest", "image_id", "os_release", "glibc"):
        if not isinstance(image.get(key), str) or not image[key]:
            raise BuilderError(f"image.{key} отсутствует или не строка")
    digest = image["digest"]
    if not _HEX64_COLON.match(digest):
        raise BuilderError(f"image.digest: ожидается sha256:<64hex>, получено {digest!r}")
    if digest not in image["reference"]:
        raise BuilderError("image.reference обязан нести тот же digest, что image.digest")

    sources = pins.get("sources")
    if not isinstance(sources, dict):
        raise BuilderError("пины билдера без sources")
    for name in ("cpython", "sqlite"):
        entry = sources.get(name)
        if not isinstance(entry, dict):
            raise BuilderError(f"sources.{name} отсутствует")
        for key in ("version", "url", "sha256"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise BuilderError(f"sources.{name}.{key} отсутствует или не строка")
        hex64(entry["sha256"], f"sources.{name}.sha256")
        if not isinstance(entry.get("size_bytes"), int) or entry["size_bytes"] <= 0:
            raise BuilderError(f"sources.{name}.size_bytes должен быть положительным числом")
        if not entry["url"].startswith("https://"):
            raise BuilderError(f"sources.{name}.url: только https, получено {entry['url']!r}")
    sqlite = sources["sqlite"]
    if "sha3_256" not in sqlite:
        raise BuilderError("sources.sqlite.sha3_256: upstream публикует SHA3-256 — обязателен")
    hex64(sqlite["sha3_256"], "sources.sqlite.sha3_256")

    if not isinstance(pins.get("apt_packages"), list) or not pins["apt_packages"]:
        raise BuilderError("пины билдера без списка apt_packages")
    apt_text = " ".join(str(item) for item in pins["apt_packages"]).lower()
    for forbidden in ("readline", "gdbm", "ncurses", "tk-dev", "tcl"):
        if forbidden in apt_text:
            raise BuilderError(
                f"GPL/ненужная зависимость {forbidden} в apt_packages: "
                "лицензионное закрытие не должно обрастать GPL-компонентами"
            )
    flags = pins.get("cpython_configure_flags")
    if not isinstance(flags, list) or "--without-readline" not in flags:
        raise BuilderError("cpython_configure_flags обязан содержать --without-readline")
    if "--enable-shared" not in flags:
        raise BuilderError(
            "PyInstaller 6.x требует общую libpython: --enable-shared обязателен"
        )
    sqlite_flags = pins.get("sqlite_configure_flags")
    if not isinstance(sqlite_flags, list) or "--enable-fts5" not in sqlite_flags:
        raise BuilderError("sqlite_configure_flags обязан содержать --enable-fts5")
    return pins


def _snapshot_tar_options() -> list[str]:
    """Опции tar для snapshot проекта: исключения и по префиксу, и по базовому имени."""
    options: list[str] = []
    for exclude in SNAPSHOT_EXCLUDES:
        options.append(f"--exclude=./{exclude}")
        options.append(f"--exclude={exclude}")
    return options


def podman_build_argv(
    pins: dict[str, object],
    *,
    tag: str,
    context: Path | None = None,
) -> list[str]:
    """Argv сборки образа билдера: пины передаются build-arg'ами и проверяются внутри.

    ``BASE_IMAGE`` передаётся из ``pins['image']['reference']`` — пин образа
    управляет фактической рецептурой, значение по умолчанию в Containerfile
    остаётся лишь второй линией защиты (review native round 1, P2).
    """
    cpython = pins["sources"]["cpython"]
    sqlite = pins["sources"]["sqlite"]
    return [
        "podman",
        "build",
        f"--build-arg=BASE_IMAGE={pins['image']['reference']}",
        f"--build-arg=CPYTHON_URL={cpython['url']}",
        f"--build-arg=CPYTHON_SHA256={cpython['sha256']}",
        f"--build-arg=CPYTHON_VERSION={cpython['version']}",
        f"--build-arg=SQLITE_URL={sqlite['url']}",
        f"--build-arg=SQLITE_SHA256={sqlite['sha256']}",
        f"--build-arg=SQLITE_VERSION={sqlite['version']}",
        "-f",
        str(BUILDER_DIR / CONTAINERFILE_NAME),
        "-t",
        tag,
        str(context if context is not None else PACKAGING_DIR),
    ]


def _containerfile_continuation_tokens(text: str, anchor: str) -> list[str]:
    """Токены shell-команды с продолжениями: от строки с anchor до конца команды.

    Команда заканчивается строкой без ``\\``-продолжения либо строкой, чья
    содержательная часть завершается ``;`` (терминатор shell-команды до
    физического продолжения RUN).
    """
    lines = text.splitlines()
    tokens: list[str] = []
    collecting = False
    for line in lines:
        stripped = line.strip()
        if not collecting:
            if anchor in stripped:
                collecting = True
            else:
                continue
        ended = not stripped.endswith("\\")
        piece = stripped.rstrip("\\").strip()
        piece_ended = piece.endswith(";")
        tokens.extend(piece.split())
        if ended or piece_ended:
            break
    else:
        if collecting:
            raise BuilderError(f"Containerfile: команда с {anchor!r} обрывается продолжением")
    return [token.rstrip(";,:") for token in tokens]


def verify_containerfile_recipe(pins: dict[str, object]) -> None:
    """Рецепт Containerfile обязан соответствовать пинам (fail-closed).

    Проверяются: ARG BASE_IMAGE == pins image.reference; список пакетов
    apt-get install == pins apt_packages (как множество — без лишних и
    без пропущенных); флаги configure CPython/SQLite == pins (как
    множество). Рецепт и пины не могут разойтись незаметно — любое
    расхождение блокирует сборку до правки одной из сторон (review
    native round 1, P2).
    """
    text = (BUILDER_DIR / CONTAINERFILE_NAME).read_text(encoding="utf-8")
    match = re.search(r"^ARG BASE_IMAGE=(\S+)", text, flags=re.MULTILINE)
    if match is None:
        raise BuilderError("Containerfile: нет ARG BASE_IMAGE — образ не запинен рецептом")
    if match.group(1) != pins["image"]["reference"]:
        raise BuilderError(
            "image: Containerfile ARG BASE_IMAGE="
            f"{match.group(1)} != пин {pins['image']['reference']}; "
            "рецепт и пины разошлись"
        )

    apt_anchor = "apt-get install -y --no-install-recommends"
    apt_tokens = _containerfile_continuation_tokens(text, apt_anchor)
    package_start = apt_tokens.index("--no-install-recommends") + 1
    actual_apt = set(apt_tokens[package_start:])
    expected_apt = {str(item) for item in pins["apt_packages"]}
    if actual_apt != expected_apt:
        raise BuilderError(
            "apt: Containerfile и пины разошлись: только в рецепте "
            f"{sorted(actual_apt - expected_apt)}, только в пинах "
            f"{sorted(expected_apt - actual_apt)}"
        )

    for name, anchor, pin_key in (
        ("cpython", "configure --prefix=/opt/python", "cpython_configure_flags"),
        ("sqlite", "configure --prefix=/opt/sqlite", "sqlite_configure_flags"),
    ):
        tokens = _containerfile_continuation_tokens(text, anchor)
        start = tokens.index("./configure") + 1 if "./configure" in tokens else 0
        actual = {token for token in tokens[start:]}
        expected = {str(item) for item in pins[pin_key]}
        if actual != expected:
            raise BuilderError(
                f"{name}: флаги configure разошлись: только в рецепте "
                f"{sorted(actual - expected)}, только в пинах "
                f"{sorted(expected - actual)}"
            )


def _bundle_inner_script() -> str:
    """Скрипт внутри контейнера билдера: snapshot и штатная сборка."""
    excludes = " ".join(_snapshot_tar_options())
    return (
        "set -euo pipefail\n"
        "mkdir -p /work /work-tmp\n"
        f"tar -C /src {excludes} -cf - . | tar -C /work -xf -\n"
        "cd /work\n"
        "export PYTHONPATH=/work/src\n"
        "export TMPDIR=/work-tmp\n"
        "/opt/venv/bin/python tools/build_portable.py "
        f"--target {_TARGET} --output /out\n"
    )


def podman_bundle_argv(
    pins: dict[str, object],
    *,
    tag: str,
    project_root: Path,
    output_dir: Path,
) -> list[str]:
    """Argv сборки комплекта: проект только read-only, вывод — отдельный каталог."""
    return [
        "podman",
        "run",
        "--rm",
        "-v",
        f"{project_root}:/src:ro",
        "-v",
        f"{output_dir}:/out",
        tag,
        "bash",
        "-c",
        _bundle_inner_script(),
    ]


#: Постоянный скрипт дыма: имя архива никогда не интерполируется
#: (монтирование под фиксированными именами — review native round 1, P2).
_SMOKE_SCRIPT = """set -euo pipefail
cd /archive
awk '{print $1"  bundle.tar.gz"}' checksum.txt | sha256sum -c -
mkdir -p /tmp/smoke /tmp/state
tar -xzf bundle.tar.gz -C /tmp/smoke
cd /tmp/smoke/keklib-cli
./keklib-cli --help >/dev/null
export HOME=/tmp/state
export XDG_CONFIG_HOME=/tmp/state/.config
export XDG_CACHE_HOME=/tmp/state/.cache
./keklib-cli setup --library /fixture/library >/dev/null
./keklib-cli index --refresh >/dev/null
test -f /tmp/state/.cache/keklib-cli/index.sqlite3
echo smoke-ok: bundle + FTS5 index on $(. /etc/os-release; echo $PRETTY_NAME)
"""


def podman_smoke_argv(
    pins: dict[str, object],
    *,
    archive: Path,
    fixture: Path,
) -> list[str]:
    """Argv дыма в чистом baseline-образе (не в билдере): без инструментов сборки.

    Архив и контрольная сумма монтируются под фиксированными именами
    (``/archive/bundle.tar.gz`` и ``/archive/checksum.txt``): произвольное
    имя архива на хосте — включая пробелы, кавычки и shell-метасимволы —
    является одиночным argv-элементом монтирования и не попадает в
    bash-скрипт. Семантика ``sha256sum -c`` сохранена: digest-поле
    исходной суммы проверяется против байтов архива (файл суммы не
    переписывается, исходное имя остаётся записанным в нём).
    """
    checksum = archive.with_name(archive.name + ".sha256")
    return [
        "podman",
        "run",
        "--rm",
        "-v",
        f"{archive}:/archive/bundle.tar.gz:ro",
        "-v",
        f"{checksum}:/archive/checksum.txt:ro",
        "-v",
        f"{fixture}:/fixture:ro",
        str(pins["image"]["reference"]),
        "bash",
        "-c",
        _SMOKE_SCRIPT,
    ]


def write_smoke_fixture(directory: Path) -> Path:
    """Синтетическая библиотека для дыма (переиспользует фикстуру самопроверки)."""
    if str(PROJECT_ROOT) not in sys.path:
        # Прямой запуск ``python tools/build_in_container.py``: пакет tools
        # не находится без репозитория в sys.path.
        sys.path.insert(0, str(PROJECT_ROOT))
    from tools.build_portable import _write_selfcheck_library

    library = directory / "library"
    if not (library / "librusec_flib.inpx").is_file():
        _write_selfcheck_library(library)
    return library


def _run(argv: Sequence[str]) -> None:
    print(f"[container] $ {' '.join(str(item) for item in argv)}", file=sys.stderr)
    result = subprocess.run([str(item) for item in argv], check=False)
    if result.returncode != 0:
        raise BuilderError(f"команда завершилась с кодом {result.returncode}: {argv[0]}")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI управления контейнерной сборкой (см. докстринг модуля)."""
    parser = argparse.ArgumentParser(
        prog="build_in_container",
        description="Rootless-контейнерная baseline-сборка portable linux-x86_64.",
    )
    parser.add_argument("--build-image", action="store_true", help="собрать образ билдера")
    parser.add_argument(
        "--build-bundle", action="store_true", help="собрать комплект в контейнере билдера"
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="дым готового архива в чистом baseline-образе (нужен --archive)",
    )
    parser.add_argument("--all", action="store_true", help="build-image + build-bundle + smoke")
    parser.add_argument(
        "--write-fixture",
        type=Path,
        default=None,
        metavar="DIR",
        help="записать синтетическую библиотеку дыма в DIR",
    )
    parser.add_argument("--tag", default=DEFAULT_TAG, help="имя образа билдера")
    parser.add_argument(
        "--output", type=Path, default=None, help="каталог архива (только не очищается)"
    )
    parser.add_argument("--archive", type=Path, default=None, help="архив для --smoke")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help="корень проекта (монтируется read-only)",
    )
    args = parser.parse_args(argv)

    try:
        pins = load_builder_pins()
        if args.write_fixture is not None:
            library = write_smoke_fixture(args.write_fixture)
            print(library)
        if args.all or args.build_image:
            verify_containerfile_recipe(pins)
            _run(podman_build_argv(pins, tag=args.tag))
        if args.all or args.build_bundle:
            if args.output is None:
                parser.error("--build-bundle требует --output")
            output = args.output.resolve()
            output.mkdir(parents=True, exist_ok=True)
            _run(
                podman_bundle_argv(
                    pins, tag=args.tag, project_root=args.project_root.resolve(), output_dir=output
                )
            )
        if args.all or args.smoke:
            archive = args.archive
            if archive is None:
                if args.output is None:
                    parser.error("--smoke требует --archive (или --output для поиска)")
                found = sorted(args.output.resolve().glob(f"keklib-cli-*-{_TARGET}.tar.gz"))
                if len(found) != 1:
                    parser.error(
                        f"ожидался ровно один архив {_TARGET} в {args.output}, найдено: {found}"
                    )
                archive = found[0]
            fixture_dir = (archive.parent / "smoke-fixture").resolve()
            write_smoke_fixture(fixture_dir)
            _run(podman_smoke_argv(pins, archive=archive.resolve(), fixture=fixture_dir))
    except BuilderError as exc:
        print(f"build_in_container: ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
