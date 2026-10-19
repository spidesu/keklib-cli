"""Нумерованный терминальный интерфейс keklib-cli (§4 спецификации).

UI получает потоки явно (внедрение зависимостей): глобальные ``input`` и
``print`` внутри компонентов не используются. ``display_text`` заменяет
пробелом все Unicode-категории C* (управляющие C0/C1, форматные, включая
bidi-переопределения, суррогаты, private use, неназначенные), чтобы
метаданные книг не управляли терминалом и не рвали таблицу; ``write_line``
дополнительно заменяет символы, непредставимые в кодировке потока, поэтому
узкая кодировка терминала не приводит к traceback и не меняет данные на
диске.

Каналы (§4): таблица результатов и итоговый путь — ``stdout`` (его разрешено
перенаправлять); приглашение выбора, сообщения о страницах и ошибках —
``stderr``. Динамический текст (пути, диагностика, ввод) во всех
пользовательских каналах проходит :func:`display_text` через
:func:`write_sanitized_line`; доверенные тексты (таблица, справка) пишутся
:func:`write_line` с сохранением собственных переводов строк. Нумерация
строк сквозная в рамках поиска (``page * PAGE_SIZE + offset + 1``), выбор
действует только по номерам текущей страницы.
"""

from __future__ import annotations

import math
import unicodedata
from typing import TextIO

from keklib_cli.models import Book, SearchPage
from keklib_cli.output import _readable_author
from keklib_cli.search import PAGE_SIZE, SearchSession

# Приватный _readable_author переиспользуется намеренно (как закрытые
# помощники индексатора в search.py): форма «Имя Фамилия» в таблице и в имени
# извлекаемого файла обязана совпадать; вторая реализация разошлась бы.
__all__ = [
    "choose_book",
    "display_text",
    "render_page",
    "write_line",
    "write_sanitized_line",
]

#: Предел длины цифрового ответа. Любой номер страницы короче; значение
#: заведомо ниже предела преобразования десятичных строк Python 3.11+
#: (4300 цифр), который глобально не отключается.
_MAX_NUMBER_DIGITS = 18


def display_text(value: str) -> str:
    """Заменить пробелом все символы Unicode-категорий C*.

    Cc (управляющие, включая ESC и переводы строк), Cf (форматные, включая
    bidi RLO/LRO), Cs (суррогаты), Co (private use) и Cn (неназначенные) не
    управляют терминалом и не переносят строки таблицы. Остальной текст,
    включая пунктуацию и пробелы, проходит без изменений: исходные
    метаданные на диске не меняются, санитизируется только показ.
    """
    return "".join(
        " " if unicodedata.category(char)[0] == "C" else char for char in value
    )


def write_line(stream: TextIO, text: str = "") -> None:
    """Записать доверенную строку с переводом строки; кодировка безопасна.

    Символы, которых нет в кодировке потока (например, эмодзи в узкой
    кодировке терминала), заменяются на «?» — отсутствие возможности вывести
    отдельный символ не приводит к traceback и не портит данные (§10).
    Переводы строк внутри ``text`` сохраняются: канал преднан для
    собственных структурных текстов (строки таблицы, справка).
    """
    stream.write(_encodable(text, stream) + "\n")


def write_sanitized_line(stream: TextIO, text: str = "") -> None:
    """Записать динамический текст одной строкой: Unicode C* → пробелы.

    Пути, сообщения предметных ошибок и другой динамический текст не
    управляют терминалом и не подделывают строки диагностики: ESC, bidi и
    внедрённые переводы строк заменяются пробелом через :func:`display_text`;
    затем применяется безопасная замена непредставимых в кодировке потока
    символов. Реальные значения на диске не меняются — санитизируется только
    показ. Собственный структурный перевод строки добавляется в конце.
    """
    stream.write(_encodable(display_text(text), stream) + "\n")


def render_page(page: SearchPage, stdout: TextIO) -> None:
    """Напечатать таблицу страницы результатов со сквозной нумерацией.

    Номер строки — глобальный: ``page * PAGE_SIZE + offset + 1``; выбор
    возможен только по номерам текущей страницы. Строка таблицы: номер,
    автор, название, язык, серии с номерами, размер (§4).
    """
    if not page.books:
        return
    page_count = max(1, math.ceil(page.total / PAGE_SIZE))
    write_line(
        stdout,
        f"Найдено книг: {page.total}; страница {page.page + 1} из {page_count}",
    )
    first = page.page * PAGE_SIZE + 1
    width = len(str(first + len(page.books) - 1))
    for offset, book in enumerate(page.books):
        number = first + offset
        write_line(stdout, f"{number:>{width}}. {_describe(book)}")


def choose_book(
    session: SearchSession,
    query: str,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> Book | None:
    """Показывать страницы ``query`` и читать выбор номера; вернуть книгу.

    При отсутствии результатов выводится сообщение без приглашения выбора.
    EOF в приглашении эквивалентен ``q``; неправильный ввод (в том числе
    номер вне текущей страницы) повторяет приглашение без извлечения.
    ``n``/``p`` не выходят за границы первой/последней страницы. Выбранный
    номер — всегда строка текущей страницы снимка индекса этой сессии.
    Таблица и приглашение сбрасываются (flush) до блокирующего чтения;
    динамические сообщения проходят санитизацию Unicode C* (§4, §10).

    :return: выбранная ``Book`` либо ``None`` при ``q``/EOF/пустой выдаче.
    :raises SearchError: запрос не содержит слов или индекс не читается.
    """
    page = session.search(query)
    if page.total == 0:
        write_sanitized_line(stderr, f"по запросу {query!r} книг не найдено")
        return None
    while True:
        render_page(page, stdout)
        page_count = max(1, math.ceil(page.total / PAGE_SIZE))
        stderr.write(
            _encodable(
                f"Страница {page.page + 1} из {page_count}; "
                "введите номер книги, n, p или q: ",
                stderr,
            )
        )
        # Оба канала сбрасываются до блокирующего чтения: иначе перенаправленный
        # stdout или буферизованный stderr могут показать таблицу/приглашение
        # только после ответа пользователя.
        stdout.flush()
        stderr.flush()
        answer = stdin.readline()
        if answer == "":
            write_sanitized_line(stderr, "конец ввода; выход без извлечения")
            return None
        command = answer.strip().lower()
        if command == "q":
            write_sanitized_line(stderr, "выход без извлечения")
            return None
        if command == "n":
            if page.page + 1 >= page_count:
                write_sanitized_line(stderr, "последняя страница")
                continue
            page = session.search(query, page=page.page + 1)
            continue
        if command == "p":
            if page.page == 0:
                write_sanitized_line(stderr, "первая страница")
                continue
            page = session.search(query, page=page.page - 1)
            continue
        if command.isascii() and command.isdigit():
            # Длина проверяется до int(): ввод длиной в тысячи цифр —
            # неправильный ввод с повтором приглашения, а не ValueError
            # (предел десятичных строк Python не отключается). readline
            # читает строку целиком, поэтому «хвост» длинного ввода не
            # становится следующей командой.
            if len(command) > _MAX_NUMBER_DIGITS:
                write_sanitized_line(
                    stderr, "слишком длинное число; введите номер книги"
                )
                continue
            first = page.page * PAGE_SIZE + 1
            last = first + len(page.books) - 1
            number = int(command)
            if first <= number <= last:
                return page.books[number - first]
            write_sanitized_line(stderr, f"номер должен быть от {first} до {last}")
            continue
        write_sanitized_line(
            stderr,
            f"неправильный ввод {answer.strip()!r}; "
            "введите номер книги, n, p или q",
        )


def _encodable(text: str, stream: TextIO) -> str:
    """Вернуть ``text``, заменяя символы вне кодировки потока на «?».

    Проверка выполняется до записи, поэтому частичный вывод невозможен;
    неизвестное имя кодировки не блокирует вывод (пишется как есть).
    """
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return text  # поток без кодировки не может отказать в записи
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace").decode(
            encoding, errors="replace"
        )
    except LookupError:
        return text
    return text


def _describe(book: Book) -> str:
    """Собрать строку таблицы: автор, название, язык, серии, размер."""
    author = display_text(_readable_author(book.author)).strip()
    title = display_text(book.title).strip()
    head = " — ".join(part for part in (author, title) if part)
    if not head:
        head = display_text(book.file_id).strip() or "?"
    parts = [head]
    language = display_text(book.language).strip()
    if language:
        parts.append(f"[{language}]")
    series = _describe_series(book)
    if series:
        parts.append(f"({series})")
    parts.append(f"{book.size} Б" if book.size is not None else "размер неизвестен")
    return " ".join(parts)


def _describe_series(book: Book) -> str:
    """Серии книги с номерами через «; » в порядке появления."""
    names: list[str] = []
    for item in book.series:
        name = display_text(item.name).strip()
        number = display_text(item.number).strip()
        if name and number:
            names.append(f"{name} #{number}")
        elif name:
            names.append(name)
    return "; ".join(names)
