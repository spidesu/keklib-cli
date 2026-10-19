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
# Консольный JSON отчёта smoke на узких кодировках терминала


class ConsoleJsonTests(unittest.TestCase):
    """Печать отчёта в stdout не падает на ascii/cp1252 (нативный Windows CI).

    Нативный прогон Windows упал на ``print(json.dumps(report,
    ensure_ascii=False, ...))`` с UnicodeEncodeError в cp1252: кириллица
    путей/диагностики непредставима в этой кодировке консоли. Требования:
    консольный JSON — чистый ASCII (``ensure_ascii=True``; escape
    ``\\uXXXX`` сохраняет все поля и Unicode-семантику значений, и
    ``json.loads`` вывода восстанавливает точный исходный отчёт),
    сохранённый ``smoke-report.json`` остаётся UTF-8 c
    ``ensure_ascii=False``, коды выхода не меняются. Запреты: не
    подменять кодировку окружением и не ловить UnicodeEncodeError без
    отчёта.
    """

    REPORT: dict[str, object] = {
        "schema_version": 1,
        "tool": "smoke_portable",
        "archive": "C:\\Игрок\\библиотека\\keklib-cli-0.1.0-windows-x86_64.zip",
        "bundle_dir": "C:\\smoke\\bundle\\keklib-cli",
        "flow": [{"step": "поиск", "title": "Мастер и Маргарита", "rc": 0}],
        "extractions_expected": {"Мастер и Маргарита": "b" * 64},
        "failures": ["SmokeError: шаг поиск завершился кодом 1"],
        "passed": False,
    }

    def test_console_payload_roundtrips_narrow_encodings(self):
        """ensure_ascii=True: ascii/cp1252 печать и точный json.loads-roundtrip."""
        for encoding in ("ascii", "cp1252"):
            with self.subTest(encoding=encoding):
                raw = io.BytesIO()
                narrow = io.TextIOWrapper(raw, encoding=encoding)
                with contextlib.redirect_stdout(narrow):
                    print(smoke_portable.console_payload(self.REPORT))
                narrow.flush()
                payload = raw.getvalue().decode(encoding)
                self.assertTrue(
                    payload.isascii(),
                    "консольный JSON обязан быть чистым ASCII для узких "
                    "кодировок терминала",
                )
                self.assertEqual(json.loads(payload), self.REPORT)

    def test_failed_report_prints_and_exit_code_stable(self):
        """Полный main() на cp1252-stdout: отчёт печатается, код остаётся 1.

        Отсутствующий архив с кириллицей в пути — самый ранний отказ
        smoke: отчёт не пишется на диск (workdir не создаётся), все
        не-ASCII данные выходят только через stdout. До исправления
        print(ensure_ascii=False) поднимал UnicodeEncodeError из main()
        вместо честного кода 1.
        """
        with tempfile.TemporaryDirectory(prefix="keklib-console-json-") as tmp:
            archive = (
                Path(tmp) / "библиотека" / "keklib-cli-0.1.0-windows-x86_64.zip"
            )
            workdir = Path(tmp) / "smoke-workdir"
            raw = io.BytesIO()
            narrow = io.TextIOWrapper(raw, encoding="cp1252")
            with contextlib.redirect_stdout(narrow):
                code = smoke_portable.main(
                    ["--archive", str(archive), "--workdir", str(workdir)]
                )
            narrow.flush()
            self.assertEqual(code, 1, "код выхода упавшего smoke не меняется")
            self.assertFalse(workdir.exists())
            report = json.loads(raw.getvalue().decode("cp1252"))
            self.assertIs(report["passed"], False)
            self.assertEqual(report["archive"], str(archive.resolve()))
            self.assertTrue(
                any("архив не найден" in str(item) for item in report["failures"])
            )

    def test_file_report_stays_utf8_ensure_ascii_false(self):
        """Инвариант: smoke-report.json остаётся UTF-8 без ASCII-escape."""
        with tempfile.TemporaryDirectory(prefix="keklib-console-json-") as tmp:
            workdir = Path(tmp)
            smoke_portable.write_report(workdir, self.REPORT)
            data = (workdir / "smoke-report.json").read_bytes()
            self.assertIn("Мастер и Маргарита".encode("utf-8"), data)
            self.assertIn("библиотека".encode("utf-8"), data)
            self.assertNotIn(b"\\u", data)
            self.assertEqual(
                json.loads(data.decode("utf-8")), self.REPORT
            )


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

    def test_portable_smoke_failure_retains_report_without_masking(self):
        """Отчёт упавшего smoke сохраняется артефактом, отказ не глотается.

        Нативный Windows smoke упал на печати отчёта в cp1252, и отчёт с
        диагностикой не попал в артефакты (cp выполнялся только после
        успеха smoke). Узкое усиление: копия в target-имя и upload
        выполняются всегда (``if: always()``) с проверкой существования
        файла; continue-on-error в smoke-job запрещён — упавший smoke
        оставляет job failed, гейт (needs: smoke) не запускается, failed
        JSON не становится verified-asset.
        """
        text = self.PORTABLE.read_text(encoding="utf-8")
        job = self._job_section(text, "smoke:")
        self.assertIsNotNone(job, "smoke-job отсутствует в portable.yml")
        assert job is not None
        self.assertNotIn(
            "continue-on-error", job, "отказ smoke не проглатывается"
        )
        run_step = job.index("tools/smoke_portable.py")
        always_positions = [m.start() for m in re.finditer(r"if: always\(\)", job)]
        after_run = [pos for pos in always_positions if pos > run_step]
        self.assertEqual(
            len(after_run),
            2,
            "копия и upload отчёта обязаны выполняться даже при отказе smoke",
        )
        retention, upload_always = after_run
        guard = job.index('-f "$WORKDIR/smoke-report.json"', run_step)
        copy = job.index('cp "$WORKDIR/smoke-report.json"', run_step)
        upload_step = job.index("actions/upload-artifact", upload_always)
        self.assertLess(
            retention, guard, "копия отчёта — шаг if: always() с guards"
        )
        self.assertLess(guard, copy, "существование отчёта проверяется до копии")
        self.assertLess(
            copy, upload_always, "target-имя копируется до upload"
        )
        self.assertLess(upload_always, upload_step, "upload тоже if: always()")

    def test_portable_scope_input_choice_with_all_default(self):
        """Режим подготовки: input scope — choice, default all, ровно два варианта.

        all (default) — полное выпускное поведение без изменений;
        windows-macos — подготовительный прогон только трёх native-target.
        """
        text = self.PORTABLE.read_text(encoding="utf-8")
        inputs = text.index("inputs:")
        scope = text.index("scope:", inputs)
        body = text[scope : text.index("jobs:")]
        self.assertIn("type: choice", body)
        self.assertIn("default: all", body)
        options = body[body.index("options:") :]
        option_lines = [
            line.strip() for line in options.splitlines() if line.strip().startswith("- ")
        ]
        self.assertEqual(option_lines, ["- all", "- windows-macos"])

    def test_portable_gate_guarded_by_full_scope(self):
        """Выпускной гейт запускается только в полном режиме (scope=all).

        Подготовительный windows-macos НЕ является release-acceptance: gate
        не выполняется, SHA256SUMS и verified-portable не создаются — они
        существуют только внутри gate-job'а (шаг release_gate.py и upload).
        """
        text = self.PORTABLE.read_text(encoding="utf-8")
        gate_job = self._job_section(text, "gate:")
        self.assertIsNotNone(gate_job, "gate-job отсутствует в portable.yml")
        assert gate_job is not None
        self.assertIn("if:", gate_job)
        self.assertIn("inputs.scope == 'all'", gate_job)
        # SHA256SUMS и verified-portable существуют только внутри gate.
        gate_start = text.index("  gate:")
        after_gate = text[gate_start:]
        self.assertIn("release_gate.py", after_gate)
        self.assertIn("verified-portable", after_gate)

    def test_portable_matrix_filters_scope_without_hardcoding(self):
        """Матрица фильтруется по scope поверх targets.json, без списка имён."""
        text = self.PORTABLE.read_text(encoding="utf-8")
        matrix_job = self._job_section(text, "matrix:")
        self.assertIsNotNone(matrix_job, "matrix-job отсутствует")
        assert matrix_job is not None
        self.assertIn("inputs.scope", matrix_job)
        # Фильтр — по system из targets.json (Linux исключается в windows-macos),
        # а не по захардкоженному списку target в workflow.
        self.assertIn('"Linux"', matrix_job)
        for target in ("windows-x86_64", "macos-x86_64", "macos-arm64"):
            self.assertNotIn(f'"{target}"', matrix_job, "имена target не дублируются в workflow")

    def test_portable_build_and_smoke_mandatory_in_both_scopes(self):
        """build и smoke не зависят от scope: обязательны в любом режиме."""
        text = self.PORTABLE.read_text(encoding="utf-8")
        for job in ("build:", "smoke:"):
            section = self._job_section(text, job)
            self.assertIsNotNone(section, job)
            assert section is not None
            self.assertNotIn("inputs.scope", section, f"{job} не conditioned на scope")
            self.assertIn("matrix.name", section, f"{job} работает по матрице")

    def test_portable_all_scope_keeps_four_target_gate(self):
        """Полный режим не ослаблен: gate по-прежнему качает все четыре target."""
        text = self.PORTABLE.read_text(encoding="utf-8")
        gate_start = text.index("  gate:")
        gate = text[gate_start:]
        for target in _all_targets():
            self.assertIn(f"name: keklib-cli-{target}", gate, target)
            self.assertIn(f"name: smoke-report-{target}", gate, target)

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
# Маршрут Linux-сборки CI через закреплённый rootless-билдер.
#
# Полный CI-run собрал Linux на host runtime setup-python: SQLite 3.37.2
# != пин 3.54.1 — release-гейт честно отказал. Регрессии ниже закрепляют:
# Linux build-job обязан использовать существующий драйвер
# tools/build_in_container.py (packaging/builder/, пины не меняются),
# host Python допускается только как оркестрация драйвера; rootless
# проверяется реально (podman info) и fail-closed. Это контрактные тесты
# извлечённых shell-веток на fake-исполняемых файлах, а не нативная
# приёмка CI на hosted runner.


LINUX_PODMAN_STEP = "Rootless Podman на ubuntu-22.04 (linux-x86_64)"
LINUX_DRIVER_STEP = "Сборка linux-x86_64 в закреплённом rootless-билдере"

_FAKE_SUDO = (
    "#!/usr/bin/env bash\n"
    '{ printf \'%s\\0\' "$@"; printf \'\\n\'; } >> "$FAKE_LOG_DIR/sudo.log"\n'
    "exit 0\n"
)
_FAKE_PODMAN = (
    "#!/usr/bin/env bash\n"
    '{ printf \'%s\\0\' "$@"; printf \'\\n\'; } >> "$FAKE_LOG_DIR/podman.log"\n'
    'if [ "${1:-}" = info ]; then\n'
    '  printf \'%s\\n\' "${PODMAN_INFO_OUTPUT:-true}"\n'
    '  exit "${PODMAN_INFO_STATUS:-0}"\n'
    "fi\n"
    "exit 0\n"
)
_FAKE_PYTHON = (
    "#!/usr/bin/env bash\n"
    '{ printf \'%s\\0\' "$@"; printf \'\\n\'; } >> "$FAKE_LOG_DIR/python.log"\n'
    "exit 0\n"
)

_BASH_CACHE: str | None = None
_GIT_BASH_UNAME = re.compile(r"^(MINGW|MSYS|CYGWIN)")


def _reset_bash_cache() -> None:
    global _BASH_CACHE
    _BASH_CACHE = None


def _probe_bash_uname(candidate: str) -> tuple[int, str, str]:
    """Реальная проба кандидата командой ``uname -s``: rc, stdout, stderr."""
    try:
        probe = subprocess.run(
            [candidate, "-c", "uname -s"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError as exc:
        return -1, "", f"OSError: {exc}"
    return probe.returncode, probe.stdout.strip(), probe.stderr.strip()


def _bash_executable() -> str:
    """Верифицированный bash для контрактных тестов (fail-closed, без skip).

    POSIX — ``bash``. Windows — только явно построенные известные
    расположения Git-for-Windows (``ProgramFiles``/``ProgramFiles(x86)``/
    ``ProgramW6432``/``LOCALAPPDATA`` → ``…/Git/bin/bash.exe``); каждый
    кандидат проверяется реальной пробой ``uname -s`` на
    MINGW/MSYS/CYGWIN. «Bash» из PATH кандидатом не является вовсе:
    неквалифицированный PATH-bash на hosted Windows может разрешиться не
    в Git Bash (гипотеза H1 по сигнатуре отказа rc=1/пустой stderr
    run 39305339493; нативно не доказана), поэтому опора — только явные
    пути. Нет верифицируемого bash — явный AssertionError с
    диагностикой всех проб, а не skip.
    """
    global _BASH_CACHE
    if _BASH_CACHE is not None:
        return _BASH_CACHE
    if os.name != "nt":
        _BASH_CACHE = "bash"
        return "bash"
    env = os.environ
    candidates: list[str] = []
    roots = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        if env.get(var):
            # Строки с '/' вместо Path: на Windows оба разделителя валидны,
            # а Path под os.name='nt' на POSIX-хосте не инстанцируется.
            roots.append(f"{env[var]}/Git/bin/bash.exe")
    if env.get("LOCALAPPDATA"):
        roots.append(f"{env['LOCALAPPDATA']}/Programs/Git/bin/bash.exe")
    candidates.extend(item for item in roots if os.path.isfile(item))
    tried = []
    for candidate in candidates:
        code, out, err = _probe_bash_uname(candidate)
        tried.append(f"{candidate}: rc={code} uname={out!r} stderr={err!r}")
        if code == 0 and _GIT_BASH_UNAME.match(out):
            _BASH_CACHE = candidate
            return candidate
    detail = "; ".join(tried) if tried else "кандидатов нет"
    raise AssertionError(
        "не найден верифицируемый Git-for-Windows bash в известных расположениях "
        f"(ожидался uname MINGW/MSYS/CYGWIN); пробы: {detail}"
    )


def _bash_path(bash: str, value: str) -> str:
    """POSIX-форма Bash-лицевого пути (FAKE_LOG_DIR, path-argv).

    POSIX — тождество. Windows — ``cygpath -u`` того самого
    верифицированного bash: путь ``C:\\native`` в Bash-окружении сам в
    POSIX не превращается. Отказ cygpath — явная ошибка с диагностикой.
    """
    if os.name != "nt":
        return value
    try:
        probe = subprocess.run(
            [bash, "-c", 'cygpath -u -- "$1"', "cygpath", value],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError as exc:
        raise AssertionError(f"cygpath не запускается в {bash!r}: {exc}") from exc
    if probe.returncode != 0:
        raise AssertionError(
            f"cygpath -u {value!r} в {bash!r}: rc={probe.returncode} "
            f"stdout={probe.stdout!r} stderr={probe.stderr!r}"
        )
    return probe.stdout.strip()


def _bash_diag(result: subprocess.CompletedProcess[str], bash: str) -> str:
    """Диагностика отказа bash-скрипта: фактический bash, rc, stdout И stderr."""
    return (
        f"bash={bash!r} rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def _step_block(job_text: str, step_name: str) -> str:
    """Текст одного шага job-секции workflow: от '- name:' до следующего шага."""
    lines = job_text.splitlines()
    anchor = f"- name: {step_name}"
    starts = [i for i, line in enumerate(lines) if line.strip() == anchor]
    if len(starts) != 1:
        raise AssertionError(f"шаг {step_name!r} встречается {len(starts)} раз, ожидался один")
    start = starts[0]
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^ {6}- ", lines[i]) or re.match(r"^ {4}\S", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


def _step_run_script(job_text: str, step_name: str) -> str:
    """Run-скрипт шага (inline или блок после 'run: |') — то, что выполнит runner."""
    block = _step_block(job_text, step_name)
    lines = block.splitlines()
    run_lines = [i for i, line in enumerate(lines) if line.strip().startswith("run:")]
    if len(run_lines) != 1:
        raise AssertionError(f"у шага {step_name!r} ожидался ровно один run:")
    run_idx = run_lines[0]
    inline = lines[run_idx].strip()[len("run:") :].strip()
    if inline and inline not in ("|", ">", "|-", ">-", "|+", ">+"):
        return inline
    base = len(lines[run_idx]) - len(lines[run_idx].lstrip())
    script: list[str] = []
    for line in lines[run_idx + 1 :]:
        if not line.strip():
            script.append("")
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base:
            break
        script.append(line[base + 2 :])
    return "\n".join(script).strip("\n")


def _step_condition(job_text: str, step_name: str) -> str | None:
    """Условие 'if:' шага (без префикса), None если шаг безусловный."""
    match = re.search(r"^ {8}if: (.+)$", _step_block(job_text, step_name), flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _write_fake(bin_dir: Path, name: str, body: str) -> None:
    # Bash-stubs пишутся точными UTF-8/LF-байтами (R1): текстовая запись по
    # умолчанию переводит '\n' в os.linesep — на Windows это CRLF, shebang
    # становится 'bash\r', и fake-команда не запускается вовсе.
    path = bin_dir / name
    path.write_bytes(body.encode("utf-8"))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _fake_calls(log: Path) -> list[list[str]]:
    """Вызовы fake-исполняемого: по одному списку argv на строку лога."""
    if not log.exists():
        return []
    calls = []
    for line in log.read_text(encoding="utf-8").splitlines():
        argv = [item for item in line.split("\0") if item]
        if argv:
            calls.append(argv)
    return calls


class LinuxCiBuilderRouteTests(unittest.TestCase):
    """Контракт маршрута linux-x86_64 в build-job portable.yml (fake-executables)."""

    PORTABLE = WorkflowStructureTests.PORTABLE

    def _build_job(self) -> str:
        text = self.PORTABLE.read_text(encoding="utf-8")
        job = WorkflowStructureTests._job_section(text, "build:")
        self.assertIsNotNone(job, "build-job отсутствует в portable.yml")
        assert job is not None
        return job

    def _execute(
        self,
        script: str,
        *,
        bin_dir: Path,
        cwd: Path,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Исполнить извлечённый shell-фрагмент с fake-исполняемыми в PATH.

        Bash — только верифицированный ``_bash_executable()`` (не «bash»
        из PATH: на Windows такой кандидат не верифицирован — гипотеза H1
        по сигнатуре отказа rc=1/пустой stderr run 39305339493, нативно
        не доказана); FAKE_LOG_DIR конвертируется в POSIX-форму через
        ``_bash_path`` — Windows-путь ``C:\\native`` в Bash-окружении сам
        не превращается.
        """
        bash = _bash_executable()
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(bin_dir), env.get("PATH", "")])
        env["FAKE_LOG_DIR"] = _bash_path(bash, str(cwd))
        env.pop("PODMAN_INFO_OUTPUT", None)
        env.pop("PODMAN_INFO_STATUS", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [bash, "-c", script],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def setUp(self) -> None:
        _reset_bash_cache()

    @contextlib.contextmanager
    def _mock_windows_discovery(
        self,
        *,
        uname_answers: dict[str, tuple[int, str, str]],
        isfile: set[str],
        which: str | None = "C:/Windows/System32/bash.exe",
    ):
        """Полностью контролируемые входы discovery под os.name='nt'.

        Все четыре Git-переменные и PATH заменяются (никакой утечки host env),
        which замокан — он вообще не должен использоваться для пробы;
        uname-пробы отвечаются моком subprocess.run. Никакой текстовый
        файл не выдаётся за native .exe (rereview-2 R2) — исполняемых
        синтетических обёрток больше нет. yield (probes, which_mock).
        """
        from unittest import mock

        probes: list[list[str]] = []

        def fake_run(argv, *args, **kwargs):
            probes.append([str(item) for item in argv])
            answer = uname_answers.get(str(argv[0]))
            if answer is None:
                return subprocess.CompletedProcess(argv, 0, stdout="Linux\n", stderr="")
            code, out, err = answer
            return subprocess.CompletedProcess(argv, code, stdout=out + "\n", stderr=err)

        env = {
            "ProgramFiles": "C:/Program Files",
            "ProgramFiles(x86)": "C:/Program Files (x86)",
            "ProgramW6432": "C:/ProgramW6432",
            "LOCALAPPDATA": "C:/Users/runneradmin/AppData/Local",
            "PATH": "C:/nowhere",
        }
        with (
            mock.patch("os.name", "nt"),
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch("os.path.isfile", side_effect=lambda p: p in isfile),
            mock.patch(
                "tests.test_release.shutil.which", return_value=which
            ) as which_mock,
            mock.patch("tests.test_release.subprocess.run", side_effect=fake_run),
        ):
            yield probes, which_mock

    def test_discovery_selects_known_git_location_verified_by_uname(self):
        """Discovery: известное расположение Git, верифицированное uname.

        Все входы контролируются (env/which/isfile/probe) — ни один хостовый
        кандидат не может быть даже опробован; проба ровно одна и адресная.
        """
        git_bash = "C:/Program Files (x86)/Git/bin/bash.exe"
        with self._mock_windows_discovery(
            uname_answers={git_bash: (0, "MINGW64_NT-10.0-19045", "")},
            isfile={git_bash},
        ) as (probes, which_mock):
            discovered = _bash_executable()
        self.assertEqual(discovered, git_bash)
        self.assertEqual(probes, [[git_bash, "-c", "uname -s"]])
        which_mock.assert_not_called()

    def test_discovery_fails_closed_and_never_probes_bare_which_bash(self):
        """Fail-closed без верифицируемых расположений; bare which-bash не пробуется.

        Регрессия native-границы (rereview-2): «bash» из PATH —
        неквалифицированный кандидат (может быть не Git Bash; сигнатура
        rc=1/пустой stderr run 39305339493 — гипотеза H1, нативно не
        доказана) и потому не кандидат вовсе. Без известных расположений —
        явный AssertionError с диагностикой, а не skip и не PATH-поиск.
        """
        with self._mock_windows_discovery(
            uname_answers={},
            isfile=set(),
            which="C:/Windows/System32/bash.exe",
        ) as (probes, which_mock):
            with self.assertRaises(AssertionError) as ctx:
                _bash_executable()
        self.assertIn("Git-for-Windows", str(ctx.exception))
        which_mock.assert_not_called()
        self.assertEqual(probes, [], "ни один спавн для discovery не допускается")

    def test_discovery_rejects_non_msys_uname_and_tries_next_known_location(self):
        """uname без MINGW/MSYS/CYGWIN отвергается; следующий кандидат проверяется."""
        first = "C:/Program Files/Git/bin/bash.exe"
        second = "C:/Program Files (x86)/Git/bin/bash.exe"
        with self._mock_windows_discovery(
            uname_answers={
                first: (0, "Linux", ""),
                second: (0, "MSYS_NT-10.0-19045", ""),
            },
            isfile={first, second},
        ) as (probes, which_mock):
            discovered = _bash_executable()
        self.assertEqual(discovered, second)
        self.assertEqual(
            probes,
            [[first, "-c", "uname -s"], [second, "-c", "uname -s"]],
        )
        which_mock.assert_not_called()

    def test_bash_path_converts_via_cygpath_argv_of_given_bash(self):
        """Конвертация — cygpath -u указанного bash, путь как argv-элемент."""
        from unittest import mock

        seen: dict[str, list[str]] = {}

        def fake_run(argv, *args, **kwargs):
            seen["argv"] = [str(item) for item in argv]
            return subprocess.CompletedProcess(
                argv, 0, stdout="/c/Users/RUNNER~1/tmp\n", stderr=""
            )

        bash = "C:/Program Files/Git/bin/bash.exe"
        windows_path = r"C:\Users\RUNNER~1\AppData\Local\Temp\keklib"
        with (
            mock.patch("os.name", "nt"),
            mock.patch("tests.test_release.subprocess.run", side_effect=fake_run),
        ):
            converted = _bash_path(bash, windows_path)
        self.assertEqual(converted, "/c/Users/RUNNER~1/tmp")
        self.assertEqual(
            seen["argv"],
            [bash, "-c", 'cygpath -u -- "$1"', "cygpath", windows_path],
            "Windows-путь передаётся argv-элементом, не интерполяцией в скрипт",
        )

        def failing_run(argv, *args, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="cygpath: failure")

        with (
            mock.patch("os.name", "nt"),
            mock.patch("tests.test_release.subprocess.run", side_effect=failing_run),
        ):
            with self.assertRaises(AssertionError) as ctx:
                _bash_path(bash, windows_path)
        self.assertIn("cygpath", str(ctx.exception))
        self.assertIn("cygpath: failure", str(ctx.exception))

        if os.name != "nt":
            with mock.patch("tests.test_release.subprocess.run") as run_mock:
                self.assertEqual(_bash_path("bash", "/tmp/opencode/x"), "/tmp/opencode/x")
            run_mock.assert_not_called()

    def test_execute_spawns_verified_bash_and_converts_log_dir(self):
        """Shell-контракты исполняются верифицированным bash хоста.

        Spy только ЗАПИСЫВАЕТ argv и делегирует настоящему
        subprocess.run: никакого текстового файла за native .exe (R2) —
        спавнится ровно тот bash, который вернул _bash_executable() на
        этой ОС (POSIX — bash хоста; Windows — верифицированный Git
        Bash), FAKE_LOG_DIR идёт через _bash_path, fake python получает
        точный argv.
        """
        from unittest import mock

        with tempfile.TemporaryDirectory(prefix="keklib-ci-plumb-") as tmp:
            case = Path(tmp)
            bin_dir = case / "bin"
            bin_dir.mkdir()
            _write_fake(bin_dir, "python", _FAKE_PYTHON)
            script = "python --plumb-probe"
            bash = _bash_executable()
            real_run = subprocess.run
            spawned: list[list[str]] = []

            def spy_run(argv, *args, **kwargs):
                spawned.append([str(item) for item in argv])
                return real_run(argv, *args, **kwargs)

            real_path = _bash_path
            with (
                mock.patch("tests.test_release.subprocess.run", side_effect=spy_run),
                mock.patch(
                    "tests.test_release._bash_path", side_effect=real_path
                ) as path_mock,
            ):
                result = self._execute(script, bin_dir=bin_dir, cwd=case)
            self.assertEqual(result.returncode, 0, _bash_diag(result, bash))
            self.assertEqual(spawned, [[bash, "-c", script]])
            path_mock.assert_called_once_with(bash, str(case))
            self.assertEqual(_fake_calls(case / "python.log"), [["--plumb-probe"]])

    def test_linux_build_runs_existing_driver_with_both_flags(self):
        """Linux-сборка — существующий драйвер билдера, не второй рецепт.

        Точный argv проверяется исполнением извлечённой строки с fake
        ``python``: оба флага ``--build-image``/``--build-bundle``, вывод
        ``--output dist``. ``--all`` запрещён: он создаёт smoke-фикстуру
        в каталоге вывода, а upload обязан нести только архив+sidecar
        (полный изолированный smoke — отдельный обязательный job).
        """
        job = self._build_job()
        script = _step_run_script(job, LINUX_DRIVER_STEP)
        self.assertEqual(
            _step_condition(job, LINUX_DRIVER_STEP), "matrix.name == 'linux-x86_64'"
        )
        with tempfile.TemporaryDirectory(prefix="keklib-ci-route-") as tmp:
            case = Path(tmp)
            bin_dir = case / "bin"
            bin_dir.mkdir()
            _write_fake(bin_dir, "python", _FAKE_PYTHON)
            bash = _bash_executable()
            result = self._execute(script, bin_dir=bin_dir, cwd=case)
            self.assertEqual(result.returncode, 0, _bash_diag(result, bash))
            calls = _fake_calls(case / "python.log")
            self.assertEqual(len(calls), 1, "ожидался ровно один вызов python")
            argv = calls[0]
            self.assertEqual(
                argv,
                [
                    "tools/build_in_container.py",
                    "--build-image",
                    "--build-bundle",
                    "--output",
                    "dist",
                ],
            )
            self.assertNotIn("--all", argv)
            self.assertNotIn("--smoke", argv)
            self.assertNotIn("--write-fixture", argv)

    def test_native_build_steps_restricted_to_non_linux(self):
        """Нативные setup-python шаги build_portable ограничены non-Linux.

        Host runtime runner'а (SQLite 3.37.2) не равен пину 3.54.1 — на
        Linux эти шаги обязаны быть выключены условием matrix.name.
        """
        job = self._build_job()
        for name in (
            "Сборочные зависимости (точные пины с хешами)",
            "Установка проекта (src-layout) в окружение job'а",
            "Сборка portable со всеми fail-closed гейтами",
        ):
            self.assertEqual(
                _step_condition(job, name),
                "matrix.name != 'linux-x86_64'",
                msg=name,
            )
        # build_portable вызывается ровно один раз и только в non-Linux ветке
        self.assertEqual(job.count("build_portable.py"), 1)
        self.assertIn(
            "build_portable.py",
            _step_block(job, "Сборка portable со всеми fail-closed гейтами"),
        )

    def test_rootless_podman_step_fails_closed(self):
        """Rootless-проверка реальна (podman info) и fail-closed, без rootful.

        Извлечённый скрипт исполняется с fake ``sudo``/``podman``: сначала
        установка host-Podman prerequisites через apt-get, затем реальный
        запрос ``podman info``; rootless=false или отказ самого podman
        обязаны валить шаг. ``sudo`` никогда не оборачивает ``podman``.
        """
        job = self._build_job()
        script = _step_run_script(job, LINUX_PODMAN_STEP)
        self.assertEqual(
            _step_condition(job, LINUX_PODMAN_STEP), "matrix.name == 'linux-x86_64'"
        )
        active = [
            line.strip()
            for line in script.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertTrue(
            any("podman info" in line for line in active),
            "проверка rootless — реальный вызов podman info, не комментарий",
        )
        self.assertTrue(
            any(line.startswith("test") and "true" in line for line in active),
            "результат podman info сверяется со строкой true",
        )
        self.assertLess(
            script.index("apt-get install"),
            script.index("podman info"),
            "host-Prerequisites ставятся до rootless-проверки",
        )

        def run_case(extra_env: dict[str, str] | None = None):
            case = Path(tempfile.mkdtemp(prefix="keklib-ci-podman-"))
            self.addCleanup(shutil.rmtree, case, ignore_errors=True)
            bin_dir = case / "bin"
            bin_dir.mkdir()
            _write_fake(bin_dir, "sudo", _FAKE_SUDO)
            _write_fake(bin_dir, "podman", _FAKE_PODMAN)
            result = self._execute(
                script, bin_dir=bin_dir, cwd=case, extra_env=extra_env
            )
            return result, case

        # Успех: rootless=true
        result, case = run_case()
        self.assertEqual(
            result.returncode, 0, _bash_diag(result, _bash_executable())
        )
        sudo_calls = _fake_calls(case / "sudo.log")
        self.assertEqual(sudo_calls[0][:2], ["apt-get", "update"])
        install = sudo_calls[1]
        self.assertEqual(install[:2], ["apt-get", "install"])
        self.assertIn("podman", install, "устанавливается сам host Podman")
        podman_calls = _fake_calls(case / "podman.log")
        self.assertEqual(podman_calls[0][0], "info")
        for call in sudo_calls:
            self.assertNotEqual(call[0], "podman", "sudo podman (rootful) запрещён")

        # Fail-closed: rootless=false — шаг обязан упасть
        result, _ = run_case({"PODMAN_INFO_OUTPUT": "false"})
        self.assertNotEqual(result.returncode, 0, "rootless=false не может проходить")

        # Fail-closed: сам podman info сломан — шаг обязан упасть
        result, _ = run_case({"PODMAN_INFO_STATUS": "1"})
        self.assertNotEqual(result.returncode, 0, "отказ podman info не может проходить")

    def test_build_job_without_privileged_or_security_weakening(self):
        """Никаких привилегированных режимов и ослабления security ради fake-pass.

        Проверяется исполняемый/структурный текст job'а без строк-комментариев:
        комментарий может декларировать запрет, но не может его включить.
        """
        job = self._build_job()
        active = "\n".join(
            line for line in job.splitlines() if not line.strip().startswith("#")
        )
        for forbidden in (
            "--privileged",
            "--cap-add",
            "security-opt",
            "seccomp",
            "/var/run/docker.sock",
            "sudo podman",
        ):
            self.assertNotIn(forbidden, active)

    def test_dist_upload_unconditional_for_both_routes(self):
        """Upload dist безусловен: обе ветки публикуют один и тот же артефакт."""
        job = self._build_job()
        self.assertIsNone(_step_condition(job, "Опубликовать архив комплекта"))
        self.assertIn("path: dist", _step_block(job, "Опубликовать архив комплекта"))

    def test_linux_route_step_order(self):
        """Порядок: checkout → CPython → rootless Podman → драйвер билдера → upload."""
        job = self._build_job()
        checkout = job.index("- name: Checkout")
        cpython = job.index("- name: CPython 3.14.8")
        podman_step = job.index(f"- name: {LINUX_PODMAN_STEP}")
        driver_step = job.index(f"- name: {LINUX_DRIVER_STEP}")
        upload = job.index("- name: Опубликовать архив комплекта")
        self.assertLess(checkout, cpython)
        self.assertLess(cpython, podman_step)
        self.assertLess(podman_step, driver_step)
        self.assertLess(driver_step, upload)

    def test_ci_routes_keep_pinned_sqlite(self):
        """Подключение билдера к CI пины не меняет: Linux 3.54.1 == рецепту
        билдера, native-target — принятые 3.50.4.

        Смена любого пина — осознанное решение с правкой этого теста
        (release-гейт сверяет пин с фактическим runtime каждой сборки).
        """
        toolchain = json.loads(
            (REPO_ROOT / "packaging" / "toolchain.json").read_text(encoding="utf-8")
        )["targets"]
        builder = json.loads(
            (REPO_ROOT / "packaging" / "builder" / "builder-pins.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(toolchain["linux-x86_64"]["sqlite"], "3.54.1")
        self.assertEqual(
            builder["sources"]["sqlite"]["version"],
            toolchain["linux-x86_64"]["sqlite"],
            "пин Linux обязан совпадать с рецептом билдера, который его строит",
        )
        for target in ("windows-x86_64", "macos-x86_64", "macos-arm64"):
            self.assertEqual(toolchain[target]["sqlite"], "3.50.4", target)

    def test_fake_bash_stubs_forced_lf_bytes(self):
        """Bash-stubs пишутся точными LF-байтами, CRLF ломает shebang (R1, P1).

        ``Path.write_text`` без явного ``newline`` переводит ``\\n`` в
        ``os.linesep`` — на Windows это CRLF, ``/usr/bin/env`` получает
        ``bash\\r``, fake-команды не запускаются и обязательные Windows
        source-jobs падают (а с ними build/smoke/gate всего конвейера).
        Регрессия детерминированно эмулирует Windows-преобразование на
        Linux: все записи перехватываются на ``Path.open`` → чистый
        ``_pyio`` с ``os.linesep='\\r\\n'`` (``_pyio`` читает ``os.linesep``
        в момент открытия, в отличие от кэширующего C ``_io``). Старая
        запись по умолчанию на этой эмуляции даёт CRLF-байты и тест
        падает; ``write_bytes`` от эмуляции не зависит. Контроль эмуляции
        воспроизводит доказательство reviewer'а: CRLF-stub не исполняется.
        """
        import _pyio
        from unittest import mock

        def windows_default_open(self: Path, mode: str = "r", *args: object, **kwargs: object):
            with mock.patch("os.linesep", "\r\n"):
                return _pyio.open(self, mode, *args, **kwargs)  # type: ignore[arg-type]

        bodies = {"python": _FAKE_PYTHON, "sudo": _FAKE_SUDO, "podman": _FAKE_PODMAN}
        with tempfile.TemporaryDirectory(prefix="keklib-ci-stubs-") as tmp:
            case = Path(tmp)
            bin_dir = case / "bin"
            bin_dir.mkdir()
            # Запись stub'ов под Windows-эмуляцией: старый write_text по
            # умолчанию здесь обязан дать CRLF и провалить точные байты.
            with mock.patch.object(Path, "open", windows_default_open):
                for name, body in bodies.items():
                    _write_fake(bin_dir, name, body)
            for name, body in bodies.items():
                content = (bin_dir / name).read_bytes()
                self.assertEqual(
                    content,
                    body.encode("utf-8"),
                    f"{name}: bash-stub обязан быть точными LF-байтами body",
                )
                self.assertNotIn(b"\r", content, f"{name}: CR в bash-stub")
                self.assertEqual(content.split(b"\n")[0], b"#!/usr/bin/env bash", name)

            # Контроль эмуляции (доказательство R1): та же текстовая запись
            # по умолчанию действительно даёт CRLF, и такой stub не исполняется.
            broken = case / "broken-stub"
            with mock.patch.object(Path, "open", windows_default_open):
                broken.write_text(_FAKE_PYTHON, encoding="utf-8")
            broken.chmod(0o755)
            self.assertIn(b"\r\n", broken.read_bytes(), "эмуляция обязана давать CRLF")
            bash = _bash_executable()
            env = dict(os.environ, FAKE_LOG_DIR=_bash_path(bash, str(case)))
            r_broken = subprocess.run(
                [bash, "-c", 'exec "$1" --probe', "probe", _bash_path(bash, str(broken))],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertNotEqual(
                r_broken.returncode, 0, "CRLF-stub ('bash\\r') не должен запускаться"
            )

            # Фактическое исполнение настоящего stub через shebang.
            result = subprocess.run(
                [
                    bash,
                    "-c",
                    'exec "$1" --probe',
                    "probe",
                    _bash_path(bash, str(bin_dir / "python")),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, _bash_diag(result, bash))
            self.assertEqual(_fake_calls(case / "python.log"), [["--probe"]])

    def test_smoke_and_gate_not_weakened_by_linux_route(self):
        """Изолированный smoke без checkout и выпускной гейт не задеты маршрутом."""
        text = self.PORTABLE.read_text(encoding="utf-8")
        smoke = WorkflowStructureTests._job_section(text, "smoke:")
        self.assertIsNotNone(smoke)
        assert smoke is not None
        self.assertNotIn("actions/checkout", smoke, "smoke остаётся без checkout")
        self.assertNotIn("build_in_container", smoke)
        gate = WorkflowStructureTests._job_section(text, "gate:")
        self.assertIsNotNone(gate)
        assert gate is not None
        self.assertNotIn("apt-get", gate)
        self.assertNotIn("build_in_container", gate)


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
