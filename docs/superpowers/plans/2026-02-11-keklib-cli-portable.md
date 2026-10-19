# keklib-cli — план реализации portable standalone

> **Для агента-исполнителя:** обязательный навык — `subagent-driven-development` или `executing-plans`, по выбору пользователя. Выполнять задачи последовательно; шаги отмечаются `- [ ]`. Прочитать этот план и спецификацию до изменения кода.

**Цель:** реализовать нативный CLI для Linux, Windows и macOS: поиск в обновляемом INPX, выбор книги по номеру и безопасное извлечение FB2; поставлять portable-архивы без требования установить Python.

**Архитектура:** общая Python-логика CLI, INPX, SQLite FTS5 и ZIP отделена от платформенных путей, блокировок и публикации файлов. Читатели удерживают shared-блокировку до закрытия SQLite; пересборка требует exclusive-блокировки и атомарной замены закрытой базы. PyInstaller `onedir` упаковывает тот же entry point, без второй реализации приложения.

**Стек:** исходный код — CPython ≥3.11, SQLite FTS5 и стандартная библиотека; сборка — setuptools и PyInstaller; Windows TTY-тесты — отдельная test-only зависимость `pywinpty`. Версии сборочных инструментов и их зависимостей фиксируются в задаче 11 после проверки совместимости выбранного toolchain; runtime приложения не зависит от этих установленных пакетов.

**Спецификация:** [2026-02-11-keklib-cli-design.md](../specs/2026-02-11-keklib-cli-design.md), пересмотренная редакция подтверждена пользователем. Этот план заменяет прежний Linux-план [2026-02-11-keklib-cli.md](2026-02-11-keklib-cli.md), который исполнять нельзя.

## Общие ограничения

- Пользователь выбрал `subagent-driven-development`. Все implementation/fix subagents используют только `zai-coding-plan/glm-5.3`; все task/re-review/final-review subagents — только `openai/gpt-6.1-sol`. Модель задаётся явно в каждом dispatch. Автоматическая замена, Flash/Highspeed/Fast-варианты и смена модели при escalation без разрешения пользователя запрещены. Закрепить это правило в корневом `AGENTS.md` и execution ledger.
- Название команды и дистрибутива — `keklib-cli`, Python-пакет — `keklib_cli`; каталоги состояния также используют `keklib-cli`.
- Исходные INPX и ZIP открываются только для чтения.
- Основное распространение — portable standalone: архив с исполняемым файлом и его зависимостями, без установки Python конечным пользователем.
- В корне исходного проекта обязательны `README.md`, `BUILD.md`, `INSTALL.md` и `AGENTS.md`; документация и инструкции составляются на русском языке.
- Исходный код поддерживает CPython 3.11 или новее; portable-сборка содержит свой фиксированный Python runtime и SQLite с FTS5.
- В поисковый индекс входят только записи с `DEL`, равным строке `0`, и `EXT=fb2` без учёта регистра.
- Результаты выдаются страницами по 20 книг.
- Нормализация: Unicode NFKC, `casefold()`, замена `ё` на `е`.
- Все слова запроса обязательны, каждое ищется как префикс слова в авторе или названии.
- Разные идентичности не объединяются даже при совпадении автора и названия.
- При ошибке, нехватке места или отмене старый индекс сохраняется.
- Не использовать `extractall`, не создавать пути из имён ZIP-записей.
- Начальные артефакты: `linux-x86_64.tar.gz`, `windows-x86_64.zip`, `macos-x86_64.tar.gz`, `macos-arm64.tar.gz`; имя начинается с `keklib-cli-<version>-`.
- Общие модули не импортируют безусловно `fcntl`, `msvcrt`, `pty` или `resource`. Нативные тесты нельзя заменять mock-тестами другой ОС.
- В первую версию не входят `onefile`, установщики, автообновление, доверенные сертификаты и Apple notarization. macOS ad-hoc подпись, если нужна для запуска, выполняется при сборке.

## Особое внимание при проверке

1. Пользователь переместил portable-каталог или сделал его read-only: настройки/индекс остаются в нативных пользовательских каталогах, программа не ищет их рядом с executable. Задачи 2, 11, 12.
2. Windows-имена, drive-relative/UNC-пути, ESC/bidi в метаданных и коллизии регистра/Unicode: одинаковые проверки на всех ОС и отсутствие перезаписи. Задачи 3, 7, 8, 9.
3. Активный читатель Windows, авария писателя или изменение INPX во время сборки: никакого удаления/замены открытой базы, зависшей блокировки или молчаливого использования устаревшего индекса. Задачи 2, 5, 6.
4. INPX пополнился раньше ZIP либо файл исчез после поиска: показать точную недоступную пару ZIP/member, не подбирать другое издание. Задачи 7, 9, 10.
5. Сборка случайно использует Python/checkout разработчика, потеряла FTS5 либо не соответствует CPU: проверять распакованный артефакт в изоляции на его собственной ОС/архитектуре. Задачи 11, 12.

## Размещение и правила исполнения

Предлагаемый новый проект — `/home/user/opencode/keklib-cli`; путь требует подтверждения вместе с планом. Не создавать его на стадии планирования. Перед исполнением проверить существование каталога и инструкции `AGENTS.md` его родителей; если там появились чужие данные, остановиться, не затирать. Новый Git-репозиторий, если он нужен, создаётся только в подтверждённом проекте, не в каталоге библиотеки. Worktree существующего репозитория создаётся через `using-git-worktrees` только под `/home/user/opencode/`.

В новый проект копируются актуальная спецификация, этот план и прежний помеченный как исторический план с сохранением путей `docs/superpowers/`, чтобы ссылки документов оставались рабочими. Создание проекта, окружения и установка сборочных зависимостей разрешаются только после подтверждения исполнения. Публикация репозитория, удалённый push, запуск платного CI и публикация публичного релиза требуют отдельного разрешения; написание CI-конфигурации не означает наличия доступных runners.

Все пути ниже относительны будущему проекту. Команды `python ...` означают Python активного проектного venv: POSIX — `python3 -m venv .venv`, затем `. .venv/bin/activate`; PowerShell — `py -3.14 -m venv .venv`, далее можно обращаться напрямую к `.venv\Scripts\python.exe` без изменения execution policy. В CI используется выбранный Python runner. На текущем Linux для временных тестовых данных задавать `TMPDIR=/tmp/opencode`; на других ОС использовать `tempfile`, не жёсткий Linux-путь.

После задачи 1 пакет установлен editable. Общая проверка после каждой задачи: `python -m unittest discover -s tests -t . -v`, ожидается `OK`; платформенные проверки исполняются на своей ОС. Отсутствие Windows/macOS runner фиксируется как непроверенная платформа, а не как успех. Каждый коммит содержит только перечисленные файлы задачи и собственные изменения.

## Структура проекта

```text
README.md, BUILD.md, INSTALL.md, AGENTS.md  # Корневые русскоязычные документы
pyproject.toml, .gitignore                 # Упаковка Python и локальные артефакты
src/keklib_cli/
  __init__.py, __main__.py, errors.py, models.py
  config.py                               # Проверка/сохранение настроек
  inpx.py                                 # Структура INPX, строки, серии
  text.py, index_store.py                  # Нормализация и неизменяемая SQLite
  indexer.py, search.py                    # Пересборка и поисковая сессия
  archive.py, output.py                    # Чтение ZIP и итоговый FB2
  terminal.py, cli.py                      # Нумерованный UI и команды
  platforms/
    __init__.py, paths.py                  # Диспетчер и нативные пути
    posix.py, windows.py                   # Блокировки, no-replace, память
tests/
  __init__.py, fixtures.py
  test_config.py, test_platforms.py, test_inpx.py
  test_index_store.py, test_indexer.py, test_search.py
  test_archive.py, test_output.py, test_terminal.py, test_cli.py
  test_integration.py, test_packaging.py, test_release.py
  harness/__init__.py, library_fixture.py, tty.py, permissions.py
tools/
  verify_library.py                        # Явная приёмка реального архива
  build_portable.py, smoke_portable.py     # Сборка/изолированная проверка
  release_gate.py                          # Четыре отчёта → SHA256SUMS
packaging/
  entry.py, keklib-cli.spec                # Один CLI entry point, onedir
  targets.json, toolchain.json             # ОС/CPU, проверяемые baseline и версии
  requirements-build.lock                 # Зафиксированные сборочные зависимости
  requirements-test-windows.lock           # Только Windows console harness
  THIRD_PARTY_NOTICES.md                   # Уведомления включённых компонентов
.github/workflows/ci.yml, portable.yml
docs/superpowers/specs/2026-02-11-keklib-cli-design.md
docs/superpowers/plans/2026-02-11-keklib-cli-portable.md
```

Зависимости задач: `1 → 2 → 3 → 4 → 5 → 6`, `1+2 → 7 → 8`, затем `9 → 10 → 11 → 12`. Реализация последовательная: общие контракты не меняются конкурентно. Новые обязанности не добавлять в CLI вместо профильных модулей.

### Задача 1. Модели, конфигурация и основа команды

**Файлы:** создать `pyproject.toml`, `.gitignore`, `src/keklib_cli/{__init__,__main__,errors,models,config,cli}.py`, `tests/{__init__,fixtures,test_config}.py`, `tests/harness/{__init__,library_fixture}.py`, корневые `README.md`, `AGENTS.md`; скопировать актуальные спецификацию/план и исторический план с пометкой «не исполнять». `BUILD.md`/`INSTALL.md` с проверенными командами завершаются в задаче 11, не заполняются фиктивными результатами.

**Интерфейсы:**
- Dataclass `Library(root: Path, catalog: Path, archives: Path)`; `StatePaths(config: Path, cache: Path, index: Path)`; абсолютные пути.
- `Series(name: str, number: str)`; `Book(archive: str, member: str, file_id: str, author: str, title: str, language: str, series: tuple[Series, ...], size: int | None, libid: str, metadata: dict[str, str])`. `archive` — простое имя ZIP относительно `Library.archives`; metadata — исходные поля без SERIES/SERNO.
- Изменяемый `ImportStats(rows: int = 0, excluded: int = 0, duplicates: int = 0, books: int = 0)`; инвариант `rows == excluded + duplicates + books`.
- `IndexMeta(root: str, catalog_rel: str, sha256: str, schema_version: int, rules_version: int, built_at: str, stats: ImportStats)`; `IndexResult(path: Path, rebuilt: bool, meta: IndexMeta)`; `SearchPage(books: tuple[Book, ...], page: int, total: int)` со страницами от нуля.
- `UserError` и наследники `ConfigError`, `CatalogError`, `IndexBuildError`, `SearchError`, `ExtractionError`; `IndexBusyError(IndexBuildError)`, `TargetExistsError(ExtractionError)`.
- `inspect_library(root: Path) -> Library`, `load_library(paths: StatePaths) -> Library`, `save_library(paths: StatePaths, library: Library) -> None` в `config.py`.
- `main(argv: Sequence[str] | None = None, *, stdin: TextIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int` в `cli.py`; пока только справка, без фиктивного успеха поиска.
- `tests.fixtures.make_library(root: Path, groups: Mapping[str, Sequence[dict[str, str]]], *, fields: Sequence[str] = DEFAULT_FIELDS, members: Mapping[str, Mapping[str, bytes]] | None = None) -> Library` — обёртка над `tests.harness.library_fixture.write_library` с теми же параметрами, возвращающим Path корня, и `inspect_library`. Чистый writer не импортирует приложение, всегда создаёт `lib.rus.ec/`; ключи groups — INP, members — ZIP; значения members — имя FB2 → байты. В library_fixture определён `DEFAULT_FIELDS = ('AUTHOR','GENRE','TITLE','SERIES','SERNO','FILE','SIZE','LIBID','DEL','EXT','DATE','LANG','LIBRATE','KEYWORDS','YEAR','SOURCELIB')`; отсутствующие значения пустые. Это генератор тестовых данных, не второй INPX-importer; он пригоден и вне checkout приложения.

- [ ] **1. Написать падающие тесты `ConfigTests`:** `test_roundtrip` — `self.assertEqual(load_library(paths), library)`; `test_bad_json_or_version_preserved` — `ConfigError`, байты конфигурации прежние; `test_invalid_root_not_saved` — отсутствующий INPX/каталог ZIP не создаёт конфигурацию; `test_spaces_and_cyrillic` — корень `Библиотека книг` сохраняется без искажения.
- [ ] **2. Красная стадия:** `python -m unittest tests.test_config -v`; ожидаются отсутствующие контракты, не синтаксическая ошибка теста.
- [ ] **3. Реализовать модели и config.** JSON `{"version":1,"library_root":"..."}`, временный файл рядом с конфигурацией, закрытие и `os.replace`. `inspect_library` проверяет пути, содержимое structure проверяется до сохранения в задаче 9. `pyproject.toml`: `0.1.0`, Python `>=3.11`, без runtime-зависимостей, setuptools backend, console entry point `keklib-cli = "keklib_cli.cli:main"`; `__main__` — `SystemExit(main())`. Установить editable через `python -m pip install -e .`. README описывает цель, AGENTS — модули, неизменность источников, текущую тестовую команду и ссылку на утверждённую спецификацию; будущие непроверенные сборки не объявлять готовыми.
- [ ] **4. Зелёная стадия:** повторить тест; `python -m keklib_cli --help`; общий набор. Ожидаются `OK`, правильное имя команды и отсутствие созданного индекса.
- [ ] **5. Коммит:** `git add pyproject.toml .gitignore src/keklib_cli tests/__init__.py tests/fixtures.py tests/test_config.py tests/harness README.md AGENTS.md docs/superpowers`; `git commit -m "feat: bootstrap keklib-cli models and configuration"`.

### Задача 2. Нативные пути и shared/exclusive блокировки

**Файлы:** создать `src/keklib_cli/platforms/{__init__,paths,posix,windows}.py`, `tests/test_platforms.py`.

**Интерфейсы:** потребляет `StatePaths`, `IndexBusyError`; предоставляет `state_paths(*, system: str | None = None, env: Mapping[str, str] | None = None, home: Path | None = None) -> StatePaths`, контекстный менеджер `index_lock(path: Path, *, exclusive: bool) -> Iterator[None]` и `peak_rss_kib() -> int | None` через `platforms.__init__`. Для тестов ОС называется `Linux`, `Windows`, `Darwin`; dispatch нативных операций использует реальную ОС, не тестовую подмену пути.

- [ ] **1. Написать падающие тесты `PlatformTests`:** `test_native_paths` сверяет все три строки таблицы §5 спецификации; `test_xdg_fallback` — пустое/относительное XDG даёт `.config`/`.cache`; `test_windows_appdata_fallback` — `home/AppData/Roaming` и `home/AppData/Local`. `test_two_readers` — два настоящих процесса одновременно удерживают shared; `test_writer_excludes_readers_and_writers` и `test_reader_excludes_writer` дают `IndexBusyError` без ожидания; `test_process_death_releases_lock` — после завершения дочернего процесса новый writer входит. `test_import_isolation` — импорт пакета на Windows не требует fcntl, а на POSIX — Windows DLL. `test_memory_units` — Linux KiB, Darwin bytes делятся на 1024, недоступное измерение даёт `None`, не 0.
- [ ] **2. Красная стадия:** `python -m unittest tests.test_platforms -v`; по каждой ОС требуется её нативный прогон, mock только дополняет.
- [ ] **3. Реализовать пути строго по §5.** В POSIX — `flock(LOCK_SH/LOCK_EX | LOCK_NB)`; в Windows — `LockFileEx`/`UnlockFileEx` через `ctypes` на общем byte-range с `LOCKFILE_FAIL_IMMEDIATELY`, exclusive-флаг только для writer. Зафиксировать WinAPI argtypes/restype и pointer-sized поля OVERLAPPED; не использовать `msvcrt.locking` как якобы shared-lock. Все процессы блокируют один и тот же `index.lock`; файл не удаляется/не заменяется для «освобождения». Ошибку занятости отделить от прав/IO. Память: getrusage на POSIX, GetProcessMemoryInfo на Windows либо `None` с доступным для отчёта пояснением; без unconditional resource. Дескрипторы закрывать в finally.
- [ ] **4. Зелёная стадия:** тесты задачи и общий набор; ожидается `OK`. Дочерние процессы запускать через `sys.executable`, сигналы готовности через pipe/event, без sleep как условия корректности и без наследования уже взятой блокировки.
- [ ] **5. Коммит:** `git add src/keklib_cli/platforms tests/test_platforms.py`; `git commit -m "feat: add native state paths and reader writer locks"`.

### Задача 3. Потоковый INPX и объединение серий

**Файлы:** создать `src/keklib_cli/inpx.py`, `tests/test_inpx.py`; при необходимости дополнить `tests/fixtures.py`.

**Интерфейсы:** потребляет модели задачи 1; предоставляет `read_structure(catalog: Path) -> tuple[str, ...]`, `iter_books(catalog: Path, stats: ImportStats) -> Iterator[Book]`. Генератор объединяет строки в пределах одного INP и обновляет переданный stats, не загружая весь INPX в память.

- [ ] **1. Написать падающие тесты `InpxTests`:** `test_order_bom_crlf` — переставленные поля/BOM/CRLF сохраняют TITLE; `test_filter` — четыре DEL `0,1,2,''` плюс EPUB дают `stats.books == 1`, `stats.excluded == 4`; `test_merge_series` — три активные строки одного member с двумя сериями дают одну Book, две Series, `stats.duplicates == 2`; `test_other_member_is_edition` — одинаковые названия разных FILE остаются двумя книгами. `test_conflict_reports_both_lines`, `test_duplicate_structure`, `test_bad_encoding_or_size`, `test_truncated_required_fields` требуют `CatalogError`; `test_unknown_and_optional_fields` сохраняет extra metadata/нескольких авторов, пустой SIZE даёт `None`; `test_empty_catalog` даёт пустой список. `test_cross_platform_source_names` отклоняет `../x`, `..\\x`, `C:x`, UNC/device-пути и NUL даже на Linux.
- [ ] **2. Красная стадия:** `python -m unittest tests.test_inpx -v`.
- [ ] **3. Реализовать импорт.** Единственный `structure.info`, уникальные имена полей, обязательные AUTHOR/TITLE/FILE/EXT/DEL; разделитель структуры `;`, строк — `\x04`; финальное пустое поле допустимо. UTF-8-sig, снятие только CR/LF. Активные FB2 — DEL точно `0`, EXT без учёта регистра; остальные исключаются. Для активной книги пустой FILE, отрицательный/нецелый непустой SIZE и небезопасные имена — ошибка; пустой AUTHOR/TITLE допускается. Идентичность `(inp_name[:-4] + '.zip', FILE + '.' + EXT)`. Повторы сравниваются по всем исходным полям кроме SERIES/SERNO, включая неизвестные; серии дедуплицируются парами в исходном порядке, пустая серия пропускается. Различие core — ошибка с обеими строками. ZIP книг не открывать.
- [ ] **4. Зелёная стадия:** тесты задачи и общий набор; `rows == books + duplicates + excluded`, ошибки с точной строкой источника.
- [ ] **5. Коммит:** `git add src/keklib_cli/inpx.py tests/test_inpx.py tests/fixtures.py`; `git commit -m "feat: import INPX and merge series records"`.

### Задача 4. SQLite FTS5 и нормализация

**Файлы:** создать `src/keklib_cli/text.py`, `src/keklib_cli/index_store.py`, `tests/test_index_store.py`.

**Интерфейсы:** `normalize(text: str) -> str`; `write_index(path: Path, books: Iterable[Book], meta: IndexMeta) -> None`; `read_meta(path: Path) -> IndexMeta`; `validate_index(path: Path, *, quick: bool = False) -> None`; `SCHEMA_VERSION = 1`, `RULES_VERSION = 1`. `meta.stats` — тот же объект, который обновляет importer; сериализуется после полного исчерпания books.

- [ ] **1. Написать падающие тесты `IndexStoreTests`:** `test_roundtrip` восстанавливает metadata/series/None-size и IndexMeta; `test_stats_after_generator` проверяет финальные, не нулевые счётчики; `test_normalize` — `normalize('ЁЖ') == 'еж'`, совместимые полноширинные буквы приводятся NFKC; `test_fts_prefix` — MATCH `"булг"* AND "мастер"*` находит строку. `test_empty_index`, `test_missing_fts5`, `test_corrupt_or_wrong_count`, `test_existing_target_preserved` проверяют пустой валидный индекс, понятный IndexBuildError и отсутствие перезаписи чужого файла.
- [ ] **2. Красная стадия:** `python -m unittest tests.test_index_store -v`.
- [ ] **3. Реализовать схему.** `meta(key TEXT PRIMARY KEY,value TEXT)` и `book(id INTEGER PRIMARY KEY,archive TEXT,member TEXT,file_id TEXT,author TEXT,title TEXT,language TEXT,series_json TEXT,size INTEGER,libid TEXT,metadata_json TEXT,norm_author TEXT,norm_title TEXT,UNIQUE(archive,member))`. FTS `book_fts(norm_title,norm_author,content='book',content_rowid='id',tokenize='unicode61 remove_diacritics 0')`. Одна транзакция импорта, FTS rebuild, integrity-check FTS на пишущем соединении, затем commit/close. Файл создаётся эксклюзивно по отсутствующему пути; journal DELETE, не WAL. Метаданные в UTF-8 JSON. `validate_index` открывает URI mode=ro, выполняет quick_check либо integrity_check, проверяет поля и счётчик books; SQL/FTS-ошибки становятся IndexBuildError. SQLite URI строить через `Path.as_uri()`, не ручной конкатенацией Windows-пути.
- [ ] **4. Зелёная стадия:** тесты задачи и общий набор; после закрытия готовой базы нет `-wal`/`-shm`, исходные данные не меняются.
- [ ] **5. Коммит:** `git add src/keklib_cli/text.py src/keklib_cli/index_store.py tests/test_index_store.py`; `git commit -m "feat: build validated immutable FTS5 catalog index"`.

### Задача 5. Проверка актуальности и атомарная пересборка

**Файлы:** создать `src/keklib_cli/indexer.py`, `tests/test_indexer.py`.

**Интерфейсы:** потребляет задачи 1–4; `sha256_file(path: Path) -> str`, `ensure_index(library: Library, paths: StatePaths, *, force: bool = False, progress: Callable[[str], None] | None = None) -> IndexResult`. При возврате нет открытого SQLite-соединения или удерживаемой блокировки.

- [ ] **1. Написать падающие тесты `IndexerTests`:** `test_first_then_unchanged` — rebuilt `True/False`, прежние байты/mtime базы во втором случае; `test_same_size_same_mtime_new_hash` — изменение содержимого того же размера с восстановленным mtime вызывает пересборку. `test_force_schema_rules_root`, `test_add_edit_delete_del_and_inp` покрывают все причины обновления; `test_failure_cancel_disk_full_preserve_old` сравнивает SHA-256 старой базы до/после. `test_changed_live_source` реально меняет INPX между snapshot и финальным hash, публикации нет. `test_busy_writer`, `test_corrupt_index_rebuilt`, `test_cleanup_not_ours`, `test_replace_failure_preserves_old` требуют диагностики и сохранности чужих/старых данных; нет удаления старого индекса до replace.
- [ ] **2. Красная стадия:** `python -m unittest tests.test_indexer -v`.
- [ ] **3. Реализовать §7 спецификации.** Свежесть проверять под shared-lock: root/catalog SHA-256/schema/rules и quick validation. При необходимости закрыть чтение/освободить shared, взять exclusive nonblocking и проверить повторно. В cache создать собственный `build-*` с маркером, закрытый снимок INPX с SHA-256, новую базу по отсутствующему пути. Заполнить IndexMeta со stats импортера и UTC built_at; validate; повторно hash живого источника; все соединения закрыть до `os.replace(new, paths.index)`. При force всегда пытаться exclusive. При несовместимой активной блокировке — IndexBusyError; при сбое — ошибка без stale fallback. Автоочистка только собственных помеченных build-каталогов под exclusive и без следования симлинкам. Не оставлять открытый NamedTemporaryFile перед replace на Windows.
- [ ] **4. Зелёная стадия:** тесты задачи и общий набор. Проверить реальную конкуренцию процессов, не только mocks, и очистку temp при KeyboardInterrupt.
- [ ] **5. Коммит:** `git add src/keklib_cli/indexer.py tests/test_indexer.py`; `git commit -m "feat: refresh catalog safely under exclusive index lock"`.

### Задача 6. Поисковая сессия под общей блокировкой

**Файлы:** создать `src/keklib_cli/search.py`, `tests/test_search.py`; дополнить `src/keklib_cli/text.py`.

**Интерфейсы:** `query_tokens(query: str) -> tuple[str, ...]`, `build_match(tokens: tuple[str, ...]) -> str` в text; `PAGE_SIZE = 20`; `SearchSession.search(query: str, *, page: int = 0) -> SearchPage`; публичный контекстный менеджер `open_search(library: Library, paths: StatePaths, *, progress: Callable[[str], None] | None = None) -> Iterator[SearchSession]`. Только open_search открывает сессию; CLI не создаёт соединение напрямую.

- [ ] **1. Написать падающие тесты `SearchTests`:** `test_required_prefix_tokens` находит `булг мастер` только при совпадении всех слов; `test_yo_case_nfkc`, `test_punctuation_and_operators` — `build_match(('or',)) == '"or"*'`, запрос `!!!` даёт SearchError, кавычки не исполняют синтаксис. `test_exact_title_first`; `test_pages` — 41 результат даёт 20/20/1 и одинаковый порядок, page<0 отклоняется. `test_session_blocks_refresh` — writer получает IndexBusyError, после close успешно обновляет; `test_two_sessions` — shared-сессии совместимы. `test_changed_between_ensure_and_open` — метаданные проверяются заново до списка; `test_close_before_unlock` требует закрытого SQLite до допуска writer. Серии одной Book не дублируют результат, другой member остаётся отдельным изданием.
- [ ] **2. Красная стадия:** `python -m unittest tests.test_search -v`.
- [ ] **3. Реализовать prefix AND и snapshot.** Токены — Unicode-буквы/цифры после normalize, `_` и пунктуация — разделители; токен в экранированных двойных кавычках с `*`, соединение AND, MATCH как связанный SQL-параметр. Сортировка: exact norm_title DESC, bm25 ASC, author/title/archive/member; COUNT, LIMIT 20/OFFSET page*20. open_search вызывает ensure_index, берёт shared, проверяет текущие metadata/hash/версии и открывает read-only transaction. При обнаружении смены источника освобождает shared до повторного ensure; максимум две попытки, затем понятная ошибка «каталог изменяется». Соединение закрыть до unlock; никакого lock upgrade. Даже неизменившийся номер книги всегда относится к Book из этой сессии.
- [ ] **4. Зелёная стадия:** тесты задачи и общий набор. Во всех ОС writer отказывается обновлять активного читателя; не использовать старый тест «заменить открытый SQLite-файл», несовместимый с Windows.
- [ ] **5. Коммит:** `git add src/keklib_cli/search.py src/keklib_cli/text.py tests/test_search.py`; `git commit -m "feat: search stable pages under shared index lock"`.

### Задача 7. Чтение точного файла из ZIP

**Файлы:** создать `src/keklib_cli/archive.py`, `tests/test_archive.py`.

**Интерфейсы:** контекстный менеджер `open_book(library: Library, book: Book) -> Iterator[BinaryIO]`; поток переводит ошибки своих read в ExtractionError, ZIP и source-дескрипторы принадлежат менеджеру.

- [ ] **1. Написать падающие тесты `ArchiveTests`:** `test_exact_bytes` — `stream.read() == fb2_bytes`; `test_missing_new_zip_or_member` — точная пара в ошибке, соседний member не читается; `test_duplicate_member`, `test_crc_corruption`, `test_encrypted_or_unsupported` — ExtractionError. `test_cross_platform_escape` — отказ для `../`, `..\\`, `C:x`, UNC и симлинка/junction наружу, а не сравнение строкового prefix каталогов. `test_directory_or_symlink_entry`, `test_zip_replaced_after_search` проверяют неподходящую запись/исчезновение выбранного member; `test_caller_error_not_relabelled` сохраняет OSError тела вызывающего кода. CRC-fixture: ZIP_STORED с изменённым байтом данных и старым CRC.
- [ ] **2. Красная стадия:** `python -m unittest tests.test_archive -v`.
- [ ] **3. Реализовать проверки.** Повторить валидацию archive/member независимо от доверия базе; `resolve()` и `relative_to(library.archives.resolve())`, проверка платформенных junction там, где они разрешаются ОС. Требовать ровно один точный member по infolist, обычный файл без шифрования/UNIX-symlink. Чтение до EOF завершает CRC. Ошибки своих open/read локально переводить в ExtractionError; не ловить всё тело caller вокруг yield и не маскировать KeyboardInterrupt. ZIP не извлекать встроенными extract/extractall.
- [ ] **4. Зелёная стадия:** тесты задачи и общий набор; дескрипторы закрыты, исходные ZIP побайтно прежние.
- [ ] **5. Коммит:** `git add src/keklib_cli/archive.py tests/test_archive.py`; `git commit -m "feat: validate and read the selected ZIP member"`.

### Задача 8. Переносимые имена и атомарное no-replace извлечение

**Файлы:** создать `src/keklib_cli/output.py`, `tests/test_output.py`; дополнить `src/keklib_cli/platforms/{__init__,posix,windows}.py` и `tests/test_platforms.py`.

**Интерфейсы:** `safe_filename(book: Book, *, max_bytes: int = 240) -> str`, `extract_book(library: Library, book: Book, destination: Path) -> Path` в output; `publish_no_replace(temp: Path, target: Path) -> None` в платформенном фасаде. Вызов происходит после закрытия temp/source; target никогда не удаляется для восстановления.

- [ ] **1. Написать падающие тесты `OutputTests`:** `test_utf8_limit` — длина имени ≤240 UTF-8 байт, хвост `' [66372].fb2'` сохранён; `test_empty_metadata` — `'66372.fb2'`; `test_windows_names_everywhere` — нет запрещённых символов/CON/NUL/COM1-форм и завершающих пробелов/точек, даже на POSIX. `test_extract_exact_bytes`; `test_existing_file_symlink_and_race` — TargetExistsError, прежние байты/ссылка сохранены при реальном создании target перед publish. `test_case_unicode_collision` проверяет нативную семантику FS и отказ перезаписи при коллизии. `test_crc_disk_write_cancel` — нет готового частичного FB2/своего temp; `test_unsupported_publish` — отказ без небезопасного fallback; `test_long_full_path` — понятная диагностика, без изменения Windows policy.
- [ ] **2. Красная стадия:** `python -m unittest tests.test_output tests.test_platforms -v`.
- [ ] **3. Реализовать имя и публикацию.** Формат из §9; AUTHOR `Фамилия,Имя,Отчество:` преобразовать в читаемое имя, авторов разделять `; `. Unicode C*, `/\\<>:"|?*` заменять пробелами, схлопнуть пробелы, убрать недопустимые хвосты и зарезервированные Windows-имена; ID тоже проверить/обезопасить, слишком длинный ID отклонить. UTF-8 обрезать без разрыва кодовой точки, сохраняя ID/расширение. Потоково писать в mkstemp в destination, дочитать CRC, flush/fsync/close. Базовая атомарная публикация — hardlink temp→target и unlink только своего temp; при неподдерживаемости link — Linux renameat2(RENAME_NOREPLACE), Darwin renamex_np(RENAME_EXCL), Windows os.rename с его no-overwrite семантикой. Нативные вызовы изолировать и проверять errno/GetLastError; EEXIST/его Windows-эквивалент всегда конфликт, не повод для overwrite. Если безопасный primitive отсутствует — ExtractionError. Каталог назначения по умолчанию задаст CLI как Path.home()/'Books'.
- [ ] **4. Зелёная стадия:** тесты задачи и общий набор; дополнительно нативные ext4/NTFS/APFS. Mock чужого API не является доказательством поддержки FS; гонка должна доходить до реального primitive публикации.
- [ ] **5. Коммит:** `git add src/keklib_cli/output.py src/keklib_cli/platforms tests/test_output.py tests/test_platforms.py`; `git commit -m "feat: extract portable filenames without overwriting files"`.

### Задача 9. Команды и нумерованный терминальный интерфейс

**Файлы:** создать `src/keklib_cli/terminal.py`, `tests/test_terminal.py`, `tests/test_cli.py`; изменить `src/keklib_cli/cli.py`.

**Интерфейсы:** `display_text(value: str) -> str`, `render_page(page: SearchPage, stdout: TextIO) -> None`, `choose_book(session: SearchSession, query: str, *, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> Book | None`; завершить main из задачи 1. Конечный UI получает потоки, не глобальные input/print внутри компонентов.

- [ ] **1. Написать падающие тесты `TerminalTests`/`CliTests`:** `test_page_numbers` — вторая страница допускает 21, не 1; `test_n_p_bounds`, `test_bad_input_then_valid`, `test_q_eof_empty` — корректные границы/повтор ввода, выход None без извлечения. `test_controls_and_narrow_encoding` — ESC/newline/bidi не управляют терминалом, символ вне кодировки не вызывает traceback. `test_setup_structure_before_save` сохраняет старую настройку при плохом INPX; `test_non_tty_before_index` отклоняет stdin не-TTY; `test_extract_flow` — `main(...) == 0`, один FB2 верных байтов и абсолютный путь в stdout. `test_output_override_and_tilde`, `test_missing_updated_zip`, `test_failed_refresh_no_stale_selection`, `test_busy_reader_diagnostic` проверяют пути и ошибки. `test_codes_and_channels` — 0/1/2/130 по правилам ниже, диагностика в stderr.
- [ ] **2. Красная стадия:** `python -m unittest tests.test_terminal tests.test_cli -v`.
- [ ] **3. Реализовать argparse и оркестрацию.** setup --library: inspect→read_structure→save; index --refresh: ensure(force=True); search QUERY [--output PATH]: validate query, stdin.isatty, state_paths/load, open_search, choose_book, extract_book. `stdout` разрешено перенаправлять. Библиотека/выходные пути раскрывают пользовательское `~` приложением, не только shell. Текущая таблица хранит Book→сквозной номер page*20+offset+1, выбор только с текущей страницы. display_text заменяет Unicode C* пробелами; вывод с безопасной заменой непредставимых символов, не меняя metadata на диске. Прогресс/приглашения/ошибки — stderr, таблица и итоговый путь — stdout. Коды: 0 успех/нет книг/q/EOF; 1 предметная/IO ошибка; 2 неверные аргументы/пустой запрос/non-TTY; 130 Ctrl+C. Не скрывать программные ошибки общим except Exception. Конфликт имён не перезаписывает книгу, повтор с другим --output описан в INSTALL.
- [ ] **4. Зелёная стадия:** тесты задачи, общий набор и `python -m keklib_cli search --help`; правильные команды и --output, никакого обращения к настоящей библиотеке в unit-тестах.
- [ ] **5. Коммит:** `git add src/keklib_cli/terminal.py src/keklib_cli/cli.py tests/test_terminal.py tests/test_cli.py`; `git commit -m "feat: wire native numbered search and extraction CLI"`.

### Задача 10. Нативный сквозной тест и приёмка реальной библиотеки

**Файлы:** создать `tests/harness/tty.py`, `tests/test_integration.py`, `tools/verify_library.py`, `packaging/requirements-test-windows.lock`; дополнить `AGENTS.md` проверенными командами. `tests/harness/__init__.py` и чистый writer уже созданы задачей 1.

**Интерфейсы:** `run_tty(argv: Sequence[str], commands: Sequence[str], *, env: Mapping[str,str], cwd: Path, timeout: float = 30.0) -> subprocess.CompletedProcess[str]` в harness; общий результат содержит слитый transcript и реальный returncode, commands передаются после ожидания приглашения. `tools/verify_library.py --library PATH --workdir PATH [--reuse-index]` использует публичные компоненты, только выделенный workdir вне library, никогда пользовательский config/cache.

- [ ] **1. Написать падающие тесты `IntegrationTests`:** `test_native_setup_search_update_select` запускает реальный module CLI, выбирает номер через TTY и сверяет SHA-256 FB2; затем новый INPX/ZIP отражается в поиске. `test_read_only_source_bytes` сравнивает SHA-256/mtime_ns, не atime; `test_library_switch` исключает старые результаты. `test_verify_refuses_library_workdir` не допускает записи внутрь library; `test_verify_report` использует fixture известной книги; `test_verify_reuse` — rebuilt False и прежний hash базы/FB2; `test_verify_missing_known_book` даёт явный отказ. `test_windows_tty_required` на Windows требует установленный harness, не skip всего сценария.
- [ ] **2. Красная стадия:** `python -m unittest tests.test_integration -v`; POSIX и Windows исполняют свой путь.
- [ ] **3. Реализовать harness и verify.** POSIX PTY и Windows ConPTY через test-only pywinpty, установленный из зафиксированного lock; с таймаутами и гарантированным завершением только собственного subprocess. Общий CLI остаётся без test-зависимостей. Verify измеряет отдельно hash/build/SearchSession.search через perf_counter, память через peak_rss_kib; null объясняется. Ищет Булгаков Мастер и среди страниц точную известную пару fb2-060424-074391.zip/66372.fb2 с названием Мастер и Маргарита; отсутствие — отказ, не выбор случайного издания. Извлекает в свой output, сравнивает hash с open_book, hash INPX и выбранного ZIP до/после, stat остальных архивов. Отчёт JSON: catalog_sha256,row_count,book_count,hash_seconds,build_seconds,query_seconds,peak_rss_kib,memory_note,rebuilt,index_sha256,selected_archive,selected_member,output_sha256,source_unchanged,query_target_met. reuse разрешён только по собственному маркеру workdir; FB2 не перезаписывается. Предел проверки остальных 451 ГБ явно указан.
- [ ] **4. Зелёная стадия и локальная реальная проверка:** тесты задачи, общий набор, `python -m compileall -q src tests tools`. Затем на текущем Linux `python tools/verify_library.py --library '/run/media/user/Untitled/_Lib.rus.ec - Официальная' --workdir /tmp/opencode/keklib-cli-acceptance` и повтор с --reuse-index. Если workdir чужой/занят — выбрать новый, не очищать. Ожидаются source_unchanged True, совпадение FB2, query_seconds ≤1, повтор без пересборки. Фактические время/память записать, не подменять прогнозом; превышение ориентира исследовать до заявления готовности.
- [ ] **5. Коммит:** `git add tests/harness tests/test_integration.py tools/verify_library.py packaging/requirements-test-windows.lock AGENTS.md`; `git commit -m "test: cover native CLI flow and real library acceptance"`. Книги/локальный отчёт не включать в Git.

### Задача 11. Portable-сборка, упаковка и корневые инструкции

**Файлы:** создать `packaging/{entry.py,keklib-cli.spec,targets.json,toolchain.json,requirements-build.lock,THIRD_PARTY_NOTICES.md}`, `tools/build_portable.py`, `tests/test_packaging.py`, корневые `BUILD.md`, `INSTALL.md`; обновить `README.md`, `AGENTS.md`, `.gitignore`.

**Интерфейсы:** `python tools/build_portable.py --target TARGET --output DIR`; target — один из четырёх из общих ограничений и обязан совпадать с реальной ОС/CPU. `build_portable(target: str, output: Path) -> Path` возвращает путь архива; `archive_bundle(bundle: Path, output: Path, *, version: str, target: str) -> Path` в этом же tool. Команда не перезаписывает существующий архив и не очищает чужой output. build-info.json внутри комплекта: version,target,python,sqlite,pyinstaller,schema_version,rules_version,minimum_os,minimum_glibc. Начальный Linux-baseline — glibc, не musl/Alpine. INSTALL должен объяснять, что SHA-256 подтверждает целостность, а не заменяет доверенную цифровую подпись.

- [ ] **1. Написать падающие тесты `PackagingTests`:** `test_four_target_names` — ровно четыре ожидаемых суффикса; `test_reject_cross_target` — Linux не выдаётся за Windows/macOS; `test_archive_allowlist` — executable,_internal,README.md,INSTALL.md,build-info.json и лицензионные уведомления, но нет INPX/ZIP книг/SQLite/config/секретов/tests; `test_posix_execute_bit`, `test_checksum_matches`, `test_existing_archive_preserved`, `test_documents_at_root`. `test_build_metadata` требует полные зафиксированные версии и ненулевые сведения SQLite/FTS5; `test_no_onefile` проверяет onedir/console, не GUI и не onefile.
- [ ] **2. Красная стадия:** `python -m unittest tests.test_packaging -v`.
- [ ] **3. Реализовать сборку и документы.** Выбрать доступный исправленный CPython 3.14.x и совместимые версии PyInstaller/setuptools, зафиксировать точные версии runtime по target в toolchain.json и `==`-версии/хеши зависимостей в lock, не оставлять плавающие release-пины. В targets.json записать runner/CPU и проверяемые baseline ОС; Linux-builder — Ubuntu 22.04 x64, заявляемый glibc floor 2.35 проверяется по итоговому bundle; Mac Intel-builder — macos-15-intel, ARM-builder — macos-14; Windows-builder — windows-2022 x64. Это исходные проверяемые baseline, не обещание поддержки более старых/клиентских ОС без отдельного native smoke. Если label недоступен или toolchain требует более новый baseline, остановиться и согласовать изменение вместо выпуска непроверенного target. entry.py вызывает тот же cli.main; spec включает платформенные модули/_sqlite3/ctypes и console=True, проверяет FTS5 во включённом runtime. Собственный staging через tempfile под build/, чужие каталоги не удалять. Корень архива `keklib-cli/`, формат ZIP или tar.gz по target, сохранить execute bits, приложить README/INSTALL/необходимые лицензии, рядом `<archive>.sha256`. BUILD содержит фактически проверенные команды venv/pip/tests/build для PowerShell и POSIX, версии и ограничения; INSTALL — выбор CPU, checksum, распаковка целой папки, запуск без Python, PATH опционально, библиотека, поиск, обновление отдельной папкой, удаление без книг, предупреждения неподписанной поставки. AGENTS закрепляет архитектуру и безопасные команды, README связывает документы. Не помещать индекс или настройки в bundle.
- [ ] **4. Зелёная стадия:** тесты задачи и общий набор. Установить только сборочные зависимости в проектный venv, выполнить нативный build текущего target, распаковать в отдельный собственный temp и проверить executable --help и FTS-индексацию маленькой fixture; версия/архив/checksum соответствуют факту. На других ОС выполнить тот же шаг нативно при наличии доступа, не считать создание spec-файла готовой сборкой всех платформ.
- [ ] **5. Коммит:** `git add packaging/entry.py packaging/keklib-cli.spec packaging/targets.json packaging/toolchain.json packaging/requirements-build.lock packaging/THIRD_PARTY_NOTICES.md tools/build_portable.py tests/test_packaging.py BUILD.md INSTALL.md README.md AGENTS.md .gitignore`; `git commit -m "build: package portable bundles and document build and install"`.

### Задача 12. Изолированный smoke, CI-матрица и выпуск проверенных артефактов

**Файлы:** создать `tools/smoke_portable.py`, `tools/release_gate.py`, `tests/harness/permissions.py`, `tests/test_release.py`, `.github/workflows/ci.yml`, `.github/workflows/portable.yml`; уточнить BUILD/INSTALL/AGENTS по фактическим результатам.

**Интерфейсы:** `python tools/smoke_portable.py --archive PATH --workdir PATH` распаковывает только в новый собственный workdir, генерирует synthetic library и отчёт `smoke-report.json`. `validate_reports(reports: Sequence[dict[str, object]], targets: Sequence[str]) -> None` в release_gate проверяет ровно один успешный основной отчёт на каждый target одной версии и нативное подтверждение заявленных минимумов ОС; `python tools/release_gate.py --artifacts DIR` дополнительно сверяет четыре архива по SHA-256 и создаёт SHA256SUMS без публичной публикации. В отчёте обязательны version,target,host_os,host_os_version,host_arch,minimum_os,minimum_os_verified,archive_sha256,passed и флаги изоляции ниже. Дополнительный native-отчёт для клиентского Windows/минимальной ОС прикладывается как baseline evidence, а не как второй основной отчёт того же target. `readonly_bundle(path: Path) -> Iterator[None]` в permissions — тестовый контекст с восстановлением только своих разрешений.

- [ ] **1. Написать падающие тесты `ReleaseTests`:** `test_reject_missing_failed_or_wrong_cpu_report` отклоняет отсутствующий/неуспешный target и несовпадение host CPU; `test_checksum_gate` сравнивает каждый archive hash; `test_mixed_version_or_duplicate_target` отклоняет комплект разных версий и повтор одного target; `test_declared_minimum_requires_native_evidence` не принимает более новую/server-ОС как доказательство заявленного минимума/client-ОС. `test_isolation` требует отсутствия Python/python3 в PATH child, PYTHONPATH/PYTHONHOME и checkout-зависимостей; `test_readonly_bundle` подтверждает реальный отказ записи в каталог программы. `test_frozen_tty_flow` через run_tty выполняет --help/setup/search/выбор, обновление INPX, повторный поиск и FB2 hash; `test_state_outside_bundle` проверяет нативные config/cache снаружи. `test_four_reports_required` — отчёты Linux не заменяют Windows/macOS. Один локальный smoke запускается на реально построенном архиве, остальные обязательны в native jobs, не fake-pass.
- [ ] **2. Красная стадия:** `python -m unittest tests.test_release -v`; отсутствие собственного build можно покрыть unit-fixture для правил gate, но frozen smoke не отмечать успешным до выполнения на артефакте.
- [ ] **3. Реализовать изоляцию и workflows.** Smoke использует только чистый library_fixture и TTY/permissions harness, не импортирует keklib_cli. Build job упаковывает отдельный вспомогательный harness-архив с tools/smoke_portable.py, tools/release_gate.py, tests/__init__.py, tests/harness и test-only Windows-lock; он не является частью пользовательской поставки. Smoke job запускается на новой машине без checkout исходного приложения, скачивает только harness и пользовательский артефакт; Python тестового harness не содержит установленного keklib_cli. При прямом запуске tool разрешено добавлять в sys.path только корень этого вспомогательного harness для импорта tests, не checkout/src приложения. Для child: PATH без Python с необходимыми системными путями Windows, удалённые PYTHONPATH/PYTHONHOME, cwd вне checkout; домашние/XDG/AppData перенаправлены в собственный sandbox. Распакованный bundle read-only через chmod на POSIX и NTFS ACL на Windows, не DOS readonly-атрибут папки; проверить реальную невозможность создать файл и восстановить только свой ACL в finally. Запускать абсолютный executable из архива через нативный TTY, не `python -m`; test harness может иметь Python, child — не зависит от него. Отчёт фиксирует target/host OS/CPU, hashes, FTS5, source_unchanged, no_system_python, source_checkout_absent, readonly_bundle, passed. Source CI: Linux/Windows/macOS × Python 3.11/3.14, дополнительная проверка Intel/ARM Mac в portable jobs; ci.yml поддерживает workflow_call. Portable workflow по workflow_dispatch вызывает source-tests как reusable workflow, строит четыре target нативно по targets.json, запускает отдельные smoke jobs, затем gate с needs всех source/build/smoke jobs; только после успеха создаёт проверенные CI-артефакты/SHA256SUMS. Публичный GitHub Release и подписи не создавать автоматически. Actions фиксировать commit SHA, минимальные permissions; Windows pywinpty только в тестовом job. BUILD/INSTALL указывают фактические минимумы ОС/архитектур: Windows Server runner сам по себе не доказывает Windows 10/11, поэтому для объявления клиентского Windows support добавить ручной или self-hosted smoke на заявленной минимальной клиентской версии с сохранённым отчётом; аналогично не обещать Mac ниже проверенного baseline.
- [ ] **4. Зелёная стадия и gate:** полный unittest, compileall, установленный console entry point, нативный build и `python tools/smoke_portable.py --archive <созданный-архив> --workdir <новый-собственный-каталог>`; затем source/portable CI и `python tools/release_gate.py --artifacts <каталог-четырёх-проверенных-комплектов>`. Ожидаются четыре реальных passed-отчёта верных CPU, проверенные контрольные суммы и документы; никакого claim Windows/macOS только по локальному Linux. Если нет разрешённого удалённого репозитория/runners/клиентского Windows, остановить этот пункт как непроверенный, перечислить недостающие прогоны. Не публиковать частичную матрицу как полный релиз. Повторить реальную приёмку задачи 10 и проверить отсутствие продуктовых данных в Git.
- [ ] **5. Коммит:** `git add tools/smoke_portable.py tools/release_gate.py tests/harness/permissions.py tests/test_release.py .github/workflows BUILD.md INSTALL.md AGENTS.md`; `git commit -m "ci: gate portable releases on native isolated smoke tests"`. Перед итогом использовать `verification-before-completion`, затем согласованный независимый review; merge/push/public release — только отдельным решением пользователя.

## Покрытие спецификации и самопроверка

| Требование спецификации | Задачи |
|---|---|
| §1–3: цель, имена, Python, ОС, границы | 1, 2, 9, 11, 12 |
| §2.1: четыре portable-комплекта, onedir, SHA-256, версии, offline | 11, 12 |
| §2.2: README/BUILD/INSTALL/AGENTS в корне | 1, 10, 11, 12 |
| §4: команды, страницы, выбор, EOF/Ctrl+C и потоки | 9, 10, 12 |
| §5: XDG/AppData/Library, временные пути вне bundle | 2, 5, 11, 12 |
| §6: структура, DEL, размеры, несколько серий и издания | 3, 4 |
| §7: hashes, snapshot, shared/exclusive, rollback и cleanup | 2, 5, 6 |
| §8: Unicode, prefix AND, ранжирование и snapshot | 4, 6 |
| §9: ZIP/member, traversal, имена, CRC, no-replace | 7, 8 |
| §10–11: границы, реальные native тесты, память и приёмка | 1–12 |

- [x] Пять дополнительных рисков имеют собственные тесты в ответственных задачах.
- [x] Public signatures/types определены до использования; config, indexer, search и packaging не имеют второго владельца одной логики.
- [x] Каждая задача содержит пять проверяемых шагов TDD/реализации/коммита; тела продукта не записаны вместо плана.
- [x] Безусловные Linux-only импорты и замена открытой SQLite-базы исключены; старый план не используется как инструкция.
- [x] Заявление о готовности всех ОС требует реальных native/frozen отчётов, а не наличия workflow-файла.
- [x] Корневые инструкции и AGENTS — конкретные результаты задач, не только упоминания в README.
- [x] Реализация, создание проекта, установка зависимостей и удалённые операции на этапе планирования не выполняются.

## Передача к исполнению

Пользователь проверяет этот план, подтверждает путь `/home/user/opencode/keklib-cli` и выбирает способ:
- **Native:** задачи последовательно реализуются в текущей сессии через `executing-plans`, затем один независимый обзор всей ветки. Меньше затрат на передачу контекста, но нет отдельного review после каждой задачи.
- **Subagent-driven:** `subagent-driven-development`, отдельные исполнитель и проверки для каждой задачи, затем общий review. Рекомендуется для этой редакции: нативные блокировки/no-replace и упаковка четырёх платформенных артефактов имеют разные режимы отказа, поэтому промежуточные независимые проверки полезнее минимизации числа передач контекста.

Пользователь выбрал Subagent-driven и явно закрепил GLM 5.3 для реализации и GPT 6.1 Sol для всех проверок. Модели и выбранный способ исполнения обязательны согласно общим ограничениям; другие варианты выше оставлены как описание первоначального выбора. Native CI-матрица обязательна независимо от способа исполнения.
