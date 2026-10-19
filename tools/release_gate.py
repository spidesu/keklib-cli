"""Выпускной гейт portable-комплектов keklib-cli (задача 12).

Проверяет комплект из четырёх нативных сборок перед выпуском:

- ``validate_reports(reports, targets)`` — ровно один успешный основной
  отчёт smoke на каждый target, единая версия, нативное совпадение ОС/CPU и
  подтверждение заявленного минимума ОС фактами хоста (флагам не верит:
  минимум перепроверяется по фактам отчёта);
- ``verify_artifacts(artifacts_dir, reports_dir)`` — архивы всех target по
  имени ``keklib-cli-<version>-<target>.<ext>``, sidecar-суммы ``.sha256``,
  build-info.json внутри архива, связь «отчёт ↔ архив» по SHA-256; итог —
  ``SHA256SUMS`` без перезаписи и без какой-либо публикации.

Правило минимума ОС ``minimum_os_evidence`` переиспользуется инструментом
``tools/smoke_portable.py`` (обе утилиты живут во вспомогательном
harness-архиве), поэтому модуль не читает файлы при импорте и не импортирует
приложение ``keklib_cli``. ``packaging/targets.json`` требуется только для
гейта комплектов (запускается из checkout).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tarfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGETS_FILE = PROJECT_ROOT / "packaging" / "targets.json"

SCHEMA_VERSION = 1
ARCHIVE_ROOT = "keklib-cli"

#: Обязательные ключи основного отчёта smoke (задача 12, протокол отчёта).
REQUIRED_REPORT_KEYS = (
    "version",
    "target",
    "host_os",
    "host_os_version",
    "host_arch",
    "minimum_os",
    "minimum_os_verified",
    "archive_sha256",
    "passed",
)

#: Флаги-наблюдения: строго ``bool`` и строго ``True`` (truthiness запрещён).
REQUIRED_TRUE_FLAGS = (
    "minimum_os_verified",
    "passed",
    "fts5",
    "source_unchanged",
    "no_system_python",
    "source_checkout_absent",
    "readonly_bundle",
    "state_outside_bundle",
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NAME_PREFIX = "keklib-cli-"


class ReleaseGateError(RuntimeError):
    """Комплект не проходит выпускной гейт."""


# ---------------------------------------------------------------------------
# Целевые платформы


def load_target_specs(path: Path | None = None) -> dict[str, dict[str, object]]:
    """Прочитать и проверить packaging/targets.json (лениво, при вызове)."""
    targets_path = path if path is not None else TARGETS_FILE
    try:
        data = json.loads(targets_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseGateError(f"нечитаем файл целей {targets_path}: {exc}") from exc
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseGateError(
            f"неизвестная схема targets.json: {data.get('schema_version')!r}"
        )
    targets = data.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise ReleaseGateError("targets.json не содержит описаний target")
    for name, spec in targets.items():
        if not isinstance(spec, dict):
            raise ReleaseGateError(f"target {name!r} описан не объектом")
        for key in ("system", "machine", "runner", "baseline_os", "minimum_os",
                    "archive_format", "executable"):
            if not isinstance(spec.get(key), str) or not spec[key]:
                raise ReleaseGateError(f"target {name!r}: поле {key} отсутствует")
        if spec["archive_format"] not in ("tar.gz", "zip"):
            raise ReleaseGateError(
                f"target {name!r}: неизвестный формат архива {spec['archive_format']!r}"
            )
    return {name: dict(spec) for name, spec in targets.items()}


# ---------------------------------------------------------------------------
# Нативное подтверждение заявленного минимума ОС


def _glibc_key(value: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in value.strip().split("."))
    except (ValueError, AttributeError):
        return None


def minimum_os_evidence(
    declared_minimum: object,
    minimum_glibc: object,
    facts: Mapping[str, object],
) -> tuple[bool, str]:
    """Подтверждает ли хост заявленный минимум ОС по фактам, а не флагам.

    Более новая/серверная ОС не считается доказательством заявленного
    минимума: baseline-ОС должна совпадать с заявленной (Ubuntu 22.04,
    Windows Server 2022, macOS с заявленной мажорной версией), а для Linux
    glibc хоста — не ниже заявленного floor. Клиентский минимум Windows
    нативно не проверялся и подтверждён быть не может.
    """
    declared = str(declared_minimum or "")
    system = str(facts.get("host_os") or "")

    if system == "Linux":
        match = re.search(r"Ubuntu\s+(\d+\.\d+)", declared)
        if not match:
            return False, f"заявленный минимум Linux не разобран: {declared!r}"
        needed = match.group(1)
        os_id = facts.get("os_id")
        version_id = facts.get("os_version_id")
        if os_id != "ubuntu":
            return False, f"хост не Ubuntu (os_id={os_id!r}), заявлен {needed}"
        if str(version_id or "") != needed:
            return False, (
                f"хост Ubuntu {version_id!r}, заявлен минимум Ubuntu {needed}: "
                "другая/более новая ОС не доказывает заявленный минимум"
            )
        floor = str(minimum_glibc or "").strip()
        glibc = facts.get("host_glibc")
        if floor:
            host_key = _glibc_key(str(glibc or ""))
            floor_key = _glibc_key(floor)
            if host_key is None or floor_key is None:
                return False, f"версия glibc не определяется: хост {glibc!r}, floor {floor!r}"
            if host_key < floor_key:
                return False, f"glibc хоста {glibc} ниже заявленного минимума {floor}"
        return True, f"нативный baseline Ubuntu {needed}, glibc {glibc} >= {floor or '—'}"

    if system == "Windows":
        match = re.search(r"Windows\s+Server\s+(\d{4})", declared)
        if not match:
            return False, (
                "клиентский минимум Windows нативно не проверялся и не может "
                f"быть подтверждён заявлением {declared!r}"
            )
        needed = match.group(1)
        release = str(facts.get("windows_release") or "")
        if release != f"{needed}Server":
            return False, (
                f"хост windows_release={release!r} не является заявленным "
                f"baseline Windows Server {needed}"
            )
        return True, f"нативный baseline Windows Server {needed}"

    if system == "Darwin":
        match = re.search(r"macOS\s+(\d+)", declared)
        if not match:
            return False, f"заявленный минимум macOS не разобран: {declared!r}"
        needed = match.group(1)
        version = str(facts.get("macos_version") or "")
        major = version.split(".")[0] if version else ""
        if major != needed:
            return False, (
                f"хост macOS {version or 'неизвестна'}, заявлен минимум macOS {needed}"
            )
        return True, f"нативный baseline macOS {needed}"

    return False, f"неизвестная ОС хоста: {system!r}"


# ---------------------------------------------------------------------------
# Отчёты smoke


def validate_reports(reports: Sequence[object], targets: Sequence[str]) -> None:
    """Ровно один успешный основной отчёт на каждый target единой версии.

    Проверяются строгие типы (bool-флаги — именно ``bool``), нативное
    совпадение host ОС/CPU с target, совпадение заявленного минимума с
    targets.json и фактическое (пересчитанное) подтверждение минимума.
    """
    specs = load_target_specs()
    expected = list(targets)
    for name in expected:
        if name not in specs:
            raise ReleaseGateError(f"неизвестный target: {name!r}")

    if not reports:
        raise ReleaseGateError("пустой набор отчётов smoke")

    seen: dict[str, dict[str, object]] = {}
    versions: set[str] = set()
    for index, report in enumerate(reports):
        if not isinstance(report, Mapping):
            raise ReleaseGateError(f"отчёт #{index} не является объектом")
        absent = [key for key in REQUIRED_REPORT_KEYS if key not in report]
        absent += [flag for flag in REQUIRED_TRUE_FLAGS if flag not in report]
        if absent:
            raise ReleaseGateError(
                f"отчёт #{index} без обязательных ключей: {', '.join(sorted(set(absent)))}"
            )
        for key in ("version", "target", "host_os", "host_os_version",
                    "host_arch", "minimum_os"):
            if not isinstance(report[key], str) or not report[key]:
                raise ReleaseGateError(f"отчёт #{index}: {key} не непустая строка")
        for flag in REQUIRED_TRUE_FLAGS:
            if type(report[flag]) is not bool:
                raise ReleaseGateError(
                    f"отчёт target {report.get('target')!r}: флаг {flag} должен "
                    f"быть строго bool, получено {report[flag]!r}"
                )
            if report[flag] is not True:
                raise ReleaseGateError(
                    f"отчёт target {report.get('target')!r}: {flag} не подтверждён "
                    f"успешным прогоном ({report[flag]!r})"
                )
        digest = report["archive_sha256"]
        if not isinstance(digest, str) or not _HEX64.match(digest):
            raise ReleaseGateError(f"отчёт #{index}: archive_sha256 не SHA-256 hex")

        target = str(report["target"])
        if target not in expected:
            raise ReleaseGateError(
                f"отчёт постороннего target: {target!r}; ожидаются: "
                f"{', '.join(expected)}"
            )
        if target in seen:
            raise ReleaseGateError(
                f"повтор основного отчёта target {target!r}: основной отчёт "
                "должен быть ровно один"
            )
        seen[target] = dict(report)
        versions.add(str(report["version"]))

    if len(versions) > 1:
        raise ReleaseGateError(
            f"комплект разных версий: {', '.join(sorted(versions))}"
        )
    missing = [name for name in expected if name not in seen]
    if missing:
        raise ReleaseGateError(
            "нет успешного основного отчёта для target: " + ", ".join(missing)
        )

    for target, report in seen.items():
        spec = specs[target]
        if report["host_os"] != spec["system"]:
            raise ReleaseGateError(
                f"{target}: отчёт с host_os={report['host_os']!r}, target "
                f"требует {spec['system']!r}"
            )
        if report["host_arch"] != spec["machine"]:
            raise ReleaseGateError(
                f"{target}: отчёт с host_arch={report['host_arch']!r}, target "
                f"требует CPU {spec['machine']!r}"
            )
        if report["minimum_os"] != spec["minimum_os"]:
            raise ReleaseGateError(
                f"{target}: отчёт заявляет минимум {report['minimum_os']!r}, "
                f"targets.json — {spec['minimum_os']!r}"
            )
        confirmed, reason = minimum_os_evidence(
            spec["minimum_os"], spec.get("minimum_glibc"), report
        )
        if not confirmed:
            raise ReleaseGateError(
                f"{target}: заявленный минимум ОС не подтверждён нативными "
                f"фактами хоста: {reason}"
            )
        if report["minimum_os_verified"] is not True:
            raise ReleaseGateError(
                f"{target}: smoke не подтвердил минимум ОС собственным прогоном"
            )


# ---------------------------------------------------------------------------
# Архивы и контрольные суммы


def _read_build_info(archive: Path) -> dict[str, object]:
    """Прочитать build-info.json из архива без распаковки на диск."""
    member = f"{ARCHIVE_ROOT}/build-info.json"
    payload: bytes
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zipped:
            with zipped.open(member) as stream:
                payload = stream.read(1 << 20)  # guard: не больше 1 MiB
    else:
        with tarfile.open(archive, "r:gz") as tar:
            handle = tar.extractfile(member)
            if handle is None:
                raise ReleaseGateError(f"{archive.name}: {member} не является файлом")
            payload = handle.read(1 << 20)
    try:
        info = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ReleaseGateError(f"{archive.name}: build-info.json не читается: {exc}") from exc
    if not isinstance(info, dict):
        raise ReleaseGateError(f"{archive.name}: build-info.json не объект")
    return info


def verify_artifacts(
    artifacts_dir: Path,
    reports_dir: Path | None = None,
) -> list[tuple[str, str]]:
    """Проверить полный комплект и записать SHA256SUMS.

    Возвращает отсортированный список ``(имя_файла, digest)``. Существующий
    ``SHA256SUMS`` не перезаписывается: гейт не трогает чужие файлы.
    """
    artifacts = Path(artifacts_dir)
    reports_path = artifacts if reports_dir is None else Path(reports_dir)
    specs = load_target_specs()

    versions: set[str] = set()
    digests: dict[str, str] = {}
    names: dict[str, str] = {}
    for target, spec in specs.items():
        ext = str(spec["archive_format"])
        matches = sorted(artifacts.glob(f"{_NAME_PREFIX}*-{target}.{ext}"))
        if not matches:
            raise ReleaseGateError(
                f"{artifacts}: нет архива keklib-cli-<version>-{target}.{ext}"
            )
        if len(matches) > 1:
            listing = ", ".join(item.name for item in matches)
            raise ReleaseGateError(
                f"несколько архивов target {target}: {listing}"
            )
        archive = matches[0]
        suffix = f"-{target}.{ext}"
        version = archive.name[len(_NAME_PREFIX):-len(suffix)]
        if not version:
            raise ReleaseGateError(f"не разбирается версия в имени: {archive.name}")
        versions.add(version)

        sidecar = archive.with_name(archive.name + ".sha256")
        if not sidecar.is_file():
            raise ReleaseGateError(f"нет sidecar-суммы: {sidecar.name}")
        first = sidecar.read_text(encoding="utf-8").split()
        if not first or not _HEX64.match(first[0].lower()):
            raise ReleaseGateError(f"sidecar-сумма не является SHA-256: {sidecar.name}")
        actual = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                actual.update(chunk)
        if actual.hexdigest() != first[0].lower():
            raise ReleaseGateError(
                f"контрольная сумма не совпадает: {archive.name}"
            )

        info = _read_build_info(archive)
        if info.get("target") != target:
            raise ReleaseGateError(
                f"{archive.name}: build-info объявляет target {info.get('target')!r}"
            )
        if info.get("version") != version:
            raise ReleaseGateError(
                f"{archive.name}: build-info объявляет версию {info.get('version')!r}, "
                f"имя — {version!r}"
            )
        if info.get("minimum_os") != spec["minimum_os"]:
            raise ReleaseGateError(
                f"{archive.name}: build-info заявляет минимум {info.get('minimum_os')!r}, "
                f"targets.json — {spec['minimum_os']!r}"
            )
        digests[target] = actual.hexdigest()
        names[target] = archive.name

    if len(versions) > 1:
        raise ReleaseGateError(
            f"комплект архивов разных версий: {', '.join(sorted(versions))}"
        )
    version = versions.pop()

    reports: list[dict[str, object]] = []
    for target in sorted(specs):
        report_file = reports_path / f"smoke-report-{target}.json"
        if not report_file.is_file():
            raise ReleaseGateError(f"нет отчёта smoke: {report_file}")
        try:
            report = json.loads(report_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReleaseGateError(f"{report_file} не читается: {exc}") from exc
        if not isinstance(report, dict):
            raise ReleaseGateError(f"{report_file} не объект")
        if report.get("archive_sha256") != digests[target]:
            raise ReleaseGateError(
                f"{report_file.name}: сумма архива {report.get('archive_sha256')!r} "
                f"не совпадает с фактической для {names[target]}"
            )
        # SHA связывает байты, но не содержание: версия и target отчёта
        # обязаны соответствовать именно этому архиву, а не только сумме.
        if report.get("version") != version:
            raise ReleaseGateError(
                f"{report_file.name}: версия отчёта {report.get('version')!r} "
                f"не совпадает с версией архива {names[target]} ({version!r})"
            )
        if report.get("target") != target:
            raise ReleaseGateError(
                f"{report_file.name}: target отчёта {report.get('target')!r} "
                f"не совпадает с архивом {names[target]} ({target!r})"
            )
        reports.append(report)

    validate_reports(reports, sorted(specs))

    lines = [
        f"{digests[target]}  {names[target]}"
        for target in sorted(specs)
    ]
    sums_path = artifacts / "SHA256SUMS"
    if sums_path.exists() or sums_path.is_symlink():
        raise ReleaseGateError(f"SHA256SUMS уже существует и не перезаписывается: {sums_path}")
    handle = os.open(sums_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write("".join(line + "\n" for line in lines))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            sums_path.unlink()
        raise

    print(f"release-gate: OK, версия {version}, target: {', '.join(sorted(specs))}")
    print(f"release-gate: SHA256SUMS → {sums_path}")
    return sorted((names[target], digests[target]) for target in specs)


# ---------------------------------------------------------------------------
# CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="release_gate",
        description=(
            "Выпускной гейт portable keklib-cli: четыре архива по SHA-256, "
            "отчёты нативного изолированного smoke и SHA256SUMS без публикации"
        ),
    )
    parser.add_argument("--artifacts", type=Path, required=True,
                        help="каталог с архивами keklib-cli-<version>-<target> и их .sha256")
    parser.add_argument("--reports", type=Path, default=None,
                        help="каталог с smoke-report-<target>.json (по умолчанию --artifacts)")
    args = parser.parse_args(argv)

    try:
        verify_artifacts(args.artifacts, args.reports)
    except ReleaseGateError as exc:
        print(f"release-gate: ОТКАЗ: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
