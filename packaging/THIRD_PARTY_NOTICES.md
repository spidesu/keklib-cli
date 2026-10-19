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
  `.pyd` на любой глубине, файлы с ELF-magic) и отказывается
  публиковать архив, пока хотя бы одна позиция не покрыта, не
  подтверждена или ссылается на отсутствующий файл уведомления.
  Неизвестная/вложенная библиотека блокирует выпуск; компонент без
  явного `distribution_ok: true` блокирует выпуск.

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

## Границы этого документа

- Закрытие относится к фактическому составу **этой** сборки
  linux-x86_64; составы Windows/macOS-сборок не инвентаризировались
  (их выпуск до инвентаризации заблокирован гейтами).
- Файл — уведомление о компонентах и механизме закрытия, а не
  юридическая консультация. Полные тексты — в `licenses/` архива и в
  исходниках соответствующих компонентов (исходники CPython/SQLite
  идентифицированы хешами в `packaging/builder/builder-pins.json`).
- Контрольная сумма SHA-256 архива подтверждает целостность данных,
  но не заменяет доверенную цифровую подпись (см. `INSTALL.md`).
