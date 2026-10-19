# Сборка keklib-cli

Сборочная документация. Команды разработки проверены на нативном Linux
(CachyOS, glibc 2.44, CPython 3.14.8); **нативная portable-сборка
linux-x86_64 проверена в rootless-контейнере Podman на Ubuntu 22.04.5
(glibc 2.35)** — принятый архив и полный отчёт ниже. Всё, что не
проверено, помечено явно; фиктивных «проверенных» команд других
платформ здесь нет.

## Текущий статус

Конвейер portable-упаковки реализован и покрыт тестами: `packaging/`
(spec PyInstaller onedir/console, targets/toolchain, залоченные
сборочные зависимости, лицензионный манифест и уведомления, пины и
Containerfile baseline-билдера) и `tools/build_portable.py` (preflight
toolchain, сборка, самопроверка executable, glibc-гейт, release-гейт
метаданных runtime, лицензионный гейт, атомарная упаковка без
перезаписи, SHA-256, протокол публикации пары архив+сумма).

**Принятый portable-артефакт существует для `linux-x86_64`**
(финальная исправительная волна, 2026-10-18): сборка выполнена в
rootless-контейнере Podman на закреплённом digest'ом образе Ubuntu 22.04.5
с CPython 3.14.8 и SQLite 3.54.1 из upstream-исходников с проверенными
хешами; все гейты прошли, архив и контрольная сумма опубликованы, дым
распакованного комплекта выполнен в чистом образе Ubuntu 22.04 без
инструментов сборки. Продуктовые правки волны: установка src-layout
проекта в build-job CI (F1), точное зеркалирование лексики запроса и
фактического токенизатора FTS5 unicode61 с повышением `RULES_VERSION`
до 2 (F2), предметные `CatalogError` для повреждённого ZIP-контейнера
INPX (F3) и SIZE вне диапазона SQLite INTEGER 0..2**63-1 (F4),
best-effort ROLLBACK при отказе COMMIT поисковой сессии (F5). Windows
и macOS: нативных сборок нет, пины SQLite этих target остаются `null` и
`native_build_verified: false` — их выпуск до инвентаризации
заблокирован fail-closed гейтами (кросс-сборка запрещена). Изолированный
smoke принятого артефакта и выпускной гейт реализованы и выполнены на
Linux (задача 12) — см. раздел «Изолированный smoke и выпускной гейт».

Артефакт приёмки (локальный, не публикуется без отдельного разрешения);
сводная таблица — фактический состав последней принятой сборки:

```text
keklib-cli-0.1.0-linux-x86_64.tar.gz  (final wave, 2026-10-18)
  размер:      27 570 629 байт
  SHA-256:     a0d6eb6a29905ec38ae8a05113b5a364af19b897045797221722b28760910ea2
  build-info:  python 3.14.8, sqlite 3.54.1 (FTS5), pyinstaller 6.22.3,
               rules_version 2, minimum_glibc 2.35 (Ubuntu 22.04)
  состав:      keklib-cli (0755), _internal/, README.md, INSTALL.md,
               THIRD_PARTY_NOTICES.md, build-info.json, licenses/
               (16 уведомлений, включая обязательные уведомления
               статических частей CPython: dtoa/getopt/cfuhash/qsbr/
               Secret Labs с оригинальными copyright-строками)
```

Суперседированные архивы сохранены без изменений и принятием не
считаются: сборка задачи 11 fix round 2
(`/tmp/opencode/portable-linux-out-r2/…`, SHA-256 `8aec5847531064acdd56327c1d72fd122ae103528282023a74c116cb1ac91240`)
— предыдущая принятая ревизия без правок финальной волны F1–F5; первая
сборка
(`/tmp/opencode/portable-linux-out/…`, SHA-256 `86741449659d4f9e089eab908aa2258022e29ee91aacc77a4b611fbe4252b134`)
— без уведомлений статических частей CPython (review native round 1);
сборка fix round 1
(`/tmp/opencode/portable-linux-out-r1/…`, SHA-256 `3c6761bc6bf843ddad6ceac45fb8d8b64352e35967292f91fceacea1ece912bd`)
— в Secret Labs уведомлении отсутствовали оригинальные copyright-строки
(review native rereview 1). 46 из 47 ELF-файлов текущей сборки побайтно
идентичны r2 (идентичный toolchain); изменился только исполняемый файл
загрузчика PyInstaller, встроивший исправленный Python-код.

## Сборка portable linux-x86_64 в rootless-контейнере (проверено)

Baseline-билдер определён репозиторией: пины
`packaging/builder/builder-pins.json` (digest образа Ubuntu 22.04,
исходники CPython 3.14.8/SQLite 3.54.1 с хешами и независимыми сверками,
apt-состав, конфиг-флаги), определение
`packaging/builder/linux-x86_64.Containerfile`, проверки
`packaging/builder/verify_runtime.py`, драйвер
`tools/build_in_container.py`. Требования: rootless Podman, сеть для
загрузки образа/исходников/зависимостей (все загрузки внутри
контейнера, хеши проверяются). Проект монтируется только read-only,
сборка идёт в одноразовой копии внутри контейнера, результат
публикуется в один явный каталог вывода.

```bash
# Образ билдера (исходники скачиваются и проверяются при сборке образа)
TMPDIR=/tmp/opencode .venv/bin/python tools/build_in_container.py --build-image

# Полная сборка комплекта (все гейты tools/build_portable.py)
TMPDIR=/tmp/opencode .venv/bin/python tools/build_in_container.py \
    --build-bundle --output /tmp/opencode/portable-linux-out

# Дым архива в чистом образе Ubuntu 22.04 (без инструментов сборки):
# проверка SHA-256, распаковка, --help, setup, index --refresh (FTS5)
TMPDIR=/tmp/opencode .venv/bin/python tools/build_in_container.py \
    --smoke --output /tmp/opencode/portable-linux-out
```

Финальная волна собрана этой же командой с отдельным выводом
`--output /tmp/opencode/portable-linux-out-final` (новый own-каталог,
прежние каталоги не перезаписывались).

Фактический порядок нативной приёмки 2026-10-18 (все гейты без
обхода; полный вывод — в
`.superpowers/sdd/2026-02-11-keklib-cli-portable/task-11-native-report.md`):

1. прогон на состоянии «SQLite-pin ещё null»: preflight → onedir-сборка
   → самопроверка точки входа (`--help`, `setup`, `index --refresh` с
   FTS5) → **glibc-гейт floor 2.35: нарушений нет** → остановка
   release-гейтом на null-пине (чистый отказ, как задумано);
2. заполнение SQLite-pin фактом (3.54.1) → прогон 2: остановка
   лицензионного гейта с **полным фактическим инвентарём** комплекта;
3. лицензионное закрытие по этому инвентарю → прогон 3: все гейты
   пройдены, архив + `.sha256` опубликованы;
4. приёмка: `sha256sum -c` на хосте; дым в чистом `ubuntu:22.04`;
   независимая сверка `readelf --version-info` всех 47 ELF-файлов
   комплекта (максимум GLIBC_2.35 — совпало с собственным сканером
   гейта; маркеров `GLIBC_ABI_DT_RELR` нет).

Состав закрытия и пины: `sqlite: 3.54.1` + `native_build_verified:
true` только у `linux-x86_64` в `packaging/toolchain.json`; Windows/macOS
— без выдуманных значений (null блокирует их выпуск). Лицензионный
манифест закрыт по фактическому составу: PSF (CPython + статические
libmpdec/HACL*), public domain (SQLite, XZ), Apache-2.0 (OpenSSL),
Zlib, BSD (bzip2), MIT (libffi, HACL*), исключение загрузчика
PyInstaller; **GNU Readline (GPL-3.0) в сборку не входит** (builder
отключает readline конфигом и не ставит dev-пакет) и остаётся
заблокированным компонентом манифеста.

Ограничение воспроизводимости: apt-срез jammy не пинируется по датам —
вместо этого образ фиксирует снимок `dpkg-query -W`
(`/opt/builder/apt-versions.txt`, ключевые версии: gcc 11.4.0,
binutils 2.38, libssl3 3.0.2-0ubuntu1.30, libffi8 3.4.2-4,
libbz2-1.0 1.0.8-5ubuntu0.1, liblzma5 5.2.5-2ubuntu1.1); CONFIG_ARGS
CPython и версии pip-зависимостей — там же (`/opt/builder/`).

## Изолированный smoke и выпускной гейт (задача 12, проверено на Linux)

Test-only инструменты выпуска — не входят в пользовательскую поставку и
не импортируют `keklib_cli`:

- `tools/smoke_portable.py` — распаковка архива **только в новый
  собственный workdir** собственным распаковщиком без `extractall`
  (traversal/абсолютные пути/дубликаты/спецфайлы/жёсткие и внешние
  ссылки отвергаются до касания диска; внутренние относительные symlink
  Linux-onedir сохраняются), прогон **абсолютного frozen executable**
  через нативный TTY (`--help`, `setup`, поиск с выбором номера,
  обновление INPX, повторный поиск, SHA-256 обоих извлечённых FB2),
  изоляция child как наблюдаемых условий (PATH без python — проверено
  поиском исполняемых файлов и `shutil.which`; без `PYTHON*`; HOME/XDG/
  AppData в собственном sandbox), read-only комплекта с реальной пробой
  невозможности записи (`tests/harness/permissions.py`; запуск от root
  честно отказывает, а не «зелёнеет»), состояние вне комплекта,
  неизменность источников; отчёт `smoke-report.json` со строгими bool.
  `minimum_os_verified` — наблюдение о хосте, а не условие smoke:
  dev-хост может не быть baseline-ОС; отсутствие нативного подтверждения
  минимума блокируется гейтом, а не подменяется успехом прогона.
- `tools/release_gate.py` — `validate_reports` (ровно один успешный
  основной отчёт на target, единая версия, host ОС/CPU target,
  пересчитанное по фактам подтверждение заявленного минимума — более
  новая/серверная ОС доказательством не принимается; строгие bool без
  truthiness) и `--artifacts DIR` (архивы всех target по SHA-256 со
  sidecar-суммами, build-info.json из архива, связь «отчёт ↔ архив» по
  digest, `SHA256SUMS` без перезаписи и без какой-либо публикации).

Фактические прогоны 2026-10-18 на принятом артефакте
`8aec5847531064acdd56327c1d72fd122ae103528282023a74c116cb1ac91240`
(обе версии r1 и r2 после fix round 1 review: `passed=true`, все флаги
изоляции `true`, оба этапа `source_stage*_unchanged` `true`, FTS5,
состояние вне комплекта; шаги help/setup/search/search_after_update
— код 0). Актуальные доказательства — r2 (исправленные распаковщик и
двухэтапное наблюдение источников):

1. **Standalone smoke из harness-архива на dev-хосте** (CachyOS,
   `/usr/bin/python3 -I`, без checkout-импортов): harness r2 собран
   и распакован в чистый каталог, отчёт —
   `/tmp/opencode/keklib-t12/standalone-smoke-r2/smoke-report.json`;
   `minimum_os_verified=false` с честной причиной «хост не Ubuntu» —
   dev-хост не доказывает baseline-минимум.
2. **Baseline-смоук в контейнере** (rootless Podman, образ билдера на
   закреплённом digest Ubuntu 22.04.5, `--userns=keep-id` — прогон не от
   root, PTY внутри контейнера, python harness — `/opt/venv/bin/python
   -I`, проект не монтировался): отчёт —
   `/tmp/opencode/keklib-t12/container-smoke-r2/smoke/smoke-report.json`;
   `os_id=ubuntu`, `os_version_id=22.04`, `host_glibc=2.35` и
   **`minimum_os_verified=true`** («нативный baseline Ubuntu 22.04,
   glibc 2.35 >= 2.35») — это и есть нативное подтверждение заявленного
   минимума linux-x86_64.

Выпускной гейт на реальном комплекте из одного linux-архива
(копия артефакта + sidecar + отчёт baseline-смоука) — честный отказ:
`release-gate: ОТКАЗ: … нет архива keklib-cli-<version>-windows-x86_64.zip`,
код 1, `SHA256SUMS` не создаётся. Четыре Windows/macOS-прогона не
выполнялись — подмены их отчётов нет.

## CI (workflows определены, НЕ выполнялись)

`.github/workflows/ci.yml` (source-tests, reusable через
`workflow_call`): Linux/Windows/macOS × CPython 3.11/3.14, полный
unittest + compileall, test-only pywinpty из lock по маркеру платформы.
`.github/workflows/portable.yml` (`workflow_dispatch`): вызывает
source-tests, строит матрицу четырёх target **из targets.json**, нативно
собирает комплекты, пакует отдельный вспомогательный harness-архив и
запускает smoke-jobs **на новых машинах без checkout исходников**
(скачиваются только harness и пользовательский артефакт), затем гейт по
`needs` всех source/build/smoke — и только после успеха проверенные
артефакты и `SHA256SUMS`. Публичный GitHub Release и подписи не
создаются. Действия зафиксированы по commit SHA, права минимальные
(`contents: read`).

Workflows не запускались (нет авторизации на CI-инфраструктуру);
ожидаемо до инвентаризации SQLite: сборки Windows/macOS падают на
release-гейте null-пина, и конвейер остаётся красным — это честное
поведение, а не сбой. Нативные Windows (ConPTY) и macOS-прогоны smoke
не выполнялись.

## Гейты конвейера (все fail-closed)

1. Нативность target (кросс-сборка запрещена) и schema targets.json.
2. **Preflight toolchain** (`verify_toolchain_preflight`): фактический
   CPython обязан равняться пину `toolchain.json`, каждая применимая к
   платформе запись `requirements-build.lock` — быть установленной ровно
   в запиненной версии; расхождения toolchain.json ↔ lock — отказ.
3. PyInstaller onedir по spec в собственный staging под `build/`.
4. Документы и `build-info.json` (версии — фактические, floor — из
   targets.json).
5. Самопроверка executable реальной точкой входа (`--help`, `setup`,
   `index --refresh` с FTS5) в изолированном sandbox-окружении.
6. **glibc-гейт** (Linux): требования всех ELF-файлов комплекта читаются
   по семантике ELF — динамический сегмент (`PT_DYNAMIC`/`DT_VERNEED`),
   при его отсутствии секции типа `SHT_GNU_verneed` независимо от имени
   секции; строгие цепочки `vn_cnt`/`vn_aux`/границы; усечённые,
   неподдерживаемые (ELF32/MSB) и не имеющие таблиц ELF, повреждённые
   цепочки, неизвестные маркеры версий и `GLIBC_PRIVATE` — отказ, не
   пропуск; `GLIBC_ABI_DT_RELR` учитывается как glibc ≥ 2.36; провайдер
   verneed не может скрыть требование GLIBC; превышение floor 2.35
   останавливает публикацию списком файлов. Независимая сверка с
   `readelf --version-info` покрыта тестами.
7. **Release-гейт метаданных**: null SQLite-pin блокирует выпуск;
   запиненное значение сверяется с фактическим SQLite runtime и
   build-info.json; python/pyinstaller в build-info обязаны равняться
   пинам.
8. **Лицензионный гейт** (`verify_license_closure`): инвентарь
   фактических библиотек комплекта (executable, `base_library.zip`,
   верхнеуровневые `.so*`/`.dll`/`.dylib` из `_internal`) сверяется с
   `license-closure.json`: полное покрытие, явное `distribution_ok`,
   существующие файлы уведомлений; уведомления копируются в `licenses/`
   архива.
9. Упаковка: allowlist корня (исполнимый файл, `_internal`, README.md,
   INSTALL.md, THIRD_PARTY_NOTICES.md, build-info.json, опционально
   `licenses/`), полный обход дерева с проверкой типов записей и
   containment символических ссылок/reparse-точек (внешние, битые и
   покидающие комплект ссылки запрещены; специальные файлы запрещены);
   tar хранит внутренние ссылки с относительным именем, ZIP
   материализует внутренние ссылки файлов байтами цели (посторонние
   байты в архив попасть не могут), ссылки на каталоги в ZIP
   непредставимы и отвергаются; биты исполнения нормализуются
   (исполнимый файл 0755).

## Протокол публикации пары «архив + .sha256»

Оба временных файла готовятся и fsync-ятся **до** первой публикации;
затем атомарно без перезаписи (`publish_no_replace`) публикуется архив и
рядом контрольная сумма. **Опубликованный архив никогда не удаляется** —
ни при отказе публикации суммы, ни при сбое процесса: равенство байтов
SHA-256 не доказывает владение файлом (чужая same-byte замена с другим
inode неотличима), а удаление чужой записи недопустимо. При отказе
второй публикации диагностика сообщает фактический digest и явную
команду ремонта:

```bash
.venv/bin/python tools/build_portable.py \
    --repair-checksum <путь-к-архиву> --expected-sha256 <digest>
```

Ремонт проверяет, что путь — обычный файл, байты совпадают с digest, и
создаёт отсутствующую соседнюю сумму атомарно без перезаписи; удалений
нет, существующая (в т.ч. чужая) сумма не трогается. Операция
восстанавливает парность файлов, но не подтверждает происхождение
архива и не является приёмом выпуска. **Пара не транзакционна при сбое
процесса** между двумя публикациями: может остаться архив без суммы;
повторная сборка откажется перезаписывать существующий архив — сумма
дописывается приведённой командой ремонта либо используется новый
каталог. Crash-transaction гарантия не реализована и не заявляется.

## Матрица target (packaging/targets.json)

| target | runner CI | формат | baseline |
| --- | --- | --- | --- |
| `linux-x86_64` | `ubuntu-22.04` | tar.gz | Ubuntu 22.04, glibc >= 2.35 (проверяется ELF-сканером по итоговому bundle и подтверждён нативным baseline-смоуком в контейнере Ubuntu 22.04.5) |
| `windows-x86_64` | `windows-2022` | zip | Windows Server 2022 (сборочная ОС; клиентские минимумы не проверялись) |
| `macos-x86_64` | `macos-15-intel` | tar.gz | macOS 15 Intel (сборочная ОС; не проверялась) |
| `macos-arm64` | `macos-14` | tar.gz | macOS 14 ARM64 (сборочная ОС; не проверялась) |

Это исходные проверяемые baseline сборочных машин, а не обещание поддержки
более старых/клиентских ОС без отдельного native-прогона. Клетки
«не проверялась»/«не проверялись» означают отсутствие нативных прогонов —
никаких предположений о клиентских минимумах эти строки не содержат.

## Окружение разработки (Linux, проверено)

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m keklib_cli --help
.venv/bin/python -m keklib_cli search --help
```

Тесты (синтетические библиотеки во временных каталогах):

```bash
TMPDIR=/tmp/opencode .venv/bin/python -m unittest -v
```

Компиляция всех исходников и инструментов:

```bash
TMPDIR=/tmp/opencode .venv/bin/python -m compileall -q src tests tools packaging/entry.py
```

Нативный TTY-инструментарий: `tests/harness/tty.py` (POSIX PTY —
`pty`/`select`; Windows — ConPTY через test-only `pywinpty` из
`packaging/requirements-test-windows.lock`; нативная Windows-установка —
задача 12). Приёмка реальной библиотеки — `tools/verify_library.py`
(измерения — в `README.md`).

## Сборочные зависимости portable (Linux, проверено)

Только build-time, устанавливаются в проектный `.venv`; конечному
пользователю не нужны. Точные `==`-версии и SHA-256 всех дистрибутивов
зафиксированы в `packaging/requirements-build.lock` (по данным PyPI JSON
API на 2026-10-18):

```bash
.venv/bin/python -m pip install -r packaging/requirements-build.lock
```

Фактически установлено и проверено: `pyinstaller==6.22.3`,
`altgraph==0.17.5`, `pyinstaller-hooks-contrib==2026.8`,
`packaging==26.3`, `setuptools==84.0.0` (венёвские `macholib`,
`pefile`, `pywin32-ctypes` — только на своих ОС). Закреплённый
интерпретатор сборки — CPython 3.14.8 (`packaging/toolchain.json`,
поле `python`); версия SQLite включённого runtime фиксируется по факту
сборки в `build-info.json` каждого комплекта (в пройденном прогоне —
3.54.0 с FTS5).

## Нативная сборка portable (Linux, проверена до glibc-гейта)

```bash
TMPDIR=/tmp/opencode .venv/bin/python tools/build_portable.py \
    --target linux-x86_64 --output /tmp/opencode/portable-out
```

Команда проверена на текущем Linux-хосте. Стадии и фактический результат
прогона 2026-10-18 (полный перечень гейтов — выше):

1. preflight toolchain и проверка нативности target — ок;
2. PyInstaller onedir по `packaging/keklib-cli.spec` в собственном
   staging под `build/` — ок;
3. README.md/INSTALL.md/THIRD_PARTY_NOTICES.md и `build-info.json` в
   корень комплекта — ок;
4. самопроверка executable через реальную точку входа (`--help`,
   `setup`, `index --refresh` с FTS5-индексацией синтетической библиотеки
   в изолированном sandbox) — ок;
5. **glibc-гейт: отказ, код 1.** Сканер требований всех ELF-файлов
   комплекта нашёл GLIBC 2.36–2.44 выше floor 2.35 (включая
   `libpython3.14.so.1.0` → 2.38 и `libsqlite3.so.0` → 2.44). Архив не
   создаётся, вывод перечисляет каждый файл; полный вывод прогона — в
   `.superpowers/sdd/2026-02-11-keklib-cli-portable/task-11-report.md`.

Гейт намеренно fail-closed: неизвестное имя версии GLIBC (например,
`GLIBC_ABI_DT_RELR`, учитывается как glibc >= 2.36; `GLIBC_PRIVATE` —
отказ), нечитаемый/усечённый ELF и повреждённые цепочки останавливают
сборку, а не пропускаются молча. Существующий архив и `.sha256` не
перезаписываются, чужой `--output` не очищается. На floor-совместимом
builder выпуск дополнительно остановили бы release-гейт метаданных
(null SQLite-pin) и лицензионный гейт — это заявленные blockers, а не
случайные отказы.

### Windows/macOS

Проверенных команд нет: нативного доступа к этим ОС из текущего окружения
не было. Форма команд (PowerShell) для задачи 12, **не проверена**:

```powershell
# НЕ ПРОВЕРЕНО: выполняется на нативной Windows (runner windows-2022)
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m pip install -r packaging\requirements-build.lock
.venv\Scripts\python.exe tools\build_portable.py --target windows-x86_64 --output C:\build\portable-out
```

macOS (zsh, для runner macos-15-intel / macos-14), **не проверено**:

```bash
# НЕ ПРОВЕРЕНО: выполняется на нативной macOS целевой архитектуры
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install -r packaging/requirements-build.lock
TMPDIR=${TMPDIR:-/tmp} .venv/bin/python tools/build_portable.py \
    --target macos-x86_64 --output ./build/portable-out
```

Эти шаблоны заполняются проверенными командами после реальных нативных
прогонов (задача 12); создание spec-файла готовой сборкой платформы не
считается.

## Состав portable-архива

Корень архива — `keklib-cli/`: исполняемый файл (`keklib-cli` /
`keklib-cli.exe`), `_internal/` (runtime PyInstaller onedir), `README.md`,
`INSTALL.md`, `THIRD_PARTY_NOTICES.md`, `build-info.json` (version,
target, python, sqlite, pyinstaller, schema_version, rules_version,
minimum_os, minimum_glibc) и, по факту завершённого лицензионного
закрытия, `licenses/` с текстами уведомлений. Формат: tar.gz (Linux/macOS,
биты исполнения нормализуются: исполняемый файл — 0755, внутренние
символические ссылки хранятся ссылками с относительной целью) или zip
(Windows, внутренние ссылки файлов материализуются байтами цели).
Рядом с архивом — `<archive>.sha256` в формате `sha256sum`. Книги, INPX,
индексы, настройки, тесты и секреты в архив не входят и отвергаются
allowlist-ом и containment-проверками упаковщика.

## См. также

- `README.md` — назначение и статус проекта.
- `INSTALL.md` — установка (статус и планируемая форма).
- `AGENTS.md` — правила проекта для разработчиков и агентов.
