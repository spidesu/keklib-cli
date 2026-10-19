"""Тесты нумерованного терминального интерфейса (`keklib_cli.terminal`).

UI получает потоки явно; настоящая библиотека не используется — все сценарии
идут по синтетическим библиотекам во временных каталогах. Проверяются
сквозная нумерация страниц, границы n/p, повтор приглашения при
неправильном вводе, q/EOF без извлечения, санитизация Unicode C* и запись в
поток с узкой кодировкой без traceback.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from keklib_cli import terminal
from keklib_cli.models import Book, SearchPage, Series, StatePaths
from keklib_cli.search import open_search
from keklib_cli.terminal import choose_book, display_text, render_page, write_line
from tests.fixtures import make_library

#: Запрос, которому соответствуют все книги страничного фиксжа.
QUERY = "альфа"


def _row(file_id: str, author: str, title: str, **extra: str) -> dict[str, str]:
    """Строка INP активной FB2-книги с необязательными полями."""
    record = {
        "AUTHOR": author,
        "TITLE": title,
        "FILE": file_id,
        "DEL": "0",
        "EXT": "fb2",
    }
    record.update(extra)
    return record


class _ScriptedTTY:
    """stdin с запрограммированными строками; после исчерпания — EOF."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)

    def readline(self) -> str:
        return self._answers.pop(0) if self._answers else ""

    def isatty(self) -> bool:
        return True


def _book(file_id: str, author: str = "Автор", title: str = "Название") -> Book:
    """Книга со всеми отображаемыми полями для ручных SearchPage."""
    return Book(
        archive="a.zip",
        member=f"{file_id}.fb2",
        file_id=file_id,
        author=author,
        title=title,
        language="ru",
        series=(Series(name="Серия", number="1"),),
        size=1000,
        libid=file_id,
        metadata={},
    )


class DisplayTests(unittest.TestCase):
    def test_display_text_strips_all_c_categories(self):
        cases = {
            "А\x1b[31mБ": "А [31mБ",  # Cc: ESC не управляет терминалом
            "Стр\x00ока": "Стр ока",  # Cc: NUL
            "перевод\nстроки": "перевод строки",  # Cc: перевод строки не рвёт таблицу
            "Мастер\u202eи Маргарита": "Мастер и Маргарита",  # Cf: bidi RLO
            # Замена — именно пробел: существующий хвостовой пробел сохраняется.
            "суррогат \ud800": "суррогат  ",  # Cs
            "private \ue000": "private  ",  # Co
            "unassigned \u0378": "unassigned  ",  # Cn
            "Обычный текст, ё:?*": "Обычный текст, ё:?*",  # пунктуация сохранена
        }
        for value, expected in cases.items():
            with self.subTest(value=repr(value)):
                self.assertEqual(display_text(value), expected)

    def test_write_line_narrow_encoding(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1251")
        write_line(stream, "Мастер \U0001f4d6 и Маргарита")
        stream.flush()
        text = raw.getvalue().decode("cp1251")
        self.assertIn("Мастер", text)
        self.assertIn("?", text)  # непредставимый символ заменён, а не traceback
        ascii_raw = io.BytesIO()
        ascii_stream = io.TextIOWrapper(ascii_raw, encoding="ascii")
        write_line(ascii_stream, "Ошибка: книга не найдена")
        ascii_stream.flush()
        self.assertEqual(
            ascii_raw.getvalue().decode("ascii"), "??????: ????? ?? ???????\n"
        )
        buffer = io.StringIO()
        write_line(buffer, "текст")
        self.assertEqual(buffer.getvalue(), "текст\n")

    def test_write_sanitized_line_dynamic_text(self):
        """Динамический текст (пути/диагностика) — одной строкой без C*."""
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1251")
        terminal.write_sanitized_line(
            stream, "путь /lib/\x1b[2J\u202e\nкаталог \U0001f4d6"
        )
        stream.flush()
        text = raw.getvalue().decode("cp1251")
        self.assertIn("путь /lib/", text)
        self.assertIn("каталог", text)
        self.assertIn("?", text)  # непредставимая эмодзи заменена
        self.assertNotIn("\x1b", text)  # ESC не управляет терминалом
        self.assertNotIn("\u202e", text)  # bidi нейтрализован
        # Внедрённый перевод строки — пробел; собственный структурный
        # перевод строки ровно один, в конце.
        self.assertEqual(text.count("\n"), 1)
        self.assertTrue(text.endswith("\n"))
        # Доверенный канал отличается: собственные переводы строк сохраняются.
        trusted = io.StringIO()
        write_line(trusted, "строка 1\nстрока 2")
        self.assertEqual(trusted.getvalue(), "строка 1\nстрока 2\n")


class RenderPageTests(unittest.TestCase):
    def test_numbering_and_columns(self):
        books = tuple(_book(str(number)) for number in (1, 2))
        page = SearchPage(books=books, page=0, total=30)
        out = io.StringIO()
        render_page(page, out)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "Найдено книг: 30; страница 1 из 2")
        self.assertTrue(lines[1].startswith("1. Автор — Название [ru] (Серия #1) 1000 Б"))
        self.assertTrue(lines[2].startswith("2. "))
        second = SearchPage(books=(books[0],), page=1, total=30)
        out2 = io.StringIO()
        render_page(second, out2)
        lines2 = out2.getvalue().splitlines()
        self.assertEqual(lines2[0], "Найдено книг: 30; страница 2 из 2")
        self.assertTrue(lines2[1].startswith("21."))

    def test_optional_fields_and_controls(self):
        plain = Book(
            archive="a.zip",
            member="9.fb2",
            file_id="9",
            author="Фамилия,Имя",
            title="Стр\x1bока",
            language="",
            series=(),
            size=None,
            libid="9",
            metadata={},
        )
        out = io.StringIO()
        render_page(SearchPage(books=(plain,), page=0, total=1), out)
        line = out.getvalue().splitlines()[1]
        self.assertIn("Имя Фамилия — Стр ока", line)  # ESC → пробел; автор читаем
        self.assertNotIn("[", line)  # пустой язык опущен
        self.assertNotIn("(", line)  # серий нет
        self.assertIn("размер неизвестен", line)


class TerminalTests(unittest.TestCase):
    """choose_book по настоящей поисковой сессии синтетической библиотеки."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        state = self.base / "state"
        self.paths = StatePaths(
            config=state / "config.json",
            cache=state / "cache",
            index=state / "cache" / "index.sqlite3",
        )

    def _library(self, count: int = 25):
        rows = [
            _row(str(number), "Автор Альфа", f"Альфа {number:02d}")
            for number in range(1, count + 1)
        ]
        return make_library(self.base / "library", {"a": rows})

    def _choose(self, library, answers, query=QUERY, stdout=None, stderr=None):
        stdout = io.StringIO() if stdout is None else stdout
        stderr = io.StringIO() if stderr is None else stderr
        with open_search(library, self.paths) as session:
            book = choose_book(
                session,
                query,
                stdin=_ScriptedTTY(answers),
                stdout=stdout,
                stderr=stderr,
            )
        return book, stdout.getvalue(), stderr.getvalue()

    def test_page_numbers(self):
        library = self._library(25)
        with open_search(library, self.paths) as session:
            expected = session.search(QUERY, page=1).books[0]
        book, out, err = self._choose(library, ["n", "1", "21"])
        self.assertEqual(book, expected)
        self.assertIn("номер должен быть от 21 до 25", err)
        self.assertIn("страница 2 из 2", out)
        self.assertTrue(any(line.startswith("21.") for line in out.splitlines()))

    def test_n_p_bounds(self):
        library = self._library(25)
        with open_search(library, self.paths) as session:
            expected = session.search(QUERY).books[4]
        book, out, err = self._choose(library, ["p", "n", "n", "p", "5"])
        self.assertEqual(book, expected)
        self.assertIn("первая страница", err)
        self.assertIn("последняя страница", err)
        # Страница 1 показана трижды: исходно, после отказа p и после возврата p.
        self.assertEqual(out.count("страница 1 из 2"), 3)
        self.assertEqual(out.count("страница 2 из 2"), 2)

    def test_bad_input_then_valid(self):
        library = self._library(25)
        with open_search(library, self.paths) as session:
            expected = session.search(QUERY).books[1]
        book, out, err = self._choose(library, ["abc", "0", "-3", "20.5", "2"])
        self.assertEqual(book, expected)
        self.assertEqual(err.count("неправильный ввод"), 3)
        self.assertIn("номер должен быть от 1 до 20", err)

    def test_q_eof_empty(self):
        library = self._library(3)
        book, out, err = self._choose(library, ["q"])
        self.assertIsNone(book)
        self.assertIn("выход без извлечения", err)
        # Пустая строка Enter — это "\n", а "" из readline означает EOF.
        book2, out2, err2 = self._choose(library, ["\n", "лишний ввод", ""])
        self.assertIsNone(book2)
        self.assertIn("конец ввода", err2)
        self.assertEqual(err2.count("неправильный ввод"), 2)

    def test_no_results_without_prompt(self):
        library = self._library(3)
        book, out, err = self._choose(library, ["1"], query="несуществующее")
        self.assertIsNone(book)
        self.assertIn("книг не найдено", err)
        self.assertEqual(out, "")
        self.assertNotIn("номер книги", err)

    def test_long_numeric_input_reprompts(self):
        """Длинный числовой ввод не аварийен: повтор приглашения, затем выбор.

        Предел преобразования десятичных строк Python 3.11+ не отключается;
        ввод длиной 5000 цифр отвергается до int(), хвост строки не
        становится следующей командой.
        """
        library = self._library(3)
        with open_search(library, self.paths) as session:
            expected = session.search(QUERY).books[1]
        book, out, err = self._choose(library, ["9" * 5000, "2"])
        self.assertEqual(book, expected)
        self.assertIn("слишком длинное", err)
        # После длинного ввода q по-прежнему обрабатывается.
        book2, _, err2 = self._choose(library, ["9" * 5000, "q"])
        self.assertIsNone(book2)
        self.assertIn("слишком длинное", err2)
        self.assertIn("выход без извлечения", err2)

    def test_output_flushed_before_readline(self):
        """Таблица и приглашение видны в момент блокирующего чтения."""

        class _ProbeTTY:
            def __init__(self, raw_out: io.BytesIO, raw_err: io.BytesIO) -> None:
                self._raw_out = raw_out
                self._raw_err = raw_err
                self.seen: list[tuple[bytes, bytes]] = []

            def readline(self) -> str:
                self.seen.append((self._raw_out.getvalue(), self._raw_err.getvalue()))
                return "1\n"

            def isatty(self) -> bool:
                return True

        raw_out, raw_err = io.BytesIO(), io.BytesIO()
        # Без line_buffering: без явного flush байты не доходят до буферов.
        stdout = io.TextIOWrapper(raw_out, encoding="utf-8")
        stderr = io.TextIOWrapper(raw_err, encoding="utf-8")
        probe = _ProbeTTY(raw_out, raw_err)
        library = self._library(2)
        with open_search(library, self.paths) as session:
            book = choose_book(
                session, QUERY, stdin=probe, stdout=stdout, stderr=stderr
            )
        self.assertIsNotNone(book)
        out_at_read, err_at_read = probe.seen[0]
        self.assertIn("1.".encode("utf-8"), out_at_read)  # таблица сброшена
        self.assertIn("номер книги".encode("utf-8"), err_at_read)  # приглашение

    def test_controls_and_narrow_encoding(self):
        rows = [
            _row(
                "1",
                "Перумов\x1b[2J, Ник",
                "Кольцо\u202e Тьмы \U0001f4d6",
                SERIES="Хроники\x00Хьёрварда",
                SERNO="1",
                LANG="ru",
                SIZE="1000",
            ),
            _row("2", "Автор Второй", "Другая книга", LANG="ru", SIZE="500"),
        ]
        library = make_library(self.base / "library", {"a": rows})
        catalog_sha = hashlib.sha256(library.catalog.read_bytes()).hexdigest()
        raw_out, raw_err = io.BytesIO(), io.BytesIO()
        stdout = io.TextIOWrapper(raw_out, encoding="cp1251")
        stderr = io.TextIOWrapper(raw_err, encoding="cp1251")
        with open_search(library, self.paths) as session:
            book = choose_book(
                session,
                "кольцо",
                stdin=_ScriptedTTY(["1"]),
                stdout=stdout,
                stderr=stderr,
            )
        stdout.flush()
        stderr.flush()
        self.assertIsNotNone(book)
        text = raw_out.getvalue().decode("cp1251")
        self.assertNotIn("\x1b", text)
        self.assertIn("Кольцо  Тьмы", text)  # RLO стал пробелом, а не «?»
        self.assertIn("Хроники Хьёрварда #1", text)  # NUL стал пробелом
        self.assertIn("?", text)  # непредставимая в cp1251 эмодзи заменена
        # Метаданные на диске не изменились от показа.
        self.assertEqual(
            hashlib.sha256(library.catalog.read_bytes()).hexdigest(), catalog_sha
        )


if __name__ == "__main__":
    unittest.main()
