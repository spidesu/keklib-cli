# Уведомления о сторонних компонентах keklib-cli

Этот файл включается в каждый portable-архив keklib-cli рядом с
`README.md` и `INSTALL.md`. Он не является исполнением лицензионных
условий сам по себе: фактическое закрытие каждого выпуска проверяется
машинным гейтом `verify_license_closure` в `tools/build_portable.py` по
манифесту `packaging/license-closure.json` и файлам уведомлений в
`packaging/licenses/`; уведомления разрешённых компонентов копируются
в `licenses/` внутри архива.

## Как устроено закрытие

- Манифест `packaging/license-closure.json` (schema 2) перечисляет
  компоненты с их лицензиями, файлами уведомлений и явным булевым
  `distribution_ok`; контракт `coverage` сопоставляет каждый файл
  фактического инвентаря одному компоненту **или набору компонентов** —
  составные файлы (исполнимый файл = собственный код + встроенный
  загрузчик PyInstaller; `_decimal` = CPython + libmpdec;
  `_sha2`/`_sha3`/`_blake2` = CPython + HACL*; `_hashlib`/`_ssl` =
  CPython + OpenSSL) требуют разрешения каждой включённой части.
- Сборка рекурсивно инвентаризирует **фактический** состав комплекта
  (исполнимый файл, `base_library.zip`, все `.so*`/`.dll`/`.dylib`/
  `.pyd`/`.exe` на любой глубине в любом регистре имени, а также файлы
  с binary-magic без библиотечного расширения — ELF, Mach-O, включая
  extensionless framework-runtime macOS, и PE) и отказывается
  публиковать архив, пока хотя бы одна позиция не покрыта, не
  подтверждена или ссылается на отсутствующий файл уведомления.
  Неизвестная/вложенная библиотека или бинарь блокирует выпуск;
  компонент без явного `distribution_ok: true` блокирует выпуск.

## Фактический состав принятой сборки linux-x86_64

Native-приёмка 2026-10-18 (rootless-билдер Ubuntu 22.04.5, glibc 2.35;
CPython 3.14.8 из upstream-исходника python.org; SQLite 3.54.1 из
upstream-исходника sqlite.org; PyInstaller 6.22.3). Комплект включает
только перечисленное ниже; полные тексты уведомлений — в `licenses/`
архива:

| Компонент | Лицензия | Что в комплекте |
| --- | --- | --- |
| keklib-cli (собственный код) | условия владельца проекта | исполнимый файл `keklib-cli` |
| PyInstaller bootloader | GPL с исключением для упакованных приложений | загрузчик, встроенный в исполнимый файл |
| CPython 3.14.8 | PSF License (включая исторические разделы CWI/CNRI/BeOpen/ZERO-CLAUSE BSD) | `libpython3.14.so.1.0`, модули `lib-dynload`, stdlib в `base_library.zip` |
| dtoa (David M. Gay / Lucent Technologies) | ISC-style notice (полное уведомление обязательно в копиях и документации) | статически в `libpython` (`_Py_dg_dtoa`/`_Py_dg_strtod`) |
| getopt (David Gottner) | permissive notice + дисклеймер | статически в `libpython` (`_PyOS_GetOpt`) |
| cfuhash (Don Owens) | BSD | статически в `libpython` (`_Py_hashtable_*`) |
| qsbr (Jeffrey Roberson, FreeBSD) | BSD-2 | статически в `libpython` (`_Py_qsbr_*`) |
| Secret Labs AB / Fredrik Lundh | permissive notice | оригинальный строковый тип, статически в `libpython` |
| SQLite 3.54.1 (+FTS5) | public domain | `libsqlite3.so`, `_sqlite3` |
| OpenSSL 3.0.2 | Apache-2.0 | `libcrypto.so.3`, `libssl.so.3`, `_hashlib`, `_ssl` |
| zlib 1.2.11 | Zlib License | `libz.so.1`, `zlib` |
| bzip2 1.0.8 | BSD (bzip2) | `libbz2.so.1.0`, `_bz2` |
| XZ Utils 5.2.5 | public domain | `liblzma.so.5`, `_lzma` |
| libffi 3.4.2 | MIT | `libffi.so.8`, `ctypes` |
| libmpdec 2.5.1 (vendored) | BSD-2-Clause | статически в `_decimal` |
| HACL* (vendored) | MIT | статически в `_sha2`/`_sha3`/`_blake2`/`_md5`/`_sha1` |

Статические части CPython определены по фактической сборке: объекты,
скомпилированные в `libpython` (Python/dtoa.c, Python/getopt.c,
Python/hashtable.c, Python/qsbr.c, Objects/unicodeobject.c — Secret
Labs), подтверждены символами в комплекте; `Python/dynamic_annotations.c`
(Google BSD) в `libpython` не входит; `getaddrinfo/getnameinfo` (WIDE
Project) не компилируются (системный getaddrinfo); pyexpat/_elementtree
(Secret Labs/expat) в комплект не попадают. Полные тексты всех
обязательных уведомлений — в `licenses/` архива.

В сборку **намеренно не входят** (и их появление блокирует выпуск):
GNU Readline (GPL-3.0 — builder отключает readline конфигом
`--without-readline` и не ставит dev-пакет), ncurses, gdbm, tkinter,
zstd, brotli. Точная версия каждого фактического runtime — в
`build-info.json` этого архива.

## Подготовленное закрытие windows-x86_64 / macos-x86_64 / macos-arm64

Фактический состав будущих комплектов этих target известен из
диагностических инвентаризаций runtime python.org CPython 3.14.8 на
соответствующих сборочных ОС (Windows Server 2022, macOS 15 Intel,
macOS 14 Apple Silicon; SQLite 3.50.4 с FTS5, OpenSSL 3.5.9). На этих
фактах подготовлены platform-компоненты манифеста и полные тексты
уведомлений; нативные сборки и изолированные smoke этих target ещё не
выполнялись — каждый выпуск по-прежнему проверяется машинным гейтом по
фактическому дереву, и никакое заявление о принятом выпуске здесь не
делается.

- **windows-x86_64** (`python314.dll` + `.pyd`-модули + `base_library.zip`
  из официального дистрибутива python.org 3.14.8 x64): CPython PSF +
  условия Microsoft Distributable Code этого дистрибутива
  (`licenses/cpython-win/LICENSE.txt`,
  `licenses/ms-vcrt/NOTICE.txt` — VCRUNTIME140*.dll); статически
  вкомпилированные zlib-ng 2.2.4 (Zlib License), libmpdec 4.0.0 (BSD-2),
  liblzma из XZ Utils 5.2.5 (public domain), bzip2 1.0.8 (BSD) —
  `licenses/zlib-ng/`, `licenses/mpdecimal/COPYRIGHT-4.0.0.txt`,
  `licenses/xz/COPYING-5.2.5.txt`, `licenses/bzip2/LICENSE-1.0.8.txt`;
  отдельные `libcrypto-3.dll`/`libssl-3.dll` (OpenSSL 3.5.9,
  Apache-2.0), `sqlite3.dll` (SQLite 3.50.4, public domain),
  `libffi-8.dll` (libffi 3.4.4, MIT) — `licenses/openssl/`,
  `licenses/sqlite/PUBLIC-DOMAIN.txt`, `licenses/libffi/`. Системные
  DLL Windows (api-ms-*, ucrtbase, JDK-библиотеки) в комплект не входят
  и появлением блокируют выпуск.
- **macos-x86_64 / macos-arm64** (framework-сборка python.org
  CPython 3.14.8): CPython PSF (`licenses/cpython-mac/LICENSE.txt`);
  статические части исходника CPython — dtoa/getopt/cfuhash/qsbr/
  Secret Labs, libmpdec 2.5.1, HACL*, а также статически вкомпилированные
  SQLite 3.50.4, liblzma (XZ Utils 5.2.5), Expat (MIT); отдельные
  `libcrypto.3.dylib`/`libssl.3.dylib` (OpenSSL 3.5.9) и
  `libzstd.1.dylib` (zstd 1.5.7, BSD-3). Системные библиотеки macOS
  (`libz`, `libbz2`, `libffi`, `libSystem`, `libedit`, фреймворки)
  линкуются динамически и в комплекте не распространяются. Модуль
  `readline` macOS использует системную `libedit` (не GNU Readline);
  появление реального GNU Readline-бинаря в комплекте любого target
  блокирует выпуск.

## Границы этого документа

- Закрытие Linux относится к фактическому составу принятой сборки
  linux-x86_64; закрытие Windows/macOS подготовлено по фактическим
  инвентаризациям runtime и подтверждается гейтом только при реальной
  сборке каждого target (до этого их выпуск заблокирован).
- Файл — уведомление о компонентах и механизме закрытия, а не
  юридическая консультация. Полные тексты — в `licenses/` архива и в
  исходниках соответствующих компонентов (исходники CPython/SQLite
  Linux-сборки идентифицированы хешами в
  `packaging/builder/builder-pins.json`; provenance platform-текстов —
  в `packaging/license-closure.json`).
- Контрольная сумма SHA-256 архива подтверждает целостность данных,
  но не заменяет доверенную цифровую подпись (см. `INSTALL.md`).
