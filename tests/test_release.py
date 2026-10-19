"""Тесты изолированного smoke и выпускного гейта portable (задача 12).

Правила гейта и контрольных сумм проверяются на **явно синтетических**
фикстурах (маленькие архивы и отчёты, собранные здесь же); они не являются
нативными прогонами Windows/macOS и успехом выпуска не считаются. Реальный
frozen smoke (`FrozenSmokeTests`) запускается только на действительно
построенном архиве: без артефакта тест честно пропускается, а не «зелёнеет».

`tools/smoke_portable.py` и `tools/release_gate.py` — test-only инструменты:
приложение `keklib_cli` они не импортируют и запускаются из вспомогательного
harness-архива без checkout исходников.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.harness.permissions import PermissionIsolationError, readonly_bundle
from tools import release_gate, smoke_portable

#: Архив linux-x86_64, принятый в задаче 11 (review r2).
DEFAULT_SMOKE_ARCHIVE = Path(
    "/tmp/opencode/portable-linux-out-r2/keklib-cli-0.1.0-linux-x86_64.tar.gz"
)

#: Известная пара приёмки из `tests/test_integration.py` — та же синтетика.
KNOWN_AUTHOR = "Булгаков, Михаил Афансьевич"
KNOWN_TITLE = "Мастер и Маргарита"
KNOWN_PAYLOAD = b"<FictionBook id='66372'/>"
NEW_PAYLOAD = b"<FictionBook id='777' new/>"


# ---------------------------------------------------------------------------
# Синтетические фикстуры правил гейта (не нативные прогоны!)


def _specs() -> dict[str, dict]:
    return release_gate.load_target_specs()


def _host_facts(target: str) -> dict[str, object]:
    """Факты хоста, ровно совпадающие с baseline-ОС target из targets.json."""
    facts: dict[str, object] = {
        "linux-x86_64": {
            "host_os": "Linux",
            "host_arch": "x86_64",
            "host_os_version": "5.15.0-synthetic",
            "os_id": "ubuntu",
            "os_version_id": "22.04",
            "host_glibc": "2.35",
        },
        "windows-x86_64": {
            "host_os": "Windows",
            "host_arch": "x86_64",
            "host_os_version": "10.0.20348",
            "windows_release": "2022Server",
        },
        "macos-x86_64": {
            "host_os": "Darwin",
            "host_arch": "x86_64",
            "host_os_version": "24.0-synthetic",
            "macos_version": "15.0",
        },
        "macos-arm64": {
            "host_os": "Darwin",
            "host_arch": "arm64",
            "host_os_version": "23.0-synthetic",
            "macos_version": "14.0",
        },
    }
    return dict(facts[target])


def _report(target: str, *, version: str = "0.0.0-synthetic", **override: object):
    """Полный валидный основной отчёт smoke (синтетика для правил гейта)."""
    spec = _specs()[target]
    facts = _host_facts(target)
    ok, _reason = release_gate.minimum_os_evidence(
        spec["minimum_os"], spec.get("minimum_glibc"), facts
    )
    assert ok, f"синтетический baseline {target} должен подтверждаться правилом"
    report: dict[str, object] = {
        "schema_version": 1,
        "version": version,
        "target": target,
        "minimum_os": spec["minimum_os"],
        "minimum_os_verified": True,
        "archive_sha256": "a" * 64,
        "fts5": True,
        "source_unchanged": True,
        "no_system_python": True,
        "source_checkout_absent": True,
        "readonly_bundle": True,
        "state_outside_bundle": True,
        "passed": True,
    }
    report.update(facts)
    report.update(override)
    return report


def _all_targets() -> list[str]:
    return sorted(_specs())


def _four_reports(**per_target: object) -> list[dict]:
    return [
        _report(target, **(per_target.get(target, {}) or {}))  # type: ignore[arg-type]
        for target in _all_targets()
    ]


# ---------------------------------------------------------------------------
# Синтетические архивы для гейта контрольных сумм (не настоящие комплекты!)


def _write_gate_archive(directory: Path, target: str, version: str) -> Path:
    """Минимальный архив с корректным build-info.json (синтетика)."""
    spec = _specs()[target]
    executable = str(spec["executable"])
    ext = str(spec["archive_format"])
    name = f"keklib-cli-{version}-{target}.{ext}"
    path = directory / name
    info = {
        "version": version,
        "target": target,
        "python": "3.14.8",
        "sqlite": "3.54.1",
        "pyinstaller": "6.22.3",
        "schema_version": 1,
        "rules_version": 1,
        "minimum_os": spec["minimum_os"],
        "minimum_glibc": spec.get("minimum_glibc"),
    }
    if ext == "zip":
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("keklib-cli/build-info.json", json.dumps(info))
            archive.writestr(f"keklib-cli/{executable}", b"synthetic exe")
    else:
        with tarfile.open(path, "w:gz") as archive:
            root = tarfile.TarInfo("keklib-cli")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            payload = json.dumps(info).encode("utf-8")
            member = tarfile.TarInfo("keklib-cli/build-info.json")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
            exe = tarfile.TarInfo(f"keklib-cli/{executable}")
            exe.size = 12
            exe.mode = 0o755
            archive.addfile(exe, io.BytesIO(b"synthetic exe"))
    digest = smoke_portable.sha256_file(path)
    path.with_name(path.name + ".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8"
    )
    return path


def _write_reports(directory: Path, reports: list[dict], *, digests: dict[str, str]):
    for report in reports:
        target = str(report["target"])
        copy = dict(report)
        copy["archive_sha256"] = digests.get(target, str(report["archive_sha256"]))
        (directory / f"smoke-report-{target}.json").write_text(
            json.dumps(copy), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Правила validate_reports


class ReleaseGateTests(unittest.TestCase):
    """Правила `validate_reports` на синтетических отчётах."""

    def test_reject_missing_failed_or_wrong_cpu_report(self):
        targets = _all_targets()
        with self.assertRaises(release_gate.ReleaseGateError) as missed:
            release_gate.validate_reports(
                [_report(t) for t in targets if t != "windows-x86_64"], targets
            )
        self.assertIn("windows-x86_64", str(missed.exception))

        def _reports(**windows_override: object) -> list[dict]:
            return [
                _report(t, **(windows_override if t == "windows-x86_64" else {}))  # type: ignore[arg-type]
                for t in targets
            ]

        with self.assertRaises(release_gate.ReleaseGateError) as truthy:
            release_gate.validate_reports(_reports(passed="yes"), targets)
        self.assertIn("windows-x86_64", str(truthy.exception))
        self.assertIn("bool", str(truthy.exception))

        with self.assertRaises(release_gate.ReleaseGateError) as failed:
            release_gate.validate_reports(_reports(passed=False), targets)
        self.assertIn("windows-x86_64", str(failed.exception))

        wrong_cpu = dict(_report("macos-arm64"), host_arch="x86_64")
        with self.assertRaises(release_gate.ReleaseGateError) as cpu:
            release_gate.validate_reports(
                [_report(t) for t in targets if t != "macos-arm64"] + [wrong_cpu],
                targets,
            )
        self.assertIn("macos-arm64", str(cpu.exception))

    def test_mixed_version_or_duplicate_target(self):
        targets = _all_targets()
        mixed = _four_reports()
        mixed[0]["version"] = "1.0.0"
        mixed[1]["version"] = "2.0.0"
        with self.assertRaises(release_gate.ReleaseGateError) as versions:
            release_gate.validate_reports(mixed, targets)
        self.assertIn("версий", str(versions.exception))

        duplicate = _four_reports() + [_report("linux-x86_64")]
        with self.assertRaises(release_gate.ReleaseGateError) as dup:
            release_gate.validate_reports(duplicate, targets)
        self.assertIn("linux-x86_64", str(dup.exception))

    def test_declared_minimum_requires_native_evidence(self):
        targets = _all_targets()
        # Более новая Ubuntu не доказывает минимум 22.04 — даже с флагом True:
        # гейт перепроверяет факты, а не верит флагу.
        newer = dict(_report("linux-x86_64"), os_version_id="24.04", host_glibc="2.39")
        with self.assertRaises(release_gate.ReleaseGateError) as fresh:
            release_gate.validate_reports(
                [_report(t) for t in targets if t != "linux-x86_64"] + [newer], targets
            )
        self.assertIn("linux-x86_64", str(fresh.exception))

        # Другой дистрибутив и старая glibc — не доказательство.
        debian = dict(_report("linux-x86_64"), os_id="debian", os_version_id="12")
        with self.assertRaises(release_gate.ReleaseGateError):
            release_gate.validate_reports(
                [_report(t) for t in targets if t != "linux-x86_64"] + [debian], targets
            )
        old_glibc = dict(_report("linux-x86_64"), host_glibc="2.31")
        with self.assertRaises(release_gate.ReleaseGateError):
            release_gate.validate_reports(
                [_report(t) for t in targets if t != "linux-x86_64"] + [old_glibc],
                targets,
            )

        # Сервер новее заявленного baseline — не доказательство.
        server_2025 = dict(_report("windows-x86_64"), windows_release="2025Server")
        with self.assertRaises(release_gate.ReleaseGateError):
            release_gate.validate_reports(
                [_report(t) for t in targets if t != "windows-x86_64"] + [server_2025],
                targets,
            )

        # macOS новее/старее заявленной мажорной версии — не доказательство.
        mac_14 = dict(_report("macos-x86_64"), macos_version="14.5")
        with self.assertRaises(release_gate.ReleaseGateError):
            release_gate.validate_reports(
                [_report(t) for t in targets if t != "macos-x86_64"] + [mac_14], targets
            )

        # Клиентский минимум Windows не подтверждается ничем из заявленного.
        ok, reason = release_gate.minimum_os_evidence(
            "Windows 11", None, _host_facts("windows-x86_64")
        )
        self.assertFalse(ok)
        self.assertIn("клиентск", reason)

        # Точное совпадение baseline проходит.
        release_gate.validate_reports(_four_reports(), targets)

    def test_state_outside_bundle_required_strict(self):
        """`state_outside_bundle` обязателен: строго bool и строго True."""
        targets = _all_targets()

        missing = _four_reports()
        del missing[0]["state_outside_bundle"]
        with self.assertRaises(release_gate.ReleaseGateError):
            release_gate.validate_reports(missing, targets)

        negative = _four_reports()
        negative[0]["state_outside_bundle"] = False
        with self.assertRaises(release_gate.ReleaseGateError):
            release_gate.validate_reports(negative, targets)

        truthy = _four_reports()
        truthy[0]["state_outside_bundle"] = "yes"
        with self.assertRaises(release_gate.ReleaseGateError):
            release_gate.validate_reports(truthy, targets)

    def test_four_reports_required(self):
        targets = _all_targets()
        linux_only = [_report("linux-x86_64")]
        with self.assertRaises(release_gate.ReleaseGateError) as missing:
            release_gate.validate_reports(linux_only, targets)
        for absent in ("windows-x86_64", "macos-x86_64", "macos-arm64"):
            self.assertIn(absent, str(missing.exception))

        # Отчёт постороннего target не принимается.
        alien = _report("linux-x86_64")
        alien["target"] = "plan9-arm"
        with self.assertRaises(release_gate.ReleaseGateError):
            release_gate.validate_reports(_four_reports() + [alien], targets)


def _four_targets_reports(**override: object) -> list[dict]:
    return [
        _report(target, **{key: value for key, value in override.items()})
        for target in _all_targets()
    ]


# ---------------------------------------------------------------------------
# Гейт контрольных сумм


class ChecksumGateTests(unittest.TestCase):
    """`verify_artifacts` на синтетических архивах — не реальные комплекты."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.artifacts = self.base / "artifacts"
        self.reports = self.base / "reports"
        self.artifacts.mkdir()
        self.reports.mkdir()
        # Гейт печатает итог; в выводе unittest ему не место.
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()
        self.addCleanup(self._quiet.__exit__, None, None, None)

    def _prepare(self) -> dict[str, str]:
        digests = {}
        for target in _all_targets():
            path = _write_gate_archive(self.artifacts, target, "0.0.0-synthetic")
            digests[target] = smoke_portable.sha256_file(path)
        _write_reports(self.reports, _four_reports(), digests=digests)
        return digests

    def test_checksum_gate(self):
        digests = self._prepare()
        verified = release_gate.verify_artifacts(self.artifacts, self.reports)
        expected = sorted(
            (f"keklib-cli-0.0.0-synthetic-{target}.{ext}", digests[target])
            for target, ext in (
                (name, str(spec["archive_format"])) for name, spec in _specs().items()
            )
        )
        self.assertEqual(sorted(verified), expected)
        sums = self.artifacts / "SHA256SUMS"
        self.assertTrue(sums.is_file())
        lines = sums.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 4)
        for line in lines:
            digest, name = line.split("  ", 1)
            self.assertIn(name, os.listdir(self.artifacts))
            actual = smoke_portable.sha256_file(self.artifacts / name)
            self.assertEqual(digest, actual)

        # Подделка байтов архива ломает сторону суммы.
        victim = next(self.artifacts.glob("keklib-cli-*-macos-arm64.tar.gz"))
        victim.write_bytes(victim.read_bytes() + b"tamper")
        with self.assertRaises(release_gate.ReleaseGateError):
            release_gate.verify_artifacts(self.artifacts, self.reports)

    def test_checksum_gate_rejects_missing_or_mismatched(self):
        digests = self._prepare()
        # Нет архива.
        next(self.artifacts.glob("keklib-cli-*-windows-x86_64.zip")).unlink()
        with self.assertRaises(release_gate.ReleaseGateError):
            release_gate.verify_artifacts(self.artifacts, self.reports)

        self._prepare()
        # Нет sidecar-сумы.
        next(self.artifacts.glob("*.sha256")).unlink()
        with self.assertRaises(release_gate.ReleaseGateError):
            release_gate.verify_artifacts(self.artifacts, self.reports)

        self._prepare()
        # Сумма в отчёте не совпадает с фактической.
        lying = self.reports / "smoke-report-linux-x86_64.json"
        report = json.loads(lying.read_text(encoding="utf-8"))
        report["archive_sha256"] = "b" * 64
        lying.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(release_gate.ReleaseGateError):
            release_gate.verify_artifacts(self.artifacts, self.reports)

        self._prepare()
        # Существующий SHA256SUMS не перезаписывается.
        (self.artifacts / "SHA256SUMS").write_text("foreign\n", encoding="utf-8")
        with self.assertRaises(release_gate.ReleaseGateError):
            release_gate.verify_artifacts(self.artifacts, self.reports)

    def test_report_version_and_target_bound_to_archive(self):
        """Версия/target отчёта обязаны соответствовать своему архиву.

        Правильного digest недостаточно: SHA связывает байты, но gate
        отвечает и за согласованность версии отчёта с проверяемым
        комплектом (review fix round 1, P2).
        """
        self._prepare()
        lying = self.reports / "smoke-report-linux-x86_64.json"
        report = json.loads(lying.read_text(encoding="utf-8"))
        report["version"] = "9.9.9-not-the-artifact"
        lying.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(release_gate.ReleaseGateError) as failure:
            release_gate.verify_artifacts(self.artifacts, self.reports)
        self.assertIn("верси", str(failure.exception))

        self._prepare()
        lying = self.reports / "smoke-report-linux-x86_64.json"
        report = json.loads(lying.read_text(encoding="utf-8"))
        report["target"] = "windows-x86_64"  # digest при этом корректен для linux
        lying.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(release_gate.ReleaseGateError) as failure:
            release_gate.verify_artifacts(self.artifacts, self.reports)
        self.assertIn("target", str(failure.exception))


# ---------------------------------------------------------------------------
# Безопасная распаковка архива


def _tar_bytes(members: list[tuple[str, bytes]], *, types: dict[str, str] | None = None,
               links: dict[str, str] | None = None) -> bytes:
    """Собрать tar.gz с произвольными (в том числе запрещёнными) записями."""
    types = types or {}
    links = links or {}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in list(members) + [(link, b"") for link in links]:
            info = tarfile.TarInfo(name)
            if name in links:
                info.type = tarfile.SYMTYPE
                info.linkname = links[name]
                info.size = 0
                archive.addfile(info)
                continue
            info.type = types.get(name, tarfile.REGTYPE)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class UnpackTests(unittest.TestCase):
    #: Валидный build-info для распаковщика (формат тот же, что в комплекте).
    BUILD_INFO = (
        b'{"version": "0.0.0-test", "target": "linux-x86_64", '
        b'"minimum_os": "synthetic"}'
    )

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def _unpack(self, archive: Path) -> Path:
        destination = self.base / "bundle-out"
        bundle, _info = smoke_portable.unpack_bundle(archive, destination)
        return bundle

    def test_unpack_bundle_ok(self):
        archive = self.base / "good.tar.gz"
        archive.write_bytes(
            _tar_bytes(
                [
                    ("keklib-cli", b""),
                    ("keklib-cli/_internal", b""),
                    ("keklib-cli/keklib-cli", b"#!/bin/true"),
                    ("keklib-cli/build-info.json", self.BUILD_INFO),
                    ("keklib-cli/_internal/data.bin", b"\x00\x01"),
                    ("keklib-cli/_internal/empty", b""),
                ],
                types={
                    "keklib-cli": tarfile.DIRTYPE,
                    "keklib-cli/_internal": tarfile.DIRTYPE,
                },
                links={"keklib-cli/_internal/keklib-cli": "../keklib-cli"},
            )
        )
        bundle = self._unpack(archive)
        self.assertEqual((bundle / "keklib-cli").read_bytes(), b"#!/bin/true")
        self.assertEqual((bundle / "_internal/data.bin").read_bytes(), b"\x00\x01")
        self.assertEqual((bundle / "_internal/empty").read_bytes(), b"")
        self.assertTrue(
            (bundle / "_internal/keklib-cli").is_symlink(), "внутренняя ссылка допустима"
        )
        self.assertEqual(
            os.readlink(bundle / "_internal/keklib-cli"), "../keklib-cli"
        )
        if os.name == "posix":
            # Unix-бит исполнения — реальная гарантия ОС: unpacker ставит
            # 0755 исполняемому. На Windows chmod не отражает execute-биты
            # в st_mode (только read-only атрибут); фактическую
            # исполняемость там доказывает запуск executable в
            # FrozenSmokeTests, поэтому здесь проверяется регулярный файл.
            mode = (bundle / "keklib-cli").stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR, "executable должен быть исполняемым")
        else:
            self.assertTrue(
                (bundle / "keklib-cli").is_file(),
                "executable должен быть регулярным файлом",
            )

    def test_unpack_zip_ok(self):
        archive = self.base / "good.zip"
        build_info = (
            b'{"version": "0.0.0-test", "target": "windows-x86_64", '
            b'"minimum_os": "synthetic"}'
        )
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("keklib-cli/build-info.json", build_info)
            zipped.writestr("keklib-cli/keklib-cli.exe", b"MZ synthetic")
        bundle = self._unpack(archive)
        self.assertEqual((bundle / "keklib-cli.exe").read_bytes(), b"MZ synthetic")

    def test_unpack_rejects_unsafe_entries(self):
        bad = [
            ("traversal", _tar_bytes(
                [("keklib-cli/build-info.json", self.BUILD_INFO),
                 ("keklib-cli/../evil", b"x")]), None, None, "компонент"),
            ("absolute", _tar_bytes(
                [("keklib-cli/build-info.json", self.BUILD_INFO),
                 ("/etc/passwd", b"x")]), None, None, "недопустимое имя"),
            ("outside-root", _tar_bytes(
                [("keklib-cli/build-info.json", self.BUILD_INFO),
                 ("other/file", b"x")]), None, None, "корня"),
            ("duplicate", _tar_bytes(
                [("keklib-cli/build-info.json", self.BUILD_INFO),
                 ("keklib-cli/a", b"1"), ("keklib-cli/a", b"2")]),
             None, None, "повтор"),
            ("fifo", _tar_bytes(
                [("keklib-cli/build-info.json", self.BUILD_INFO),
                 ("keklib-cli/pipe", b"")],
                types={"keklib-cli/pipe": tarfile.FIFOTYPE}), None, None, "спецфайл"),
            ("hardlink", _tar_bytes(
                [("keklib-cli/build-info.json", self.BUILD_INFO),
                 ("keklib-cli/hl", b"")],
                types={"keklib-cli/hl": tarfile.LNKTYPE}), None, None, "жёстк"),
            ("external-link", _tar_bytes(
                [("keklib-cli/build-info.json", self.BUILD_INFO)],
                links={"keklib-cli/link": "../../../outside"}), None, None, "покидает"),
            ("absolute-link", _tar_bytes(
                [("keklib-cli/build-info.json", self.BUILD_INFO)],
                links={"keklib-cli/link": "/etc/passwd"}), None, None, "покидает"),
            ("backslash", _tar_bytes(
                [("keklib-cli/build-info.json", self.BUILD_INFO),
                 ("keklib-cli/a\\b", b"x")]), None, None, "недопустимое имя"),
        ]
        for name, payload, types, links, expected in bad:
            archive = self.base / f"bad-{name}.tar.gz"
            archive.write_bytes(payload)
            with self.assertRaises(smoke_portable.SmokeError, msg=name) as failure:
                self._unpack(archive)
            self.assertIn(expected, str(failure.exception), msg=name)

    def test_workdir_must_be_new(self):
        existing = self.base / "workdir"
        existing.mkdir()
        with self.assertRaises(smoke_portable.SmokeError):
            smoke_portable.prepare_workdir(existing)
        foreign_file = self.base / "occupied"
        foreign_file.write_text("foreign", encoding="utf-8")
        with self.assertRaises(smoke_portable.SmokeError):
            smoke_portable.prepare_workdir(foreign_file)


# ---------------------------------------------------------------------------
# Изоляция child-окружения


class IsolationTests(unittest.TestCase):
    def test_isolation(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sandbox = Path(tmp.name) / "sandbox"
        env = smoke_portable.child_environment(sandbox)

        for key in list(env):
            self.assertFalse(key.startswith("PYTHON"), f"PYTHON-переменная: {key}")
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PYTHONHOME", env)

        # PATH действительно без python/python3/py — наблюдение, а не флаг.
        path_entries = [item for item in env["PATH"].split(os.pathsep) if item]
        self.assertTrue(path_entries)
        for entry in path_entries:
            self.assertFalse(
                smoke_portable._python_in_dir(Path(entry)),
                f"python найден в PATH-каталоге child: {entry}",
            )
        self.assertIsNone(shutil.which("python", path=env["PATH"]))
        self.assertIsNone(shutil.which("python3", path=env["PATH"]))
        if sys.platform == "win32":
            self.assertIsNone(shutil.which("py", path=env["PATH"]))

        # Состояние и рабочие пути child — внутри собственного sandbox.
        sandbox_root = sandbox.resolve()
        for key in ("HOME", "USERPROFILE", "TMPDIR", "TMP", "TEMP",
                    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "APPDATA", "LOCALAPPDATA"):
            value = env.get(key)
            if value:
                self.assertTrue(
                    Path(value).resolve().is_relative_to(sandbox_root),
                    f"{key}={value} вне sandbox",
                )

        # PATH child не зависит от checkout исходников.
        for entry in path_entries:
            self.assertFalse(
                Path(entry).resolve().is_relative_to(REPO_ROOT),
                f"PATH child ссылается на checkout: {entry}",
            )


# ---------------------------------------------------------------------------
# read-only bundle


class PermissionsTests(unittest.TestCase):
    def test_readonly_bundle(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "program"
        inner = root / "_internal"
        inner.mkdir(parents=True)
        (root / "keklib-cli").write_text("exe", encoding="utf-8")
        (inner / "data.bin").write_bytes(b"123")
        (inner / "self-link").symlink_to("data.bin")
        before = {
            path: stat.S_IMODE(path.lstat().st_mode)
            for path in (root, inner, root / "keklib-cli", inner / "data.bin")
        }

        with readonly_bundle(root):
            with self.assertRaises(PermissionError):
                (root / "probe").write_text("no", encoding="utf-8")
            with self.assertRaises(PermissionError):
                (inner / "probe").write_text("no", encoding="utf-8")
            # Изоляция не мешает читать и исполнять.
            self.assertEqual((root / "keklib-cli").read_text(encoding="utf-8"), "exe")
            self.assertEqual((inner / "self-link").read_bytes(), b"123")

        after = {
            path: stat.S_IMODE(path.lstat().st_mode)
            for path in (root, inner, root / "keklib-cli", inner / "data.bin")
        }
        self.assertEqual(before, after, "права восстановлены исходными")
        (root / "probe-after").write_text("yes", encoding="utf-8")  # снова writable

    def test_readonly_bundle_fails_closed_under_root(self):
        if sys.platform == "win32" or not hasattr(os, "geteuid"):
            self.skipTest("проба root-обхода — только POSIX")
        if os.geteuid() != 0:
            self.skipTest("прогон не от root: обход chmod не наблюдается")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "program"
        root.mkdir()
        with self.assertRaises(PermissionIsolationError):
            with readonly_bundle(root):
                pass


# ---------------------------------------------------------------------------
# Реальный frozen smoke на построенном архиве


def _smoke_archive() -> Path | None:
    """Найти реальный архив для локального frozen smoke (host linux-x86_64)."""
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        return None
    candidate = os.environ.get("KEKLIB_SMOKE_ARCHIVE")
    if candidate:
        path = Path(candidate)
        return path if path.is_file() else None
    return DEFAULT_SMOKE_ARCHIVE if DEFAULT_SMOKE_ARCHIVE.is_file() else None


class FrozenSmokeTests(unittest.TestCase):
    """Полный изолированный smoke реального frozen executable через TTY.

    Smoke запускается subprocess'ом `python -S tools/smoke_portable.py`:
    `-S` исключает site-packages окружения тестов, поэтому внутри smoke
    пакет `keklib_cli` не импортируем — условие «без checkout» наблюдается.
    """

    @classmethod
    def setUpClass(cls):
        archive = _smoke_archive()
        if archive is None:
            raise unittest.SkipTest(
                "нет построенного portable-архива для этой ОС/CPU "
                "(KEKLIB_SMOKE_ARCHIVE не задан, файл по умолчанию отсутствует): "
                "frozen smoke не выполняется и успешным не считается"
            )
        tmp = tempfile.TemporaryDirectory(prefix="keklib-frozen-")
        cls.addClassCleanup(tmp.cleanup)
        cls.base = Path(tmp.name)
        cls.workdir = cls.base / "smoke-workdir"
        cls.result = subprocess.run(
            [
                sys.executable,
                "-S",
                str(REPO_ROOT / "tools" / "smoke_portable.py"),
                "--archive",
                str(archive),
                "--workdir",
                str(cls.workdir),
            ],
            capture_output=True,
            text=True,
            timeout=1200,
            cwd=str(cls.base),
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("PYTHON")
            }
            | {"TMPDIR": str(cls.base)},
            check=False,
        )
        report_path = cls.workdir / "smoke-report.json"
        cls.report = (
            json.loads(report_path.read_text(encoding="utf-8"))
            if report_path.is_file()
            else {}
        )

    def test_frozen_tty_flow(self):
        self.assertEqual(
            self.result.returncode, 0, self.result.stdout + self.result.stderr
        )
        self.assertTrue(self.report.get("passed"), json.dumps(self.report, indent=2))
        for key in (
            "version",
            "target",
            "host_os",
            "host_os_version",
            "host_arch",
            "minimum_os",
            "minimum_os_verified",
            "archive_sha256",
            "passed",
            "fts5",
            "source_unchanged",
            "no_system_python",
            "source_checkout_absent",
            "readonly_bundle",
        ):
            self.assertIn(key, self.report)
        for flag in (
            "fts5",
            "source_unchanged",
            "no_system_python",
            "source_checkout_absent",
            "readonly_bundle",
        ):
            self.assertIs(self.report[flag], True, flag)

        steps = {step["step"]: step for step in self.report["flow"]}
        for name in ("help", "setup", "search", "search_after_update"):
            self.assertEqual(steps[name]["returncode"], 0, name)
        self.assertTrue(steps["help"]["usage_shown"])
        self.assertTrue(steps["setup"]["configured"])
        self.assertTrue(steps["search"]["title_shown"])
        self.assertTrue(steps["search"]["selected"])
        self.assertTrue(steps["search_after_update"]["title_shown"])
        self.assertTrue(steps["search_after_update"]["selected"])

        extractions = self.report["extractions"]
        self.assertEqual(extractions["Мастер и Маргарита"], hashlib.sha256(KNOWN_PAYLOAD).hexdigest())
        self.assertEqual(extractions["Книга Новая"], hashlib.sha256(NEW_PAYLOAD).hexdigest())
        self.assertTrue(self.report["source_unchanged"])

    def test_state_outside_bundle(self):
        self.assertTrue(self.report.get("passed"))
        self.assertIs(self.report.get("state_outside_bundle"), True)
        config = Path(str(self.report["state_config"]))
        index = Path(str(self.report["state_index"]))
        self.assertTrue(config.is_file(), str(config))
        self.assertTrue(index.is_file(), str(index))
        bundle = Path(str(self.report["bundle_dir"]))
        self.assertFalse(
            config.is_relative_to(bundle) or index.is_relative_to(bundle),
            "состояние не должно жить внутри комплекта",
        )


# ---------------------------------------------------------------------------
# Символические цепочки распаковщика (review fix round 1, P1)


class UnpackChainTests(unittest.TestCase):
    """Цепочки symlink не могут вывести запись за комплект; отказ до записей."""

    BUILD_INFO = (
        b'{"version": "0.0.0-test", "target": "linux-x86_64", '
        b'"minimum_os": "synthetic"}'
    )

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.destination = self.base / "dest"

    def _chain_archive(self, deep: bool) -> Path:
        escaped = (
            "keklib-cli/b/deep/c/escaped.txt" if deep else "keklib-cli/b/escaped.txt"
        )
        archive = self.base / ("chain-deep.tar.gz" if deep else "chain.tar.gz")
        archive.write_bytes(
            _tar_bytes(
                [
                    ("keklib-cli", b""),
                    ("keklib-cli/build-info.json", self.BUILD_INFO),
                    ("keklib-cli/keklib-cli", b"exe"),
                    (escaped, b"outside-write"),
                ],
                types={"keklib-cli": tarfile.DIRTYPE},
                links={"keklib-cli/a": ".", "keklib-cli/b": "a/.."},
            )
        )
        return archive

    def test_symlink_chain_escape_rejected_before_writes(self):
        for deep in (False, True):
            with self.subTest(deep=deep):
                archive = self._chain_archive(deep)
                with self.assertRaises(smoke_portable.SmokeError) as failure:
                    smoke_portable.unpack_bundle(archive, self.destination)
                self.assertIn(
                    "символическ", str(failure.exception), msg=str(failure.exception)
                )
                # Отказ до записей: назначения нет вовсе, файлов снаружи нет.
                self.assertFalse(self.destination.exists())
                self.assertEqual(list(self.base.rglob("escaped*")), [])

    def test_escaping_link_itself_rejected(self):
        archive = self.base / "escape.tar.gz"
        archive.write_bytes(
            _tar_bytes(
                [
                    ("keklib-cli", b""),
                    ("keklib-cli/build-info.json", self.BUILD_INFO),
                    ("keklib-cli/keklib-cli", b"exe"),
                ],
                types={"keklib-cli": tarfile.DIRTYPE},
                links={"keklib-cli/a": ".", "keklib-cli/b": "a/.."},
            )
        )
        with self.assertRaises(smoke_portable.SmokeError) as failure:
            smoke_portable.unpack_bundle(archive, self.destination)
        self.assertIn("покидает комплект", str(failure.exception))
        self.assertFalse(self.destination.exists())

    def test_link_cycle_rejected(self):
        archive = self.base / "cycle.tar.gz"
        archive.write_bytes(
            _tar_bytes(
                [
                    ("keklib-cli", b""),
                    ("keklib-cli/build-info.json", self.BUILD_INFO),
                    ("keklib-cli/keklib-cli", b"exe"),
                ],
                types={"keklib-cli": tarfile.DIRTYPE},
                links={"keklib-cli/x": "y", "keklib-cli/y": "x"},
            )
        )
        with self.assertRaises(smoke_portable.SmokeError) as failure:
            smoke_portable.unpack_bundle(archive, self.destination)
        self.assertIn("цикл", str(failure.exception))
        self.assertFalse(self.destination.exists())

    def test_ancestor_type_conflict_rejected(self):
        archive = self.base / "conflict.tar.gz"
        archive.write_bytes(
            _tar_bytes(
                [
                    ("keklib-cli", b""),
                    ("keklib-cli/build-info.json", self.BUILD_INFO),
                    ("keklib-cli/keklib-cli", b"exe"),
                    ("keklib-cli/file", b"regular"),
                    ("keklib-cli/file/inner.txt", b"inside-file"),
                ],
                types={"keklib-cli": tarfile.DIRTYPE},
            )
        )
        with self.assertRaises(smoke_portable.SmokeError):
            smoke_portable.unpack_bundle(archive, self.destination)
        self.assertFalse(self.destination.exists())

    def test_nested_empty_dir_tar_without_parent_entries(self):
        """Корректный tar с пустым вложенным каталогом без записи родителя.

        Регрессия fix round 2 (re-review R1): замыкание предков считалось
        только по файлам/ссылкам, и mkdir без parents падал на валидном
        безопасном архиве.
        """
        archive = self.base / "empty-nested.tar.gz"
        archive.write_bytes(
            _tar_bytes(
                [
                    ("keklib-cli", b""),
                    ("keklib-cli/build-info.json", self.BUILD_INFO),
                    ("keklib-cli/keklib-cli", b"exe"),
                    ("keklib-cli/empty-parent/empty-child", b""),
                ],
                types={
                    "keklib-cli": tarfile.DIRTYPE,
                    "keklib-cli/empty-parent/empty-child": tarfile.DIRTYPE,
                },
            )
        )
        bundle = smoke_portable.unpack_bundle(archive, self.destination)[0]
        self.assertTrue((bundle / "empty-parent").is_dir())
        self.assertTrue((bundle / "empty-parent" / "empty-child").is_dir())

    def test_nested_empty_dir_zip_without_parent_entries(self):
        """То же для zip: запись каталога с «/» без записи родителя."""
        archive = self.base / "empty-nested.zip"
        build_info = (
            b'{"version": "0.0.0-test", "target": "windows-x86_64", '
            b'"minimum_os": "synthetic"}'
        )
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("keklib-cli/build-info.json", build_info)
            zipped.writestr("keklib-cli/keklib-cli.exe", b"MZ synthetic")
            zipped.writestr("keklib-cli/empty-parent/empty-child/", "")
        bundle = smoke_portable.unpack_bundle(archive, self.destination)[0]
        self.assertTrue((bundle / "empty-parent").is_dir())
        self.assertTrue((bundle / "empty-parent" / "empty-child").is_dir())


# ---------------------------------------------------------------------------
# Структурные регрессии workflows (stdlib, без YAML-парсера)


class WorkflowStructureTests(unittest.TestCase):
    CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    PORTABLE = REPO_ROOT / ".github" / "workflows" / "portable.yml"

    def test_ci_installs_project_and_entry_point_before_tests(self):
        text = self.CI.read_text(encoding="utf-8")
        install = text.index("python -m pip install .")
        entry = text.index("keklib-cli --help")
        tests = text.index("python -m unittest")
        self.assertLess(install, tests, "установка проекта обязательна до тестов")
        self.assertLess(entry, tests, "проверка entry point обязательна до тестов")

    def test_ci_installs_test_tooling_lock_before_tests(self):
        """Source CI обязан ставить test-tooling зависимости до unittest.

        Тесты сборочного tooling (tests/test_packaging.py →
        tools/build_portable.py:lock_pins) импортируют
        ``packaging.requirements``; установка одного runtime-проекта этот
        пакет в окружение job'а не ставит, и оба toolchain-теста падают
        ModuleNotFoundError/PackagingError (первый CI, все 6 jobs).
        Зависимость воспроизводима: отдельный lock-файл с теми же пинами и
        хешами, что и requirements-build.lock.
        """
        text = self.CI.read_text(encoding="utf-8")
        tooling = text.index("packaging/requirements-test-tooling.lock")
        tests = text.index("python -m unittest")
        self.assertLess(
            tooling, tests, "test-tooling lock ставится до полного набора тестов"
        )

    def test_portable_build_job_installs_project_before_build(self):
        """Build-job обязан устанавливать src-layout проект до сборки (F1).

        ``tools/build_portable.py`` безусловно импортирует
        ``keklib_cli.platforms``; checkout держит пакет под ``src/``, а
        ``script``-пути (``tools/``) его не видят. Установка в
        ``source-tests`` выполняется на другой машине и не переносится —
        без собственной установки build-job падает на ModuleNotFoundError
        до preflight/PyInstaller.
        """
        text = self.PORTABLE.read_text(encoding="utf-8")
        build_job = self._job_section(text, "build:")
        self.assertIsNotNone(build_job, "build-job отсутствует в portable.yml")
        assert build_job is not None
        install = build_job.find("python -m pip install .")
        build = build_job.find("python tools/build_portable.py")
        deps = build_job.find("python -m pip install -r packaging/requirements-build.lock")
        self.assertGreaterEqual(install, 0, "нет установки проекта в build-job")
        self.assertGreaterEqual(build, 0, "нет вызова tools/build_portable.py")
        self.assertGreaterEqual(
            deps, 0, "нет установки сборочных зависимостей в build-job"
        )
        self.assertLess(
            deps, install, "lock-зависимости ставятся до установки проекта"
        )
        self.assertLess(
            install, build, "установка проекта обязательна до сборки portable"
        )

    @staticmethod
    def _job_section(text: str, job: str) -> str | None:
        """Текст одного job: от его ключа (2 пробела) до следующего job."""
        start = text.find(f"  {job}\n")
        if start < 0:
            return None
        rest = text[start + len(job) + 2 :]
        following = re.search(r"\n  [A-Za-z-]+:\n", rest)
        return rest[: following.start()] if following else rest

    def test_portable_smoke_reports_unique_per_target(self):
        text = self.PORTABLE.read_text(encoding="utf-8")
        rename = text.index("smoke-report-${{ matrix.name }}.json")
        upload = text.index("name: smoke-report-${{ matrix.name }}")
        gate = text.index("--reports reports")
        self.assertLess(rename, upload, "отчёт переименовывается до upload")
        self.assertLess(upload, gate, "upload отчёта раньше гейта")
        for target in _all_targets():
            self.assertIn(f"name: smoke-report-{target}", text)
            self.assertIn(f"name: keklib-cli-{target}", text)

    def test_actions_pinned_by_commit_sha(self):
        for path in (self.CI, self.PORTABLE):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if "uses:" not in stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("uses: ./"):
                    continue  # локальный reusable workflow, не action
                self.assertRegex(
                    stripped,
                    r"uses: [^@]+@[0-9a-f]{40}( # .*)?$",
                    msg=f"{path.name}: {stripped}",
                )


# ---------------------------------------------------------------------------
# Windows-ветка permissions: argv и cwd icacls (синтетика, не native NTFS)


class PermissionsWindowsArgvTests(unittest.TestCase):
    def test_icacls_save_restore_explicit_cwd(self):
        from unittest import mock

        from tests.harness import permissions

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Корень вне cwd процесса — прежний код терял адресацию.
        root = Path(tmp.name) / "outside-cwd" / "bundle" / "keklib-cli"
        root.mkdir(parents=True)
        recorded: list[tuple[list[str], object]] = []

        def fake_icacls(argv, *, cwd=None):
            recorded.append((list(argv), cwd))

        with (
            mock.patch.object(permissions, "_icacls", fake_icacls),
            mock.patch.object(permissions, "_probe_denied", lambda *a, **k: None),
            mock.patch.object(permissions, "_probe_allowed", lambda *a, **k: None),
        ):
            readonly = contextlib.contextmanager(permissions._readonly_windows)
            with readonly(root):
                pass

        self.assertEqual(len(recorded), 3, recorded)
        save_argv, save_cwd = recorded[0]
        self.assertEqual(save_argv[0], root.name)
        self.assertEqual(save_cwd, root.parent)
        deny_argv, _deny_cwd = recorded[1]
        self.assertEqual(deny_argv[0], str(root))
        restore_argv, restore_cwd = recorded[2]
        self.assertEqual(restore_argv[0], ".")
        self.assertEqual(restore_cwd, root.parent)


# ---------------------------------------------------------------------------
# Нормализация архитектуры хоста (AMD64/aarch64)


class HostFactsTests(unittest.TestCase):
    def test_machine_normalized_and_raw_preserved(self):
        for raw, normalized in (
            ("AMD64", "x86_64"),
            ("x86_64", "x86_64"),
            ("aarch64", "arm64"),
            ("arm64", "arm64"),
            ("ARM64", "arm64"),
        ):
            with self.subTest(raw=raw):
                facts = smoke_portable._normalize_arch_facts({"host_arch": raw})
                self.assertEqual(facts["host_arch"], normalized)
                self.assertEqual(facts["host_arch_raw"], raw)


# ---------------------------------------------------------------------------
# Двухэтапное наблюдение неизменности источников (review fix round 1, P2)


class SourceStagesTests(unittest.TestCase):
    def _library(self, base: Path) -> Path:
        library = base / "library"
        smoke_portable._write_library(library, with_new_book=False)
        return library

    @staticmethod
    def _fake_run(library: Path, out1: Path, out2: Path, *, mutate_first_search: bool):
        calls: list[str] = []

        def run(step: str, argv: list[str], commands: list[str]) -> tuple[int, str]:
            calls.append(step)
            if step == "help":
                return 0, "usage: keklib-cli"
            if step == "setup":
                return 0, "библиотека настроена"
            if step == "search":
                if mutate_first_search:
                    # «Порча» источчика только при первом поиске: старый
                    # односнимковый smoke этого не замечал.
                    (library / "librusec_flib.inpx").write_bytes(b"tampered-by-child")
                out1.mkdir(exist_ok=True)
                (out1 / "Мастер и Маргарита [66372].fb2").write_bytes(KNOWN_PAYLOAD)
                return 0, "Мастер и Маргарита\nвведите номер книги, n, p или q: "
            if step == "search_after_update":
                out2.mkdir(exist_ok=True)
                (out2 / "Книга Новая [777].fb2").write_bytes(NEW_PAYLOAD)
                return 0, "Книга Новая\nвведите номер книги, n, p или q: "
            raise AssertionError(f"неожидаемый шаг {step}")

        return run

    def test_fake_child_first_search_mutation_detected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        library = self._library(base)
        out1, out2 = base / "out1", base / "out2"
        run = self._fake_run(library, out1, out2, mutate_first_search=True)
        flow, extractions, stage1, stage2 = smoke_portable._flow_steps(
            library, out1, out2, run=run
        )
        self.assertFalse(stage1, "порча источника первым поиском обязана быть замечена")
        self.assertTrue(stage2)
        self.assertFalse(stage1 and stage2)
        self.assertEqual(
            extractions.get("Мастер и Маргарита"),
            hashlib.sha256(KNOWN_PAYLOAD).hexdigest(),
        )

    def test_clean_flow_both_stages_unchanged(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        library = self._library(base)
        out1, out2 = base / "out1", base / "out2"
        run = self._fake_run(library, out1, out2, mutate_first_search=False)
        flow, extractions, stage1, stage2 = smoke_portable._flow_steps(
            library, out1, out2, run=run
        )
        self.assertTrue(stage1)
        self.assertTrue(stage2)
        self.assertEqual(len(flow), 4)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
