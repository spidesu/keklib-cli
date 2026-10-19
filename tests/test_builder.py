"""Тесты rootless-контейнерного baseline-билдера linux-x86_64.

Native-continuation задачи 11 (разрешение пользователя на Ubuntu 22.04 +
rootless Podman). Проверяются:

- пины builder'а (`packaging/builder/builder-pins.json`): закреплённый
  digest образа Ubuntu 22.04, исходники CPython/SQLite с хешами,
  согласованность с `packaging/toolchain.json`;
- GPL/ненужные зависимости исключены из состава сборки (readline, gdbm,
  ncurses, tkinter) — лицензионное закрытие не должно молча обрасти
  GPL-компонентами;
- определение Containerfile привязано к digest'у и проверяет хеши
  загружаемых исходников;
- argv команд podman: источник проекта монтируется только read-only,
  output — отдельный явный каталог, snapshot не тащит .venv/build,
  smoke выполняется в чистом baseline-образе, а не в сборщике;
- фактическое состояние пинов toolchain по итогам нативного прогона
  (linux заполнен и проверен; Windows/macOS остаются честно незаполненными).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import build_in_container as bic  # noqa: E402

HEX64 = re_hex64 = None  # заменяется ниже импортом re
import re  # noqa: E402

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class BuilderPinsTests(unittest.TestCase):
    """Контракт пинов сборочного контейнера (машинная воспроизводимость)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pins = bic.load_builder_pins()

    def test_schema_and_required_fields(self) -> None:
        self.assertEqual(self.pins["schema_version"], bic.PINS_SCHEMA_VERSION)
        image = self.pins["image"]
        for key in ("reference", "digest", "image_id", "os_release", "glibc"):
            self.assertIn(key, image, f"image.{key} обязан быть в пинах")
        for source in ("cpython", "sqlite"):
            entry = self.pins["sources"][source]
            for key in ("version", "url", "sha256", "size_bytes"):
                self.assertIn(key, entry, f"sources.{source}.{key} отсутствует")

    def test_image_is_pinned_ubuntu_digest(self) -> None:
        reference = self.pins["image"]["reference"]
        digest = self.pins["image"]["digest"]
        self.assertTrue(reference.startswith("docker.io/library/ubuntu@sha256:"))
        self.assertTrue(_HEX64.match(digest.split(":", 1)[1]))
        self.assertIn(digest, reference, "reference обязан нести тот же digest")
        self.assertIn("22.04", self.pins["image"]["os_release"])
        self.assertTrue(self.pins["image"]["glibc"].startswith("2.35"))

    def test_hashes_and_urls(self) -> None:
        cpython = self.pins["sources"]["cpython"]
        sqlite = self.pins["sources"]["sqlite"]
        self.assertTrue(_HEX64.match(cpython["sha256"]))
        self.assertTrue(_HEX64.match(sqlite["sha256"]))
        self.assertRegex(cpython["url"], r"^https://www\.python\.org/ftp/python/3\.14\.8/")
        self.assertRegex(sqlite["url"], r"^https://www\.sqlite\.org/20\d\d/sqlite-autoconf-")
        self.assertGreater(cpython["size_bytes"], 0)
        self.assertGreater(sqlite["size_bytes"], 0)
        self.assertIn("sha3_256", sqlite, "upstream публикует SHA3-256 — храним его")
        self.assertTrue(_HEX64.match(sqlite["sha3_256"]))

    def test_pins_consistent_with_toolchain(self) -> None:
        toolchain = json.loads(
            (PROJECT_ROOT / "packaging" / "toolchain.json").read_text(encoding="utf-8")
        )
        linux = toolchain["targets"]["linux-x86_64"]
        self.assertEqual(linux["python"], self.pins["sources"]["cpython"]["version"])
        self.assertEqual(linux["sqlite"], self.pins["sources"]["sqlite"]["version"])

    def test_gpl_and_unneeded_dependencies_excluded(self) -> None:
        """readline (GPL), gdbm, ncurses, tkinter не входят в сборку runtime."""
        apt = " ".join(self.pins["apt_packages"]).lower()
        for forbidden in ("readline", "gdbm", "ncurses", "tk-dev", "tcl"):
            self.assertNotIn(forbidden, apt, f"GPL/ненужная зависимость в apt: {forbidden}")
        flags = self.pins["cpython_configure_flags"]
        self.assertIn("--without-readline", flags)
        self.assertIn("--disable-test-modules", flags)
        self.assertIn(
            "--enable-shared",
            flags,
            "PyInstaller 6.x требует общую libpython: --enable-shared обязателен",
        )
        self.assertIn("--enable-fts5", self.pins["sqlite_configure_flags"])

    def test_containerfile_bound_to_pins(self) -> None:
        text = (BUILDER := PROJECT_ROOT / "packaging" / "builder").joinpath(
            bic.CONTAINERFILE_NAME
        ).read_text(encoding="utf-8")
        digest = self.pins["image"]["digest"]
        self.assertIn(digest, text, "Containerfile обязан исходить из pinned digest")
        self.assertNotIn("ubuntu:latest", text)
        self.assertIn("sha256sum -c", text, "исходники проверяются по хешу при сборке образа")
        self.assertIn("--without-readline", text)
        self.assertIn("--disable-test-modules", text)
        self.assertIn("--enable-fts5", text)


class ContainerToolTests(unittest.TestCase):
    """Argv команд podman строится по пинам без запуска podman."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pins = bic.load_builder_pins()

    def test_build_argv_uses_pins(self) -> None:
        argv = bic.podman_build_argv(self.pins, tag="keklib-cli-builder:test")
        self.assertEqual(argv[:2], ["podman", "build"])
        joined = " ".join(argv)
        self.assertIn(f"--build-arg=CPYTHON_URL={self.pins['sources']['cpython']['url']}", joined)
        self.assertIn(
            f"--build-arg=CPYTHON_SHA256={self.pins['sources']['cpython']['sha256']}", joined
        )
        self.assertIn(f"--build-arg=SQLITE_URL={self.pins['sources']['sqlite']['url']}", joined)
        self.assertIn(
            f"--build-arg=SQLITE_SHA256={self.pins['sources']['sqlite']['sha256']}", joined
        )
        self.assertIn(str(bic.BUILDER_DIR), joined)
        self.assertIn(bic.CONTAINERFILE_NAME, joined)

    def test_build_argv_passes_base_image_pin(self) -> None:
        """Пины образа обязаны управлять фактической рецептурой (review P2).

        Mutation-проба: согласованно заменённый digest в pins должен менять
        build argv (передаётся --build-arg=BASE_IMAGE), а не только smoke.
        """
        import copy  # noqa: PLC0415

        tampered = copy.deepcopy(self.pins)
        digest = "sha256:" + "1" * 64
        reference = "docker.io/library/ubuntu@" + digest
        tampered["image"]["digest"] = digest
        tampered["image"]["reference"] = reference
        argv = bic.podman_build_argv(tampered, tag="t")
        self.assertIn(f"--build-arg=BASE_IMAGE={reference}", argv)
        baseline = bic.podman_build_argv(self.pins, tag="t")
        self.assertTrue(
            any(
                self.pins["image"]["reference"] in element and "BASE_IMAGE" in element
                for element in baseline
            ),
            "исходный pinned reference передаётся как BASE_IMAGE",
        )

    def test_smoke_uses_plain_baseline_image(self) -> None:
        argv = bic.podman_smoke_argv(
            self.pins,
            archive=Path("/tmp/opencode/out/keklib-cli-0.1.0-linux-x86_64.tar.gz"),
            fixture=Path("/tmp/opencode/smoke-fixture"),
        )
        self.assertEqual(argv[:2], ["podman", "run"])
        joined = " ".join(argv)
        self.assertIn(self.pins["image"]["reference"], joined)
        self.assertNotIn("keklib-cli-builder", joined)
        self.assertIn(":/archive/", joined)
        self.assertIn(":/fixture:ro", joined)
        self.assertIn("./keklib-cli --help", joined)
        self.assertIn("index --refresh", joined)
        self.assertIn("index.sqlite3", joined)

    def test_smoke_script_independent_of_archive_name(self) -> None:
        """Имя архива не интерполируется в bash-скрипт дыма (review P2).

        Монтирование под фиксированными именами + постоянный скрипт:
        пробелы/кавычки/метасимволы имени не попадают в shell.
        """
        hostile = Path("/tmp/opencode/dir with spaces/архив';$(rm -rf /).tar.gz")
        argv_normal = bic.podman_smoke_argv(
            self.pins,
            archive=Path("/tmp/opencode/out/keklib-cli-0.1.0-linux-x86_64.tar.gz"),
            fixture=Path("/tmp/opencode/f"),
        )
        argv_hostile = bic.podman_smoke_argv(self.pins, archive=hostile, fixture=Path("/tmp/opencode/f"))
        script_normal = argv_normal[-1]
        script_hostile = argv_hostile[-1]
        self.assertEqual(script_normal, script_hostile, "скрипт дыма — константа")
        for fragment in (hostile.name, "archive.name", "$(rm"):
            self.assertNotIn(fragment, script_hostile)
        self.assertIn("cd /archive", script_hostile)
        self.assertIn("bundle.tar.gz", script_hostile)
        # Контрольная сумма проверяется по digest-полю с перенаправлением на
        # фиксированное имя монтирования (форма sha256sum -c сохранена).
        self.assertIn("sha256sum -c -", script_hostile)
        self.assertIn("awk '{print $1", script_hostile)
        # Host-сторона: источник монтирования — одиночный argv-элемент без shell.
        self.assertIn(f"-v {hostile}:/archive/bundle.tar.gz:ro", " ".join(argv_hostile))


class BuilderRecipeTests(unittest.TestCase):
    """Рецепт Containerfile обязан соответствовать пинам (fail-closed)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pins = bic.load_builder_pins()

    def test_recipe_matches_pins(self) -> None:
        bic.verify_containerfile_recipe(self.pins)

    def test_recipe_rejects_pin_drift(self) -> None:
        """Любое расхождение пинов с рецептом — отказ (mutation-пробы)."""
        import copy  # noqa: PLC0415

        def tamper(**changes: object) -> dict[str, object]:
            clone = copy.deepcopy(self.pins)
            for key, value in changes.items():
                clone[key] = value
            return clone

        digest = "sha256:" + "2" * 64
        reference = "docker.io/library/ubuntu@" + digest
        mutations = {
            "image-reference": tamper(
                image={**self.pins["image"], "digest": digest, "reference": reference}
            ),
            "apt-extra": tamper(apt_packages=[*self.pins["apt_packages"], "libreadline-dev"]),
            "apt-missing": tamper(
                apt_packages=[p for p in self.pins["apt_packages"] if p != "liblzma-dev"]
            ),
            "cpython-flag-missing": tamper(
                cpython_configure_flags=[
                    f for f in self.pins["cpython_configure_flags"] if f != "--without-readline"
                ]
            ),
            "cpython-flag-extra": tamper(
                cpython_configure_flags=[*self.pins["cpython_configure_flags"], "--with-lto"]
            ),
            "sqlite-flag-missing": tamper(
                sqlite_configure_flags=[
                    f for f in self.pins["sqlite_configure_flags"] if f != "--enable-fts5"
                ]
            ),
        }
        for name, mutated in mutations.items():
            with self.assertRaises(bic.BuilderError, msg=name) as ctx:
                bic.verify_containerfile_recipe(mutated)
            self.assertIn(name.split("-")[0], str(ctx.exception).lower(), name)

    def test_bundle_run_source_readonly_and_explicit_output(self) -> None:
        output = Path("/tmp/opencode/portable-out")
        argv = bic.podman_bundle_argv(
            self.pins,
            tag="builder:test",
            project_root=PROJECT_ROOT,
            output_dir=output,
        )
        self.assertEqual(argv[:2], ["podman", "run"])
        # Контракт argv — native Path/str: источник монтирования — точно
        # str(Path) переданного каталога (на Windows это native-форма
        # пути), цель и режим — фиксированные контейнерные имена. Одна
        # POSIX-строка ожидала бы Linux-путь даже на Windows-хосте тестов.
        mount_specs = [argv[i + 1] for i, item in enumerate(argv) if item == "-v"]
        self.assertIn(f"{PROJECT_ROOT}:/src:ro", mount_specs)
        self.assertIn(f"{output}:/out", mount_specs)
        joined = " ".join(argv)
        self.assertNotIn("--privileged", joined)
        self.assertNotIn(":/root", joined)
        self.assertNotIn(":/home", joined)
        # snapshot внутри контейнера не копирует .venv/build и сборку ведёт
        # из копии, а не из смонтированного проекта.
        self.assertIn("tar -C /src", joined)
        for exclude in bic.SNAPSHOT_EXCLUDES:
            self.assertIn(f"--exclude=./{exclude}", joined)
        self.assertIn("--target linux-x86_64", joined)


def _read_notice(component: str, notice: str) -> str:
    return (PROJECT_ROOT / "packaging" / notice).read_text(encoding="utf-8")


class LinuxNativeStateTests(unittest.TestCase):
    """Состояние toolchain/лицензий после нативного прогона на builder."""

    def test_toolchain_linux_pinned_others_honest(self) -> None:
        toolchain = json.loads(
            (PROJECT_ROOT / "packaging" / "toolchain.json").read_text(encoding="utf-8")
        )
        linux = toolchain["targets"]["linux-x86_64"]
        self.assertIsNotNone(linux["sqlite"], "linux SQLite-pin заполнен фактом сборки")
        self.assertTrue(linux["native_build_verified"])
        self.assertIsNotNone(linux["verified_on"])
        for other in ("windows-x86_64", "macos-x86_64", "macos-arm64"):
            entry = toolchain["targets"][other]
            # Pin заполнен только фактом диагностической инвентаризации
            # runtime python.org 3.14.8 на нативных runner'ах (все три
            # target показали SQLite 3.50.4 с FTS5), а не выдуман.
            self.assertEqual(
                entry["sqlite"], "3.50.4", f"{other}: pin по факту инвентаризации"
            )
            # Пин ≠ приёмка: реальные сборки/smoke этих target не выполнялись,
            # выпуск по-прежнему блокируется до native build + full smoke.
            self.assertFalse(entry["native_build_verified"])
            self.assertIsNone(entry["verified_on"])

    def test_pending_target_notes_are_neutral(self) -> None:
        """Notes pending-target не несут PII/путей scratch-окружения."""
        toolchain = json.loads(
            (PROJECT_ROOT / "packaging" / "toolchain.json").read_text(encoding="utf-8")
        )
        for other in ("windows-x86_64", "macos-x86_64", "macos-arm64"):
            note = str(toolchain["targets"][other].get("note", ""))
            for forbidden in (".superpowers", "/home/", "/tmp/", "C:\\", "spidesu"):
                self.assertNotIn(forbidden, note, f"{other}: note содержит {forbidden!r}")

    def test_cleared_license_components_have_notices(self) -> None:
        manifest = json.loads(
            (PROJECT_ROOT / "packaging" / "license-closure.json").read_text(encoding="utf-8")
        )
        cleared = 0
        for name, entry in manifest["components"].items():
            if entry.get("distribution_ok") is True:
                cleared += 1
                notice = entry.get("notice")
                self.assertIsInstance(notice, str, f"{name}: разрешён без уведомления")
                self.assertTrue(
                    (PROJECT_ROOT / "packaging" / notice).is_file(),
                    f"{name}: файл уведомления {notice} отсутствует",
                )
        self.assertGreater(cleared, 0, "linux-закрытие завершено — есть разрешённые компоненты")
        readline = manifest["components"]["readline-gpl"]
        self.assertFalse(readline["distribution_ok"], "GPL readline не разрешён автоматом")

    def test_required_notice_contents(self) -> None:
        """Уведомления содержат обязательные тексты, а не пустые файлы.

        Состав обязательных уведомлений статических частей CPython-runtime
        определён по фактической сборке (review native round 1): dtoa
        (David M. Gay/Lucent, Python/dtoa.c), getopt (David Gottner),
        cfuhash (Don Owens, Python/hashtable.c), qsbr (Jeffrey Roberson,
        FreeBSD), Secret Labs AB/Fredrik Lundh (Objects/unicodeobject.c);
        libpython принятой сборки содержит соответствующие символы.
        """
        manifest = json.loads(
            (PROJECT_ROOT / "packaging" / "license-closure.json").read_text(encoding="utf-8")
        )
        required = {
            "dtoa": ("David M. Gay", "Lucent Technologies", "entire notice"),
            "getopt": ("David Gottner", "DISCLAIM ALL WARRANTIES"),
            "cfuhash": ("Don Owens", "cfuhash", "Redistribution and use"),
            "qsbr": ("Jeffrey Roberson", "FreeBSD", "Redistribution and use"),
            "secret-labs": (
                "Copyright (c) 1999 by Secret Labs AB",
                "Copyright (c) 1999 by Fredrik Lundh",
                "supporting documentation",
                "DISCLAIMS ALL WARRANTIES",
            ),
            "cpython-runtime": ("PYTHON SOFTWARE FOUNDATION",),
            "sqlite": ("disclaims copyright",),
            "mpdecimal": ("Stefan Krah",),
            "hacl": ("INRIA",),
            "openssl": ("Apache License", "OpenSSL"),
            "zlib": ("zlib",),
            "bzip2": ("bzip2",),
            "xz": ("public domain",),
            "libffi": ("MIT",),
            "pyinstaller-bootloader": ("PyInstaller",),
        }
        components = manifest["components"]
        for name, needles in required.items():
            notice = components[name]["notice"]
            self.assertIsInstance(notice, str, name)
            text = (PROJECT_ROOT / "packaging" / notice).read_text(encoding="utf-8")
            for needle in needles:
                self.assertIn(needle, text, f"{name}: уведомление без {needle!r}")

    def test_secretlabs_notice_preserves_original_block(self) -> None:
        """Уведомление Secret Labs сохраняет исходный блок дословно (rereview 1).

        Разрешение требует «the above copyright notice … appears in all
        copies»: обе оригинальные copyright-строки (с годами) обязаны
        присутствовать в уведомлении вместе с полным текстом разрешения и
        дисклеймером — заголовок с именами авторов их не заменяет.
        """
        original_block = (
            "The original string type implementation is:\n"
            "\n"
            "  Copyright (c) 1999 by Secret Labs AB\n"
            "  Copyright (c) 1999 by Fredrik Lundh\n"
            "\n"
            "By obtaining, using, and/or copying this software and/or its\n"
            "associated documentation, you agree that you have read, understood,\n"
            "and will comply with the following terms and conditions:\n"
            "\n"
            "Permission to use, copy, modify, and distribute this software and its\n"
            "associated documentation for any purpose and without fee is hereby\n"
            "granted, provided that the above copyright notice appears in all\n"
            "copies, and that both that copyright notice and this permission notice\n"
            "appear in supporting documentation, and that the name of Secret Labs\n"
            "AB or the author not be used in advertising or publicity pertaining to\n"
            "distribution of the software without specific, written prior\n"
            "permission.\n"
            "\n"
            "SECRET LABS AB AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO\n"
            "THIS SOFTWARE, INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND\n"
            "FITNESS.  IN NO EVENT SHALL SECRET LABS AB OR THE AUTHOR BE LIABLE FOR\n"
            "ANY SPECIAL, INDIRECT OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES\n"
            "WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN\n"
            "ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT\n"
            "OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.\n"
            "--------------------------------------------------------------------\n"
        )
        notice = (
            PROJECT_ROOT / "packaging" / "licenses" / "secretlabs" / "NOTICE.txt"
        ).read_text(encoding="utf-8")
        self.assertIn(
            original_block,
            notice,
            "уведомление обязано содержать полный исходный блок Objects/unicodeobject.c",
        )

    def test_libpython_compound_clearance(self) -> None:
        """libpython требует разрешения всех статических частей; мутации блокируют.

        Отзыв любого компонента набора (dtoa/getopt/cfuhash/qsbr/
        secret-labs/cpython-runtime) обязан блокировать комплект с
        libpython — проверка состава, а не булева в файле.
        """
        import tempfile  # noqa: PLC0415

        from tests.test_packaging import _make_bundle_dir  # noqa: PLC0415

        from tools import build_portable as bp  # noqa: PLC0415

        manifest_path = PROJECT_ROOT / "packaging" / "license-closure.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        coverage_key = "_internal/libpython*.so*"
        components_names = manifest["coverage"][coverage_key]
        self.assertIn("cpython-runtime", components_names)
        for name in ("dtoa", "getopt", "cfuhash", "qsbr", "secret-labs"):
            self.assertIn(name, components_names, name)

        with tempfile.TemporaryDirectory() as tmp:
            bundle = _make_bundle_dir(Path(tmp) / "lib")
            (bundle / "_internal" / "libpython3.14.so.1.0").write_bytes(b"x")
            bp.verify_license_closure(bundle, target="linux-x86_64")  # всё разрешено

            for revoked in ("dtoa", "qsbr", "secret-labs"):
                mutated = json.loads(manifest_path.read_text(encoding="utf-8"))
                mutated["components"][revoked]["distribution_ok"] = False
                broken = Path(tmp) / f"manifest-{revoked}.json"
                broken.write_text(json.dumps(mutated), encoding="utf-8")
                with self.assertRaises(bp.PackagingError) as ctx:
                    bp.verify_license_closure(
                        bundle, target="linux-x86_64", manifest_path=broken
                    )
                self.assertIn(revoked, str(ctx.exception))
                self.assertIn("libpython", str(ctx.exception))

    def test_hacl_clearance_covers_md5_sha1(self) -> None:
        """_md5/_sha1 содержат HACL*-код и покрываются составным контрактом.

        Фактическая сборка: _md5…so содержит Hacl_Hash_MD5, _sha1…so —
        Hacl_Hash_SHA1 (review native round 1); их отзыв блокирует комплект.
        """
        import tempfile  # noqa: PLC0415

        from tests.test_packaging import _make_bundle_dir  # noqa: PLC0415

        from tools import build_portable as bp  # noqa: PLC0415

        manifest_path = PROJECT_ROOT / "packaging" / "license-closure.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        md5_key = "_internal/python3.14/lib-dynload/_md5.cpython-314-x86_64-linux-gnu.so"
        sha1_key = "_internal/python3.14/lib-dynload/_sha1.cpython-314-x86_64-linux-gnu.so"
        for key in (md5_key, sha1_key):
            components_names = manifest["coverage"][key]
            self.assertIn("cpython-runtime", components_names)
            self.assertIn("hacl", components_names)

        with tempfile.TemporaryDirectory() as tmp:
            bundle = _make_bundle_dir(Path(tmp) / "hash")
            for name in (md5_key, sha1_key):
                target = bundle / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"x")
            bp.verify_license_closure(bundle, target="linux-x86_64")

            mutated = json.loads(manifest_path.read_text(encoding="utf-8"))
            mutated["components"]["hacl"]["distribution_ok"] = False
            broken = Path(tmp) / "manifest-hacl.json"
            broken.write_text(json.dumps(mutated), encoding="utf-8")
            with self.assertRaises(bp.PackagingError) as ctx:
                bp.verify_license_closure(bundle, target="linux-x86_64", manifest_path=broken)
            self.assertIn("hacl", str(ctx.exception))
            self.assertTrue(
                "_md5" in str(ctx.exception) or "_sha1" in str(ctx.exception),
                "отзыв hacl обязан блокировать именно _md5/_sha1",
            )

    def test_gpl_readline_still_blocks_license_gate(self) -> None:
        """Комплект с readline остаётся заблокирован лицензионным гейтом."""
        from tests.test_packaging import _make_bundle_dir  # noqa: PLC0415

        from tools import build_portable as bp  # noqa: PLC0415

        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            bundle = _make_bundle_dir(Path(tmp) / "gpl")
            (bundle / "_internal" / "libreadline.so.8").write_bytes(b"x")
            with self.assertRaises(bp.PackagingError) as ctx:
                bp.verify_license_closure(bundle, target="linux-x86_64")
        self.assertIn("readline-gpl", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
