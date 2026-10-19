# Сборка keklib-cli

Сборочная документация: окружение разработки, сборка portable-архивов и
гейты конвейера. Все команды приведены в форме Linux и проверены на
Linux; непроверенные формы других платформ помечены явно — фиктивных
«проверенных» команд здесь нет. Сборка строго нативная: кросс-сборка
запрещена и отвергается инструментом до каких-либо действий.

## Окружение разработки (Linux, проверено)

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m keklib_cli --help
.venv/bin/python -m keklib_cli search --help
```

### Тестовые зависимости — до запуска тестов

Тесты сборочного tooling импортируют `packaging.requirements` (через
`tools/build_portable.py:lock_pins`). В свежем venv без этой зависимости
полный набор тестов падает, поэтому установка обязательна **до** unittest
(пин и хеши — точное подмножество `requirements-build.lock`, расхождение
блокируется тестом):

```bash
.venv/bin/python -m pip install -r packaging/requirements-test-tooling.lock
```

### Тесты и компиляция

```bash
.venv/bin/python -m unittest -v
.venv/bin/python -m compileall -q src tests tools packaging/entry.py packaging/builder/verify_runtime.py
```

Вспомогательный инструментарий:

- `tests/harness/tty.py` — нативный TTY-harness интеграционных тестов:
  POSIX PTY (`pty`/`select`); Windows — ConPTY через test-only `pywinpty`
  из `packaging/requirements-test-windows.lock` (нативная установка в
  Windows-среде разработки не выполнялась).
- `tools/verify_library.py` — явная приёмка реальной библиотеки
  публичными компонентами: собственный workdir, отчёт JSON, источники
  только читаются (байты INPX и выбранного ZIP сверяются до/после).

## Зависимости: runtime и build-time

- **Runtime** — только стандартная библиотека Python; конечному
  пользователю ничего устанавливать не нужно.
- **Build-time** — только на время сборки, ставятся в проектный `.venv`.
  Точные `==`-версии и SHA-256 всех дистрибутивов зафиксированы в
  `packaging/requirements-build.lock` (pyinstaller 6.22.3, altgraph
  0.17.5, pyinstaller-hooks-contrib 2026.8, packaging 26.3, setuptools
  84.0.0; `macholib`/`pefile`/`pywin32-ctypes` — маркеры своих ОС):

```bash
.venv/bin/python -m pip install -r packaging/requirements-build.lock
```

## Закреплённый toolchain и матрица target

`packaging/toolchain.json`: пин CPython сборки (3.14.8) и версий
build-зависимостей на каждый target; поле `sqlite` — пин версии SQLite
включённого runtime. **Null-пин SQLite блокирует выпуск target**
(release-гейт, см. ниже): пока версия не инвентаризована нативной
сборкой, значение не заполняется и выпуск невозможен. Флаг
`native_build_verified` отмечает target с завершённой нативной сборкой
и приёмкой.

| target | runner CI | формат | заявленный baseline сборки |
| --- | --- | --- | --- |
| `linux-x86_64` | `ubuntu-22.04` | tar.gz | Ubuntu 22.04, glibc >= 2.35 |
| `windows-x86_64` | `windows-2022` | zip | Windows Server 2022 |
| `macos-x86_64` | `macos-15-intel` | tar.gz | macOS 15 Intel |
| `macos-arm64` | `macos-14` | tar.gz | macOS 14 ARM64 |

Baseline — это проверяемая сборочная ОС, а не обещание более старых или
клиентских выпусков: такие заявления требуют отдельного нативного
прогона на целевой ОС и в этой документации не делаются.

## Нативная сборка portable на хосте

```bash
.venv/bin/python tools/build_portable.py --target linux-x86_64 --output ./build/portable-out
```

Стадии: preflight toolchain → PyInstaller onedir (`packaging/
keklib-cli.spec`, staging под `build/`) → документы и `build-info.json`
→ самопроверка реальной точки входа (`--help`, `setup`,
`index --refresh` с FTS5) → glibc-гейт → release-гейт метаданных →
лицензионный гейт → упаковка и публикация пары архив+сумма (гейты —
ниже). На хосте новее baseline glibc-гейт честно отказывает, перечисляя
нарушающие ELF-файлы, — для релизных сборок используйте baseline-билдер
следующего раздела. Существующие архивы и `.sha256` не перезаписываются,
чужой `--output` не очищается. Формы Windows/macOS (нативные runner'ы,
**не проверено**):

```powershell
# НЕ ПРОВЕРЕНО: выполняется на нативной Windows (runner windows-2022)
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m pip install -r packaging\requirements-build.lock
.venv\Scripts\python.exe tools\build_portable.py --target windows-x86_64 --output C:\build\portable-out
```

```bash
# НЕ ПРОВЕРЕНО: выполняется на нативной macOS; target выбирается строго
# по архитектуре машины: Intel → macos-x86_64, Apple Silicon → macos-arm64
# (runner'ы — в таблице выше; нативность-гейт запрещает сборку не на своей
# архитектуре/ОС)
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install -r packaging/requirements-build.lock
.venv/bin/python tools/build_portable.py --target macos-x86_64 --output ./build/portable-out
```

## Сборка linux-x86_64 в rootless-контейнере (проверено)

Воспроизводимый baseline-билдер — часть репозитория: пины
`packaging/builder/builder-pins.json` (digest образа Ubuntu 22.04,
исходники CPython/SQLite с хешами и независимыми сверками, apt-состав,
конфиг-флаги `--without-readline`/`--enable-fts5`/`--enable-shared`),
определение `packaging/builder/linux-x86_64.Containerfile`, проверки
`packaging/builder/verify_runtime.py` (fail-closed), драйвер
`tools/build_in_container.py`. Требования: rootless Podman и сеть для
загрузки образа/исходников/зависимостей (все хеши проверяются).
Проект монтируется только read-only, сборка идёт в одноразовой копии,
результат публикуется в один явный каталог вывода:

```bash
# Образ билдера (исходники скачиваются и проверяются при сборке образа)
.venv/bin/python tools/build_in_container.py --build-image

# Полная сборка комплекта (все гейты tools/build_portable.py)
.venv/bin/python tools/build_in_container.py --build-bundle --output ./build/portable-out

# Дым архива в чистом образе Ubuntu 22.04 без инструментов сборки:
# SHA-256, распаковка, --help, setup, index --refresh (FTS5)
.venv/bin/python tools/build_in_container.py \
    --smoke --archive ./build/portable-out/keklib-cli-<version>-linux-x86_64.tar.gz
```

Ограничение воспроизводимости: apt-срез Ubuntu не пинируется по датам —
образ фиксирует снимок `dpkg-query -W`, CONFIG_ARGS CPython и версии
pip-зависимостей (в `/opt/builder/` внутри образа).

## Гейты конвейера (все fail-closed)

1. Нативность target (кросс-сборка запрещена) и schema targets.json.
2. **Preflight toolchain** (`verify_toolchain_preflight`): фактический
   CPython обязан равняться пину `toolchain.json`, каждая применимая к
   платформе запись `requirements-build.lock` — быть установленной ровно
   в запиненной версии; расхождения toolchain.json ↔ lock — отказ.
3. PyInstaller onedir по spec в собственном staging под `build/`.
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
   архива. Состав закрытия linux-билдера: PSF (CPython + статические
   libmpdec/HACL*), public domain (SQLite, XZ), Apache-2.0 (OpenSSL),
   Zlib, BSD (bzip2), MIT (libffi, HACL*), исключение загрузчика
   PyInstaller; **GNU Readline (GPL-3.0) в сборку не входит** (builder
   отключает readline конфигом) и остаётся заблокированным компонентом
   манифеста.
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
каталог. Crash-transaction гарантия не реализуется и не заявляется.

## Изолированный smoke и выпускной гейт (test-only)

Инструменты выпуска не входят в пользовательскую поставку и не
импортируют `keklib_cli`:

- `tools/smoke_portable.py` — изолированная проверка готового архива:
  распаковка **только в новый собственный workdir** собственным
  распаковщиком без `extractall` (traversal/абсолютные пути/дубликаты/
  спецфайлы/жёсткие и внешние ссылки отвергаются до касания диска),
  прогон **абсолютного frozen executable** через нативный TTY (`--help`,
  `setup`, поиск с выбором номера, обновление INPX, повторный поиск,
  SHA-256 обоих извлечённых FB2), изоляция child-среды (PATH без python,
  без `PYTHON*`, собственные HOME/XDG/AppData/TMP), read-only комплекта
  с реальной пробой невозможности записи, состояние вне комплекта,
  неизменность источников; отчёт `smoke-report.json` со строгими bool.
  `minimum_os_verified` — наблюдение о хосте, а не условие smoke:
  отсутствие нативного подтверждения минимума блокируется гейтом, а не
  подменяется успехом прогона.

  Smoke сам строго проверяет собственную изоляцию: `find_spec(
  "keklib_cli")` в процессе harness обязан вернуть None (`source_checkout_absent`),
  поэтому его **нельзя** запускать окружением с установленным приложением —
  в частности, обычным dev-`.venv` с editable-установкой smoke честно
  откажет именно по этому наблюдению. Угловые заполнители в командах
  (`<архив>`, `<новый-каталог>`) замените фактическими значениями до
  выполнения; `--workdir` обязан ещё не существовать (существующий
  каталог отвергается без записи в него).

  Linux/POSIX — из checkout тем же интерпретатором venv, но с полной
  изоляцией импортов: `-I` игнорирует `PYTHONPATH`/переменные окружения
  и не добавляет каталог скрипта, `-S` отключает site-packages
  (editable-установка приложения недоступна); скрипт сам добавляет в
  `sys.path` только корень harness для импорта `tests.harness`:

  ```bash
  .venv/bin/python -I -S tools/smoke_portable.py \
      --archive ./build/portable-out/keklib-cli-<version>-linux-x86_64.tar.gz \
      --workdir ./build/smoke-workdir
  ```

  Пример с `-I -S` — только Linux/POSIX. На Windows harness требует
  test-only `pywinpty` (ConPTY) из `packaging/requirements-test-windows.lock`:
  создайте для smoke отдельный harness-venv, установите в него только
  этот lock и **не** устанавливайте туда keklib-cli — изоляция
  (`find_spec("keklib_cli") is None`) проверяется самим инструментом
  так же строго.

- `tools/release_gate.py` — выпускной гейт: ровно один успешный отчёт
  smoke на target (единая версия, host ОС/CPU == target, пересчитанное
  по фактам подтверждение заявленного минимума — более новая или
  серверная ОС доказательством не принимается; строгие bool без
  truthiness), архивы всех target со сверенными sidecar-суммами,
  согласованность `build-info.json` из архива и связь «отчёт ↔ архив»
  по digest, `SHA256SUMS` создаётся без перезаписи; публикация не
  выполняется. На неполной матрице target гейт честно отказывает.

  ```bash
  .venv/bin/python tools/release_gate.py --artifacts <каталог-архивов> --reports <каталог-отчётов>
  ```

## CI (механизм)

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

Зелёный source CI — это проверка исходников на матрице ОС/Python, а не
приёмка portable-сборок (для неё требуются нативные smoke-отчёты и
выпускной гейт). До инвентаризации SQLite сборки Windows/macOS в
portable-конвейере ожидаемо останавливаются release-гейтом на null-пине
— это честное поведение, а не сбой.

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

- `README.md` — назначение и доступность поставки.
- `INSTALL.md` — установка и первый запуск.
- `AGENTS.md` — правила проекта для разработчиков и агентов.
