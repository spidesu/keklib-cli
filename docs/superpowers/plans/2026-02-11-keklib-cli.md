# keklib-cli — план реализации

> Актуальная замена: [план portable standalone](2026-02-11-keklib-cli-portable.md). Этот файл сохраняется только как история прежней Linux-редакции.

> **СТАТУС: НЕАКТУАЛЕН — НЕ ИСПОЛНЯТЬ.** После составления этого плана пользователь уточнил обязательную нативную поддержку Linux, Windows и macOS. Спецификация пересмотрена; приведённые ниже задачи сохраняются как прежняя редакция, а не как утверждённые инструкции. В частности, прежние ограничения Linux, безусловные `fcntl`/`pty`/`resource`, XDG-пути на всех ОС, замена открытой SQLite-базы и Linux-only публикация FB2 подлежат пересмотру. Этот план будет обновлён после итоговой проверки пересмотренной спецификации; способ распространения уже выбран — portable standalone.

> **Дополнение к пересмотру:** пользователь выбрал portable standalone без установки Python. Новая редакция плана обязана включать PyInstaller `onedir`, нативную сборку и smoke-тест каждого целевого артефакта, а также `README.md`, `BUILD.md`, `INSTALL.md` и `AGENTS.md` в корне исходного проекта. Указанные ниже прежние задачи ещё не реализуют эти требования.

> **Для агента-исполнителя:** обязательный навык — `subagent-driven-development` или `executing-plans`. Выполнять задачи по очереди, отмечая шаги `- [ ]`; способ исполнения выбирает пользователь до начала реализации.

**Цель:** реализовать `keklib-cli`: поиск книг по обновляемому INPX, выбор по номеру и безопасное извлечение одного FB2.

**Архитектура:** Python-пакет `keklib_cli`, команды через `argparse`, локальный SQLite FTS5. Изменение SHA-256 INPX запускает полную пересборку отдельной базы с атомарной публикацией. Исходная библиотека остаётся доступной только для чтения; повторные строки одного ZIP/member объединяются по сериям.

**Стек:** Linux, Python ≥3.11, стандартная библиотека (`sqlite3`, `zipfile`, `unittest`, `fcntl`, `ctypes`), SQLite с FTS5. Сборочная зависимость — `setuptools>=68`; внешних зависимостей времени исполнения и тестирования нет.

**Спецификация:** [2026-02-11-keklib-cli-design.md](../specs/2026-02-11-keklib-cli-design.md), подтверждена пользователем с названием `keklib-cli`.

## Общие ограничения

- Название команды и дистрибутива — `keklib-cli`, Python-пакет — `keklib_cli`; каталоги состояния также используют `keklib-cli`.
- Исходные INPX и ZIP открываются только для чтения.
- Целевая версия Python — 3.11 или новее; FTS5 обязателен и проверяется с понятной диагностикой.
- В поисковый индекс входят только записи с `DEL`, равным строке `0`, и `EXT=fb2` без учёта регистра.
- Результаты выдаются страницами по 20 книг.
- Нормализация: Unicode NFKC, `casefold()`, замена `ё` на `е`.
- Все слова запроса обязательны, каждое ищется как префикс слова в авторе или названии.
- Разные идентичности не объединяются даже при совпадении автора и названия.
- Каталог назначения по умолчанию — `~/Books`.
- Не использовать `extractall`, не создавать пути из имён ZIP-записей.
- При ошибке, нехватке места или отмене старый индекс сохраняется.
- Документация и инструкции — на русском языке. Сообщения новой утилиты — на русском языке; имена команд и параметры не переводятся.

## Особое внимание при проверке

1. Пустые или относительные XDG-переменные, неизвестная версия либо повреждённый JSON: использовать стандартные XDG-пути для пустых/относительных значений; повреждённую конфигурацию не перезаписывать автоматически. Тесты задачи 1.
2. Названия с ESC, переводами строк и управляющими Unicode-символами: не позволять метаданным управлять терминалом или добавлять строки интерфейса. Тесты задачи 8.
3. Запрос из пунктуации, кавычки, `AND`/`OR`, символы разных форм Unicode: никакого исполнения FTS-синтаксиса и ложного поиска всех книг. Тесты задачи 5.
4. INPX уже обновился, а ZIP ещё отсутствует либо заменён: результат поиска допустим, но выбранная книга проверяется заново, без подстановки другого издания. Тесты задач 6 и 8.
5. Длинное кириллическое имя, существующая символьная ссылка назначения и конкурентное создание файла: ограничение имени в байтах и отсутствие перезаписи при любых конфликтах. Тесты задачи 7.

## Размещение и условия начала

Предлагаемый новый проект: `/home/user/opencode/keklib-cli`. На момент составления плана такого подкаталога в `/home/user/opencode/` нет. Путь требует подтверждения вместе с планом; не создавать его заранее и не затирать ничего, если он появится.

Текущий каталог библиотеки не является Git-репозиторием. После разрешения исполнения сначала проверить инструкции `AGENTS.md` в выбранном месте. Создавать репозиторий и устанавливать сборочные зависимости разрешается только в рамках подтверждённого исполнения этого плана. Если нужен worktree существующего репозитория, использовать `using-git-worktrees` и размещать его только под `/home/user/opencode/`; не инициализировать Git в библиотеке.

В новый проект копируются подтверждённая спецификация и этот план с сохранением относительных ссылок. Текущие файлы библиотеки не перемещаются. Документы в новом проекте становятся рабочими версиями на этапе исполнения.

Все пути ниже относительны корню нового проекта. Все проверки запускаются из него с `TMPDIR=/tmp/opencode`; тесты используют только собственные `TemporaryDirectory` и очищают только свои файлы. Не выполнять команды реализации из этого документа на стадии планирования.

## Структура файлов и ответственности

```text
pyproject.toml                         # Дистрибутив, entry point, Python ≥3.11
README.md                              # Установка, команды, обновление, ограничения
.gitignore                             # .venv, __pycache__, сборочные артефакты
src/keklib_cli/
  __init__.py                          # Версия пакета
  __main__.py                          # python -m keklib_cli
  errors.py                            # Предметные ошибки без UI
  models.py                            # Общие dataclass-контракты
  config.py                            # XDG, проверка корня, атомарное сохранение JSON
  inpx.py                              # Структура полей, поток строк, объединение серий
  text.py                              # Нормализация и безопасные FTS-токены
  index_store.py                       # Схема SQLite, создание и проверка базы
  indexer.py                           # SHA-256, блокировка, снимок, замена индекса
  search.py                            # Сессия чтения, ранжирование, страницы
  archive.py                           # Проверка ZIP и чтение точного member
  output.py                            # Безопасное имя и атомарная публикация FB2
  terminal.py                          # Безопасный вывод и нумерованный выбор
  cli.py                               # argparse и оркестрация компонентов
tests/
  __init__.py
  fixtures.py                          # Маленькие INPX/ZIP, не настоящая библиотека
  test_config.py
  test_inpx.py
  test_index_store.py
  test_indexer.py
  test_search.py
  test_archive.py
  test_output.py
  test_terminal.py
  test_cli.py
  test_integration.py
tools/verify_library.py                # Явная приёмка на реальной библиотеке
docs/superpowers/specs/2026-02-11-keklib-cli-design.md
docs/superpowers/plans/2026-02-11-keklib-cli.md
```

## Порядок работы

Зависимости: `1 → 2 → 3 → 4 → 5`, `1 → 6 → 7`, затем `8 → 9`. Даже при выборе агентного исполнения не вести конкурентные изменения общих моделей. Каждая задача заканчивается самостоятельным проверяемым результатом и отдельным коммитом; не включать чужие изменения в `git add`.

### Задача 1. Контракты, конфигурация и устанавливаемая команда

**Файлы:** создать `pyproject.toml`, `.gitignore`, `src/keklib_cli/{__init__,__main__,errors,models,config,cli}.py`, `tests/{__init__,fixtures,test_config}.py`; скопировать два документа из раздела размещения.

**Интерфейсы:**
- `Library(root: Path, catalog: Path, archives: Path)`; пути абсолютные и разрешены через `resolve()`.
- `StatePaths(config: Path, cache: Path, index: Path)`; `cache` — каталог, остальные — файлы.
- `Series(name: str, number: str)`.
- `Book(archive: str, member: str, file_id: str, author: str, title: str, language: str, series: tuple[Series, ...], size: int | None, libid: str, metadata: dict[str, str])`; `archive` относительно `library.archives`, `metadata` содержит исходные поля, кроме `SERIES`/`SERNO`.
- `ImportStats(rows: int = 0, excluded: int = 0, duplicates: int = 0, books: int = 0)`; инвариант `rows == excluded + duplicates + books` после успешного импорта.
- `IndexMeta(root: str, catalog_rel: str, sha256: str, schema_version: int, rules_version: int, built_at: str, stats: dict[str, int])`.
- `IndexResult(path: Path, rebuilt: bool, meta: IndexMeta)`; `SearchPage(books: tuple[Book, ...], page: int, total: int)`; страницы с нуля.
- `UserError(Exception)` и наследники `ConfigError`, `CatalogError`, `IndexBuildError`, `IndexBusyError`, `SearchError`, `ExtractionError`, `TargetExistsError`; последнее — наследник `ExtractionError`, `IndexBusyError` — `IndexBuildError`.
- `state_paths(env: Mapping[str, str], home: Path) -> StatePaths`, `inspect_library(root: Path) -> Library`, `load_library(paths: StatePaths) -> Library`, `save_library(paths: StatePaths, library: Library) -> None` в `config.py`.
- Тестовый `make_library(root: Path, groups: Mapping[str, Sequence[dict[str, str]]], *, fields: Sequence[str] = DEFAULT_FIELDS, members: Mapping[str, Mapping[str, bytes]] | None = None) -> Library` в `tests/fixtures.py`. Ключ `groups` — имя INP, ключ `members` — имя ZIP; helper всегда создаёт `lib.rus.ec/`, но не индекс. `DEFAULT_FIELDS = ('AUTHOR', 'GENRE', 'TITLE', 'SERIES', 'SERNO', 'FILE', 'SIZE', 'LIBID', 'DEL', 'EXT', 'DATE', 'LANG', 'LIBRATE', 'KEYWORDS', 'YEAR', 'SOURCELIB')`; отсутствующие значения строки записываются пустыми.
- `main(argv: Sequence[str] | None = None, *, stdin: TextIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int` в `cli.py`; пока поддержать только справку и каркас парсера, не выдавать фиктивный успех остальных команд.

- [ ] **1. Написать падающие тесты** `ConfigTests`: `test_xdg_paths` — `self.assertEqual(paths.index, cache / 'keklib-cli/index.sqlite3')`; `test_empty_or_relative_xdg` — для `''` и `'relative'` используется `home / '.cache/keklib-cli/index.sqlite3'`; `test_roundtrip` — `self.assertEqual(load_library(paths), library)`; `test_invalid_config_preserved` — неверный JSON/версия дают `ConfigError`, байты файла не меняются; `test_invalid_root_not_saved` — нет INPX либо `lib.rus.ec/`, конфигурация не появляется. Добавить fixture с пробелами и кириллицей в пути.
- [ ] **2. Проверить красную стадию:** `TMPDIR=/tmp/opencode PYTHONPATH=src python3 -m unittest tests.test_config -v`. Ожидается ошибка отсутствующих контрактов, а не синтаксическая ошибка тестов.
- [ ] **3. Реализовать контракты, config и упаковку.** JSON: `{"version": 1, "library_root": "..."}`; запись через свой временный файл и `os.replace`. Относительные XDG-переменные игнорировать по правилам XDG. `inspect_library` проверяет пути, а семантическую проверку `structure.info` выполняет задача 8 до сохранения. `pyproject.toml`: версия `0.1.0`, `requires-python = ">=3.11"`, без runtime-зависимостей, entry point `keklib-cli = "keklib_cli.cli:main"`; `__main__` вызывает `raise SystemExit(main())`.
- [ ] **4. Проверить зелёную стадию:** повторить команду тестов; затем `PYTHONPATH=src python3 -m keklib_cli --help`. Ожидается `OK`, справка с `keklib-cli`, отсутствие созданного индекса.
- [ ] **5. Зафиксировать только файлы задачи:** `git add pyproject.toml .gitignore src/keklib_cli tests/__init__.py tests/fixtures.py tests/test_config.py docs/superpowers` и `git commit -m "feat: bootstrap keklib-cli configuration and contracts"`.

### Задача 2. Чтение INPX и несколько серий одной книги

**Файлы:** создать `src/keklib_cli/inpx.py`, `tests/test_inpx.py`; расширять `tests/fixtures.py` только для дополнительных форматов тестового INPX.

**Интерфейсы:** потребляет `Book`, `Series`, `ImportStats`, `CatalogError`; предоставляет `read_structure(catalog: Path) -> tuple[str, ...]` и `iter_books(catalog: Path, stats: ImportStats) -> Iterator[Book]`. Генератор изменяет переданный `stats`; объединение выполняется в пределах одного INP перед выдачей его книг, без загрузки всего каталога в память.

- [ ] **1. Написать падающие тесты** `InpxTests`: `test_reordered_fields_bom_crlf` — `self.assertEqual(book.title, 'Мастер и Маргарита')` после перестановки столбцов, BOM и CRLF; `test_filter` — для `DEL=['0','1','2','']` и дополнительного EPUB `self.assertEqual(stats.excluded, 4)` и остаётся один FB2; `test_series_merge` — три строки файла `66372` с двумя сериями дают `self.assertEqual(len(books), 1)`, две `Series` в исходном порядке, `stats.duplicates == 2`; `test_different_members_are_editions` — два разных FILE дают две книги. `test_conflicting_core_metadata` требует `CatalogError` с именем INP и обеими строками. `test_bad_structure` отклоняет отсутствующее обязательное поле, повтор имени столбца и два `structure.info`; `test_bad_rows` — неверный UTF-8/число SIZE и недостающие обязательные столбцы; `test_optional_fields` сохраняет неизвестные поля/нескольких авторов и даёт `size is None` при пустом SIZE; `test_empty_catalog` даёт `books == []`; `test_unsafe_source_names` отклоняет опасные FILE/имена INP.
- [ ] **2. Красная стадия:** `TMPDIR=/tmp/opencode PYTHONPATH=src python3 -m unittest tests.test_inpx -v`; ожидаются отсутствующие функции.
- [ ] **3. Реализовать парсер.** Требовать единственный `structure.info`, без повторяющихся имён полей; он разделяется `;`, финальный пустой элемент от завершающего разделителя удаляется. INP читаются построчно UTF-8-sig, `\r\n` удаляются только с конца, поля разделяются `\x04`; завершающее пустое поле допустимо. Пустой FILE активной строки — ошибка; пустой EXT исключается как не-FB2, пустой AUTHOR/TITLE допускается для резервного имени. Хранить исходные строки первого появления для диагностики конфликтов. Сравнивать все поля, кроме SERIES/SERNO, включая неизвестные. Пустое имя серии не создаёт `Series`. Отрицательный или нецелочисленный непустой SIZE — ошибка. Запретить `/`, `\\`, NUL, компоненты `.`/`..` и управляющие символы в именах источников и member. Не открывать ZIP книг при импорте.
- [ ] **4. Зелёная стадия:** повторить тесты задачи 2 и `TMPDIR=/tmp/opencode PYTHONPATH=src python3 -m unittest discover -s tests -t . -v`; ожидается `OK` и выполнение инварианта счётчиков.
- [ ] **5. Коммит:** `git add src/keklib_cli/inpx.py tests/test_inpx.py tests/fixtures.py` и `git commit -m "feat: import INPX and merge book series"`.

### Задача 3. Схема SQLite и построение поисковой базы

**Файлы:** создать `src/keklib_cli/text.py`, `src/keklib_cli/index_store.py`, `tests/test_index_store.py`.

**Интерфейсы:** потребляет `Book`, `IndexMeta`; предоставляет `normalize(text: str) -> str`, `write_index(path: Path, books: Iterable[Book], meta: IndexMeta) -> None`, `read_meta(path: Path) -> IndexMeta`, `validate_index(path: Path) -> None`. Константы `SCHEMA_VERSION = 1`, `RULES_VERSION = 1`. Итоговые `meta.stats` заполняются индексатором до завершения записи; для этого индексатор передаёт генератор, который обновляет этот словарь по окончании импорта.

- [ ] **1. Написать падающие тесты** `IndexStoreTests`: `test_roundtrip` проверяет все поля книги, список серий и `read_meta(path) == meta`; `test_fts_cyrillic` — SQL MATCH для `"булг"* AND "мастер"*` возвращает одну строку; `test_normalization` — `self.assertEqual(normalize('ЁЖ'), 'еж')`, NFKC преобразует полноширинные буквы; `test_empty_database` — допустим нулевой набор книг; `test_missing_fts5` — понятный `IndexBuildError`, а не traceback SQLite; `test_invalid_database` — `validate_index` отклоняет испорченный файл и несовпадающие счётчики. Проверить уникальность ZIP/member и неизменность уже существующего целевого файла при вызове `write_index` с занятым путём.
- [ ] **2. Красная стадия:** `TMPDIR=/tmp/opencode PYTHONPATH=src python3 -m unittest tests.test_index_store -v`.
- [ ] **3. Реализовать schema/build.** Таблицы `meta(key TEXT PRIMARY KEY, value TEXT)` и `book(id INTEGER PRIMARY KEY, archive TEXT, member TEXT, file_id TEXT, author TEXT, title TEXT, language TEXT, series_json TEXT, size INTEGER, libid TEXT, metadata_json TEXT, norm_author TEXT, norm_title TEXT, UNIQUE(archive, member))`; метаданные и списки сериализовать JSON без ASCII-экранирования. FTS: `book_fts(norm_title, norm_author, content='book', content_rowid='id', tokenize='unicode61 remove_diacritics 0')`. Заполнять обычную таблицу одной транзакцией и затем `INSERT INTO book_fts(book_fts) VALUES ('rebuild')`; опубликованная база неизменяема, триггеры не требуются. Использовать `journal_mode=DELETE`, без WAL. Временная база создаётся эксклюзивно по ещё не существующему пути. В `write_index` выполнить FTS integrity-check до закрытия пишущего соединения; `validate_index` использует только чтение, `PRAGMA integrity_check`, проверку metadata и числа книг. Чтение метаданных — URI `mode=ro`.
- [ ] **4. Зелёная стадия:** повторить тесты задачи 3 и общий `unittest discover`; ожидается `OK`, рядом с закрытой готовой базой нет `-wal` и `-shm`.
- [ ] **5. Коммит:** `git add src/keklib_cli/text.py src/keklib_cli/index_store.py tests/test_index_store.py` и `git commit -m "feat: build immutable SQLite FTS5 index"`.

### Задача 4. Автоматическая пересборка и сохранность старого индекса

**Файлы:** создать `src/keklib_cli/indexer.py`, `tests/test_indexer.py`.

**Интерфейсы:** потребляет задачи 1–3; предоставляет `sha256_file(path: Path) -> str` и `ensure_index(library: Library, paths: StatePaths, *, force: bool = False, progress: Callable[[str], None] | None = None) -> IndexResult`. `progress` получает однострочные сообщения и не является API управления исполнением. Частные файловые операции выносить в небольшие функции модуля, чтобы тестировать сбои через `unittest.mock`, а не через специальные production-флаги.

- [ ] **1. Написать падающие тесты** `IndexerTests`: `test_first_and_unchanged` — первый `result.rebuilt is True`, второй `False`, байты и mtime индекса не меняются; `test_hash_not_mtime` — заменить INPX на другие байты той же длины с прежним mtime, пересборка обязательна; `test_force_schema_rules_root` — каждый фактор инвалидирует индекс. `test_add_edit_delete_and_removed_inp` проверяет добавление, изменение TITLE/SERIES, установку DEL и исчезновение целого INP без старых остатков. `test_build_failure_preserves_old` и `test_cancel_preserves_old` сравнивают SHA-256 старой базы до/после. `test_source_changed_mid_build` требует отказа публикации; `test_busy` использует второй процесс с `flock`, ожидает `IndexBusyError`; `test_corrupt_index_rebuilt` ожидает пригодную новую базу. `test_cleanup_ownership` подтверждает, что чужой временный файл остаётся нетронутым.
- [ ] **2. Красная стадия:** `TMPDIR=/tmp/opencode PYTHONPATH=src python3 -m unittest tests.test_indexer -v`.
- [ ] **3. Реализовать протокол обновления из §7 спецификации.** `fcntl.flock(LOCK_EX | LOCK_NB)` на `index.lock`; повторная проверка под блокировкой. Создать собственный `build-*` через `mkdtemp` в cache; источник копировать потоково в снимок INPX внутри него, одновременно считать SHA-256; новую базу строить по отсутствующему пути в том же каталоге. В конце генератора заполнить финальные счётчики метаданных. До `os.replace` вызвать `validate_index`, проверить SHA-256 живого INPX против снимка и закрыть все пишущие соединения. При отмене/ошибке удалить только собственные снимок и временную базу, прежний индекс не трогать. Для восстановления после аварии допустим только выделенный служебный каталог `build-*` с маркером владельца `keklib-cli`, под блокировкой и без следования симлинкам; чужие файлы не удалять. При неудачной актуализации бросать ошибку, а не возвращать старый индекс. Проверять версию схемы/правил, metadata и `PRAGMA quick_check` существующей базы; повреждение вызывает пересборку.
- [ ] **4. Зелёная стадия:** повторить тесты задачи 4 и общий набор; ожидается `OK`. Отдельно убедиться, что тест изменения источника действительно меняет INPX между чтением снимка и финальной проверкой, а не только mock-результат хеша.
- [ ] **5. Коммит:** `git add src/keklib_cli/indexer.py tests/test_indexer.py` и `git commit -m "feat: refresh catalog index atomically"`.

### Задача 5. Поисковая сессия и стабильные страницы

**Файлы:** создать `src/keklib_cli/search.py`, `tests/test_search.py`; расширить `src/keklib_cli/text.py`.

**Интерфейсы:** потребляет индекс задачи 3; предоставляет `query_tokens(query: str) -> tuple[str, ...]`, `build_match(tokens: tuple[str, ...]) -> str` в `text.py` и контекстный менеджер `SearchSession(index: Path)` с методом `search(query: str, *, page: int = 0) -> SearchPage`. Размер страницы — константа `PAGE_SIZE = 20`; неправильный page — `SearchError`.

- [ ] **1. Написать падающие тесты** `SearchTests`: `test_required_prefix_tokens` — `булг мастер` находит нужную книгу, но не только «Мастера» другого автора; `test_yo_case_nfkc` — эквивалентность `Ё`/`е`, регистра и совместимых Unicode-форм; `test_punctuation_and_operators` — `self.assertEqual(build_match(('or',)), '\"or\"*')`, пустые токены дают `SearchError`, кавычки не ломают запрос, `OR` не становится оператором. `test_exact_title_first` требует точного названия первым. `test_pages` с 41 книгой даёт размеры `20,20,1`, общий total 41 и устойчивый порядок без повторов. `test_snapshot_during_replacement` — открытая сессия сохраняет прежние результаты после `os.replace` базы. `test_distinct_editions_and_merged_series` показывает одну книгу с двумя сериями и отдельную книгу для другого member.
- [ ] **2. Красная стадия:** `TMPDIR=/tmp/opencode PYTHONPATH=src python3 -m unittest tests.test_search -v`.
- [ ] **3. Реализовать поиск.** Токены — последовательности Unicode-букв/цифр нормализованного запроса, без `_`; каждый заключается в двойные кавычки, кавычки экранируются, после него `*`, соединение ` AND `. MATCH передаётся SQL-параметром. Сортировка: `(norm_title = normalize(query)) DESC`, затем `bm25(book_fts) ASC`, author, title, archive, member. LIMIT 20 и OFFSET `page * 20`, COUNT по тому же MATCH. Соединение `mode=ro`, явная read-транзакция и фиксация снимка до выдачи первой страницы; закрывать при выходе из контекста. Восстанавливать `Book` и `Series` из сохранённых JSON, не перечитывать INPX.
- [ ] **4. Зелёная стадия:** повторить тесты задачи 5 и общий набор; ожидается `OK`, запросы не изменяют индекс и не создают sidecar-файлов.
- [ ] **5. Коммит:** `git add src/keklib_cli/search.py src/keklib_cli/text.py tests/test_search.py` и `git commit -m "feat: search catalog with stable numbered pages"`.

### Задача 6. Проверка архива и чтение выбранного FB2

**Файлы:** создать `src/keklib_cli/archive.py`, `tests/test_archive.py`.

**Интерфейсы:** потребляет `Library`, `Book`, `ExtractionError`; предоставляет контекстный менеджер `open_book(library: Library, book: Book) -> Iterator[BinaryIO]`. Чтение до EOF проверяет CRC; владельцем ZIP и файловых дескрипторов остаётся контекстный менеджер.

- [ ] **1. Написать падающие тесты** `ArchiveTests`: `test_exact_member` — `self.assertEqual(stream.read(), fb2_bytes)` и соседний member не читается; `test_missing_new_archive`/`test_missing_member` — ошибки с точным именем, без поиска похожего издания; `test_duplicate_zip_entries` — ошибка при двух одинаковых именах в ZIP, независимо от идентичности их байтов. `test_path_escape` — отказ для `../`, абсолютного пути, обратного слеша и симлинка наружу; `test_directory_or_symlink_member` — такие записи не трактуются как обычный FB2. `test_crc_corruption`, `test_encrypted_or_unsupported`, `test_replaced_zip_after_search` — понятный `ExtractionError` и закрытые дескрипторы. `test_caller_error_not_relabelled` — `OSError`, возникший в теле вызывающего кода, не превращается в ошибку чтения ZIP. CRC-fixture строить как ZIP_STORED и менять байт данных, сохранив старый CRC.
- [ ] **2. Красная стадия:** `TMPDIR=/tmp/opencode PYTHONPATH=src python3 -m unittest tests.test_archive -v`.
- [ ] **3. Реализовать точечное чтение.** Повторно валидировать пути из Book, разрешать путь архива относительно разрешённого `library.archives` и проверять containment через `relative_to`, не строковый prefix. Симлинк наружу запрещён. По `infolist()` требовать ровно один точный member, отклонить каталог, UNIX-symlink и зашифрованную запись. Преобразовывать `BadZipFile`, `RuntimeError`, `NotImplementedError` и файловые ошибки собственных операций открытия/чтения в предметную ошибку с контекстом; обёртка потока переводит ошибки непосредственно внутри `read`, но не ловит ошибки вызывающего кода вокруг `yield`. `KeyboardInterrupt` не маскировать. Не вызывать `extract`/`extractall`.
- [ ] **4. Зелёная стадия:** повторить тесты задачи 6 и общий набор; ожидается `OK`, байты исходных ZIP не меняются.
- [ ] **5. Коммит:** `git add src/keklib_cli/archive.py tests/test_archive.py` и `git commit -m "feat: read exact book member with archive validation"`.

### Задача 7. Безопасное имя и публикация без перезаписи

**Файлы:** создать `src/keklib_cli/output.py`, `tests/test_output.py`.

**Интерфейсы:** потребляет `open_book` и модели; предоставляет `safe_filename(book: Book, *, max_bytes: int = 240) -> str`, `publish_no_replace(temp: Path, target: Path) -> None`, `extract_book(library: Library, book: Book, destination: Path) -> Path`.

- [ ] **1. Написать падающие тесты** `OutputTests`: `test_utf8_filename` — итоговое UTF-8-имя ≤240 байт и заканчивается `' [66372].fb2'`; `test_empty_metadata_fallback` — `'66372.fb2'`; `test_unsafe_metadata` — нет `/`, `\\`, NUL и управляющих символов, файл остаётся внутри destination. `test_exact_bytes` — результат байт-в-байт совпадает с FB2; `test_existing_file_or_symlink` сохраняет старые байты и ссылку, бросает `TargetExistsError`; `test_race` создаёт target между проверкой и публикацией и требует отсутствия перезаписи. `test_crc_write_space_cancel_failures` — нет готового файла и собственного temp после каждого сбоя; `test_unsupported_publication` — понятная ошибка без небезопасного fallback; `test_existing_output_not_deleted_on_cancel` сохраняет чужие файлы.
- [ ] **2. Красная стадия:** `TMPDIR=/tmp/opencode PYTHONPATH=src python3 -m unittest tests.test_output -v`.
- [ ] **3. Реализовать вывод.** Разобрать формат авторов `Фамилия,Имя,Отчество:` в читаемую строку; несколько авторов разделять `; `. Имя строить по формату спецификации, управляющие Unicode-категории `C*` и разделители заменять пробелами, схлопывать пробелы, обрезать человеческую часть по UTF-8-байтам без разрыва кодовой точки и с сохранением ID/расширения; слишком длинный ID отклонить явно. Создать destination и temp через `mkstemp` в нём, потоково скопировать, дочитать CRC, flush/fsync/close. Публиковать сначала атомарным `os.link(temp, target, follow_symlinks=False)` с удалением собственного temp после успеха. Только при неподдерживаемости hardlink использовать Linux `renameat2(RENAME_NOREPLACE)` через `ctypes` с `use_errno=True`; EEXIST всегда конфликт, отсутствие обоих безопасных вариантов — ошибка. Не использовать `os.replace` для итогового FB2. В `finally` удалять только собственный temp, никогда target.
- [ ] **4. Зелёная стадия:** повторить тесты задачи 7 и общий набор; ожидается `OK`. Тест гонки должен доходить до реальной атомарной операции публикации, а не только предварительной проверки `exists()`.
- [ ] **5. Коммит:** `git add src/keklib_cli/output.py tests/test_output.py` и `git commit -m "feat: extract books with atomic no-clobber output"`.

### Задача 8. CLI: поиск, страницы, выбор и извлечение

**Файлы:** создать `src/keklib_cli/terminal.py`, `tests/test_terminal.py`, `tests/test_cli.py`; изменить `src/keklib_cli/cli.py`, при необходимости `tests/fixtures.py`.

**Интерфейсы:** потребляет все предыдущие компоненты; предоставляет `display_text(value: str) -> str`, `render_page(page: SearchPage, stdout: TextIO) -> None`, `choose_book(session: SearchSession, query: str, *, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> Book | None` в `terminal.py`; завершает `main` из задачи 1. CLI не разбирает строки INPX и не извлекает ZIP самостоятельно.

- [ ] **1. Написать падающие тесты** `TerminalTests` и `CliTests`: `test_number_on_current_page` — на второй странице разрешён 21, но не 1; `test_n_p_boundaries` — нет перехода до первой/после последней; `test_bad_input_then_valid`, `test_q_eof_empty_results` — неверный ввод не извлекает, выход/пустая выдача возвращают `None`. `test_control_characters` — метаданные с ESC/newline/bidi не оставляют `\x1b`, `\n` внутри поля и Unicode `C*` в отображаемом значении. `test_setup_checks_structure_before_save` — повреждённая структура не заменяет старую конфигурацию. `test_non_tty` — отказ до индексации, `test_extract_flow` — `main(...) == 0`, один файл правильных байтов, абсолютный путь в stdout; `test_failed_refresh_no_stale_selection` — нет приглашения выбора и старого результата после сбоя обновления. `test_output_override`, `test_missing_updated_zip`, `test_exit_codes_and_channels` проверяют пути, диагностику в stderr и отсутствие traceback для предметных ошибок.
- [ ] **2. Красная стадия:** `TMPDIR=/tmp/opencode PYTHONPATH=src python3 -m unittest tests.test_terminal tests.test_cli -v`.
- [ ] **3. Реализовать UI и оркестрацию.** `setup --library` вызывает `inspect_library`, затем `read_structure`, затем `save_library`. `index --refresh` вызывает `ensure_index(force=True)`. Для `search` сначала проверить запрос и `stdin.isatty()`, затем загрузить настройку, актуализировать индекс, открыть одну SearchSession, выбрать Book и вызвать `extract_book`. `stdout` можно перенаправлять; терминальность требуется для stdin. Номера — `page * 20 + offset + 1`; хранить сопоставление только текущей страницы, не вычислять выбранный Book заново. `display_text` заменяет Unicode-категории C* пробелами и схлопывает пробелы; исходные строки в базе не меняются. Прогресс/ошибки/приглашение — stderr, таблица/абсолютный итоговый путь — stdout. Коды: `0` — успех, нет результатов, `q` или EOF; `1` — предметная/файловая ошибка; `2` — некорректные аргументы/запрос и не-TTY; `130` — Ctrl+C. Файловые ошибки на границе CLI преобразовать в краткую диагностику; программные ошибки не скрывать общим `except Exception`.
- [ ] **4. Зелёная стадия:** повторить тесты задачи 8 и общий набор; затем `PYTHONPATH=src python3 -m keklib_cli search --help`. Ожидается `OK`, в справке `--output`, никакого открытия настоящей библиотеки.
- [ ] **5. Коммит:** `git add src/keklib_cli/terminal.py src/keklib_cli/cli.py tests/test_terminal.py tests/test_cli.py tests/fixtures.py` и `git commit -m "feat: wire numbered interactive book extraction CLI"`.

### Задача 9. Сквозная приёмка, измерения и документация

**Файлы:** создать `tests/test_integration.py`, `tools/verify_library.py`, `README.md`; уточнить `pyproject.toml` только по результатам упаковочной проверки.

**Интерфейсы:** потребляет готовый CLI и публичные компоненты. `tools/verify_library.py --library PATH --workdir PATH [--reuse-index]` работает только с явно указанным новым/пустым выделенным workdir вне library; там находятся его config/cache/output и JSON-отчёт. Исключение — `--reuse-index` для собственного отмеченного workdir предыдущей приёмки. Скрипт запускается из checkout через `PYTHONPATH=src`, импортирует пакет, но не содержит второго импортера INPX или второй реализации поиска. `query_seconds` измеряет `SearchSession.search`, включая COUNT и выдачу страницы, но без хеширования/построения.

- [ ] **1. Написать падающие сквозные тесты** `IntegrationTests`: `test_setup_search_select_subprocess` запускает настоящий `python -m keklib_cli`, подаёт выбор через `pty.openpty()`, проверяет код 0 и SHA-256 выходного FB2; таймаут закрывает только собственный subprocess. `test_update_then_extract` меняет синтетический INPX и добавляет ZIP, проверяет новую книгу и исчезновение старой. `test_read_only_sources` снимает SHA-256 и `mtime_ns` INPX/ZIP до/после; atime не сравнивает. `test_source_identity_change` переключает библиотеку, старые результаты не появляются. `test_verify_refuses_library_workdir` требует отказа без единой записи в library; `test_verify_report` на маленькой fixture с известной парой ZIP/member проверяет поля отчёта и выделенный output. `test_verify_reuse_unchanged` требует `rebuilt is False`, прежний SHA-256 индекса и отсутствие перезаписи FB2; `test_verify_missing_known_book` требует явного отказа без ложного успешного отчёта. `test_help_names` требует `keklib-cli` и отсутствие прежнего имени в справке.
- [ ] **2. Красная стадия:** `TMPDIR=/tmp/opencode PYTHONPATH=src python3 -m unittest tests.test_integration -v`; отсутствующий verify-скрипт/неполная интеграция должны обнаруживаться тестом, не пропускаться.
- [ ] **3. Реализовать приёмочный скрипт и README.** Скрипт строит индекс, фиксирует elapsed/hash/query отдельно через `perf_counter`, пиковый RSS через `resource.getrusage` с единицей KiB для Linux, затем ищет `Булгаков Мастер`, перебирает страницы и выбирает точное название `Мастер и Маргарита` с известной парой `fb2-060424-074391.zip`/`66372.fb2`, если она присутствует. Если известной пары в обновлённом каталоге нет, сообщить об этом, не подставлять произвольное издание и не объявлять тест пройденным. Извлекает книгу через `extract_book` в workdir/output и сравнивает SHA-256 с отдельным потоковым чтением через `open_book`. `--reuse-index` проверяет маркер своего workdir, актуальность прежнего индекса и хеш уже извлечённого FB2 без его перезаписи. Контроль источников: SHA-256 INPX и выбранного ZIP до/после, stat остальных ZIP без чтения 451 ГБ; в отчёте явно указать предел проверки. Поля отчёта: `catalog_sha256`, `row_count`, `book_count`, `hash_seconds`, `build_seconds`, `query_seconds`, `peak_rss_kib`, `rebuilt`, `index_sha256`, `selected_archive`, `selected_member`, `output_sha256`, `source_unchanged`, `query_target_met`. README описывает venv, локальную установку, команды, XDG, обновление/отмену, ограничение FTS, конфликт имён и безопасную публикацию; никаких массовых импортов в Calibre и распаковки всей библиотеки.
- [ ] **4. Выполнить полную проверку и реальную приёмку.** Сначала `TMPDIR=/tmp/opencode PYTHONPATH=src python3 -m unittest discover -s tests -t . -v` и `PYTHONPATH=src python3 -m compileall -q src tests tools`; ожидается `OK` и код 0. Затем `python3 -m venv .venv` и локальная установка `.venv/bin/python -m pip install -e .`; это единственный этап установки, только после разрешения исполнения, без sudo и глобального изменения Python. Проверить `.venv/bin/keklib-cli --help` и `.venv/bin/python -m unittest discover -s tests -t . -v` с `TMPDIR=/tmp/opencode`. После этого выполнить команду ниже с новым выделенным workdir; ожидается совпадение SHA-256, `source_unchanged=true`, SQL-поиск ≤1 секунды. Если ориентир не выполнен, исследовать причину и записать фактические цифры, не объявлять полную готовность. Повторить приёмку с тем же подготовленным индексом через отдельный поддержанный скриптом режим `--reuse-index`: `rebuilt=false` и прежний SHA-256 индекса; этот режим разрешён только для workdir с собственным маркером приёмочного скрипта.

```bash
TMPDIR=/tmp/opencode PYTHONPATH=src .venv/bin/python tools/verify_library.py --library '/run/media/user/Untitled/_Lib.rus.ec - Официальная' --workdir /tmp/opencode/keklib-cli-acceptance
TMPDIR=/tmp/opencode PYTHONPATH=src .venv/bin/python tools/verify_library.py --library '/run/media/user/Untitled/_Lib.rus.ec - Официальная' --workdir /tmp/opencode/keklib-cli-acceptance --reuse-index
```

Повторная приёмка не извлекает поверх существующего файла: сравнивает прежний результат и повторно проверяет источник. Если указанный каталог занят чужими данными, остановиться и выбрать другой, не очищать его.

- [ ] **5. Зафиксировать приёмку и документацию:** `git add tests/test_integration.py tools/verify_library.py README.md pyproject.toml` и `git commit -m "test: verify keklib-cli end to end and document usage"`. Отчёт с локальными путями и извлечённую книгу не включать в Git. До заявления о готовности выполнить `verification-before-completion`, затем согласованный обзор кода; интеграцию ветки обсуждать отдельно, не сливать автоматически.

## Самопроверка плана

Следующие отметки относятся только к прежней Linux-редакции и не подтверждают соответствие новому требованию трёх ОС. Перед исполнением проверка должна быть выполнена заново по обновлённому плану.

- [x] Цель, имена и ограничения спецификации отражены в общих условиях и задачах 1, 8, 9.
- [x] Обновление INPX с добавлениями, изменениями, удалениями и несколькими сериями покрыто задачами 2 и 4.
- [x] Неизменность библиотеки, атомарность индекса и извлечения покрыты задачами 4, 6, 7 и 9.
- [x] Все публичные типы и функции определены до использования; единственный владелец CLI — задача 8.
- [x] Пять классов дополнительных рисков привязаны к конкретным тестам, а не оставлены общими пожеланиями.
- [x] У каждой задачи есть красная/зелёная стадии и самостоятельный коммит; код продукта на стадии планирования не создаётся.
- [x] Реальная приёмка и время поиска отделены от синтетических тестов и времени хеширования/импорта.

## Передача к исполнению

Передача приостановлена до обновления этого плана под Linux, Windows и macOS. Ни один из вариантов ниже пока не разрешает исполнение прежней редакции.

Пользователь проверяет план, подтверждает путь проекта и выбирает способ исполнения:
- **Native:** задачи реализуются последовательно в текущей сессии через `executing-plans`, затем отдельный независимый обзор готовой ветки. Рекомендуется здесь: задачи тесно связаны общими моделями, а объём ограничен одним локальным CLI.
- **Subagent-driven:** `subagent-driven-development`, отдельные исполнитель и проверки для каждой задачи, затем обзор ветки. Больше независимых проверок, но больше затрат на передачу контекста.

Подтверждение спецификации не является подтверждением этого ещё не рассмотренного плана. До выбора способа и проверки документа реализация не начинается.
