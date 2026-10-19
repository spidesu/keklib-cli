"""Чтение точного файла книги из ZIP-архива библиотеки (§9 спецификации).

``open_book`` — единственная точка чтения книги: имена архива и member
проверяются заново, без доверия поисковому индексу; путь архива обязан
разрешаться внутрь ``Library.archives`` с учётом символических ссылок
(и junction на Windows — там, где ОС разрешает их ``resolve``); в самом
ZIP требуется ровно одна точная запись — обычный файл (явно
указанный UNIX-тип, отличный от регулярного, отвергается; записи без
UNIX-типизации допускаются) без шифрования и с поддерживаемым методом
сжатия. ZIP и source-дескрипторы
принадлежат менеджеру и закрываются на любом выходе; ошибки собственных
open/read переводятся в ``ExtractionError``, а исключения вызывающего
кода (включая ``OSError`` и ``KeyboardInterrupt``) проходят без перевода.
Запись на диск, безопасное имя и публикация — задача 8 (``output``);
здесь только потоковое чтение, без ``extract``/``extractall``.
"""

from __future__ import annotations

import stat
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from keklib_cli.errors import CatalogError, ExtractionError
from keklib_cli.models import Book, Library
from keklib_cli.source_names import validate_source_name

__all__ = ["SUPPORTED_COMPRESSION", "open_book"]

#: Методы сжатия, которые читает извлечение. Реальные архивы Lib.rus.ec —
#: STORED/DEFLATED; остальное отвергается явным отказом, а не молчаливой
#: попыткой распаковки.
SUPPORTED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})

#: Бит шифрования в general purpose flags (§9: пароли не поддерживаются).
_ENCRYPTED_FLAG = 0x1

#: Ошибки собственного открытия/чтения member: переводятся в
#: ``ExtractionError``. ``BadZipFile`` покрывает и «Bad CRC-32»,
#: ``zlib.error`` — повреждённый поток deflate, ``UnicodeDecodeError`` —
#: недопустимый UTF-8 в имени записи (например, локального заголовка),
#: ``RuntimeError`` — требование пароля, ``NotImplementedError`` —
#: неподдерживаемый методом zipfile тип записи.
_STREAM_ERRORS = (
    OSError,
    EOFError,
    RuntimeError,
    NotImplementedError,
    UnicodeDecodeError,
    zipfile.BadZipFile,
    zlib.error,
)


@contextmanager
def open_book(library: Library, book: Book) -> Iterator[BinaryIO]:
    """Открыть поток чтения точного файла ``book`` из её ZIP (§9).

    Проверки выполняются при каждом вызове, без доверия поисковому
    индексу: простые имена архива и member, реальное разрешение пути
    архива с containment внутри ``library.archives``, наличие ZIP,
    ровно одна точная запись — обычный файл без шифрования с
    поддерживаемым сжатием (явный UNIX-тип, отличный от регулярного,
    отвергается). Дескрипторы закрываются на любом выходе:
    нормальном, ошибке в теле ``with`` и отмене. Чтение до EOF
    завершает проверку CRC-32 zipfile; публикация файла — задача 8.

    :param library: проверенная библиотека (задача 1).
    :param book: выбранная книга; используются ``archive`` и ``member``.
    :raises ExtractionError: имя архива/member не проходит проверку,
        путь архива выходит за пределы ``library.archives`` после
        разрешения, ZIP отсутствует, не читается или содержит
        недопустимый UTF-8 в имени записи, точная запись не найдена,
        задублирована, не является обычным файлом (включая явные
        UNIX-типы каталог/FIFO/socket), зашифрована, использует
        неподдерживаемое сжатие или не читается по I/O/CRC.
    """
    archive_name = _checked_name(book.archive, "архива")
    member_name = _checked_name(book.member, "файла книги")
    archive_path = _contained_archive(library, archive_name, member_name)
    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        # UnicodeDecodeError — повреждённое имя записи центрального
        # каталога (бит UTF-8 при недопустимых байтах): это ошибка
        # собственного открытия источника, а не вызывающего кода.
        raise ExtractionError(
            f"архив {archive_name!r} ({archive_path}) не открывается как ZIP: "
            f"{exc}; файл книги {member_name!r}"
        ) from exc
    try:
        info = _select_entry(archive, archive_name, member_name)
        try:
            source = archive.open(info, "r")
        except _STREAM_ERRORS as exc:
            raise ExtractionError(
                f"не удалось открыть файл {member_name!r} "
                f"в архиве {archive_name!r}: {exc}"
            ) from exc
        stream = _MemberStream(source, archive_name, member_name)
        try:
            yield stream
        finally:
            stream.close()
    finally:
        archive.close()


def _checked_name(name: str, what: str) -> str:
    """Проверить простое имя источника; отказ каталога — ExtractionError.

    Переиспользуется общий ОС-независимый валидатор `source_names` (§6);
    здесь его ``CatalogError`` локально переводится в ошибку извлечения.

    :raises ExtractionError: имя не проходит проверку `validate_source_name`.
    """
    try:
        return validate_source_name(name)
    except CatalogError as exc:
        raise ExtractionError(f"недопустимое имя {what} книги: {exc}") from exc


def _contained_archive(
    library: Library, archive_name: str, member_name: str
) -> Path:
    """Разрешить путь архива и потребовать containment в каталоге архивов.

    Сравнение выполняется по реальным разрешённым путям (``resolve``), а
    не по строковым префиксам: символические ссылки и junction
    разрешаются ОС, и выход за пределы ``library.archives`` отвергается
    по фактическому расположению файла.

    :raises ExtractionError: путь не разрешается, разрешённый путь вышел
        за пределы каталога архивов, архив отсутствует или не обычный файл.
    """
    try:
        archives_root = library.archives.resolve()
        resolved = (library.archives / archive_name).resolve()
    except OSError as exc:
        raise ExtractionError(
            f"путь архива {archive_name!r} не разрешается: {exc}"
        ) from exc
    try:
        resolved.relative_to(archives_root)
    except ValueError:
        raise ExtractionError(
            f"архив {archive_name!r} выходит за пределы каталога архивов "
            f"{library.archives}: после разрешения путей он указывает на "
            f"{resolved}; файл книги {member_name!r} не читается"
        ) from None
    if not resolved.exists():
        raise ExtractionError(
            f"архив {archive_name!r} не найден в {library.archives} "
            f"(файл книги {member_name!r})"
        )
    if not resolved.is_file():
        raise ExtractionError(
            f"архив {archive_name!r} ({resolved}) не является обычным файлом"
        )
    return resolved


def _select_entry(
    archive: zipfile.ZipFile, archive_name: str, member_name: str
) -> zipfile.ZipInfo:
    """Выбрать ровно одну точную запись member по центральному каталогу.

    Соседние и похожие имена не выбираются автоматически; повторная
    запись того же имени — неоднозначность, а не первый попавшийся файл.

    :raises ExtractionError: точная запись отсутствует, записана несколько
        раз, не является обычным файлом (каталог/UNIX-symlink), зашифрована
        или использует неподдерживаемый метод сжатия.
    """
    matches = [info for info in archive.infolist() if info.filename == member_name]
    if not matches:
        raise ExtractionError(
            f"файл {member_name!r} не найден в архиве {archive_name!r}"
        )
    if len(matches) > 1:
        raise ExtractionError(
            f"файл {member_name!r} записан в архиве {archive_name!r} "
            f"несколько раз (совпадающих записей: {len(matches)}); "
            f"выбор неоднозначен"
        )
    info = matches[0]
    # Имя member проверено как простое (без разделителей), поэтому
    # запись-каталог по имени здесь недостижима; проверка оставлена как
    # инвариант против будущих послаблений проверки имён. Тип записи —
    # метаданные: явно указанный UNIX-тип, отличный от регулярного файла
    # (каталог без завершающего «/», FIFO, socket, устройство),
    # отвергается; нулевые биты типа (ZIP без UNIX-типизации, например
    # с Windows) и S_IFREG допускаются.
    mode = info.external_attr >> 16
    if info.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
        raise ExtractionError(
            f"запись {member_name!r} в архиве {archive_name!r} не является "
            f"обычным файлом (тип записи: {stat.filemode(mode)})"
        )
    if info.flag_bits & _ENCRYPTED_FLAG:
        raise ExtractionError(
            f"запись {member_name!r} в архиве {archive_name!r} зашифрована; "
            f"пароли не поддерживаются"
        )
    if info.compress_type not in SUPPORTED_COMPRESSION:
        raise ExtractionError(
            f"метод сжатия {info.compress_type} записи {member_name!r} в "
            f"архиве {archive_name!r} не поддерживается"
        )
    return info


class _MemberStream:
    """Поток одного member; ошибки собственных read — ``ExtractionError``.

    Тело ``with`` вызывающего кода не оборачивается: его исключения
    (включая ``OSError`` и ``KeyboardInterrupt``) проходят без перевода.
    Дескриптор member закрывается менеджером ``open_book``; повторное
    закрытие из вызывающего кода безопасно.
    """

    def __init__(
        self, source: BinaryIO, archive_name: str, member_name: str
    ) -> None:
        self._source = source
        self._where = f"{archive_name!r}:{member_name!r}"
        self.closed = False

    def readable(self) -> bool:
        return not self.closed

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError(f"read of closed member stream {self._where}")
        try:
            return self._source.read(size)
        except _STREAM_ERRORS as exc:
            raise ExtractionError(
                f"не удалось прочитать файл {self._where}: {exc}"
            ) from exc

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._source.close()

    def __enter__(self) -> "_MemberStream":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
