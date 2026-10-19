# Установка keklib-cli

Установочная документация. Основная форма распространения — portable
standalone: архив с исполняемым файлом и всеми его зависимостями,
**без установки Python** конечным пользователем.

## Доступность

Публичного готового portable-релиза пока нет: архивы не выложены в
интернете и не подписаны сертификатом. Сборки, прошедшие нативную
приёмку на своих сборочных ОС, существуют для всех четырёх target
(Linux x86-64 — Ubuntu 22.04; Windows x64 — Windows Server 2022;
macOS Intel — macOS 15; macOS Apple Silicon — macOS 14) и передаются
владельцем проекта напрямую; полный выпускной гейт четырёхплатформенной
матрицы и публикация Release — отдельный незавершённый шаг, черновики
Release публичной поставкой не являются.
Версию и параметры конкретного архива (version, target, python, sqlite,
rules_version, minimum_os) см. в его `build-info.json`.

Конфигурация и кэш portable-версии хранятся в нативных пользовательских
каталогах ОС, а не рядом с программой; состояние библиотеки (исходные
INPX/ZIP) только читается.

## Требования

- Linux x86-64, glibc >= 2.35 (baseline Ubuntu 22.04; не musl/Alpine).
- Windows x64 — baseline Windows Server 2022 (сборочная ОС); клиентские
  выпуски Windows отдельно не проверялись и этой документацией не
  заявляются.
- macOS Intel — baseline macOS 15; macOS Apple Silicon — baseline
  macOS 14. Более старые выпуски macOS не заявляются.
- Python не нужен: runtime входит в архив. CPython 3.11+ требуется
  только для запуска из исходников (см. конец файла).

## 1. Выбор архива

Архив выбирается по ОС и CPU; имя вида
`keklib-cli-<version>-<target>.<ext>`:

- Linux x64: `keklib-cli-<version>-linux-x86_64.tar.gz`;
- Windows x64: `keklib-cli-<version>-windows-x86_64.zip`;
- macOS Intel: `keklib-cli-<version>-macos-x86_64.tar.gz`;
- macOS Apple Silicon: `keklib-cli-<version>-macos-arm64.tar.gz`.

В командах этой документации угловые заполнители вида `<version>`
(и `<путь-к-библиотеке>` и т.п.) заменяйте фактическими значениями
**до** выполнения: без подстановки shell трактует `<` и `>` как
перенаправления потоков.

Поле `minimum_os` в `build-info.json` — это **заявленный baseline
сборочной машины**, а не доказанный минимальный клиентский выпуск ОС:
более старые или иные выпуски не обещаются без отдельной нативной
проверки.

## 2. Проверка SHA-256

Каждому архиву соответствует файл `<archive>.sha256`. SHA-256 подтверждает
**целостность** данных (совпадение байтов с тем, что собрал автор), но
**не заменяет доверенную цифровую подпись** и не доказывает авторство:
сумму надо сверять с источником, которому вы доверяете (страница релиза
проекта или передача из первых рук) — приложенный файл `.sha256` и есть
источник суммы. Подписи сертификатом нет.

Linux:

```bash
sha256sum -c keklib-cli-<version>-linux-x86_64.tar.gz.sha256
```

macOS (встроенная утилита понимает формат sidecar `sha256sum`):

```bash
shasum -a 256 -c keklib-cli-<version>-macos-x86_64.tar.gz.sha256
```

Windows PowerShell — сверьте выведенное значение с содержимым файла
`keklib-cli-<version>-windows-x86_64.zip.sha256` (случай символов в hex
не различается):

```powershell
Get-FileHash .\keklib-cli-<version>-windows-x86_64.zip -Algorithm SHA256
Get-Content .\keklib-cli-<version>-windows-x86_64.zip.sha256
```

## 3. Распаковка и запуск без Python

Распаковывается **вся папка** `keklib-cli/` (не один исполняемый файл):
рядом с ним лежит `_internal/` с собственным runtime. Python, pip и FTS5
устанавливать не нужно.

```bash
mkdir -p ~/Apps
tar -xzf keklib-cli-<version>-linux-x86_64.tar.gz -C ~/Apps
~/Apps/keklib-cli/keklib-cli --help
```

В Windows распакуйте ZIP проводником или PowerShell и запускайте
`keklib-cli\keklib-cli.exe`. Установка Python не требуется.

## 4. PATH (необязательно, без прав администратора)

Полный путь к программе писать не обязательно: добавьте каталог с
исполняемым файлом в PATH пользовательского окружения. В Linux/macOS —

```bash
echo 'export PATH="$HOME/Apps/keklib-cli:$PATH"' >> ~/.profile
```

— в Windows — «Параметры → Система → О системе → Дополнительные параметры
→ Переменные среды» для текущего пользователя (администратор не нужен).
Программа работает и без PATH, по полному пути.

## 5. Библиотека, поиск, обновление

```bash
~/Apps/keklib-cli/keklib-cli setup --library "/путь/к/библиотеке"
~/Apps/keklib-cli/keklib-cli search "Булгаков Мастер"
~/Apps/keklib-cli/keklib-cli search "Мастер и Маргарита" --output ~/Books
```

Те же команды в PowerShell:

```powershell
C:\Apps\keklib-cli\keklib-cli.exe setup --library "C:\Lib.rus.ec"
C:\Apps\keklib-cli\keklib-cli.exe search "Булгаков Мастер"
C:\Apps\keklib-cli\keklib-cli.exe search "Мастер и Маргарита" --output $HOME\Books
```

- `setup` проверяет корень (`librusec_flib.inpx`, `lib.rus.ec/`) до
  сохранения настройки; `search` требует интерактивного терминала,
  выбор книги — вводом номера; существующий файл не перезаписывается
  (код 1, подсказка `--output`).
- Обновление программы — распаковкой новой версии в **отдельную папку**
  (например `~/Apps/keklib-cli-0.2.0/`): замена файлов внутри работающей
  копии не поддерживается. Настройки и индекс при этом сохраняются — они
  живут в пользовательских каталогах ОС, а не в папке программы.
- Обновление библиотеки (новый INPX/ZIP) отдельной заменой не требует
  переустановки: следующий поиск сам перестроит индекс.

## 6. Где настройки и как удалить

Конфигурация и индекс: Linux — `~/.config/keklib-cli/`,
`~/.cache/keklib-cli/`; Windows — `%APPDATA%\keklib-cli\`,
`%LOCALAPPDATA%\keklib-cli\Cache\`; macOS —
`~/Library/Application Support/keklib-cli/`,
`~/Library/Caches/keklib-cli/`.

Удаление программы — удаление её папки целиком; **книги и библиотека
при этом не затрагиваются**. Чтобы удалить и настройки/индекс, удалите
перечисленные выше каталоги состояния.

## 7. Предупреждения неподписанной поставки

Архивы не подписаны доверенным сертификатом (подпись и Apple
notarization не входят в первую версию). Windows SmartScreen и macOS
Gatekeeper могут предупреждать о запуске неподписанной программы,
скачанной из сети. Не отключайте защиту ОС глобально: проверьте
происхождение файла и его SHA-256, и лишь затем разрешите запуск
конкретной программы средствами самой ОС.

## Форма разработки из исходного кода (Linux, проверена)

Форма для разработки и отладки, не способ установки для конечного
пользователя (команды CLI покрыты автотестами на Linux):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m keklib_cli --help
```

Форма для Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m keklib_cli --help
```

## См. также

- `README.md` — назначение и доступность поставки.
- `BUILD.md` — сборка, тесты и гейты конвейера.
- `AGENTS.md` — правила проекта для разработчиков и агентов.
