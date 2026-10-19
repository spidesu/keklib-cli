# -*- mode: python ; coding: utf-8 -*-
"""Spec PyInstaller для portable-комплекта keklib-cli (задача 11 плана).

Форма — onedir + console: EXE с ``exclude_binaries=True`` вместе с COLLECT
дают каталог ``keklib-cli/`` с ``_internal/`` рядом с исполняемым файлом.
onefile и GUI-варианты не используются (§2.1 спецификации: onefile,
установщики и оконный интерфейс не входят в первую версию). Платформенные
модули ``keklib_cli.platforms`` и ``_sqlite3``/``ctypes`` перечислены
явно: диспетчеризация платформенного слоя идёт по ``sys.platform`` и не
должна зависеть от эвристики статического анализа. FTS5 проверяется не
здесь, а прогоном готового executable по синтетической библиотеке в
``tools/build_portable.py`` (индексация использует FTS5-таблицу).
"""

from pathlib import Path

_spec_dir = Path(SPECPATH)
_project_root = _spec_dir.parent

a = Analysis(
    [str(_spec_dir / "entry.py")],
    pathex=[str(_project_root / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[
        "keklib_cli.platforms.paths",
        "keklib_cli.platforms.posix",
        "keklib_cli.platforms.windows",
        "sqlite3",
        "_sqlite3",
        "ctypes",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "pytest", "tkinter", "pydoc_data"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="keklib-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesize_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="keklib-cli",
)
